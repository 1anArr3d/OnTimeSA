"""Reliability aggregation over historical schedule_deviations/bunching_events.

Two entry points share the same underlying approach and response shape
(schemas.ReliabilityStats):

- compute_segment_reliability(): a single route + start stop + end stop,
  no transfers - "check my commute". Always returns whatever data exists
  for that exact segment (flagged low-confidence if sparse), since the user
  asked about a specific thing and a non-answer isn't useful to them.
- compute_group_reliability(): ranks routes or stops by on-time % over a
  date range - "worst offenders". Rows below min_samples are excluded
  entirely here (not just flagged), because a ranking is much more sensitive
  to a handful of noisy low-sample entries dominating the list than a single
  direct lookup is.

Both take an explicit start_date/end_date - reliability is a trend over a
window, not a point-in-time snapshot, so date-range filtering is load-bearing
from the start rather than bolted on.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BunchingEvent, Route, ScheduleDeviation, Stop, StopTime, Trip
from app.schemas import ReliabilityStats


def _on_time_case():
    return case(
        (
            ScheduleDeviation.delay_seconds.between(
                settings.on_time_early_threshold_seconds, settings.on_time_late_threshold_seconds
            ),
            1,
        ),
        else_=0,
    )


def _confidence(sample_count: int) -> str:
    return "high" if sample_count >= settings.min_reliable_sample_count else "low"


@dataclass
class SegmentInfo:
    direction_id: int | None
    reference_trip_id: str
    start_seq: int
    end_seq: int


def resolve_segment(session: Session, route_id: str, start_stop_id: str, end_stop_id: str) -> SegmentInfo | None:
    """Find a trip on this route that visits start_stop_id before
    end_stop_id, establishing the direction and stop_sequence range for the
    segment. Returns None if the two stops aren't connected on this route in
    that order (this project deliberately doesn't support transfers).
    """
    st_start = StopTime
    st_end_alias = StopTime.__table__.alias("st_end")

    query = (
        select(
            Trip.trip_id,
            Trip.direction_id,
            st_start.stop_sequence.label("start_seq"),
            st_end_alias.c.stop_sequence.label("end_seq"),
        )
        .join(st_start, st_start.trip_id == Trip.trip_id)
        .join(st_end_alias, st_end_alias.c.trip_id == Trip.trip_id)
        .where(
            Trip.route_id == route_id,
            st_start.stop_id == start_stop_id,
            st_end_alias.c.stop_id == end_stop_id,
            st_start.stop_sequence < st_end_alias.c.stop_sequence,
        )
        .limit(1)
    )
    row = session.execute(query).first()
    if row is None:
        return None
    return SegmentInfo(direction_id=row.direction_id, reference_trip_id=row.trip_id, start_seq=row.start_seq, end_seq=row.end_seq)


def _segment_stop_ids(session: Session, reference_trip_id: str, start_seq: int, end_seq: int) -> list[str]:
    query = select(StopTime.stop_id).where(
        StopTime.trip_id == reference_trip_id,
        StopTime.stop_sequence.between(start_seq, end_seq),
    )
    return [row.stop_id for row in session.execute(query)]


def _bunching_count(
    session: Session,
    route_id: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    stop_ids: list[str] | None = None,
) -> int:
    query = select(func.count()).select_from(BunchingEvent).where(
        BunchingEvent.route_id == route_id,
        BunchingEvent.start_time >= start_dt,
        BunchingEvent.start_time <= end_dt,
    )
    if stop_ids is not None:
        query = query.where(BunchingEvent.nearest_stop_id.in_(stop_ids))
    return session.execute(query).scalar() or 0


def compute_segment_reliability(
    session: Session,
    route_id: str,
    start_stop_id: str,
    end_stop_id: str,
    start_date: datetime.date,
    end_date: datetime.date,
) -> ReliabilityStats | None:
    """Returns None if the route doesn't exist, or the two stops aren't
    connected on it in that order (caller should treat as a 404/400).
    """
    route = session.get(Route, route_id)
    if route is None:
        return None

    segment = resolve_segment(session, route_id, start_stop_id, end_stop_id)
    if segment is None:
        return None

    stop_ids = _segment_stop_ids(session, segment.reference_trip_id, segment.start_seq, segment.end_seq)

    dev_query = (
        select(
            func.count().label("sample_count"),
            func.avg(ScheduleDeviation.delay_seconds).label("avg_delay"),
            func.sum(_on_time_case()).label("on_time_count"),
        )
        .select_from(ScheduleDeviation)
        .join(Trip, Trip.trip_id == ScheduleDeviation.trip_id)
        .where(
            ScheduleDeviation.route_id == route_id,
            Trip.direction_id == segment.direction_id,
            ScheduleDeviation.stop_id == end_stop_id,
            ScheduleDeviation.event_type == "arrival",
            ScheduleDeviation.service_date >= start_date,
            ScheduleDeviation.service_date <= end_date,
        )
    )
    sample_count, avg_delay, on_time_count = session.execute(dev_query).one()
    sample_count = sample_count or 0

    start_dt = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    end_dt = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=datetime.timezone.utc)
    bunching_count = _bunching_count(session, route_id, start_dt, end_dt, stop_ids=stop_ids)
    days = (end_date - start_date).days + 1

    start_stop = session.get(Stop, start_stop_id)
    end_stop = session.get(Stop, end_stop_id)

    return ReliabilityStats(
        scope="segment",
        route_id=route_id,
        route_short_name=route.route_short_name,
        route_long_name=route.route_long_name,
        direction_id=segment.direction_id,
        start_stop_id=start_stop_id,
        start_stop_name=start_stop.stop_name if start_stop else None,
        end_stop_id=end_stop_id,
        end_stop_name=end_stop.stop_name if end_stop else None,
        start_date=start_date,
        end_date=end_date,
        sample_count=sample_count,
        avg_delay_seconds=float(avg_delay) if avg_delay is not None else None,
        on_time_pct=(on_time_count / sample_count * 100) if sample_count else None,
        bunching_event_count=bunching_count,
        bunching_events_per_day=bunching_count / days if days else 0.0,
        confidence=_confidence(sample_count),
    )


def compute_group_reliability(
    session: Session,
    start_date: datetime.date,
    end_date: datetime.date,
    group_by: str = "route",
    route_id: str | None = None,
    limit: int = 10,
    min_samples: int | None = None,
) -> list[ReliabilityStats]:
    """Rank routes (or stops) by on-time % over a date range, worst first.

    Rows with fewer than min_samples observations are dropped entirely
    (unlike compute_segment_reliability, which always answers) - a ranking
    is too easily dominated by a handful of noisy low-sample entries.
    """
    if group_by not in ("route", "stop"):
        raise ValueError("group_by must be 'route' or 'stop'")
    min_samples = settings.min_reliable_sample_count if min_samples is None else min_samples

    group_col = ScheduleDeviation.route_id if group_by == "route" else ScheduleDeviation.stop_id

    query = (
        select(
            group_col.label("key"),
            func.count().label("sample_count"),
            func.avg(ScheduleDeviation.delay_seconds).label("avg_delay"),
            func.sum(_on_time_case()).label("on_time_count"),
        )
        .where(
            ScheduleDeviation.event_type == "arrival",
            ScheduleDeviation.service_date >= start_date,
            ScheduleDeviation.service_date <= end_date,
        )
        .group_by(group_col)
        .having(func.count() >= min_samples)
    )
    if route_id is not None:
        query = query.where(ScheduleDeviation.route_id == route_id)

    rows = session.execute(query).all()
    if not rows:
        return []

    on_time_pct_by_key = {}
    stats_by_key = {}
    for key, sample_count, avg_delay, on_time_count in rows:
        pct = (on_time_count / sample_count * 100) if sample_count else 0.0
        on_time_pct_by_key[key] = pct
        stats_by_key[key] = (sample_count, avg_delay, on_time_count)

    worst_keys = sorted(on_time_pct_by_key, key=lambda k: on_time_pct_by_key[k])[:limit]

    start_dt = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    end_dt = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=datetime.timezone.utc)
    days = (end_date - start_date).days + 1

    results: list[ReliabilityStats] = []
    if group_by == "route":
        routes = {r.route_id: r for r in session.query(Route).filter(Route.route_id.in_(worst_keys)).all()}
        for key in worst_keys:
            sample_count, avg_delay, on_time_count = stats_by_key[key]
            route = routes.get(key)
            bunching_count = _bunching_count(session, key, start_dt, end_dt)
            results.append(
                ReliabilityStats(
                    scope="route",
                    route_id=key,
                    route_short_name=route.route_short_name if route else None,
                    route_long_name=route.route_long_name if route else None,
                    start_date=start_date,
                    end_date=end_date,
                    sample_count=sample_count,
                    avg_delay_seconds=float(avg_delay) if avg_delay is not None else None,
                    on_time_pct=on_time_pct_by_key[key],
                    bunching_event_count=bunching_count,
                    bunching_events_per_day=bunching_count / days if days else 0.0,
                    confidence=_confidence(sample_count),
                )
            )
    else:
        stops = {s.stop_id: s for s in session.query(Stop).filter(Stop.stop_id.in_(worst_keys)).all()}
        # Bunching events aren't naturally per-stop across arbitrary routes,
        # so bunching_event_count is omitted (left at 0) for stop-scoped rows.
        for key in worst_keys:
            sample_count, avg_delay, on_time_count = stats_by_key[key]
            stop = stops.get(key)
            results.append(
                ReliabilityStats(
                    scope="stop",
                    end_stop_id=key,
                    end_stop_name=stop.stop_name if stop else None,
                    route_id=route_id,
                    start_date=start_date,
                    end_date=end_date,
                    sample_count=sample_count,
                    avg_delay_seconds=float(avg_delay) if avg_delay is not None else None,
                    on_time_pct=on_time_pct_by_key[key],
                    bunching_event_count=0,
                    bunching_events_per_day=0.0,
                    confidence=_confidence(sample_count),
                )
            )

    return results

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

Raw schedule_deviations only covers the last settings.raw_data_retention_days
days (see app/rollup_service.py - older rows get pruned after being rolled up
into daily_route_stats). A date range that reaches further back than that
gets split at the retention cutoff: the recent portion still queries raw
data directly (full precision, segment/direction-specific), and the older
portion is filled in from daily_route_stats (see _rollup_route_stats below).
daily_route_stats only has route-level granularity (no per-stop/direction
breakdown), so the historical portion of a segment lookup is necessarily a
route-wide approximation rather than an exact segment figure - a known
tradeoff of keeping the rollup table small. bunching_events is never pruned,
so bunching counts always query the full range directly regardless of the
split.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BunchingEvent, DailyRouteStat, Route, ScheduleDeviation, Stop, StopTime, Trip
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
class _DevStats:
    sample_count: int = 0
    avg_delay: float | None = None
    on_time_count: float = 0.0


def _raw_cutoff_date() -> datetime.date:
    """Oldest service_date still guaranteed to exist in raw schedule_deviations."""
    return datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=settings.raw_data_retention_days)


def _split_range(start_date: datetime.date, end_date: datetime.date) -> tuple[tuple | None, tuple | None]:
    """Split [start_date, end_date] into a (raw_start, raw_end) sub-range still
    covered by raw data and a (rollup_start, rollup_end) sub-range that has to
    come from daily_route_stats instead. Either half may be None if the
    requested range doesn't touch it.
    """
    cutoff = _raw_cutoff_date()
    raw_start = max(start_date, cutoff)
    raw_range = (raw_start, end_date) if raw_start <= end_date else None
    rollup_end = min(end_date, cutoff - datetime.timedelta(days=1))
    rollup_range = (start_date, rollup_end) if start_date <= rollup_end else None
    return raw_range, rollup_range


def _combine(a: _DevStats, b: _DevStats) -> _DevStats:
    sample_count = a.sample_count + b.sample_count
    if sample_count == 0:
        return _DevStats()
    weighted_delay_sum = 0.0
    weight = 0
    for stats in (a, b):
        if stats.avg_delay is not None and stats.sample_count:
            weighted_delay_sum += stats.avg_delay * stats.sample_count
            weight += stats.sample_count
    avg_delay = (weighted_delay_sum / weight) if weight else None
    return _DevStats(sample_count=sample_count, avg_delay=avg_delay, on_time_count=a.on_time_count + b.on_time_count)


def _rollup_route_stats(
    session: Session, route_id: str, start_date: datetime.date, end_date: datetime.date
) -> _DevStats:
    """Aggregate daily_route_stats rows for one route over a date range.
    Route-level only - see module docstring for why a segment/stop lookup
    can't get a more precise historical figure than this.
    """
    query = select(
        func.sum(DailyRouteStat.sample_count).label("sample_count"),
        func.sum(DailyRouteStat.avg_delay_seconds * DailyRouteStat.sample_count).label("weighted_delay"),
        func.sum(DailyRouteStat.on_time_pct * DailyRouteStat.sample_count / 100).label("on_time_count"),
    ).where(
        DailyRouteStat.route_id == route_id,
        DailyRouteStat.service_date >= start_date,
        DailyRouteStat.service_date <= end_date,
    )
    sample_count, weighted_delay, on_time_count = session.execute(query).one()
    sample_count = sample_count or 0
    if not sample_count:
        return _DevStats()
    avg_delay = (weighted_delay / sample_count) if weighted_delay is not None else None
    return _DevStats(sample_count=sample_count, avg_delay=avg_delay, on_time_count=float(on_time_count or 0))


def _rollup_route_stats_by_route(
    session: Session, route_ids: list[str], start_date: datetime.date, end_date: datetime.date
) -> dict[str, _DevStats]:
    if not route_ids:
        return {}
    query = (
        select(
            DailyRouteStat.route_id,
            func.sum(DailyRouteStat.sample_count).label("sample_count"),
            func.sum(DailyRouteStat.avg_delay_seconds * DailyRouteStat.sample_count).label("weighted_delay"),
            func.sum(DailyRouteStat.on_time_pct * DailyRouteStat.sample_count / 100).label("on_time_count"),
        )
        .where(
            DailyRouteStat.route_id.in_(route_ids),
            DailyRouteStat.service_date >= start_date,
            DailyRouteStat.service_date <= end_date,
        )
        .group_by(DailyRouteStat.route_id)
    )
    result = {}
    for route_id, sample_count, weighted_delay, on_time_count in session.execute(query):
        sample_count = sample_count or 0
        if not sample_count:
            continue
        avg_delay = (weighted_delay / sample_count) if weighted_delay is not None else None
        result[route_id] = _DevStats(sample_count=sample_count, avg_delay=avg_delay, on_time_count=float(on_time_count or 0))
    return result


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

    raw_range, rollup_range = _split_range(start_date, end_date)

    raw_stats = _DevStats()
    if raw_range is not None:
        raw_start, raw_end = raw_range
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
                ScheduleDeviation.service_date >= raw_start,
                ScheduleDeviation.service_date <= raw_end,
            )
        )
        raw_count, raw_avg_delay, raw_on_time_count = session.execute(dev_query).one()
        raw_stats = _DevStats(
            sample_count=raw_count or 0,
            avg_delay=float(raw_avg_delay) if raw_avg_delay is not None else None,
            on_time_count=float(raw_on_time_count or 0),
        )

    rollup_stats = _DevStats()
    if rollup_range is not None:
        rollup_stats = _rollup_route_stats(session, route_id, rollup_range[0], rollup_range[1])

    combined = _combine(raw_stats, rollup_stats)
    sample_count = combined.sample_count
    avg_delay = combined.avg_delay
    on_time_count = combined.on_time_count

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

    raw_range, rollup_range = _split_range(start_date, end_date)

    stats_by_key: dict[str, _DevStats] = {}
    if raw_range is not None:
        raw_start, raw_end = raw_range
        raw_query = (
            select(
                group_col.label("key"),
                func.count().label("sample_count"),
                func.avg(ScheduleDeviation.delay_seconds).label("avg_delay"),
                func.sum(_on_time_case()).label("on_time_count"),
            )
            .where(
                ScheduleDeviation.event_type == "arrival",
                ScheduleDeviation.service_date >= raw_start,
                ScheduleDeviation.service_date <= raw_end,
            )
            .group_by(group_col)
        )
        if route_id is not None:
            raw_query = raw_query.where(ScheduleDeviation.route_id == route_id)
        for key, sample_count, avg_delay, on_time_count in session.execute(raw_query):
            stats_by_key[key] = _DevStats(
                sample_count=sample_count or 0,
                avg_delay=float(avg_delay) if avg_delay is not None else None,
                on_time_count=float(on_time_count or 0),
            )

    # daily_route_stats has no per-stop breakdown, so the rollup portion of
    # the range can only extend a route-scoped ranking, not a stop-scoped one.
    if rollup_range is not None and group_by == "route":
        rollup_query = select(DailyRouteStat.route_id).where(
            DailyRouteStat.service_date >= rollup_range[0], DailyRouteStat.service_date <= rollup_range[1]
        )
        if route_id is not None:
            rollup_query = rollup_query.where(DailyRouteStat.route_id == route_id)
        candidate_route_ids = sorted({r for (r,) in session.execute(rollup_query)} | set(stats_by_key))
        rollup_stats = _rollup_route_stats_by_route(session, candidate_route_ids, rollup_range[0], rollup_range[1])
        for key, r_stats in rollup_stats.items():
            stats_by_key[key] = _combine(stats_by_key.get(key, _DevStats()), r_stats)

    stats_by_key = {key: stats for key, stats in stats_by_key.items() if stats.sample_count >= min_samples}
    if not stats_by_key:
        return []

    on_time_pct_by_key = {
        key: (stats.on_time_count / stats.sample_count * 100) if stats.sample_count else 0.0
        for key, stats in stats_by_key.items()
    }
    worst_keys = sorted(on_time_pct_by_key, key=lambda k: on_time_pct_by_key[k])[:limit]

    start_dt = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    end_dt = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=datetime.timezone.utc)
    days = (end_date - start_date).days + 1

    results: list[ReliabilityStats] = []
    if group_by == "route":
        routes = {r.route_id: r for r in session.query(Route).filter(Route.route_id.in_(worst_keys)).all()}
        for key in worst_keys:
            sample_count, avg_delay = stats_by_key[key].sample_count, stats_by_key[key].avg_delay
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
            sample_count, avg_delay = stats_by_key[key].sample_count, stats_by_key[key].avg_delay
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

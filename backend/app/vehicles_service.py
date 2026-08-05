"""Live vehicle positions for the map view: latest snapshot per vehicle,
color-coded using the most recently computed delay for that vehicle's trip,
plus enough context (direction, current/next stop, scheduled vs. actual
time) to build a real click popup - all derived from data already ingested,
no new external calls.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, String, and_, column, func, select, values
from sqlalchemy.orm import Session, aliased

from app.gtfs.timezone_utils import AGENCY_TIMEZONE
from app.models import Route, ScheduleDeviation, Stop, StopTime, Trip, VehiclePositionSnapshot
from app.schemas import LiveVehicle

DEFAULT_MAX_AGE_SECONDS = 300


def _format_scheduled_clock(seconds_since_midnight: int | None) -> str | None:
    """GTFS stop_times are already agency-local - no timezone conversion
    needed, just format as a clock time. Handles the >24:00:00 convention."""
    if seconds_since_midnight is None:
        return None
    total_minutes = (seconds_since_midnight // 60) % (24 * 60)
    hour24, minute = divmod(total_minutes, 60)
    period = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minute:02d} {period}"


def _format_actual_clock(actual_utc: datetime.datetime | None) -> str | None:
    if actual_utc is None:
        return None
    local = actual_utc.astimezone(ZoneInfo(AGENCY_TIMEZONE))
    period = "AM" if local.hour < 12 else "PM"
    hour12 = local.hour % 12 or 12
    return f"{hour12}:{local.minute:02d} {period}"


def list_live_vehicles(session: Session, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> list[LiveVehicle]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=max_age_seconds)

    ranked = (
        select(
            VehiclePositionSnapshot,
            func.row_number()
            .over(partition_by=VehiclePositionSnapshot.vehicle_id, order_by=VehiclePositionSnapshot.polled_at.desc())
            .label("rn"),
        )
        .where(VehiclePositionSnapshot.polled_at >= cutoff)
        .subquery()
    )
    RankedSnapshot = aliased(VehiclePositionSnapshot, ranked)
    latest_snapshots = session.execute(select(RankedSnapshot).where(ranked.c.rn == 1)).scalars().all()
    if not latest_snapshots:
        return []

    trip_ids = {s.trip_id for s in latest_snapshots if s.trip_id}
    route_ids = {s.route_id for s in latest_snapshots if s.route_id}
    stop_ids = {s.stop_id for s in latest_snapshots if s.stop_id}

    route_short_name_by_id = {}
    if route_ids:
        routes = session.query(Route).filter(Route.route_id.in_(route_ids)).all()
        route_short_name_by_id = {r.route_id: r.route_short_name for r in routes}

    trip_by_id: dict[str, Trip] = {}
    if trip_ids:
        trip_by_id = {t.trip_id: t for t in session.query(Trip).filter(Trip.trip_id.in_(trip_ids)).all()}

    stop_name_by_id: dict[str, str | None] = {}
    if stop_ids:
        stop_name_by_id = {
            s.stop_id: s.stop_name for s in session.query(Stop).filter(Stop.stop_id.in_(stop_ids)).all()
        }

    # Next stop per (trip, current_stop_sequence): pushes the "first stop_sequence
    # past the vehicle's current one" filter into the query itself via a VALUES
    # join + row_number(), instead of pulling every stop on every active trip's
    # full pattern into Python just to keep one row - that approach measured a
    # 44x overfetch (1,953 stop_times rows fetched for 44 trips that only ever
    # used 44 of them).
    next_stop_by_key: dict[tuple[str, int], tuple[str, str | None, int | None]] = {}
    pairs = sorted(
        {(s.trip_id, s.current_stop_sequence) for s in latest_snapshots if s.trip_id and s.current_stop_sequence is not None}
    )
    if pairs:
        pairs_values = values(column("trip_id", String), column("current_stop_sequence", Integer), name="pairs").data(pairs)
        ranked = (
            select(
                pairs_values.c.trip_id,
                pairs_values.c.current_stop_sequence,
                StopTime.stop_id,
                StopTime.arrival_time_seconds,
                Stop.stop_name,
                func.row_number()
                .over(
                    partition_by=(pairs_values.c.trip_id, pairs_values.c.current_stop_sequence),
                    order_by=StopTime.stop_sequence,
                )
                .label("rn"),
            )
            .select_from(pairs_values)
            .join(
                StopTime,
                and_(StopTime.trip_id == pairs_values.c.trip_id, StopTime.stop_sequence > pairs_values.c.current_stop_sequence),
            )
            .join(Stop, Stop.stop_id == StopTime.stop_id)
            .subquery()
        )
        rows = session.execute(select(ranked).where(ranked.c.rn == 1)).all()
        for trip_id, current_seq, next_stop_id, next_arrival_seconds, next_stop_name, _rn in rows:
            next_stop_by_key[(trip_id, current_seq)] = (next_stop_id, next_stop_name, next_arrival_seconds)

    # trip_id recurs across calendar days (the same scheduled "8:15am
    # Route 5" trip_id runs again tomorrow), so without a service_date bound
    # both queries below pull every day still in the raw retention window
    # for a currently-active trip, not just its current occurrence - measured
    # as up to 5x overfetch as the retention window fills. A live vehicle's
    # actual service_date is always either agency-local "today" or
    # "yesterday" (GTFS's >24:00:00 overnight-trip convention keeps a still-
    # running post-midnight trip filed under the previous calendar day) -
    # never further back - so a two-day floor is the tightest bound that
    # can't cut off a real overnight-spanning trip.
    today_agency_local = datetime.datetime.now(ZoneInfo(AGENCY_TIMEZONE)).date()
    service_date_floor = today_agency_local - datetime.timedelta(days=1)

    # Deviation matched to the vehicle's exact current stop-visit
    # (trip_id, stop_sequence) - precise "scheduled vs actual" for the
    # specific stop shown, not just "most recently computed for this trip"
    # (which could be a different, unrelated stop given trip_updates predict
    # many stops ahead each poll). Only ever one row per vehicle is actually
    # used from this - filtering to exactly the needed (trip_id,
    # current_stop_sequence) pairs (same `pairs` set as the next-stop query
    # above) instead of every deviation row for the trip measured a 43.4x
    # overfetch (1,344 rows fetched, 31 used). The same (trip_id,
    # stop_sequence) key can legitimately have more than one row within the
    # two-day floor (recurring trip_id, or an arrival vs. departure row for
    # the same stop) - ranked explicitly (most recent service_date, arrival
    # preferred over departure, most recent computed_at) rather than left to
    # arbitrary row order, same issue the service_date floor above surfaced.
    deviation_by_key: dict[tuple[str, int], ScheduleDeviation] = {}
    if pairs:
        ranked_exact = (
            select(
                ScheduleDeviation,
                func.row_number()
                .over(
                    partition_by=(ScheduleDeviation.trip_id, ScheduleDeviation.stop_sequence),
                    order_by=(
                        ScheduleDeviation.service_date.desc(),
                        (ScheduleDeviation.event_type != "arrival"),
                        ScheduleDeviation.computed_at.desc(),
                    ),
                )
                .label("rn"),
            )
            .join(
                pairs_values,
                and_(
                    ScheduleDeviation.trip_id == pairs_values.c.trip_id,
                    ScheduleDeviation.stop_sequence == pairs_values.c.current_stop_sequence,
                ),
            )
            .where(ScheduleDeviation.service_date >= service_date_floor)
            .subquery()
        )
        RankedExactDeviation = aliased(ScheduleDeviation, ranked_exact)
        exact_matches = session.execute(select(RankedExactDeviation).where(ranked_exact.c.rn == 1)).scalars().all()
        for d in exact_matches:
            deviation_by_key[(d.trip_id, d.stop_sequence)] = d

    # Fallback delay-by-trip (most recently computed, any stop) for the map
    # dot color when there's no exact match for the vehicle's current stop -
    # e.g. mid-block, between two predicted stops. Already client-egress-
    # efficient (the rn==1 filter below means only one row per trip_id ever
    # reaches the client, not every deviation row) - narrowing it further
    # like the exact-match query above would be redundant, since it already
    # serves a genuinely different purpose (any-stop fallback vs. exact-stop
    # match) that the narrowed query above can't answer.
    #
    # ORDER BY computed_at DESC alone ties constantly in practice - one poll
    # upserts many stops for the same trip under the same computed_at
    # timestamp (measured live: 29 of 31 active trips had >1 row sharing an
    # identical computed_at). stop_sequence DESC as a secondary key breaks
    # the tie deterministically (furthest-along stop = most recently passed).
    delay_by_trip: dict[str, float] = {}
    if trip_ids:
        ranked_deviations = (
            select(
                ScheduleDeviation,
                func.row_number()
                .over(
                    partition_by=ScheduleDeviation.trip_id,
                    order_by=(ScheduleDeviation.computed_at.desc(), ScheduleDeviation.stop_sequence.desc()),
                )
                .label("rn"),
            )
            .where(ScheduleDeviation.trip_id.in_(trip_ids), ScheduleDeviation.service_date >= service_date_floor)
            .subquery()
        )
        RankedDeviation = aliased(ScheduleDeviation, ranked_deviations)
        latest_deviations = session.execute(
            select(RankedDeviation).where(ranked_deviations.c.rn == 1)
        ).scalars().all()
        delay_by_trip = {d.trip_id: d.delay_seconds for d in latest_deviations}

    results = []
    for s in latest_snapshots:
        trip = trip_by_id.get(s.trip_id) if s.trip_id else None

        next_stop_id = next_stop_name = next_stop_scheduled_time = None
        if s.trip_id and s.current_stop_sequence is not None:
            next_stop = next_stop_by_key.get((s.trip_id, s.current_stop_sequence))
            if next_stop:
                next_stop_id, next_stop_name, next_arrival_seconds = next_stop
                next_stop_scheduled_time = _format_scheduled_clock(next_arrival_seconds)

        exact_deviation = (
            deviation_by_key.get((s.trip_id, s.current_stop_sequence))
            if s.trip_id and s.current_stop_sequence is not None
            else None
        )

        results.append(
            LiveVehicle(
                vehicle_id=s.vehicle_id,
                trip_id=s.trip_id,
                route_id=s.route_id,
                route_short_name=route_short_name_by_id.get(s.route_id),
                direction_id=trip.direction_id if trip else None,
                trip_headsign=trip.trip_headsign if trip else None,
                latitude=s.latitude,
                longitude=s.longitude,
                bearing=s.bearing,
                current_status=s.current_status,
                vehicle_timestamp=s.vehicle_timestamp,
                delay_seconds=(
                    exact_deviation.delay_seconds
                    if exact_deviation
                    else (delay_by_trip.get(s.trip_id) if s.trip_id else None)
                ),
                current_stop_id=s.stop_id,
                current_stop_name=stop_name_by_id.get(s.stop_id) if s.stop_id else None,
                next_stop_id=next_stop_id,
                next_stop_name=next_stop_name,
                next_stop_scheduled_time=next_stop_scheduled_time,
                scheduled_time=_format_scheduled_clock(exact_deviation.scheduled_time_seconds) if exact_deviation else None,
                actual_time=_format_actual_clock(exact_deviation.actual_time) if exact_deviation else None,
            )
        )
    return results

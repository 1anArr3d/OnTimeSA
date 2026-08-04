"""Live vehicle positions for the map view: latest snapshot per vehicle,
color-coded using the most recently computed delay for that vehicle's trip,
plus enough context (direction, current/next stop, scheduled vs. actual
time) to build a real click popup - all derived from data already ingested,
no new external calls.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
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

    # Next stop per trip: one query for every trip's full stop list, then pick
    # the first stop_sequence past the vehicle's current one in Python -
    # avoids an N+1 query across ~400+ vehicles.
    stop_times_by_trip: dict[str, list[tuple[int, str, str | None, int | None]]] = {}
    if trip_ids:
        rows = (
            session.query(StopTime.trip_id, StopTime.stop_sequence, StopTime.stop_id, StopTime.arrival_time_seconds, Stop.stop_name)
            .join(Stop, Stop.stop_id == StopTime.stop_id)
            .filter(StopTime.trip_id.in_(trip_ids))
            .order_by(StopTime.trip_id, StopTime.stop_sequence)
            .all()
        )
        for trip_id, seq, stop_id, arrival_seconds, stop_name in rows:
            stop_times_by_trip.setdefault(trip_id, []).append((seq, stop_id, stop_name, arrival_seconds))

    # Deviation matched to the vehicle's exact current stop-visit
    # (trip_id, stop_sequence) - precise "scheduled vs actual" for the
    # specific stop shown, not just "most recently computed for this trip"
    # (which could be a different, unrelated stop given trip_updates predict
    # many stops ahead each poll).
    deviation_by_key: dict[tuple[str, int], ScheduleDeviation] = {}
    if trip_ids:
        deviations = session.query(ScheduleDeviation).filter(ScheduleDeviation.trip_id.in_(trip_ids)).all()
        for d in deviations:
            deviation_by_key[(d.trip_id, d.stop_sequence)] = d

    # Fallback delay-by-trip (most recently computed, any stop) for the map
    # dot color when there's no exact match for the vehicle's current stop -
    # e.g. mid-block, between two predicted stops.
    delay_by_trip: dict[str, float] = {}
    if trip_ids:
        ranked_deviations = (
            select(
                ScheduleDeviation,
                func.row_number()
                .over(partition_by=ScheduleDeviation.trip_id, order_by=ScheduleDeviation.computed_at.desc())
                .label("rn"),
            )
            .where(ScheduleDeviation.trip_id.in_(trip_ids))
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
        stop_times = stop_times_by_trip.get(s.trip_id, []) if s.trip_id else []

        next_stop_id = next_stop_name = next_stop_scheduled_time = None
        if s.current_stop_sequence is not None and stop_times:
            upcoming = [st for st in stop_times if st[0] > s.current_stop_sequence]
            if upcoming:
                _, next_stop_id, next_stop_name, next_arrival_seconds = min(upcoming, key=lambda st: st[0])
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

"""Compute schedule deviations (actual vs. scheduled time) from decoded
GTFS-RT observations, matched against the static schedule already loaded
into Postgres.

Two independent sources are used:
- TripUpdates: gives an explicit per-stop prediction (arrival/departure time
  or delay) keyed by (trip_id, stop_sequence, stop_id) - matches the static
  schedule almost exactly since both come from the same underlying system.
- VehiclePositions: gives current position + "the stop it's currently at/near",
  which is noisier and needs the tolerant nearest-stop matching in
  app/gtfs/matching.py. Only STOPPED_AT/INCOMING_AT observations are used -
  IN_TRANSIT_TO means the vehicle hasn't reached that stop yet, so treating
  its timestamp as an arrival would systematically bias delay early.

All actual times are Unix epoch (UTC) straight from the RT feed. Scheduled
times are converted to UTC via app/gtfs/timezone_utils before diffing -
never diff a naive local-seconds value against an epoch value directly.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.gtfs.matching import match_stop_time
from app.gtfs.timezone_utils import infer_service_date, parse_rt_start_date, scheduled_seconds_to_utc
from app.models import ScheduleDeviation, Stop, StopTime, Trip

logger = logging.getLogger(__name__)

# IN_TRANSIT_TO deliberately excluded - see module docstring.
VEHICLE_POSITION_DEVIATION_STATUSES = {"STOPPED_AT", "INCOMING_AT"}


def _epoch_to_utc(epoch_seconds: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)


def load_trip_context(session: Session, trip_ids: set[str]) -> dict[str, dict]:
    """Load each trip's authoritative route_id and full stop_times (with stop
    lat/lon) from the static schedule. Trip ids not found in our static data
    are simply absent from the result - callers skip those observations.
    """
    if not trip_ids:
        return {}

    trips = session.query(Trip.trip_id, Trip.route_id).filter(Trip.trip_id.in_(trip_ids)).all()
    route_by_trip = {t.trip_id: t.route_id for t in trips}
    if not route_by_trip:
        return {}

    rows = (
        session.query(
            StopTime.trip_id,
            StopTime.stop_sequence,
            StopTime.stop_id,
            StopTime.arrival_time_seconds,
            StopTime.departure_time_seconds,
            Stop.stop_lat,
            Stop.stop_lon,
        )
        .join(Stop, Stop.stop_id == StopTime.stop_id)
        .filter(StopTime.trip_id.in_(route_by_trip.keys()))
        .all()
    )

    context: dict[str, dict] = {tid: {"route_id": route_id, "stop_times": []} for tid, route_id in route_by_trip.items()}
    for r in rows:
        context[r.trip_id]["stop_times"].append(
            {
                "stop_sequence": r.stop_sequence,
                "stop_id": r.stop_id,
                "arrival_time_seconds": r.arrival_time_seconds,
                "departure_time_seconds": r.departure_time_seconds,
                "stop_lat": r.stop_lat,
                "stop_lon": r.stop_lon,
            }
        )
    return context


def compute_deviations_from_trip_updates(session: Session, trip_updates: list[dict]) -> list[ScheduleDeviation]:
    if not trip_updates:
        return []

    trip_ids = {tu["trip_id"] for tu in trip_updates}
    trip_context = load_trip_context(session, trip_ids)

    deviations: list[ScheduleDeviation] = []
    skipped_unknown_trip = 0
    skipped_unmatched_stop = 0

    for tu in trip_updates:
        ctx = trip_context.get(tu["trip_id"])
        if ctx is None:
            skipped_unknown_trip += 1
            continue

        matched, match_type = match_stop_time(ctx["stop_times"], stop_sequence=tu["stop_sequence"], stop_id=tu["stop_id"])
        if matched is None:
            skipped_unmatched_stop += 1
            continue

        scheduled_seconds = matched["arrival_time_seconds"]
        event_type = "arrival"
        actual_epoch = tu["arrival_time"]
        delay = tu["arrival_delay"]
        if scheduled_seconds is None:
            scheduled_seconds = matched["departure_time_seconds"]
            event_type = "departure"
            actual_epoch = tu["departure_time"]
            delay = tu["departure_delay"]
        if scheduled_seconds is None:
            continue

        actual_utc = _epoch_to_utc(actual_epoch) if actual_epoch is not None else None

        service_date = parse_rt_start_date(tu["start_date"])
        if service_date is None:
            reference = actual_utc or datetime.datetime.now(datetime.timezone.utc)
            service_date = infer_service_date(reference, scheduled_seconds)

        scheduled_utc = scheduled_seconds_to_utc(service_date, scheduled_seconds)

        if delay is not None:
            delay_seconds = delay
            if actual_utc is None:
                actual_utc = scheduled_utc + datetime.timedelta(seconds=delay)
        elif actual_utc is not None:
            delay_seconds = round((actual_utc - scheduled_utc).total_seconds())
        else:
            continue

        deviations.append(
            ScheduleDeviation(
                trip_id=tu["trip_id"],
                route_id=ctx["route_id"],
                stop_id=matched["stop_id"],
                stop_sequence=matched["stop_sequence"],
                service_date=service_date,
                scheduled_time_seconds=scheduled_seconds,
                actual_time=actual_utc,
                delay_seconds=delay_seconds,
                event_type=event_type,
                source="trip_update",
                match_type=match_type,
            )
        )

    if skipped_unknown_trip or skipped_unmatched_stop:
        logger.info(
            "TripUpdates deviation calc: %d matched, %d skipped (unknown trip), %d skipped (no stop match)",
            len(deviations),
            skipped_unknown_trip,
            skipped_unmatched_stop,
        )

    return deviations


def compute_deviations_from_vehicle_positions(session: Session, vehicle_positions: list[dict]) -> list[ScheduleDeviation]:
    candidates = [
        vp
        for vp in vehicle_positions
        if vp["trip_id"] and vp["timestamp"] and vp["current_status"] in VEHICLE_POSITION_DEVIATION_STATUSES
    ]
    if not candidates:
        return []

    trip_ids = {vp["trip_id"] for vp in candidates}
    trip_context = load_trip_context(session, trip_ids)

    deviations: list[ScheduleDeviation] = []
    skipped_unknown_trip = 0
    skipped_unmatched_stop = 0

    for vp in candidates:
        ctx = trip_context.get(vp["trip_id"])
        if ctx is None:
            skipped_unknown_trip += 1
            continue

        matched, match_type = match_stop_time(
            ctx["stop_times"],
            stop_sequence=vp["current_stop_sequence"],
            stop_id=vp["stop_id"],
            lat=vp["latitude"],
            lon=vp["longitude"],
        )
        if matched is None:
            skipped_unmatched_stop += 1
            continue

        scheduled_seconds = matched["arrival_time_seconds"] or matched["departure_time_seconds"]
        if scheduled_seconds is None:
            continue

        actual_utc = _epoch_to_utc(vp["timestamp"])
        service_date = parse_rt_start_date(vp["start_date"]) or infer_service_date(actual_utc, scheduled_seconds)
        scheduled_utc = scheduled_seconds_to_utc(service_date, scheduled_seconds)
        delay_seconds = round((actual_utc - scheduled_utc).total_seconds())

        deviations.append(
            ScheduleDeviation(
                trip_id=vp["trip_id"],
                route_id=ctx["route_id"],
                stop_id=matched["stop_id"],
                stop_sequence=matched["stop_sequence"],
                service_date=service_date,
                scheduled_time_seconds=scheduled_seconds,
                actual_time=actual_utc,
                delay_seconds=delay_seconds,
                event_type="arrival",
                source="vehicle_position",
                match_type=match_type,
            )
        )

    if skipped_unknown_trip or skipped_unmatched_stop:
        logger.info(
            "VehiclePositions deviation calc: %d matched, %d skipped (unknown trip), %d skipped (no stop match)",
            len(deviations),
            skipped_unknown_trip,
            skipped_unmatched_stop,
        )

    return deviations


_UPSERT_COLUMNS = (
    "route_id",
    "stop_id",
    "scheduled_time_seconds",
    "actual_time",
    "delay_seconds",
    "source",
    "match_type",
    "computed_at",
)


def upsert_schedule_deviations(session: Session, deviations: list[ScheduleDeviation]) -> None:
    """Persist computed deviations, one row per (trip_id, stop_sequence,
    service_date, event_type) - see ScheduleDeviation's docstring for why
    this must be an upsert and not a plain insert. A stop's row keeps
    refining across polls as TripUpdates predictions firm up, and stops
    changing once VIA's feed drops predictions for a stop the vehicle has
    already passed.
    """
    if not deviations:
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    rows = {}
    for d in deviations:
        key = (d.trip_id, d.stop_sequence, d.service_date, d.event_type)
        # Last one wins if this poll somehow produced two candidates for the
        # same stop-visit (e.g. a nearest-geographic collision) - avoids a
        # Postgres "ON CONFLICT DO UPDATE affects row a second time" error.
        rows[key] = {
            "trip_id": d.trip_id,
            "route_id": d.route_id,
            "stop_id": d.stop_id,
            "stop_sequence": d.stop_sequence,
            "service_date": d.service_date,
            "scheduled_time_seconds": d.scheduled_time_seconds,
            "actual_time": d.actual_time,
            "delay_seconds": d.delay_seconds,
            "event_type": d.event_type,
            "source": d.source,
            "match_type": d.match_type,
            "computed_at": now,
        }

    stmt = pg_insert(ScheduleDeviation).values(list(rows.values()))
    stmt = stmt.on_conflict_do_update(
        index_elements=["trip_id", "stop_sequence", "service_date", "event_type"],
        set_={col: getattr(stmt.excluded, col) for col in _UPSERT_COLUMNS},
    )
    session.execute(stmt)

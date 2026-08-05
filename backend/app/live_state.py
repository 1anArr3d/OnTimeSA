"""In-memory state shared between the background poll loop and the API's
request handlers, now that both run inside the same process (see app/main.py's
lifespan). Two distinct kinds of state live here:

- StaticCache: the full static GTFS schedule (all trips' stop patterns, all
  routes, all stops), loaded once at startup and refreshed daily. Nothing
  in the normal 120s poll cycle should ever query Postgres for this - doing
  so would defeat the entire point of merging the poller into the API
  process (letting Neon idle between the hourly writes below).
- LiveState: the live vehicle snapshot served directly by /api/vehicles/live
  (no DB read), plus write buffers accumulated between hourly flushes to
  Postgres.

Concurrency model: only the background poll thread ever mutates LiveState's
buffers, and the hourly flush runs in that same thread (see main.py) - so
those never need a lock. The one field read from a different thread (the
API's request-handling thread/event loop) is `live_vehicles`, and it's never
mutated in place - each poll cycle builds a brand-new list and reassigns the
reference in one step. CPython's GIL makes a single reference reassignment
atomic, so a concurrent reader always sees either the fully-old or fully-new
list, never a half-built one - no lock needed for that either.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.gtfs.deviation import load_trip_context
from app.models import BunchingEvent, HeadwaySample, Route, ScheduleDeviation, Stop, Trip, VehiclePositionSnapshot
from app.schemas import LiveVehicle

logger = logging.getLogger(__name__)


@dataclass
class StaticCache:
    # trip_id -> {"route_id": ..., "stop_times": [...]} - same shape
    # load_trip_context() already produces, just loaded for every trip up
    # front instead of a narrow per-poll subset.
    trip_context: dict[str, dict]
    trips_by_id: dict[str, Trip]
    routes_by_id: dict[str, Route]
    stops_by_id: dict[str, Stop]
    loaded_at: datetime.datetime


def load_static_cache(session: Session) -> StaticCache:
    all_trip_ids = {row[0] for row in session.query(Trip.trip_id).all()}
    trip_context = load_trip_context(session, all_trip_ids)
    trips_by_id = {t.trip_id: t for t in session.query(Trip).all()}
    routes_by_id = {r.route_id: r for r in session.query(Route).all()}
    stops_by_id = {s.stop_id: s for s in session.query(Stop).all()}
    cache = StaticCache(
        trip_context=trip_context,
        trips_by_id=trips_by_id,
        routes_by_id=routes_by_id,
        stops_by_id=stops_by_id,
        loaded_at=datetime.datetime.now(datetime.timezone.utc),
    )
    logger.info(
        "Loaded static cache: %d trips, %d routes, %d stops, %d trips with stop_time context",
        len(trips_by_id),
        len(routes_by_id),
        len(stops_by_id),
        len(trip_context),
    )
    return cache


@dataclass
class LiveState:
    static_cache: StaticCache | None = None

    # Served directly by /api/vehicles/live - swapped wholesale each poll,
    # never mutated in place (see module docstring).
    live_vehicles: list[LiveVehicle] = field(default_factory=list)
    last_poll_at: datetime.datetime | None = None

    # Write buffers, flushed to Postgres roughly hourly (see app/main.py).
    pending_snapshots: list[VehiclePositionSnapshot] = field(default_factory=list)
    # Keyed exactly like upsert_schedule_deviations()'s dedup key, so many
    # polls' worth of deviations for the same stop-visit collapse into the
    # single most-recent row before ever reaching Postgres, same as the
    # upsert would have done one row at a time.
    pending_deviations: dict[tuple, ScheduleDeviation] = field(default_factory=dict)
    pending_headway_samples: list[HeadwaySample] = field(default_factory=list)

    # Bunching events within the merge window of "now", whether already
    # flushed to Postgres or not - see detect_bunching()'s `recent_events`
    # param. Kept (and pruned by age, not by flush status) specifically so
    # an event that started before an hourly flush and continues after it
    # still gets found and extended in place instead of splitting into two
    # rows - see bunching.py's docstring for the full reasoning.
    recent_bunching_events: list[BunchingEvent] = field(default_factory=list)

    last_flush_at: datetime.datetime | None = None


state = LiveState()


def prune_recent_bunching_events(now: datetime.datetime, merge_window: datetime.timedelta) -> None:
    state.recent_bunching_events = [e for e in state.recent_bunching_events if now - e.end_time <= merge_window]


def _epoch_to_utc(epoch_seconds: int | None) -> datetime.datetime | None:
    if epoch_seconds is None:
        return None
    return datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)


def build_live_vehicles(
    vehicle_positions: list[dict],
    static_cache: StaticCache,
    pending_deviations: dict[tuple, ScheduleDeviation],
) -> list[LiveVehicle]:
    """In-memory equivalent of vehicles_service.list_live_vehicles() - same
    output shape and same two-tier deviation lookup (exact stop match,
    falling back to most-recently-computed-any-stop), just built from this
    poll cycle's data plus the in-memory static cache instead of three
    database queries. The ranking rules for picking among candidate
    deviations are copied exactly from vehicles_service.py's SQL ORDER BY
    (service_date desc, arrival preferred, computed_at desc for the exact
    match; computed_at desc, stop_sequence desc for the fallback) so output
    matches byte-for-byte.
    """
    from app.vehicles_service import _format_actual_clock, _format_scheduled_clock

    by_trip: dict[str, list[ScheduleDeviation]] = {}
    for d in pending_deviations.values():
        by_trip.setdefault(d.trip_id, []).append(d)

    results: list[LiveVehicle] = []
    for vp in vehicle_positions:
        trip_id = vp["trip_id"]
        route_id = vp["route_id"]
        current_stop_sequence = vp["current_stop_sequence"]

        trip = static_cache.trips_by_id.get(trip_id) if trip_id else None
        route = static_cache.routes_by_id.get(route_id) if route_id else None
        current_stop = static_cache.stops_by_id.get(vp["stop_id"]) if vp["stop_id"] else None

        trip_devs = by_trip.get(trip_id, []) if trip_id else []
        exact_deviation = None
        if trip_id and current_stop_sequence is not None:
            same_stop = [d for d in trip_devs if d.stop_sequence == current_stop_sequence]
            if same_stop:
                exact_deviation = max(
                    same_stop, key=lambda d: (d.service_date, d.event_type == "arrival", d.computed_at)
                )
        fallback_deviation = (
            max(trip_devs, key=lambda d: (d.computed_at, d.stop_sequence)) if trip_devs else None
        )

        next_stop_id = next_stop_name = next_stop_scheduled_time = None
        if trip_id and current_stop_sequence is not None:
            ctx = static_cache.trip_context.get(trip_id)
            if ctx:
                upcoming = [st for st in ctx["stop_times"] if st["stop_sequence"] > current_stop_sequence]
                if upcoming:
                    nxt = min(upcoming, key=lambda st: st["stop_sequence"])
                    next_stop_id = nxt["stop_id"]
                    next_stop_stop = static_cache.stops_by_id.get(nxt["stop_id"])
                    next_stop_name = next_stop_stop.stop_name if next_stop_stop else None
                    next_stop_scheduled_time = _format_scheduled_clock(nxt["arrival_time_seconds"])

        results.append(
            LiveVehicle(
                vehicle_id=vp["vehicle_id"],
                trip_id=trip_id,
                route_id=route_id,
                route_short_name=route.route_short_name if route else None,
                direction_id=trip.direction_id if trip else None,
                trip_headsign=trip.trip_headsign if trip else None,
                latitude=vp["latitude"],
                longitude=vp["longitude"],
                bearing=vp["bearing"],
                current_status=vp["current_status"],
                vehicle_timestamp=_epoch_to_utc(vp["timestamp"]),
                delay_seconds=(
                    exact_deviation.delay_seconds
                    if exact_deviation
                    else (fallback_deviation.delay_seconds if fallback_deviation else None)
                ),
                current_stop_id=vp["stop_id"],
                current_stop_name=current_stop.stop_name if current_stop else None,
                next_stop_id=next_stop_id,
                next_stop_name=next_stop_name,
                next_stop_scheduled_time=next_stop_scheduled_time,
                scheduled_time=_format_scheduled_clock(exact_deviation.scheduled_time_seconds)
                if exact_deviation
                else None,
                actual_time=_format_actual_clock(exact_deviation.actual_time) if exact_deviation else None,
            )
        )
    return results

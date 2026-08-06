"""In-memory state shared between the background poll loop and the API's
request handlers, now that both run inside the same process (see app/main.py's
lifespan). Two distinct kinds of state live here:

- StaticCache: the static GTFS schedule for trips active today or yesterday
  (agency-local) - see load_static_cache()'s docstring for why it's scoped
  to that window rather than VIA's entire published schedule. Loaded once at
  startup and refreshed daily. Nothing in the normal 120s poll cycle should
  ever query Postgres for this - doing so would defeat the entire point of
  merging the poller into the API process (letting Neon idle between the
  hourly writes below).
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

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.gtfs.deviation import load_trip_context
from app.gtfs.timezone_utils import AGENCY_TIMEZONE
from app.models import (
    BunchingEvent,
    Calendar,
    HeadwaySample,
    Route,
    ScheduleDeviation,
    Stop,
    Trip,
    VehiclePositionSnapshot,
)
from app.schemas import LiveVehicle

logger = logging.getLogger(__name__)


@dataclass
class StaticCache:
    # trip_id -> {"route_id": ..., "stop_times": [...]} - same shape
    # load_trip_context() already produces, just loaded for every trip
    # active today/yesterday up front instead of a narrow per-poll subset.
    trip_context: dict[str, dict]
    trips_by_id: dict[str, Trip]
    routes_by_id: dict[str, Route]
    stops_by_id: dict[str, Stop]
    loaded_at: datetime.datetime
    # The agency-local date this cache's service window was computed for -
    # covers this date and the one before it (see load_static_cache()'s
    # docstring). app/main.py's _maybe_refresh_static_cache() compares this
    # against the current agency-local date to decide whether a refresh is
    # due, not just elapsed hours - see that function's docstring for why.
    loaded_for_date: datetime.date


_WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _active_service_ids(session: Session, dates: list[datetime.date]) -> set[str]:
    """service_ids from Calendar active on any of `dates` - weekday flag set
    for that date's day-of-week, and the date within [start_date, end_date].
    calendar_dates.txt (service exceptions/holidays) isn't ingested (see
    README's known-limitations section) - a pre-existing gap that previously
    only affected schedule-change-day accuracy and now also affects which
    trips get cached, but doesn't make it any less accurate than before.
    """
    if not dates:
        return set()
    conditions = [
        and_(getattr(Calendar, _WEEKDAY_COLUMNS[d.weekday()]).is_(True), Calendar.start_date <= d, Calendar.end_date >= d)
        for d in dates
    ]
    rows = session.query(Calendar.service_id).filter(or_(*conditions)).all()
    return {r[0] for r in rows}


def load_static_cache(session: Session) -> StaticCache:
    """Loads only trips whose service_id is active today or yesterday
    (agency-local), not VIA's entire published multi-week schedule - for a
    mostly-idle single-user deployment, caching every service pattern VIA
    will ever run (measured at 14,589 trips network-wide) rather than just
    what's relevant right now was a real, unnecessary chunk of the process's
    memory footprint (see the 2026-08-06 incident). Yesterday is included,
    not just today, so a trip still running past agency-local midnight
    (GTFS's ">24:00:00" convention for overnight trips) whose service_id
    belongs to the prior service day is still found - same 1-day-floor
    reasoning used elsewhere (see poller.py's prune_pending_deviations()).
    """
    today = datetime.datetime.now(ZoneInfo(AGENCY_TIMEZONE)).date()
    yesterday = today - datetime.timedelta(days=1)
    active_service_ids = _active_service_ids(session, [yesterday, today])

    active_trip_ids = (
        {row[0] for row in session.query(Trip.trip_id).filter(Trip.service_id.in_(active_service_ids)).all()}
        if active_service_ids
        else set()
    )
    trip_context = load_trip_context(session, active_trip_ids)
    trips_by_id = (
        {t.trip_id: t for t in session.query(Trip).filter(Trip.trip_id.in_(active_trip_ids)).all()}
        if active_trip_ids
        else {}
    )
    # Routes aren't scoped - ~89 network-wide, not worth the complexity.
    routes_by_id = {r.route_id: r for r in session.query(Route).all()}
    active_stop_ids = {st["stop_id"] for ctx in trip_context.values() for st in ctx["stop_times"]}
    stops_by_id = (
        {s.stop_id: s for s in session.query(Stop).filter(Stop.stop_id.in_(active_stop_ids)).all()}
        if active_stop_ids
        else {}
    )
    cache = StaticCache(
        trip_context=trip_context,
        trips_by_id=trips_by_id,
        routes_by_id=routes_by_id,
        stops_by_id=stops_by_id,
        loaded_at=datetime.datetime.now(datetime.timezone.utc),
        loaded_for_date=today,
    )
    logger.info(
        "Loaded static cache (service active %s/%s): %d trips, %d routes, %d stops, "
        "%d trips with stop_time context",
        yesterday,
        today,
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


def prune_pending_snapshots(now: datetime.datetime, max_age: datetime.timedelta) -> int:
    """Drop the oldest buffered snapshots once they're older than max_age -
    only bites when flush_state_to_db() has been failing for a while, since a
    healthy flush clears this list roughly every flush_interval_seconds
    anyway. Returns the number dropped so the caller can log it loudly.
    """
    cutoff = now - max_age
    kept = [s for s in state.pending_snapshots if s.polled_at >= cutoff]
    dropped = len(state.pending_snapshots) - len(kept)
    state.pending_snapshots = kept
    return dropped


def prune_pending_headway_samples(now: datetime.datetime, max_age: datetime.timedelta) -> int:
    """Same as prune_pending_snapshots(), for the other unbounded-while-flush-
    is-broken buffer."""
    cutoff = now - max_age
    kept = [s for s in state.pending_headway_samples if s.sampled_at >= cutoff]
    dropped = len(state.pending_headway_samples) - len(kept)
    state.pending_headway_samples = kept
    return dropped


def prune_pending_deviations(now: datetime.datetime) -> None:
    """Age out deviations more than a day stale. Runs every poll cycle,
    independent of flush success (unlike the buffers above, this dict is
    deduped by key rather than append-only - see flush_state_to_db()'s
    docstring for why it's kept, not cleared, across flushes - so it can't
    grow unbounded within a single day, but without this it would keep
    accumulating one day's worth of extra keys for every day a flush outage
    drags on).
    """
    today_agency_local = now.astimezone(ZoneInfo(AGENCY_TIMEZONE)).date()
    floor = today_agency_local - datetime.timedelta(days=1)
    state.pending_deviations = {k: d for k, d in state.pending_deviations.items() if d.service_date >= floor}


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

"""One GTFS-RT poll cycle: fetch both feeds, persist raw vehicle position
history, compute schedule deviations, run bunching detection, commit.

Two modes:
- poll_once() / run_scheduler(): the original DB-per-poll design. Kept for
  manual one-off polls (`python -m app.poller`) and as a reference
  implementation; no longer used in production.
- poll_once_in_memory() / flush_state_to_db(): the production path since the
  poller was merged into the API process (see app/main.py's lifespan). Polls
  every 120s exactly as before, but computes against the in-memory
  StaticCache instead of querying Postgres, and buffers results in
  app.live_state.state instead of writing them immediately - Postgres is
  only touched once/hour by flush_state_to_db(), so Neon can idle between
  flushes.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.gtfs.bunching import detect_bunching
from app.gtfs.deviation import (
    compute_deviations_from_trip_updates,
    compute_deviations_from_vehicle_positions,
    load_trip_context,
    upsert_schedule_deviations,
)
from app.gtfs.realtime import fetch_trip_updates, fetch_vehicle_positions
from app.models import VehiclePositionSnapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _epoch_to_utc(epoch_seconds: int | None) -> datetime.datetime | None:
    if epoch_seconds is None:
        return None
    return datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)


def poll_once(session: Session) -> dict:
    poll_time = datetime.datetime.now(datetime.timezone.utc)

    vehicle_positions = fetch_vehicle_positions()
    trip_updates = fetch_trip_updates()
    logger.info("Polled %d vehicle positions, %d trip update stop-times", len(vehicle_positions), len(trip_updates))

    # Nothing to persist: no vehicle is on an active trip, and TripUpdates has
    # nothing either - this is either the ~2-hour real overnight gap (checked
    # against the static schedule: every service day has a genuine window
    # with zero scheduled trips) or a feed outage. Either way there's nothing
    # to write, so return before touching `session` at all rather than after -
    # SQLAlchemy's Session doesn't open a real Postgres connection until the
    # first query/execute, so skipping that first touch here means Neon sees
    # no connection this cycle at all, not just an empty transaction. This
    # also can't be confused with "sleep because we assume service ended":
    # it re-checks the real feed every cycle (still on the normal interval),
    # so a feed outage during genuine service hours or an earlier-than-
    # expected resumption both self-correct on the very next poll instead of
    # depending on a predicted schedule.
    if not any(vp["trip_id"] for vp in vehicle_positions) and not trip_updates:
        logger.info("No active trips this cycle - skipping the database entirely so Neon can idle.")
        return {
            "polled_at": poll_time.isoformat(),
            "vehicle_positions": len(vehicle_positions),
            "trip_update_stop_times": len(trip_updates),
            "deviations_from_trip_updates": 0,
            "deviations_from_vehicle_positions": 0,
            "headway_samples": 0,
            "new_bunching_events": 0,
            "skipped_db": True,
        }

    # Load each active trip's static schedule context ONCE for the union of
    # every trip_id any of the three consumers below might need, instead of
    # each independently re-querying Postgres for its own heavily-overlapping
    # subset - measured as ~2x redundant stop_times rows fetched per poll
    # before this change (the deviation/bunching candidate sets are subsets
    # of "every trip_id with a vehicle report", so this union is a safe,
    # simple superset rather than needing to replicate each function's exact
    # candidate-filtering logic here).
    all_trip_ids = {tu["trip_id"] for tu in trip_updates} | {vp["trip_id"] for vp in vehicle_positions if vp["trip_id"]}
    trip_context = load_trip_context(session, all_trip_ids)

    snapshots = [
        VehiclePositionSnapshot(
            vehicle_id=vp["vehicle_id"],
            trip_id=vp["trip_id"],
            route_id=vp["route_id"],
            latitude=vp["latitude"],
            longitude=vp["longitude"],
            bearing=vp["bearing"],
            speed=vp["speed"],
            current_stop_sequence=vp["current_stop_sequence"],
            stop_id=vp["stop_id"],
            current_status=vp["current_status"],
            vehicle_timestamp=_epoch_to_utc(vp["timestamp"]),
            polled_at=poll_time,
        )
        for vp in vehicle_positions
    ]
    session.add_all(snapshots)

    tu_deviations = compute_deviations_from_trip_updates(session, trip_updates, trip_context=trip_context)
    vp_deviations = compute_deviations_from_vehicle_positions(session, vehicle_positions, trip_context=trip_context)
    # Upsert, not insert - see ScheduleDeviation's docstring. Without this,
    # TripUpdates' predictions-for-every-remaining-stop behavior turns this
    # into ~12M redundant rows/day instead of one row per realized stop visit.
    upsert_schedule_deviations(session, tu_deviations)
    upsert_schedule_deviations(session, vp_deviations)

    # trip_updates are the more authoritative source (explicit per-stop
    # predictions from the agency's own system) - prefer them when a trip
    # has a deviation from both sources this cycle.
    trip_delays: dict[str, float] = {d.trip_id: d.delay_seconds for d in vp_deviations}
    trip_delays.update({d.trip_id: d.delay_seconds for d in tu_deviations})

    headway_samples, bunching_events = detect_bunching(
        session, vehicle_positions, trip_delays, poll_time, trip_context=trip_context
    )
    session.add_all(headway_samples)
    session.add_all(bunching_events)

    session.commit()

    summary = {
        "polled_at": poll_time.isoformat(),
        "vehicle_positions": len(vehicle_positions),
        "trip_update_stop_times": len(trip_updates),
        "deviations_from_trip_updates": len(tu_deviations),
        "deviations_from_vehicle_positions": len(vp_deviations),
        "headway_samples": len(headway_samples),
        "new_bunching_events": len(bunching_events),
    }
    logger.info("Poll cycle complete: %s", summary)
    return summary


def poll_once_in_memory(state) -> dict:  # state: app.live_state.LiveState
    """In-memory equivalent of poll_once() - see module docstring. No
    `session` parameter at all: a normal cycle never touches the database,
    only fetches VIA's feeds (HTTP, not Postgres) and computes against
    state.static_cache, which is loaded once at startup/refreshed daily
    (see app/live_state.py) rather than queried here.
    """
    from app.live_state import (
        build_live_vehicles,
        prune_pending_deviations,
        prune_pending_headway_samples,
        prune_pending_snapshots,
        prune_recent_bunching_events,
    )

    poll_time = datetime.datetime.now(datetime.timezone.utc)

    vehicle_positions = fetch_vehicle_positions()
    trip_updates = fetch_trip_updates()
    logger.info("Polled %d vehicle positions, %d trip update stop-times", len(vehicle_positions), len(trip_updates))

    state.last_poll_at = poll_time

    if not any(vp["trip_id"] for vp in vehicle_positions) and not trip_updates:
        logger.info("No active trips this cycle - live vehicle list cleared, nothing buffered.")
        state.live_vehicles = []
        return {
            "polled_at": poll_time.isoformat(),
            "vehicle_positions": len(vehicle_positions),
            "trip_update_stop_times": len(trip_updates),
            "deviations_from_trip_updates": 0,
            "deviations_from_vehicle_positions": 0,
            "headway_samples": 0,
            "new_bunching_events": 0,
            "skipped_db": True,
        }

    trip_context = state.static_cache.trip_context

    snapshots = [
        VehiclePositionSnapshot(
            vehicle_id=vp["vehicle_id"],
            trip_id=vp["trip_id"],
            route_id=vp["route_id"],
            latitude=vp["latitude"],
            longitude=vp["longitude"],
            bearing=vp["bearing"],
            speed=vp["speed"],
            current_stop_sequence=vp["current_stop_sequence"],
            stop_id=vp["stop_id"],
            current_status=vp["current_status"],
            vehicle_timestamp=_epoch_to_utc(vp["timestamp"]),
            polled_at=poll_time,
        )
        for vp in vehicle_positions
    ]
    state.pending_snapshots.extend(snapshots)

    tu_deviations = compute_deviations_from_trip_updates(None, trip_updates, trip_context=trip_context)
    vp_deviations = compute_deviations_from_vehicle_positions(None, vehicle_positions, trip_context=trip_context)
    # Same dedup semantics as the two separate upsert_schedule_deviations()
    # calls poll_once() makes (tu first, then vp - vp wins a same-key
    # collision since ON CONFLICT DO UPDATE applies whichever runs last),
    # just as one dict instead of two SQL upserts.
    for d in tu_deviations:
        key = (d.trip_id, d.stop_sequence, d.service_date, d.event_type)
        state.pending_deviations[key] = d
    for d in vp_deviations:
        key = (d.trip_id, d.stop_sequence, d.service_date, d.event_type)
        state.pending_deviations[key] = d

    trip_delays: dict[str, float] = {d.trip_id: d.delay_seconds for d in vp_deviations}
    trip_delays.update({d.trip_id: d.delay_seconds for d in tu_deviations})

    threshold_minutes_seconds = _bunching_merge_window_seconds()
    merge_window = datetime.timedelta(seconds=threshold_minutes_seconds)
    headway_samples, bunching_events = detect_bunching(
        None,
        vehicle_positions,
        trip_delays,
        poll_time,
        trip_context=trip_context,
        recent_events=state.recent_bunching_events,
    )
    state.pending_headway_samples.extend(headway_samples)
    # detect_bunching() mutates existing events from recent_events in place
    # (already tracked there) - only brand-new ones need to be added.
    state.recent_bunching_events.extend(bunching_events)
    prune_recent_bunching_events(poll_time, merge_window)

    # Age/size-bound the write buffers every cycle, independent of whether the
    # next flush succeeds - see _pending_buffer_max_age()'s docstring for why.
    prune_pending_deviations(poll_time)
    max_pending_age = _pending_buffer_max_age()
    dropped_snapshots = prune_pending_snapshots(poll_time, max_pending_age)
    dropped_headway_samples = prune_pending_headway_samples(poll_time, max_pending_age)
    if dropped_snapshots or dropped_headway_samples:
        logger.warning(
            "Pending buffer exceeded %s of unflushed data - dropped %d snapshots, "
            "%d headway samples (%s)",
            max_pending_age,
            dropped_snapshots,
            dropped_headway_samples,
            _flush_staleness_description(poll_time),
        )

    state.live_vehicles = build_live_vehicles(vehicle_positions, state.static_cache, state.pending_deviations)

    summary = {
        "polled_at": poll_time.isoformat(),
        "vehicle_positions": len(vehicle_positions),
        "trip_update_stop_times": len(trip_updates),
        "deviations_from_trip_updates": len(tu_deviations),
        "deviations_from_vehicle_positions": len(vp_deviations),
        "headway_samples": len(headway_samples),
        "new_bunching_events": len(bunching_events),
    }
    logger.info("Poll cycle complete (in-memory): %s", summary)
    return summary


def _bunching_merge_window_seconds() -> int:
    from app.config import settings
    from app.gtfs.bunching import EVENT_MERGE_WINDOW_MULTIPLIER

    return settings.gtfs_rt_poll_seconds * EVENT_MERGE_WINDOW_MULTIPLIER


def _pending_buffer_max_age() -> datetime.timedelta:
    """How much unflushed data pending_snapshots/pending_headway_samples are
    allowed to hold before the oldest gets dropped - see
    settings.pending_buffer_retention_multiplier's docstring. A flush retries
    every poll cycle once due (see _poll_job in app/main.py), so this only
    ever bites during a sustained outage, not routine hourly flushing.
    """
    from app.config import settings

    return datetime.timedelta(seconds=settings.flush_interval_seconds * settings.pending_buffer_retention_multiplier)


def _flush_staleness_description(now: datetime.datetime) -> str:
    from app.live_state import state

    if state.last_flush_at is None:
        return "flush has never succeeded since this process started"
    return f"flush has been failing for {now - state.last_flush_at}"


def flush_state_to_db(state, session: Session) -> dict:  # state: app.live_state.LiveState
    """Write everything buffered in `state` to Postgres in one transaction -
    the only time a normal poll cycle actually touches the database. Safe to
    call with an empty buffer (e.g. right after the previous flush, or
    during a stretch of skipped/empty polls) - each step is a no-op if its
    list/dict is empty.

    Deliberately does NOT clear pending_deviations or recent_bunching_events
    afterward - both are pruned by age elsewhere (live_state.
    prune_pending_deviations() and prune_recent_bunching_events(), called
    every poll cycle regardless of flush success) rather than here, and both
    are re-flushed on subsequent hourly cycles even when unchanged
    (upsert_schedule_deviations is idempotent, and re-writing an unchanged
    BunchingEvent row is harmless), because both are still needed in memory
    afterward: pending_deviations
    for the live map's "most recently computed, any stop" fallback lookup
    (see live_state.build_live_vehicles), and recent_bunching_events so an
    event that started before this flush and continues after it is still
    found and extended rather than split into two rows.
    """
    # SQLAlchemy expires ORM object attributes after commit() by default -
    # every later read of a field on one of these objects would silently
    # trigger its own fresh SELECT. Since state.recent_bunching_events keeps
    # referencing these same objects across many poll cycles after they're
    # flushed (see docstring above), that default would mean every future
    # detect_bunching() extend-lookup quietly re-opens a database connection
    # per event touched - exactly what this whole change is meant to avoid.
    # Disabling it (on this session only, not the app-wide sessionmaker) is
    # what makes those later in-memory reads actually stay in memory.
    session.expire_on_commit = False

    flushed_at = datetime.datetime.now(datetime.timezone.utc)

    snapshot_count = len(state.pending_snapshots)
    if state.pending_snapshots:
        session.add_all(state.pending_snapshots)

    deviation_count = len(state.pending_deviations)
    if state.pending_deviations:
        upsert_schedule_deviations(session, list(state.pending_deviations.values()))

    headway_count = len(state.pending_headway_samples)
    if state.pending_headway_samples:
        session.add_all(state.pending_headway_samples)

    new_bunching = [e for e in state.recent_bunching_events if getattr(e, "_dirty", False) and e.id is None]
    updated_bunching = [e for e in state.recent_bunching_events if getattr(e, "_dirty", False) and e.id is not None]
    if new_bunching:
        session.add_all(new_bunching)
    for e in updated_bunching:
        session.merge(e)

    session.commit()

    for e in state.recent_bunching_events:
        e._dirty = False

    state.pending_snapshots = []
    state.pending_headway_samples = []
    # pending_deviations is no longer pruned here - see
    # live_state.prune_pending_deviations(), now called unconditionally every
    # poll cycle instead of only after a successful flush.

    state.last_flush_at = flushed_at
    summary = {
        "flushed_at": flushed_at.isoformat(),
        "snapshots": snapshot_count,
        "deviations": deviation_count,
        "headway_samples": headway_count,
        "new_bunching_events": len(new_bunching),
        "updated_bunching_events": len(updated_bunching),
    }
    logger.info("Flushed buffered state to Postgres: %s", summary)
    return summary


def run_scheduler() -> None:
    """Run poll_once() on a recurring interval, plus a once-daily rollup+prune
    job, via APScheduler. Blocks forever.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    from app.config import settings
    from app.db import get_sessionmaker
    from app.gtfs.timezone_utils import AGENCY_TIMEZONE
    from app.rollup_service import run_daily_maintenance

    Session = get_sessionmaker()

    def _poll_job():
        session = Session()
        try:
            poll_once(session)
        except Exception:
            logger.exception("GTFS-RT poll cycle failed")
            session.rollback()
        finally:
            session.close()

    def _maintenance_job():
        session = Session()
        try:
            summary = run_daily_maintenance(session)
            logger.info("Daily rollup+prune complete: %s", summary)
        except Exception:
            logger.exception("Daily rollup+prune job failed")
            session.rollback()
        finally:
            session.close()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _poll_job, "interval", seconds=settings.gtfs_rt_poll_seconds, next_run_time=datetime.datetime.now()
    )
    # Shortly after agency-local midnight, so the rollup covers a fully
    # completed service day before it runs.
    scheduler.add_job(_maintenance_job, "cron", hour=0, minute=10, timezone=AGENCY_TIMEZONE)
    logger.info(
        "Starting GTFS-RT poll scheduler (every %ds) + daily rollup/prune job (00:10 %s)",
        settings.gtfs_rt_poll_seconds,
        AGENCY_TIMEZONE,
    )
    scheduler.start()


if __name__ == "__main__":
    from app.db import get_sessionmaker

    Session = get_sessionmaker()
    db_session = Session()
    try:
        poll_once(db_session)
    finally:
        db_session.close()

"""One GTFS-RT poll cycle: fetch both feeds, persist raw vehicle position
history, compute schedule deviations, run bunching detection, commit.

Intended to be called on a schedule (see run_scheduler() using APScheduler,
or `python -m app.poller` for a single one-off run during development).
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

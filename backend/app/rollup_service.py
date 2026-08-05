"""Daily maintenance for the Neon free-tier storage cap (0.5 GB): roll each
day's raw schedule_deviations/bunching_events into one daily_route_stats row
per route, then prune raw rows older than settings.raw_data_retention_days.

Two entry points, meant to run in this order every day (see
app.poller.run_scheduler):

- compute_and_store_daily_stats(): rolls up ONE service_date. Idempotent -
  upserts on (route_id, service_date), so re-running it (e.g. a retry after
  a crash) just recomputes the same rows rather than duplicating them.
- prune_old_raw_data(): bulk-deletes vehicle_position_snapshots and
  schedule_deviations older than the retention window. Deliberately does not
  touch headway_samples or bunching_events - those are low-volume and/or the
  actual event log, not raw time-series noise.

The rollup must run before the prune for a given day, not concurrently -
otherwise the raw rows a same-day rollup depends on could already be gone.
run_daily_maintenance() enforces that ordering by construction: it always
rolls up first, then prunes.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.gtfs.timezone_utils import AGENCY_TIMEZONE
from app.models import BunchingEvent, DailyRouteStat, ScheduleDeviation, VehiclePositionSnapshot

logger = logging.getLogger(__name__)


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


def _agency_zoneinfo():
    from zoneinfo import ZoneInfo

    return ZoneInfo(AGENCY_TIMEZONE)


def compute_and_store_daily_stats(session: Session, service_date: datetime.date) -> int:
    """Upsert one daily_route_stats row per route that has schedule_deviation
    rows on service_date. Returns the number of routes rolled up.
    """
    dev_query = (
        select(
            ScheduleDeviation.route_id,
            func.count().label("sample_count"),
            func.avg(ScheduleDeviation.delay_seconds).label("avg_delay"),
            func.sum(_on_time_case()).label("on_time_count"),
        )
        .where(
            ScheduleDeviation.service_date == service_date,
            ScheduleDeviation.event_type == "arrival",
        )
        .group_by(ScheduleDeviation.route_id)
    )
    dev_rows = session.execute(dev_query).all()
    if not dev_rows:
        logger.info("Daily rollup for %s: no schedule_deviations found, nothing to roll up", service_date)
        return 0

    day_start = datetime.datetime.combine(service_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    day_end = datetime.datetime.combine(service_date, datetime.time.max, tzinfo=datetime.timezone.utc)
    bunching_query = (
        select(BunchingEvent.route_id, func.count().label("bunching_count"))
        .where(BunchingEvent.start_time >= day_start, BunchingEvent.start_time <= day_end)
        .group_by(BunchingEvent.route_id)
    )
    bunching_by_route = {row.route_id: row.bunching_count for row in session.execute(bunching_query)}

    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [
        {
            "route_id": r.route_id,
            "service_date": service_date,
            "on_time_pct": (r.on_time_count / r.sample_count * 100) if r.sample_count else None,
            "avg_delay_seconds": float(r.avg_delay) if r.avg_delay is not None else None,
            "bunching_event_count": bunching_by_route.get(r.route_id, 0),
            "sample_count": r.sample_count,
            "computed_at": now,
        }
        for r in dev_rows
    ]

    stmt = pg_insert(DailyRouteStat).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["route_id", "service_date"],
        set_={
            col: getattr(stmt.excluded, col)
            for col in ("on_time_pct", "avg_delay_seconds", "bunching_event_count", "sample_count", "computed_at")
        },
    )
    session.execute(stmt)
    logger.info("Daily rollup for %s: upserted %d route rows", service_date, len(rows))
    return len(rows)


def prune_old_raw_data(session: Session, cutoff_date: datetime.date) -> dict:
    """Bulk-delete vehicle_position_snapshots and schedule_deviations older
    than cutoff_date. Bulk DELETE, not row-by-row, per Neon's guidance that
    bulk operations log more efficiently.
    """
    cutoff_dt = datetime.datetime.combine(cutoff_date, datetime.time.min, tzinfo=datetime.timezone.utc)

    vp_result = session.execute(delete(VehiclePositionSnapshot).where(VehiclePositionSnapshot.polled_at < cutoff_dt))
    dev_result = session.execute(delete(ScheduleDeviation).where(ScheduleDeviation.service_date < cutoff_date))

    counts = {"vehicle_position_snapshots": vp_result.rowcount, "schedule_deviations": dev_result.rowcount}
    logger.info("Pruned raw data older than %s: %s", cutoff_date, counts)
    return counts


def run_daily_maintenance(session: Session, today: datetime.date | None = None) -> dict:
    """Roll up yesterday's data, then prune anything older than the
    retention window - always in that order, in one transaction.

    Ordering is safe by construction: a day D only ever gets pruned once
    it's `raw_data_retention_days` old, and it was already rolled up back
    when it was "yesterday" (one day old) - long before that.
    """
    today = today or datetime.datetime.now(_agency_zoneinfo()).date()
    service_date = today - datetime.timedelta(days=1)
    rolled_up_routes = compute_and_store_daily_stats(session, service_date)

    cutoff_date = today - datetime.timedelta(days=settings.raw_data_retention_days)
    pruned = prune_old_raw_data(session, cutoff_date)

    session.commit()
    return {
        "rolled_up_service_date": service_date.isoformat(),
        "rolled_up_routes": rolled_up_routes,
        "pruned": pruned,
        "prune_cutoff_date": cutoff_date.isoformat(),
    }

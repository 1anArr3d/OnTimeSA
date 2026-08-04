"""Load a parsed GTFS static feed into Postgres.

Uses upsert (INSERT ... ON CONFLICT DO UPDATE) rather than a truncate-and-reload,
because vehicle_position_snapshots / schedule_deviations / bunching_events hold
foreign keys into routes/stops/trips and must survive a static refresh even if a
trip_id drops out of the new feed. This means stale rows (e.g. a discontinued
route) are not removed automatically - acceptable for this project's scope.
"""

from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.gtfs.static import GtfsStaticFeed
from app.models import Calendar, Route, Stop, StopTime, Trip

logger = logging.getLogger(__name__)

_BATCH_SIZE = 5000


def _upsert(session: Session, model, rows: list[dict], pk_columns: list[str]) -> None:
    if not rows:
        return

    update_columns = [c for c in rows[0].keys() if c not in pk_columns]

    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start : start + _BATCH_SIZE]
        stmt = insert(model).values(batch)
        if update_columns:
            stmt = stmt.on_conflict_do_update(
                index_elements=pk_columns,
                set_={col: getattr(stmt.excluded, col) for col in update_columns},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_columns)
        session.execute(stmt)


def load_static_feed(session: Session, feed: GtfsStaticFeed) -> None:
    """Upsert a parsed feed into the DB in FK-safe order, committing once at the end."""
    _upsert(session, Route, feed.routes, ["route_id"])
    _upsert(session, Stop, feed.stops, ["stop_id"])
    _upsert(session, Calendar, feed.calendar, ["service_id"])
    session.flush()

    _upsert(session, Trip, feed.trips, ["trip_id"])
    session.flush()

    _upsert(session, StopTime, feed.stop_times, ["trip_id", "stop_sequence"])

    session.commit()
    logger.info("GTFS static load complete")

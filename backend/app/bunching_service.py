from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from app.models import BunchingEvent, Route, Stop
from app.schemas import BunchingEventOut


def list_bunching_events(
    session: Session,
    route_id: str | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    limit: int = 50,
) -> list[BunchingEventOut]:
    query = session.query(BunchingEvent, Route, Stop).join(Route, Route.route_id == BunchingEvent.route_id).outerjoin(
        Stop, Stop.stop_id == BunchingEvent.nearest_stop_id
    )

    if route_id is not None:
        query = query.filter(BunchingEvent.route_id == route_id)
    if start_date is not None:
        start_dt = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.timezone.utc)
        query = query.filter(BunchingEvent.start_time >= start_dt)
    if end_date is not None:
        end_dt = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=datetime.timezone.utc)
        query = query.filter(BunchingEvent.start_time <= end_dt)

    rows = query.order_by(BunchingEvent.start_time.desc()).limit(limit).all()

    return [
        BunchingEventOut(
            id=event.id,
            route_id=event.route_id,
            route_short_name=route.route_short_name,
            direction_id=event.direction_id,
            start_time=event.start_time,
            end_time=event.end_time,
            location_lat=event.location_lat,
            location_lon=event.location_lon,
            nearest_stop_id=event.nearest_stop_id,
            nearest_stop_name=stop.stop_name if stop else None,
            observed_headway_seconds=event.observed_headway_seconds,
            scheduled_headway_seconds=event.scheduled_headway_seconds,
            severity=event.severity,
        )
        for event, route, stop in rows
    ]

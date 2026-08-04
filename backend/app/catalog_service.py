"""Route/stop catalog lookups - powers the route + start/end stop pickers
on the frontend.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Route, Shape, Stop, StopTime, Trip
from app.schemas import DirectionSummary, RouteShapeDirection, RouteSummary, ShapePoint, StopSummary


def list_routes(session: Session) -> list[RouteSummary]:
    routes = session.query(Route).order_by(Route.route_short_name).all()
    return [
        RouteSummary(route_id=r.route_id, route_short_name=r.route_short_name, route_long_name=r.route_long_name)
        for r in routes
    ]


def _representative_trip_id(session: Session, route_id: str, direction_id: int | None) -> str | None:
    """Pick the trip with the most stop_times for this route+direction, as a
    stand-in "canonical" stop pattern (routes can have several trip variants;
    this just needs one reasonable ordering for the stop picker)."""
    query = (
        select(StopTime.trip_id, func.count().label("stop_count"))
        .join(Trip, Trip.trip_id == StopTime.trip_id)
        .where(Trip.route_id == route_id, Trip.direction_id == direction_id)
        .group_by(StopTime.trip_id)
        .order_by(func.count().desc())
        .limit(1)
    )
    row = session.execute(query).first()
    return row.trip_id if row else None


def list_route_directions(session: Session, route_id: str) -> list[DirectionSummary]:
    """Human-readable label per direction, e.g. "20-Brooks Transit Center"
    instead of the raw direction_id (0/1) - riders don't know what "Direction
    0" means, but they recognize the headsign VIA already publishes.

    Uses the same representative trip as list_route_stops so the headsign
    shown always matches the stop order the rider will see - a direction can
    have multiple headsign variants across its trips (e.g. an express
    short-turn), so picking independently could show a headsign that doesn't
    match the picked stop pattern.
    """
    direction_ids = [
        row.direction_id
        for row in session.query(Trip.direction_id).filter(Trip.route_id == route_id).distinct().all()
    ]

    results: list[DirectionSummary] = []
    for direction_id in direction_ids:
        if direction_id is None:
            continue
        trip_id = _representative_trip_id(session, route_id, direction_id)
        headsign = None
        if trip_id is not None:
            trip = session.get(Trip, trip_id)
            headsign = trip.trip_headsign if trip else None
        results.append(DirectionSummary(direction_id=direction_id, headsign=headsign))
    return results


def list_route_shapes(session: Session, route_id: str) -> list[RouteShapeDirection]:
    """The road-following polyline per direction, for drawing the route on
    the map - uses the same representative trip as list_route_stops/
    list_route_directions, so the shape drawn always matches the stop
    pattern and headsign shown for that direction.

    A trip with no shape_id (or a shape_id with no points - shapes.txt is
    optional in GTFS) simply contributes no line for that direction rather
    than erroring; the map falls back to just the stop markers.
    """
    direction_ids = [
        row.direction_id
        for row in session.query(Trip.direction_id).filter(Trip.route_id == route_id).distinct().all()
    ]

    results: list[RouteShapeDirection] = []
    for direction_id in direction_ids:
        if direction_id is None:
            continue
        trip_id = _representative_trip_id(session, route_id, direction_id)
        if trip_id is None:
            continue
        trip = session.get(Trip, trip_id)
        if trip is None or trip.shape_id is None:
            continue
        rows = (
            session.query(Shape.shape_pt_lat, Shape.shape_pt_lon)
            .filter(Shape.shape_id == trip.shape_id)
            .order_by(Shape.shape_pt_sequence)
            .all()
        )
        if not rows:
            continue
        results.append(
            RouteShapeDirection(
                direction_id=direction_id,
                points=[ShapePoint(lat=lat, lon=lon) for lat, lon in rows],
            )
        )
    return results


def list_route_stops(session: Session, route_id: str) -> list[StopSummary]:
    """Ordered stops for every direction on a route, using one representative
    trip per direction. direction_id/stop_sequence let the frontend keep
    each direction's stops in the correct order for start/end pickers."""
    direction_ids = [
        row.direction_id
        for row in session.query(Trip.direction_id).filter(Trip.route_id == route_id).distinct().all()
    ]

    results: list[StopSummary] = []
    for direction_id in direction_ids:
        trip_id = _representative_trip_id(session, route_id, direction_id)
        if trip_id is None:
            continue
        rows = (
            session.query(StopTime, Stop)
            .join(Stop, Stop.stop_id == StopTime.stop_id)
            .filter(StopTime.trip_id == trip_id)
            .order_by(StopTime.stop_sequence)
            .all()
        )
        for stop_time, stop in rows:
            results.append(
                StopSummary(
                    stop_id=stop.stop_id,
                    stop_name=stop.stop_name,
                    stop_lat=stop.stop_lat,
                    stop_lon=stop.stop_lon,
                    direction_id=direction_id,
                    stop_sequence=stop_time.stop_sequence,
                )
            )
    return results

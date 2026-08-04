from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.catalog_service import list_route_directions, list_route_shapes, list_route_stops, list_routes
from app.db import get_db
from app.models import Route
from app.schemas import DirectionSummary, RouteShapeDirection, RouteSummary, StopSummary

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/routes", response_model=list[RouteSummary])
def get_routes(db: Session = Depends(get_db)):
    return list_routes(db)


@router.get("/routes/{route_id}/directions", response_model=list[DirectionSummary])
def get_route_directions(route_id: str, db: Session = Depends(get_db)):
    if db.get(Route, route_id) is None:
        raise HTTPException(404, f"Route '{route_id}' not found")
    return list_route_directions(db, route_id)


@router.get("/routes/{route_id}/stops", response_model=list[StopSummary])
def get_route_stops(route_id: str, db: Session = Depends(get_db)):
    if db.get(Route, route_id) is None:
        raise HTTPException(404, f"Route '{route_id}' not found")
    return list_route_stops(db, route_id)


@router.get("/routes/{route_id}/shape", response_model=list[RouteShapeDirection])
def get_route_shape(route_id: str, db: Session = Depends(get_db)):
    if db.get(Route, route_id) is None:
        raise HTTPException(404, f"Route '{route_id}' not found")
    return list_route_shapes(db, route_id)

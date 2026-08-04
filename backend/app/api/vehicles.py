from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import LiveVehicle
from app.vehicles_service import DEFAULT_MAX_AGE_SECONDS, list_live_vehicles

router = APIRouter(prefix="/api", tags=["vehicles"])


@router.get("/vehicles/live", response_model=list[LiveVehicle])
def get_live_vehicles(
    max_age_seconds: int = Query(DEFAULT_MAX_AGE_SECONDS, ge=10, le=3600),
    db: Session = Depends(get_db),
):
    return list_live_vehicles(db, max_age_seconds=max_age_seconds)

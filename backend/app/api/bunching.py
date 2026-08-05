import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.bunching_service import list_bunching_events
from app.config import settings
from app.db import get_db
from app.rate_limit import limiter
from app.schemas import BunchingEventOut

router = APIRouter(prefix="/api", tags=["bunching"])


@router.get("/bunching-events", response_model=list[BunchingEventOut])
@limiter.limit(settings.rate_limit_reliability)
def get_bunching_events(
    request: Request,
    response: Response,  # required by slowapi's decorator, see vehicles.py's endpoint docstring
    route_id: str | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return list_bunching_events(db, route_id=route_id, start_date=start_date, end_date=end_date, limit=limit)

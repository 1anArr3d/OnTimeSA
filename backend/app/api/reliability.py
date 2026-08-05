import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Route
from app.rate_limit import limiter
from app.reliability_service import compute_group_reliability, compute_segment_reliability
from app.schemas import ReliabilityStats

router = APIRouter(prefix="/api/reliability", tags=["reliability"])

DEFAULT_WINDOW_DAYS = 30


def _default_date_range(
    start_date: datetime.date | None, end_date: datetime.date | None
) -> tuple[datetime.date, datetime.date]:
    end_date = end_date or datetime.date.today()
    start_date = start_date or (end_date - datetime.timedelta(days=DEFAULT_WINDOW_DAYS))
    return start_date, end_date


@router.get("/segment", response_model=ReliabilityStats)
@limiter.limit(settings.rate_limit_reliability)
def get_segment_reliability(
    request: Request,
    response: Response,  # required by slowapi's decorator, see vehicles.py's endpoint docstring
    route_id: str,
    start_stop_id: str,
    end_stop_id: str,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    db: Session = Depends(get_db),
):
    """'Check my commute': reliability for one route ridden from start_stop_id
    to end_stop_id (single route, no transfers). Always returns whatever
    history exists for the exact segment - flagged low-confidence if sparse
    rather than withheld, since the user asked about their specific commute.
    """
    start_date, end_date = _default_date_range(start_date, end_date)
    if start_date > end_date:
        raise HTTPException(400, "start_date must be on or before end_date")

    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(404, f"Route '{route_id}' not found")

    result = compute_segment_reliability(db, route_id, start_stop_id, end_stop_id, start_date, end_date)
    if result is None:
        raise HTTPException(
            400,
            f"Stops '{start_stop_id}' and '{end_stop_id}' aren't both on route '{route_id}' in that order "
            "(transfers between routes aren't supported in this version).",
        )
    return result


@router.get("/worst-offenders", response_model=list[ReliabilityStats])
@limiter.limit(settings.rate_limit_reliability)
def get_worst_offenders(
    request: Request,
    response: Response,  # required by slowapi's decorator, see vehicles.py's endpoint docstring
    group_by: str = Query("route", pattern="^(route|stop)$"),
    route_id: str | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    limit: int = Query(10, ge=1, le=100),
    min_samples: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    """Routes or stops ranked worst-on-time-percentage-first over a date
    range. Unlike /segment, rows below min_samples are dropped entirely -
    a ranked list is much more sensitive to a few noisy low-sample entries
    than a direct single-segment lookup is.
    """
    start_date, end_date = _default_date_range(start_date, end_date)
    if start_date > end_date:
        raise HTTPException(400, "start_date must be on or before end_date")

    return compute_group_reliability(
        db, start_date, end_date, group_by=group_by, route_id=route_id, limit=limit, min_samples=min_samples
    )

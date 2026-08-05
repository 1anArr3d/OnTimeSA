import datetime

from fastapi import APIRouter, Query, Request, Response

from app.config import settings
from app.live_state import state
from app.rate_limit import limiter
from app.schemas import LiveVehicle
from app.vehicles_service import DEFAULT_MAX_AGE_SECONDS

router = APIRouter(prefix="/api", tags=["vehicles"])


@router.get("/vehicles/live", response_model=list[LiveVehicle])
@limiter.limit(settings.rate_limit_vehicles_live)
def get_live_vehicles(
    request: Request,
    response: Response,
    max_age_seconds: int = Query(DEFAULT_MAX_AGE_SECONDS, ge=10, le=3600),
):
    """Served straight from the in-memory buffer the background poll loop
    maintains (see app/live_state.py) - no database read at all. Everything
    in state.live_vehicles comes from a single poll cycle, so the
    "freshness" question collapses to one check: is the whole batch within
    max_age_seconds, not per-vehicle filtering like the old DB-backed
    version did (that windowed query existed to tolerate a vehicle missing
    from one poll but present in a recent one; here there's only ever the
    latest poll's own snapshot to serve).

    The unused `response` param isn't decorative - slowapi's @limiter.limit
    needs a real starlette Response object to attach rate-limit headers to
    on the success path (this endpoint returns a plain list, not a Response,
    since response_model handles serialization); omitting it doesn't just
    skip the headers, it throws inside the decorator and 500s the request.
    """
    if state.last_poll_at is None:
        return []
    age_seconds = (datetime.datetime.now(datetime.timezone.utc) - state.last_poll_at).total_seconds()
    if age_seconds > max_age_seconds:
        return []
    return state.live_vehicles

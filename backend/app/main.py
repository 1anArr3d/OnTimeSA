"""FastAPI app entrypoint. Since the poller merged into this process (see
lifespan() below), this module is also where the background poll loop, the
hourly buffer flush, the daily rollup/prune job, and the daily static-cache
refresh all get scheduled - previously app/poller.py's run_scheduler() ran
these as a fully separate systemd service. See app/live_state.py for the
in-memory state they all share, and app/poller.py's module docstring for
why this stopped querying Postgres every poll.
"""

from __future__ import annotations

import datetime
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import bunching, catalog, reliability, vehicles
from app.config import settings
from app.rate_limit import limiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _poll_job() -> None:
    from app.db import get_sessionmaker
    from app.live_state import state
    from app.poller import flush_state_to_db, poll_once_in_memory

    try:
        poll_once_in_memory(state)
    except Exception:
        logger.exception("In-memory poll cycle failed")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    due = (
        state.last_flush_at is None
        or (now - state.last_flush_at).total_seconds() >= settings.flush_interval_seconds
    )
    if due:
        session = get_sessionmaker()()
        try:
            flush_state_to_db(state, session)
        except Exception:
            logger.exception("Scheduled hourly flush failed")
            session.rollback()
        finally:
            session.close()

    _maybe_refresh_static_cache(now)


def _maybe_refresh_static_cache(now: datetime.datetime) -> None:
    from app.db import get_sessionmaker
    from app.live_state import load_static_cache, state

    if state.static_cache is None:
        return
    age_hours = (now - state.static_cache.loaded_at).total_seconds() / 3600
    if age_hours < settings.static_cache_refresh_hours:
        return

    session = get_sessionmaker()()
    try:
        state.static_cache = load_static_cache(session)
    except Exception:
        logger.exception("Static cache refresh failed - keeping the existing cache")
    finally:
        session.close()


def _maintenance_job() -> None:
    """Flush first, unconditionally, then run the daily rollup+prune - never
    the other way around, so prune_old_raw_data() can't delete rows the
    rollup still needs and that only exist in the in-memory buffer so far.
    """
    from app.db import get_sessionmaker
    from app.live_state import state
    from app.poller import flush_state_to_db
    from app.rollup_service import run_daily_maintenance

    session = get_sessionmaker()()
    try:
        flush_state_to_db(state, session)
        summary = run_daily_maintenance(session)
        logger.info("Daily rollup+prune complete: %s", summary)
    except Exception:
        logger.exception("Daily maintenance job failed")
        session.rollback()
    finally:
        session.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.db import get_sessionmaker
    from app.gtfs.timezone_utils import AGENCY_TIMEZONE
    from app.live_state import load_static_cache, state
    from app.poller import flush_state_to_db

    session = get_sessionmaker()()
    try:
        state.static_cache = load_static_cache(session)
    finally:
        session.close()

    # A single worker thread, not APScheduler's default pool - the poll job
    # and the daily maintenance job both mutate app.live_state.state, which
    # is documented as safe only because exactly one thread ever touches its
    # write buffers at a time (see live_state.py's module docstring). A
    # multi-worker pool could run them concurrently and break that.
    scheduler = BackgroundScheduler(timezone="UTC", executors={"default": ThreadPoolExecutor(max_workers=1)})
    scheduler.add_job(
        _poll_job, "interval", seconds=settings.gtfs_rt_poll_seconds, next_run_time=datetime.datetime.now()
    )
    scheduler.add_job(_maintenance_job, "cron", hour=0, minute=10, timezone=AGENCY_TIMEZONE)
    scheduler.start()
    logger.info(
        "Poller running in-process: polling every %ds, flushing every %ds, rollup/prune at 00:10 %s",
        settings.gtfs_rt_poll_seconds,
        settings.flush_interval_seconds,
        AGENCY_TIMEZONE,
    )

    yield

    scheduler.shutdown(wait=False)
    # Best-effort final flush so a graceful redeploy loses as little of the
    # buffer as possible rather than the full hour.
    session = get_sessionmaker()()
    try:
        flush_state_to_db(state, session)
    except Exception:
        logger.exception("Final flush on shutdown failed")
        session.rollback()
    finally:
        session.close()


app = FastAPI(title="OnTimeSA API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(catalog.router)
app.include_router(reliability.router)
app.include_router(bunching.router)
app.include_router(vehicles.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

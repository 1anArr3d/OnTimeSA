"""The shared slowapi Limiter instance - its own module so both app.main
(which wires it into the FastAPI app) and app.api.* (which decorate
individual routes with it) can import it without a circular dependency
between main.py and the routers it includes.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
    # slowapi defaults this to False - without it, a 429 response has no
    # Retry-After header at all, which is a silent-ish failure for any
    # client trying to back off correctly rather than a clear "come back
    # in N seconds" signal.
    headers_enabled=True,
)

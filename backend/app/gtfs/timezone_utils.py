"""Convert GTFS static schedule times (local, no explicit UTC offset) into
timezone-aware UTC datetimes comparable against GTFS-RT timestamps (Unix epoch/UTC).

This is the classic GTFS silent-bug source: stop_times.txt stores times like
"08:15:00" with an implicit agency-local timezone (from agency.txt), and can
exceed 24:00:00 for trips that run past midnight. GTFS-RT timestamps are
plain Unix epoch seconds. Diffing them without normalizing to the same
timezone first produces deviation numbers that are wrong by a fixed offset
(e.g. 5-6 hours for America/Chicago) in a way that won't show up unless you
eyeball real output against known local times.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

AGENCY_TIMEZONE = "America/Chicago"


def scheduled_seconds_to_utc(
    service_date: datetime.date, scheduled_seconds: int, tz_name: str = AGENCY_TIMEZONE
) -> datetime.datetime:
    """Convert a GTFS stop_times seconds-since-midnight value for a given
    service date into an aware UTC datetime.

    scheduled_seconds may exceed 86400 (GTFS allows e.g. "25:30:00" for trips
    that run past midnight) - timedelta addition handles that correctly by
    spilling into the next calendar day.
    """
    tz = ZoneInfo(tz_name)
    local_midnight = datetime.datetime(service_date.year, service_date.month, service_date.day, tzinfo=tz)
    local_time = local_midnight + datetime.timedelta(seconds=scheduled_seconds)
    return local_time.astimezone(datetime.timezone.utc)


def infer_service_date(
    actual_utc: datetime.datetime, scheduled_seconds: int, tz_name: str = AGENCY_TIMEZONE
) -> datetime.date:
    """Determine which GTFS service_date a real-time observation belongs to.

    Prefer trip.start_date from the GTFS-RT feed when it's available - this
    is only a fallback for when it isn't. Tries service dates in a window
    around the actual observation's local date and picks whichever one puts
    the scheduled time closest to the observed time, which correctly handles
    trips that started the previous service day and are still running after
    local midnight (GTFS's ">24:00:00" convention).
    """
    tz = ZoneInfo(tz_name)
    local_date = actual_utc.astimezone(tz).date()

    candidates = [local_date + datetime.timedelta(days=offset) for offset in (-1, 0, 1)]
    best_date = local_date
    best_diff = None
    for candidate in candidates:
        scheduled_utc = scheduled_seconds_to_utc(candidate, scheduled_seconds, tz_name)
        diff = abs((actual_utc - scheduled_utc).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_date = candidate
    return best_date


def parse_rt_start_date(start_date: str | None) -> datetime.date | None:
    """Parse a GTFS-RT TripDescriptor.start_date ("YYYYMMDD") if present."""
    if not start_date:
        return None
    try:
        return datetime.datetime.strptime(start_date, "%Y%m%d").date()
    except ValueError:
        return None

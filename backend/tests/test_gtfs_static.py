import datetime

import pytest

from app.gtfs.static import (
    _parse_gtfs_date,
    _parse_gtfs_time_to_seconds,
    download_static_feed,
    parse_static_feed,
)


def test_parse_gtfs_time_to_seconds():
    assert _parse_gtfs_time_to_seconds("00:00:00") == 0
    assert _parse_gtfs_time_to_seconds("08:15:30") == 8 * 3600 + 15 * 60 + 30
    # GTFS allows hours past 24 for trips that run past midnight
    assert _parse_gtfs_time_to_seconds("25:30:00") == 25 * 3600 + 30 * 60
    assert _parse_gtfs_time_to_seconds("") is None
    assert _parse_gtfs_time_to_seconds(None) is None


def test_parse_gtfs_date():
    assert _parse_gtfs_date("20260504") == datetime.date(2026, 5, 4)
    assert _parse_gtfs_date("") is None
    assert _parse_gtfs_date(None) is None


@pytest.mark.integration
def test_fetch_and_parse_real_via_feed():
    """Hits VIA's live GTFS static endpoint. Requires network access."""
    zip_bytes = download_static_feed()
    assert len(zip_bytes) > 0

    feed = parse_static_feed(zip_bytes)

    assert len(feed.routes) > 0
    assert len(feed.stops) > 0
    assert len(feed.trips) > 0
    assert len(feed.stop_times) > 0
    assert len(feed.calendar) > 0

    route = feed.routes[0]
    assert route["route_id"]
    assert isinstance(route["route_type"], int)

    stop = feed.stops[0]
    assert isinstance(stop["stop_lat"], float)
    assert isinstance(stop["stop_lon"], float)

    stop_time = feed.stop_times[0]
    assert isinstance(stop_time["stop_sequence"], int)

    calendar_entry = feed.calendar[0]
    assert isinstance(calendar_entry["start_date"], datetime.date)

    # Every trip's route_id and service_id should resolve to a parsed route/calendar entry.
    route_ids = {r["route_id"] for r in feed.routes}
    service_ids = {c["service_id"] for c in feed.calendar}
    sample_trips = feed.trips[:200]
    assert all(t["route_id"] in route_ids for t in sample_trips)
    assert all(t["service_id"] in service_ids for t in sample_trips)

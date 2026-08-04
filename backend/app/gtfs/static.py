"""Download and parse VIA's GTFS static feed (google_transit.zip).

The feed is a zip of CSV files per the GTFS spec:
https://code.google.com/transit/spec/transit_feed_specification.html

This module only parses the files this project actually uses:
routes.txt, stops.txt, trips.txt, stop_times.txt, calendar.txt.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import zipfile
from dataclasses import dataclass

import requests

from app.config import settings

logger = logging.getLogger(__name__)

REQUIRED_FILES = ("routes.txt", "stops.txt", "trips.txt", "stop_times.txt", "calendar.txt")


@dataclass
class GtfsStaticFeed:
    routes: list[dict]
    stops: list[dict]
    trips: list[dict]
    stop_times: list[dict]
    calendar: list[dict]


def download_static_feed(url: str | None = None, timeout: int = 60) -> bytes:
    """Fetch VIA's GTFS static zip. Raises requests.RequestException on failure."""
    url = url or settings.gtfs_static_url
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def _parse_gtfs_time_to_seconds(value: str | None) -> int | None:
    """Convert "HH:MM:SS" to seconds since midnight.

    GTFS allows hours >= 24 for trips that run past midnight (e.g. "25:30:00"),
    so this is not a plain time-of-day parse.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def _parse_gtfs_date(value: str | None) -> datetime.date | None:
    """Convert "YYYYMMDD" to a date object."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return datetime.datetime.strptime(value, "%Y%m%d").date()


def _read_csv_rows(archive: zipfile.ZipFile, filename: str) -> list[dict]:
    with archive.open(filename) as raw_file:
        text_stream = io.TextIOWrapper(raw_file, encoding="utf-8-sig")
        reader = csv.DictReader(text_stream)
        return [row for row in reader]


def parse_static_feed(zip_bytes: bytes) -> GtfsStaticFeed:
    """Parse the GTFS zip bytes into normalized row dicts ready for DB loading."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        available = set(archive.namelist())
        missing = [f for f in REQUIRED_FILES if f not in available]
        if missing:
            raise ValueError(f"GTFS static feed is missing required files: {missing}")

        routes_raw = _read_csv_rows(archive, "routes.txt")
        stops_raw = _read_csv_rows(archive, "stops.txt")
        trips_raw = _read_csv_rows(archive, "trips.txt")
        stop_times_raw = _read_csv_rows(archive, "stop_times.txt")
        calendar_raw = _read_csv_rows(archive, "calendar.txt")

    routes = [
        {
            "route_id": r["route_id"],
            "agency_id": r.get("agency_id") or None,
            "route_short_name": r.get("route_short_name") or None,
            "route_long_name": r.get("route_long_name") or None,
            "route_type": int(r["route_type"]) if r.get("route_type") else None,
            "route_color": r.get("route_color") or None,
            "route_text_color": r.get("route_text_color") or None,
        }
        for r in routes_raw
    ]

    stops = [
        {
            "stop_id": s["stop_id"],
            "stop_code": s.get("stop_code") or None,
            "stop_name": s.get("stop_name") or None,
            "stop_lat": float(s["stop_lat"]) if s.get("stop_lat") else None,
            "stop_lon": float(s["stop_lon"]) if s.get("stop_lon") else None,
            "zone_id": s.get("zone_id") or None,
            "location_type": int(s["location_type"]) if s.get("location_type") else None,
            "parent_station": s.get("parent_station") or None,
        }
        for s in stops_raw
    ]

    trips = [
        {
            "trip_id": t["trip_id"],
            "route_id": t["route_id"],
            "service_id": t["service_id"],
            "trip_headsign": t.get("trip_headsign") or None,
            "direction_id": int(t["direction_id"]) if t.get("direction_id") not in (None, "") else None,
            "block_id": t.get("block_id") or None,
            "shape_id": t.get("shape_id") or None,
        }
        for t in trips_raw
    ]

    stop_times = [
        {
            "trip_id": st["trip_id"],
            "stop_sequence": int(st["stop_sequence"]),
            "stop_id": st["stop_id"],
            "arrival_time_seconds": _parse_gtfs_time_to_seconds(st.get("arrival_time")),
            "departure_time_seconds": _parse_gtfs_time_to_seconds(st.get("departure_time")),
            "pickup_type": int(st["pickup_type"]) if st.get("pickup_type") not in (None, "") else None,
            "drop_off_type": int(st["drop_off_type"]) if st.get("drop_off_type") not in (None, "") else None,
        }
        for st in stop_times_raw
    ]

    calendar = [
        {
            "service_id": c["service_id"],
            "monday": c.get("monday") == "1",
            "tuesday": c.get("tuesday") == "1",
            "wednesday": c.get("wednesday") == "1",
            "thursday": c.get("thursday") == "1",
            "friday": c.get("friday") == "1",
            "saturday": c.get("saturday") == "1",
            "sunday": c.get("sunday") == "1",
            "start_date": _parse_gtfs_date(c.get("start_date")),
            "end_date": _parse_gtfs_date(c.get("end_date")),
        }
        for c in calendar_raw
    ]

    logger.info(
        "Parsed GTFS static feed: %d routes, %d stops, %d trips, %d stop_times, %d calendar entries",
        len(routes),
        len(stops),
        len(trips),
        len(stop_times),
        len(calendar),
    )

    return GtfsStaticFeed(routes=routes, stops=stops, trips=trips, stop_times=stop_times, calendar=calendar)


def fetch_and_parse(url: str | None = None) -> GtfsStaticFeed:
    zip_bytes = download_static_feed(url)
    return parse_static_feed(zip_bytes)

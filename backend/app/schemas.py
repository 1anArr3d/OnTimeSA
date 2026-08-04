"""Pydantic response models shared across the reliability API.

ReliabilityStats is deliberately one shape reused by both the segment
lookup ("check my commute") and the ranked worst-offenders view, rather than
two similar-but-different response types - same fields, `scope` says what a
given row represents.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel


class ReliabilityStats(BaseModel):
    scope: Literal["segment", "route", "stop"]
    route_id: str | None = None
    route_short_name: str | None = None
    route_long_name: str | None = None
    direction_id: int | None = None

    start_stop_id: str | None = None
    start_stop_name: str | None = None
    end_stop_id: str | None = None
    end_stop_name: str | None = None

    start_date: datetime.date
    end_date: datetime.date

    sample_count: int
    avg_delay_seconds: float | None = None
    on_time_pct: float | None = None
    bunching_event_count: int
    bunching_events_per_day: float

    # "low" below settings.min_reliable_sample_count - number is still
    # returned, just flagged so the frontend can visually de-emphasize it.
    confidence: Literal["low", "high"]


class RouteSummary(BaseModel):
    route_id: str
    route_short_name: str | None
    route_long_name: str | None


class StopSummary(BaseModel):
    stop_id: str
    stop_name: str | None
    stop_lat: float | None
    stop_lon: float | None
    direction_id: int | None = None
    stop_sequence: int | None = None


class ShapePoint(BaseModel):
    lat: float
    lon: float


class RouteShapeDirection(BaseModel):
    direction_id: int
    points: list[ShapePoint]


class DirectionSummary(BaseModel):
    direction_id: int
    # From GTFS trips.txt trip_headsign on the direction's representative
    # trip (see catalog_service._representative_trip_id) - e.g. "Downtown",
    # not the raw 0/1 direction_id, which is meaningless to a rider.
    headsign: str | None = None


class BunchingEventOut(BaseModel):
    id: int
    route_id: str
    route_short_name: str | None = None
    direction_id: int | None = None
    start_time: datetime.datetime
    end_time: datetime.datetime
    location_lat: float | None
    location_lon: float | None
    nearest_stop_id: str | None
    nearest_stop_name: str | None = None
    observed_headway_seconds: float
    scheduled_headway_seconds: float
    severity: str


class LiveVehicle(BaseModel):
    vehicle_id: str
    trip_id: str | None
    route_id: str | None
    route_short_name: str | None = None
    # direction_id/trip_headsign come straight off this vehicle's own trip
    # (not a "representative trip" like catalog_service's direction picker,
    # since here we already know exactly which trip it's running).
    direction_id: int | None = None
    trip_headsign: str | None = None
    latitude: float | None
    longitude: float | None
    bearing: float | None
    current_status: str | None
    vehicle_timestamp: datetime.datetime | None
    # Most recent known delay for this vehicle's trip, if one's been computed
    # this poll cycle - drives the on-time/late color coding on the map.
    delay_seconds: float | None = None
    current_stop_id: str | None = None
    current_stop_name: str | None = None
    next_stop_id: str | None = None
    next_stop_name: str | None = None
    next_stop_scheduled_time: str | None = None
    # Scheduled/actual clock time for current_stop_id specifically - paired
    # with delay_seconds, which is computed from the same matched deviation.
    scheduled_time: str | None = None
    actual_time: str | None = None

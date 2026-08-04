"""Poll and decode VIA's GTFS-Realtime protobuf feeds (Vehicle Positions,
Trip Updates). Network/parse failures are caught and logged so a single bad
poll doesn't take down the scheduled job - callers get an empty list back
and the next poll cycle tries again.
"""

from __future__ import annotations

import logging

import requests
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30


def _fetch_feed(url: str) -> gtfs_realtime_pb2.FeedMessage | None:
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to fetch GTFS-RT feed from %s", url)
        return None

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(response.content)
    except DecodeError:
        logger.exception("Failed to decode GTFS-RT protobuf from %s", url)
        return None

    return feed


def fetch_vehicle_positions(url: str | None = None) -> list[dict]:
    """Fetch and decode VehiclePositions. Returns a flat list of dicts, one
    per vehicle entity that had a position - malformed entities are skipped
    and logged rather than aborting the whole poll.
    """
    feed = _fetch_feed(url or settings.gtfs_rt_vehicle_positions_url)
    if feed is None:
        return []

    results = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        if not vehicle.HasField("position"):
            logger.debug("Skipping vehicle entity %s with no position", entity.id)
            continue

        trip = vehicle.trip if vehicle.HasField("trip") else None
        results.append(
            {
                "vehicle_id": vehicle.vehicle.id if vehicle.HasField("vehicle") else entity.id,
                "trip_id": trip.trip_id if trip and trip.trip_id else None,
                "route_id": trip.route_id if trip and trip.route_id else None,
                "direction_id": trip.direction_id if trip and trip.HasField("direction_id") else None,
                "start_date": trip.start_date if trip and trip.start_date else None,
                "latitude": vehicle.position.latitude,
                "longitude": vehicle.position.longitude,
                "bearing": vehicle.position.bearing if vehicle.position.HasField("bearing") else None,
                "speed": vehicle.position.speed if vehicle.position.HasField("speed") else None,
                "current_stop_sequence": (
                    vehicle.current_stop_sequence if vehicle.HasField("current_stop_sequence") else None
                ),
                "stop_id": vehicle.stop_id or None,
                "current_status": (
                    gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(vehicle.current_status)
                    if vehicle.HasField("current_status")
                    else None
                ),
                "timestamp": vehicle.timestamp if vehicle.HasField("timestamp") else None,
            }
        )
    return results


def fetch_trip_updates(url: str | None = None) -> list[dict]:
    """Fetch and decode TripUpdates. Returns a flat list of dicts, one per
    (trip, stop_time_update) pair, so each row is directly comparable to a
    static stop_times row.
    """
    feed = _fetch_feed(url or settings.gtfs_rt_trip_updates_url)
    if feed is None:
        return []

    results = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip = trip_update.trip
        if not trip.trip_id:
            logger.debug("Skipping trip_update entity %s with no trip_id", entity.id)
            continue

        vehicle_id = trip_update.vehicle.id if trip_update.HasField("vehicle") else None

        for stu in trip_update.stop_time_update:
            arrival_time = stu.arrival.time if stu.HasField("arrival") and stu.arrival.HasField("time") else None
            arrival_delay = (
                stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            )
            departure_time = (
                stu.departure.time if stu.HasField("departure") and stu.departure.HasField("time") else None
            )
            departure_delay = (
                stu.departure.delay if stu.HasField("departure") and stu.departure.HasField("delay") else None
            )
            if arrival_time is None and arrival_delay is None and departure_time is None and departure_delay is None:
                continue

            results.append(
                {
                    "trip_id": trip.trip_id,
                    "route_id": trip.route_id or None,
                    "direction_id": trip.direction_id if trip.HasField("direction_id") else None,
                    "start_date": trip.start_date or None,
                    "vehicle_id": vehicle_id,
                    "stop_sequence": stu.stop_sequence if stu.HasField("stop_sequence") else None,
                    "stop_id": stu.stop_id or None,
                    "arrival_time": arrival_time,
                    "arrival_delay": arrival_delay,
                    "departure_time": departure_time,
                    "departure_delay": departure_delay,
                }
            )
    return results

"""Match a GTFS-RT observation (vehicle position or trip update stop-time
update) to the static stop_times row it corresponds to.

Real-time observations don't always cleanly map to the static schedule: a
vehicle can be off-route, deadheading, running a trip that dropped out of
the current static feed, or reporting a stop_sequence/stop_id that doesn't
line up exactly. Rather than erroring out (or silently producing nonsense),
this falls back through progressively looser matches and returns
(None, None) when nothing is close enough to trust.
"""

from __future__ import annotations

import math

DEFAULT_MAX_MATCH_DISTANCE_METERS = 500.0


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(a))


def match_stop_time(
    stop_times: list[dict],
    stop_sequence: int | None = None,
    stop_id: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    max_distance_meters: float = DEFAULT_MAX_MATCH_DISTANCE_METERS,
) -> tuple[dict | None, str | None]:
    """Find the best-matching static stop_time row for a trip's RT observation.

    `stop_times` is the trip's full list of stop_time dicts (each expected to
    have stop_sequence, stop_id, arrival_time_seconds, departure_time_seconds,
    and optionally stop_lat/stop_lon for the geographic fallback).

    Tries, in order: exact stop_sequence match, exact stop_id match (nearest
    by sequence if the stop is visited more than once on the trip), then
    nearest-by-distance among the trip's own stops within max_distance_meters.
    Returns (None, None) if nothing matches closely enough to trust.
    """
    if not stop_times:
        return None, None

    if stop_sequence is not None:
        for st in stop_times:
            if st["stop_sequence"] == stop_sequence:
                return st, "exact_sequence"

    if stop_id is not None:
        candidates = [st for st in stop_times if st["stop_id"] == stop_id]
        if candidates:
            if stop_sequence is not None:
                candidates.sort(key=lambda st: abs(st["stop_sequence"] - stop_sequence))
            return candidates[0], "exact_stop_id"

    if lat is not None and lon is not None:
        geo_candidates = [
            st for st in stop_times if st.get("stop_lat") is not None and st.get("stop_lon") is not None
        ]
        if geo_candidates:
            nearest = min(geo_candidates, key=lambda st: haversine_meters(lat, lon, st["stop_lat"], st["stop_lon"]))
            distance = haversine_meters(lat, lon, nearest["stop_lat"], nearest["stop_lon"])
            if distance <= max_distance_meters:
                return nearest, "nearest_geographic"

    return None, None

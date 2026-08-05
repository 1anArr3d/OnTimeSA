"""Bunching detection from a single poll's worth of vehicle positions.

Because polling only gives snapshots (not continuous trajectories), headway
between two consecutive vehicles isn't measured as a raw time/distance gap.
Instead it's derived from each vehicle's already-computed schedule delay:

    scheduled_headway = (follower's scheduled time at a stop) - (leader's
                         scheduled time at that same stop)
    observed_headway  = scheduled_headway + (follower's delay - leader's delay)

If the follower is catching up to the leader (running less late, or more
early), observed_headway shrinks below scheduled_headway - that's bunching.
This only requires per-vehicle delay, which we already compute, rather than
needing route shape geometry or continuous position tracking.

Every consecutive-pair comparison is logged as a HeadwaySample regardless of
outcome (per product requirement - real headway variance on VIA's network
isn't known yet, so the threshold needs real data to tune against). Only
samples that cross the threshold roll up into a BunchingEvent.

Layover/terminal false positives are filtered by excluding any vehicle
that's STOPPED_AT the first or last scheduled stop of its trip - proximity
alone would otherwise flag two buses parked at the same terminal as bunched.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.gtfs.deviation import load_trip_context
from app.models import BunchingEvent, HeadwaySample

logger = logging.getLogger(__name__)

# A detected event still "ongoing" if seen again within this many poll
# cycles' worth of time gets its end_time extended rather than duplicated.
EVENT_MERGE_WINDOW_MULTIPLIER = 3


def _is_at_terminal(vp: dict, stop_times: list[dict]) -> bool:
    if vp["current_status"] != "STOPPED_AT" or vp["current_stop_sequence"] is None or not stop_times:
        return False
    sequences = [st["stop_sequence"] for st in stop_times]
    return vp["current_stop_sequence"] in (min(sequences), max(sequences))


def _shared_stop_headway(lead_stop_times: list[dict], follow_stop_times: list[dict]) -> float | None:
    """Scheduled headway (seconds) at a stop both trips visit, or None if
    they share no stop (e.g. different route branches/patterns)."""
    lead_by_stop = {st["stop_id"]: st["arrival_time_seconds"] for st in lead_stop_times if st["arrival_time_seconds"] is not None}
    follow_by_stop = {st["stop_id"]: st["arrival_time_seconds"] for st in follow_stop_times if st["arrival_time_seconds"] is not None}
    shared_stop_ids = lead_by_stop.keys() & follow_by_stop.keys()
    if not shared_stop_ids:
        return None
    # Any shared stop works equally well for a headway diff; pick deterministically.
    stop_id = sorted(shared_stop_ids)[0]
    return follow_by_stop[stop_id] - lead_by_stop[stop_id]


def _severity(observed_headway_seconds: float, threshold_seconds: float) -> str:
    ratio = observed_headway_seconds / threshold_seconds if threshold_seconds else 0
    if ratio <= 0.33:
        return "high"
    if ratio <= 0.66:
        return "medium"
    return "low"


def detect_bunching(
    session: Session,
    vehicle_positions: list[dict],
    trip_delays: dict[str, float],
    poll_time: datetime.datetime,
    trip_context: dict | None = None,
    recent_events: list[BunchingEvent] | None = None,
) -> tuple[list[HeadwaySample], list[BunchingEvent]]:
    """Compare consecutive vehicles per (route, direction) and return the raw
    headway samples plus any newly created/extended bunching events.

    trip_context can be pre-loaded once and shared with the deviation
    calculations - see compute_deviations_from_trip_updates()'s docstring
    in app/gtfs/deviation.py.

    recent_events, if given, is searched in Python for an ongoing event to
    extend instead of querying `session` for one - see app/live_state.py's
    docstring for why (an in-memory buffer flushed only hourly means an
    event's DB row may not exist yet, or may already be flushed and need an
    UPDATE rather than a fresh INSERT on the next flush). Any event this
    function creates or extends gets `_dirty = True` set on it (a plain
    Python attribute, not a DB column) so the flush step downstream knows
    which of the events it's holding actually need writing.
    """
    threshold_seconds = settings.bunching_headway_threshold_minutes * 60
    min_scheduled_headway_seconds = settings.bunching_min_scheduled_headway_minutes * 60
    merge_window = datetime.timedelta(seconds=settings.gtfs_rt_poll_seconds * EVENT_MERGE_WINDOW_MULTIPLIER)

    candidates = [
        vp
        for vp in vehicle_positions
        if vp["trip_id"] and vp["route_id"] and vp["current_stop_sequence"] is not None
    ]
    if len(candidates) < 2:
        return [], []

    if trip_context is None:
        trip_ids = {vp["trip_id"] for vp in candidates}
        trip_context = load_trip_context(session, trip_ids)

    candidates = [vp for vp in candidates if vp["trip_id"] in trip_context]
    candidates = [
        vp for vp in candidates if not _is_at_terminal(vp, trip_context[vp["trip_id"]]["stop_times"])
    ]

    groups: dict[tuple[str, int | None], list[dict]] = {}
    for vp in candidates:
        key = (vp["route_id"], vp["direction_id"])
        groups.setdefault(key, []).append(vp)

    headway_samples: list[HeadwaySample] = []
    bunching_events: list[BunchingEvent] = []

    for (route_id, direction_id), group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda vp: vp["current_stop_sequence"], reverse=True)

        for lead, follow in zip(group, group[1:]):
            if lead["trip_id"] == follow["trip_id"]:
                continue

            lead_ctx = trip_context[lead["trip_id"]]
            follow_ctx = trip_context[follow["trip_id"]]
            scheduled_headway = _shared_stop_headway(lead_ctx["stop_times"], follow_ctx["stop_times"])
            if scheduled_headway is None or scheduled_headway <= 0:
                continue

            lead_delay = trip_delays.get(lead["trip_id"], 0.0)
            follow_delay = trip_delays.get(follow["trip_id"], 0.0)
            observed_headway = scheduled_headway + (follow_delay - lead_delay)

            crossed_threshold = observed_headway < threshold_seconds and scheduled_headway >= min_scheduled_headway_seconds

            headway_samples.append(
                HeadwaySample(
                    route_id=route_id,
                    direction_id=direction_id,
                    lead_trip_id=lead["trip_id"],
                    follow_trip_id=follow["trip_id"],
                    lead_vehicle_id=lead["vehicle_id"],
                    follow_vehicle_id=follow["vehicle_id"],
                    lead_delay_seconds=lead_delay,
                    follow_delay_seconds=follow_delay,
                    observed_headway_seconds=observed_headway,
                    scheduled_headway_seconds=scheduled_headway,
                    crossed_threshold=crossed_threshold,
                    location_lat=follow["latitude"],
                    location_lon=follow["longitude"],
                    sampled_at=poll_time,
                )
            )

            if not crossed_threshold:
                continue

            if recent_events is not None:
                candidates_for_key = [
                    e
                    for e in recent_events
                    if e.route_id == route_id
                    and e.lead_trip_id == lead["trip_id"]
                    and e.follow_trip_id == follow["trip_id"]
                    and e.end_time >= poll_time - merge_window
                ]
                existing = max(candidates_for_key, key=lambda e: e.end_time, default=None)
            else:
                existing = (
                    session.query(BunchingEvent)
                    .filter(
                        BunchingEvent.route_id == route_id,
                        BunchingEvent.lead_trip_id == lead["trip_id"],
                        BunchingEvent.follow_trip_id == follow["trip_id"],
                        BunchingEvent.end_time >= poll_time - merge_window,
                    )
                    .order_by(BunchingEvent.end_time.desc())
                    .first()
                )
            severity = _severity(max(observed_headway, 0), threshold_seconds)
            if existing:
                existing.end_time = poll_time
                existing.location_lat = follow["latitude"]
                existing.location_lon = follow["longitude"]
                existing.observed_headway_seconds = observed_headway
                existing.severity = severity
                existing._dirty = True
            else:
                new_event = BunchingEvent(
                    route_id=route_id,
                    direction_id=direction_id,
                    start_time=poll_time,
                    end_time=poll_time,
                    location_lat=follow["latitude"],
                    location_lon=follow["longitude"],
                    nearest_stop_id=follow["stop_id"],
                    lead_trip_id=lead["trip_id"],
                    follow_trip_id=follow["trip_id"],
                    vehicle_ids=[lead["vehicle_id"], follow["vehicle_id"]],
                    observed_headway_seconds=observed_headway,
                    scheduled_headway_seconds=scheduled_headway,
                    severity=severity,
                )
                new_event._dirty = True
                bunching_events.append(new_event)

    return headway_samples, bunching_events

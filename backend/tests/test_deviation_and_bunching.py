import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.gtfs.bunching import detect_bunching
from app.gtfs.deviation import compute_deviations_from_trip_updates, compute_deviations_from_vehicle_positions
from app.gtfs.timezone_utils import scheduled_seconds_to_utc
from app.models import BunchingEvent, Calendar, Route, Stop, StopTime, Trip

SERVICE_DATE = datetime.date(2026, 8, 3)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_route_and_calendar(session, route_id="R1"):
    session.add(Route(route_id=route_id, route_short_name=route_id))
    session.add(
        Calendar(
            service_id="WEEKDAY",
            monday=True, tuesday=True, wednesday=True, thursday=True, friday=True,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
    )
    session.flush()


def _seed_stop(session, stop_id, lat, lon):
    session.add(Stop(stop_id=stop_id, stop_name=stop_id, stop_lat=lat, stop_lon=lon))


def _seed_trip(session, trip_id, route_id, stop_times):
    """stop_times: list of (stop_sequence, stop_id, arrival_seconds)."""
    session.add(Trip(trip_id=trip_id, route_id=route_id, service_id="WEEKDAY", direction_id=0))
    session.flush()
    for seq, stop_id, arrival in stop_times:
        session.add(
            StopTime(trip_id=trip_id, stop_sequence=seq, stop_id=stop_id, arrival_time_seconds=arrival, departure_time_seconds=arrival)
        )
    session.flush()


def _epoch(dt: datetime.datetime) -> int:
    return int(dt.timestamp())


def test_trip_update_deviation_exact_sequence_match(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S1", 29.50, -98.50)
    _seed_stop(session, "S2", 29.51, -98.49)
    _seed_trip(session, "T1", "R1", [(1, "S1", 28800), (2, "S2", 28900)])

    scheduled_utc = scheduled_seconds_to_utc(SERVICE_DATE, 28800)
    actual_utc = scheduled_utc + datetime.timedelta(seconds=30)

    trip_updates = [
        {
            "trip_id": "T1", "route_id": "R1", "direction_id": 0, "start_date": "20260803",
            "vehicle_id": "V1", "stop_sequence": 1, "stop_id": "S1",
            "arrival_time": _epoch(actual_utc), "arrival_delay": None,
            "departure_time": None, "departure_delay": None,
        }
    ]

    deviations = compute_deviations_from_trip_updates(session, trip_updates)
    assert len(deviations) == 1
    d = deviations[0]
    assert d.delay_seconds == 30
    assert d.match_type == "exact_sequence"
    assert d.source == "trip_update"
    assert d.route_id == "R1"


def test_trip_update_uses_explicit_delay_field_when_present(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S1", 29.50, -98.50)
    _seed_trip(session, "T1", "R1", [(1, "S1", 28800)])

    trip_updates = [
        {
            "trip_id": "T1", "route_id": "R1", "direction_id": 0, "start_date": "20260803",
            "vehicle_id": "V1", "stop_sequence": 1, "stop_id": "S1",
            "arrival_time": None, "arrival_delay": 120,
            "departure_time": None, "departure_delay": None,
        }
    ]
    deviations = compute_deviations_from_trip_updates(session, trip_updates)
    assert deviations[0].delay_seconds == 120


def test_vehicle_position_deviation_falls_back_to_geographic_match(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S1", 29.50, -98.50)
    _seed_stop(session, "S2", 29.51, -98.49)
    _seed_trip(session, "T1", "R1", [(1, "S1", 28800), (2, "S2", 28900)])

    scheduled_utc = scheduled_seconds_to_utc(SERVICE_DATE, 28900)
    actual_utc = scheduled_utc + datetime.timedelta(seconds=45)

    vehicle_positions = [
        {
            "vehicle_id": "V1", "trip_id": "T1", "route_id": "R1", "direction_id": 0, "start_date": "20260803",
            "latitude": 29.5101, "longitude": -98.4901,  # very close to S2, not S1
            "bearing": None, "speed": None,
            "current_stop_sequence": 99,  # unknown sequence - forces fallback
            "stop_id": "UNKNOWN",  # unknown stop_id - forces geographic fallback
            "current_status": "STOPPED_AT",
            "timestamp": _epoch(actual_utc),
        }
    ]

    deviations = compute_deviations_from_vehicle_positions(session, vehicle_positions)
    assert len(deviations) == 1
    d = deviations[0]
    assert d.match_type == "nearest_geographic"
    assert d.stop_id == "S2"
    assert d.delay_seconds == 45


def test_vehicle_position_in_transit_excluded(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S1", 29.50, -98.50)
    _seed_trip(session, "T1", "R1", [(1, "S1", 28800)])

    vehicle_positions = [
        {
            "vehicle_id": "V1", "trip_id": "T1", "route_id": "R1", "direction_id": 0, "start_date": "20260803",
            "latitude": 29.50, "longitude": -98.50, "bearing": None, "speed": None,
            "current_stop_sequence": 1, "stop_id": "S1",
            "current_status": "IN_TRANSIT_TO",
            "timestamp": _epoch(scheduled_seconds_to_utc(SERVICE_DATE, 28800)),
        }
    ]
    assert compute_deviations_from_vehicle_positions(session, vehicle_positions) == []


def test_bunching_detected_when_follower_catches_up(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S_COMMON", 29.50, -98.50)
    _seed_stop(session, "S1", 29.49, -98.51)
    _seed_stop(session, "S2", 29.51, -98.49)
    # Scheduled 20-minute headway (1200s) between the two trips at the shared stop.
    _seed_trip(session, "LEAD", "R1", [(1, "S1", 27600), (2, "S_COMMON", 27700)])
    _seed_trip(session, "FOLLOW", "R1", [(1, "S1", 28800), (2, "S_COMMON", 28900)])

    poll_time = datetime.datetime.now(datetime.timezone.utc)
    vehicle_positions = [
        {
            "vehicle_id": "BUS_LEAD", "trip_id": "LEAD", "route_id": "R1", "direction_id": 0,
            "latitude": 29.50, "longitude": -98.50, "current_stop_sequence": 2, "stop_id": "S_COMMON",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
        {
            "vehicle_id": "BUS_FOLLOW", "trip_id": "FOLLOW", "route_id": "R1", "direction_id": 0,
            "latitude": 29.495, "longitude": -98.505, "current_stop_sequence": 1, "stop_id": "S1",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
    ]
    # Lead running 15 min late, follower on time -> observed headway collapses
    # from scheduled 1200s down to 1200 + (0 - 900) = 300s, under the 180s...
    # use a bigger lead delay to cross the 180s/3-minute default threshold.
    trip_delays = {"LEAD": 1100.0, "FOLLOW": 0.0}  # observed = 1200 + (0 - 1100) = 100s

    headway_samples, bunching_events = detect_bunching(session, vehicle_positions, trip_delays, poll_time)

    assert len(headway_samples) == 1
    assert headway_samples[0].crossed_threshold is True
    assert headway_samples[0].scheduled_headway_seconds == 1200

    assert len(bunching_events) == 1
    event = bunching_events[0]
    assert event.route_id == "R1"
    assert event.lead_trip_id == "LEAD"
    assert event.follow_trip_id == "FOLLOW"
    assert event.observed_headway_seconds == 100


def test_bunching_not_flagged_when_headway_healthy(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S_COMMON", 29.50, -98.50)
    _seed_stop(session, "S1", 29.49, -98.51)
    _seed_trip(session, "LEAD", "R1", [(1, "S1", 27600), (2, "S_COMMON", 27700)])
    _seed_trip(session, "FOLLOW", "R1", [(1, "S1", 28800), (2, "S_COMMON", 28900)])

    poll_time = datetime.datetime.now(datetime.timezone.utc)
    vehicle_positions = [
        {
            "vehicle_id": "BUS_LEAD", "trip_id": "LEAD", "route_id": "R1", "direction_id": 0,
            "latitude": 29.50, "longitude": -98.50, "current_stop_sequence": 2, "stop_id": "S_COMMON",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
        {
            "vehicle_id": "BUS_FOLLOW", "trip_id": "FOLLOW", "route_id": "R1", "direction_id": 0,
            "latitude": 29.495, "longitude": -98.505, "current_stop_sequence": 1, "stop_id": "S1",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
    ]
    trip_delays = {"LEAD": 0.0, "FOLLOW": 0.0}  # observed == scheduled == 1200s

    headway_samples, bunching_events = detect_bunching(session, vehicle_positions, trip_delays, poll_time)
    assert len(headway_samples) == 1
    assert headway_samples[0].crossed_threshold is False
    assert bunching_events == []


def test_bunching_excludes_vehicle_at_terminal(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S1", 29.49, -98.51)
    _seed_stop(session, "S_END", 29.50, -98.50)
    # LEAD is STOPPED_AT its trip's *last* stop - a terminal/layover, should be excluded.
    _seed_trip(session, "LEAD", "R1", [(1, "S1", 27600), (2, "S_END", 27700)])
    _seed_trip(session, "FOLLOW", "R1", [(1, "S1", 28800), (2, "S_END", 28900)])

    poll_time = datetime.datetime.now(datetime.timezone.utc)
    vehicle_positions = [
        {
            "vehicle_id": "BUS_LEAD", "trip_id": "LEAD", "route_id": "R1", "direction_id": 0,
            "latitude": 29.50, "longitude": -98.50, "current_stop_sequence": 2, "stop_id": "S_END",
            "current_status": "STOPPED_AT", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
        {
            "vehicle_id": "BUS_FOLLOW", "trip_id": "FOLLOW", "route_id": "R1", "direction_id": 0,
            "latitude": 29.495, "longitude": -98.505, "current_stop_sequence": 1, "stop_id": "S1",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
    ]
    trip_delays = {"LEAD": 1100.0, "FOLLOW": 0.0}

    headway_samples, bunching_events = detect_bunching(session, vehicle_positions, trip_delays, poll_time)
    assert headway_samples == []
    assert bunching_events == []


def test_ongoing_bunching_event_extended_not_duplicated(session):
    _seed_route_and_calendar(session)
    _seed_stop(session, "S_COMMON", 29.50, -98.50)
    _seed_stop(session, "S1", 29.49, -98.51)
    _seed_trip(session, "LEAD", "R1", [(1, "S1", 27600), (2, "S_COMMON", 27700)])
    _seed_trip(session, "FOLLOW", "R1", [(1, "S1", 28800), (2, "S_COMMON", 28900)])

    vehicle_positions = [
        {
            "vehicle_id": "BUS_LEAD", "trip_id": "LEAD", "route_id": "R1", "direction_id": 0,
            "latitude": 29.50, "longitude": -98.50, "current_stop_sequence": 2, "stop_id": "S_COMMON",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
        {
            "vehicle_id": "BUS_FOLLOW", "trip_id": "FOLLOW", "route_id": "R1", "direction_id": 0,
            "latitude": 29.495, "longitude": -98.505, "current_stop_sequence": 1, "stop_id": "S1",
            "current_status": "IN_TRANSIT_TO", "start_date": None, "timestamp": None, "bearing": None, "speed": None,
        },
    ]
    trip_delays = {"LEAD": 1100.0, "FOLLOW": 0.0}

    t0 = datetime.datetime.now(datetime.timezone.utc)
    _, first_events = detect_bunching(session, vehicle_positions, trip_delays, t0)
    session.add_all(first_events)
    session.commit()
    assert session.query(BunchingEvent).count() == 1

    t1 = t0 + datetime.timedelta(seconds=45)
    _, second_events = detect_bunching(session, vehicle_positions, trip_delays, t1)
    session.add_all(second_events)
    session.commit()

    # Second poll should extend the existing event's end_time, not create a new row.
    # (SQLite drops tzinfo on round-trip, unlike Postgres - compare naive here.)
    assert session.query(BunchingEvent).count() == 1
    stored_end_time = session.query(BunchingEvent).one().end_time
    assert stored_end_time.replace(tzinfo=datetime.timezone.utc) == t1

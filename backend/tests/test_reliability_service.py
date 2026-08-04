import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import BunchingEvent, Calendar, Route, ScheduleDeviation, Stop, StopTime, Trip
from app.reliability_service import compute_group_reliability, compute_segment_reliability

IN_RANGE_DATE = datetime.date(2026, 7, 1)
START_DATE = datetime.date(2026, 7, 1)
END_DATE = datetime.date(2026, 7, 31)
OUT_OF_RANGE_DATE = datetime.date(2026, 6, 1)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_calendar(session):
    session.add(
        Calendar(
            service_id="WEEKDAY", monday=True, tuesday=True, wednesday=True, thursday=True, friday=True,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
    )


def _seed_route(session, route_id, short_name):
    session.add(Route(route_id=route_id, route_short_name=short_name, route_long_name=f"{short_name} Long Name"))


def _seed_stop(session, stop_id, name="Stop"):
    session.add(Stop(stop_id=stop_id, stop_name=name, stop_lat=29.5, stop_lon=-98.5))


def _seed_trip(session, trip_id, route_id, direction_id, stop_times):
    session.add(Trip(trip_id=trip_id, route_id=route_id, service_id="WEEKDAY", direction_id=direction_id))
    session.flush()
    for seq, stop_id, arrival in stop_times:
        session.add(StopTime(trip_id=trip_id, stop_sequence=seq, stop_id=stop_id, arrival_time_seconds=arrival, departure_time_seconds=arrival))


def _add_deviation(session, trip_id, route_id, stop_id, delay_seconds, service_date=IN_RANGE_DATE, stop_sequence=2, event_type="arrival"):
    session.add(
        ScheduleDeviation(
            trip_id=trip_id, route_id=route_id, stop_id=stop_id, stop_sequence=stop_sequence,
            service_date=service_date, scheduled_time_seconds=28800,
            actual_time=datetime.datetime.combine(service_date, datetime.time(8, 0), tzinfo=datetime.timezone.utc),
            delay_seconds=delay_seconds, event_type=event_type, source="trip_update", match_type="exact_sequence",
        )
    )


def test_segment_reliability_basic_aggregation(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "A", "Start")
    _seed_stop(session, "B", "End")
    _seed_trip(session, "T1", "R1", 0, [(1, "A", 28800), (2, "B", 29100)])
    session.flush()

    # 3 on-time-ish samples, 1 way late sample - each a different service_date,
    # since one trip visits one stop at most once per real service day (the
    # schedule_deviations unique constraint enforces this).
    _add_deviation(session, "T1", "R1", "B", delay_seconds=60, service_date=IN_RANGE_DATE)
    _add_deviation(session, "T1", "R1", "B", delay_seconds=-30, service_date=IN_RANGE_DATE + datetime.timedelta(days=1))
    _add_deviation(session, "T1", "R1", "B", delay_seconds=0, service_date=IN_RANGE_DATE + datetime.timedelta(days=2))
    _add_deviation(session, "T1", "R1", "B", delay_seconds=900, service_date=IN_RANGE_DATE + datetime.timedelta(days=3))  # 15 min late - not on-time
    session.commit()

    result = compute_segment_reliability(session, "R1", "A", "B", START_DATE, END_DATE)

    assert result is not None
    assert result.sample_count == 4
    assert result.avg_delay_seconds == pytest.approx((60 - 30 + 0 + 900) / 4)
    assert result.on_time_pct == pytest.approx(75.0)  # 3 of 4 within default thresholds
    assert result.confidence == "low"  # below min_reliable_sample_count (20)


def test_segment_reliability_high_confidence_above_threshold(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "A", "Start")
    _seed_stop(session, "B", "End")
    _seed_trip(session, "T1", "R1", 0, [(1, "A", 28800), (2, "B", 29100)])
    session.flush()

    for i in range(settings.min_reliable_sample_count):
        _add_deviation(session, "T1", "R1", "B", delay_seconds=30, service_date=IN_RANGE_DATE + datetime.timedelta(days=i))
    session.commit()

    result = compute_segment_reliability(session, "R1", "A", "B", START_DATE, END_DATE)
    assert result.confidence == "high"
    assert result.sample_count == settings.min_reliable_sample_count


def test_segment_reliability_excludes_out_of_range_dates(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "A", "Start")
    _seed_stop(session, "B", "End")
    _seed_trip(session, "T1", "R1", 0, [(1, "A", 28800), (2, "B", 29100)])
    session.flush()

    _add_deviation(session, "T1", "R1", "B", delay_seconds=30, service_date=IN_RANGE_DATE)
    _add_deviation(session, "T1", "R1", "B", delay_seconds=9999, service_date=OUT_OF_RANGE_DATE)
    session.commit()

    result = compute_segment_reliability(session, "R1", "A", "B", START_DATE, END_DATE)
    assert result.sample_count == 1
    assert result.avg_delay_seconds == 30


def test_segment_reliability_none_when_stops_not_connected_in_order(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "A", "Start")
    _seed_stop(session, "B", "End")
    # B comes before A on this trip - requesting A->B should fail to resolve.
    _seed_trip(session, "T1", "R1", 0, [(1, "B", 28800), (2, "A", 29100)])
    session.commit()

    result = compute_segment_reliability(session, "R1", "A", "B", START_DATE, END_DATE)
    assert result is None


def test_segment_reliability_none_for_unrelated_stops(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "A", "Start")
    _seed_stop(session, "Z", "Nowhere near this route")
    _seed_trip(session, "T1", "R1", 0, [(1, "A", 28800)])
    session.commit()

    assert compute_segment_reliability(session, "R1", "A", "Z", START_DATE, END_DATE) is None


def test_segment_reliability_scopes_bunching_to_segment_stops(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "A", "Start")
    _seed_stop(session, "B", "Middle")
    _seed_stop(session, "C", "End")
    _seed_trip(session, "T1", "R1", 0, [(1, "A", 28800), (2, "B", 29000), (3, "C", 29200)])
    session.flush()
    _add_deviation(session, "T1", "R1", "C", delay_seconds=30)

    in_range = datetime.datetime.combine(IN_RANGE_DATE, datetime.time(9, 0), tzinfo=datetime.timezone.utc)
    # Bunching event at B - within the A->C segment, should count.
    session.add(
        BunchingEvent(
            route_id="R1", direction_id=0, start_time=in_range, end_time=in_range,
            nearest_stop_id="B", vehicle_ids=["v1", "v2"], observed_headway_seconds=60,
            scheduled_headway_seconds=900, severity="high",
        )
    )
    # Bunching event elsewhere on the route (not on this trip's stop list at all) - should not count.
    _seed_stop(session, "FAR_AWAY", "Not on this trip")
    session.add(
        BunchingEvent(
            route_id="R1", direction_id=0, start_time=in_range, end_time=in_range,
            nearest_stop_id="FAR_AWAY", vehicle_ids=["v3", "v4"], observed_headway_seconds=60,
            scheduled_headway_seconds=900, severity="high",
        )
    )
    session.commit()

    result = compute_segment_reliability(session, "R1", "A", "C", START_DATE, END_DATE)
    assert result.bunching_event_count == 1


def test_group_reliability_ranks_worst_first_and_drops_low_sample_rows(session):
    _seed_calendar(session)
    _seed_route(session, "GOOD", "Good Route")
    _seed_route(session, "BAD", "Bad Route")
    _seed_route(session, "NOISY", "Noisy Route")
    _seed_stop(session, "S1")
    _seed_trip(session, "T_GOOD", "GOOD", 0, [(1, "S1", 28800)])
    _seed_trip(session, "T_BAD", "BAD", 0, [(1, "S1", 28800)])
    _seed_trip(session, "T_NOISY", "NOISY", 0, [(1, "S1", 28800)])
    session.flush()

    for i in range(25):
        _add_deviation(session, "T_GOOD", "GOOD", "S1", delay_seconds=0, service_date=IN_RANGE_DATE + datetime.timedelta(days=i))
    for i in range(25):
        _add_deviation(session, "T_BAD", "BAD", "S1", delay_seconds=900, service_date=IN_RANGE_DATE + datetime.timedelta(days=i))  # always late
    # Only 2 samples - should be excluded from ranking despite being "bad".
    _add_deviation(session, "T_NOISY", "NOISY", "S1", delay_seconds=9999, service_date=IN_RANGE_DATE)
    _add_deviation(session, "T_NOISY", "NOISY", "S1", delay_seconds=9999, service_date=IN_RANGE_DATE + datetime.timedelta(days=1))
    session.commit()

    results = compute_group_reliability(session, START_DATE, END_DATE, group_by="route", limit=10)

    route_ids = [r.route_id for r in results]
    assert "NOISY" not in route_ids  # dropped for insufficient samples
    assert route_ids[0] == "BAD"  # worst on-time% ranked first
    assert "GOOD" in route_ids
    assert results[0].on_time_pct == 0.0
    for r in results:
        assert r.scope == "route"
        assert r.confidence == "high"


def test_group_reliability_stop_scope(session):
    _seed_calendar(session)
    _seed_route(session, "R1", "1")
    _seed_stop(session, "STOP_A", "Stop A")
    _seed_stop(session, "STOP_B", "Stop B")
    _seed_trip(session, "T1", "R1", 0, [(1, "STOP_A", 28800), (2, "STOP_B", 29100)])
    session.flush()

    for i in range(25):
        _add_deviation(session, "T1", "R1", "STOP_A", delay_seconds=30, stop_sequence=1, service_date=IN_RANGE_DATE + datetime.timedelta(days=i))
    for i in range(25):
        _add_deviation(session, "T1", "R1", "STOP_B", delay_seconds=900, stop_sequence=2, service_date=IN_RANGE_DATE + datetime.timedelta(days=i))
    session.commit()

    results = compute_group_reliability(session, START_DATE, END_DATE, group_by="stop", limit=10)
    assert results[0].end_stop_id == "STOP_B"
    assert results[0].scope == "stop"

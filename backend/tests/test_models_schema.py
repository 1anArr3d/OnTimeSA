"""Schema round-trip tests against an ephemeral in-memory SQLite DB.

This validates the ORM models (columns, FKs, relationships) are wired up
correctly without needing a real Postgres/Neon connection. The upsert loader
(app/gtfs/loader.py) uses Postgres-specific ON CONFLICT syntax and is not
exercised here - it needs a real Postgres connection to test end-to-end.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    BunchingEvent,
    Calendar,
    Route,
    ScheduleDeviation,
    Stop,
    StopTime,
    Trip,
    VehiclePositionSnapshot,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_static_schema_round_trip(session):
    route = Route(route_id="10", route_short_name="10", route_long_name="Naco / Broadway Skip", route_type=3)
    stop_a = Stop(stop_id="A1", stop_name="Start Stop", stop_lat=29.5, stop_lon=-98.5)
    stop_b = Stop(stop_id="B1", stop_name="End Stop", stop_lat=29.51, stop_lon=-98.49)
    calendar = Calendar(
        service_id="WEEKDAY",
        monday=True,
        tuesday=True,
        wednesday=True,
        thursday=True,
        friday=True,
        saturday=False,
        sunday=False,
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 12, 31),
    )
    session.add_all([route, stop_a, stop_b, calendar])
    session.flush()

    trip = Trip(trip_id="T1", route_id="10", service_id="WEEKDAY", direction_id=0)
    session.add(trip)
    session.flush()

    st1 = StopTime(trip_id="T1", stop_sequence=1, stop_id="A1", arrival_time_seconds=100, departure_time_seconds=100)
    st2 = StopTime(trip_id="T1", stop_sequence=2, stop_id="B1", arrival_time_seconds=400, departure_time_seconds=400)
    session.add_all([st1, st2])
    session.commit()

    fetched_trip = session.get(Trip, "T1")
    assert fetched_trip.route.route_short_name == "10"
    assert fetched_trip.calendar.monday is True
    assert [st.stop_sequence for st in fetched_trip.stop_times] == [1, 2]
    assert fetched_trip.stop_times[0].stop.stop_name == "Start Stop"


def test_realtime_schema_round_trip(session):
    route = Route(route_id="10", route_short_name="10")
    stop = Stop(stop_id="A1", stop_name="Start Stop", stop_lat=29.5, stop_lon=-98.5)
    calendar = Calendar(
        service_id="WEEKDAY",
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 12, 31),
    )
    session.add_all([route, stop, calendar])
    session.flush()
    trip = Trip(trip_id="T1", route_id="10", service_id="WEEKDAY")
    session.add(trip)
    session.commit()

    now = datetime.datetime.now(datetime.timezone.utc)

    snapshot = VehiclePositionSnapshot(
        vehicle_id="V1",
        trip_id="T1",
        route_id="10",
        latitude=29.5,
        longitude=-98.5,
        current_status="IN_TRANSIT_TO",
        vehicle_timestamp=now,
    )
    deviation = ScheduleDeviation(
        trip_id="T1",
        route_id="10",
        stop_id="A1",
        stop_sequence=1,
        service_date=datetime.date(2026, 1, 5),
        scheduled_time_seconds=100,
        actual_time=now,
        delay_seconds=180,
        event_type="arrival",
        source="trip_update",
        match_type="exact_sequence",
    )
    bunching = BunchingEvent(
        route_id="10",
        start_time=now,
        end_time=now + datetime.timedelta(minutes=2),
        vehicle_ids=["V1", "V2"],
        observed_headway_seconds=90,
        scheduled_headway_seconds=900,
        severity="high",
    )
    session.add_all([snapshot, deviation, bunching])
    session.commit()

    assert session.query(VehiclePositionSnapshot).count() == 1
    assert session.query(ScheduleDeviation).one().delay_seconds == 180
    stored_bunching = session.query(BunchingEvent).one()
    assert stored_bunching.vehicle_ids == ["V1", "V2"]

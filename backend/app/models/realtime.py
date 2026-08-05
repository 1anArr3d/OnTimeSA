import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Date, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VehiclePositionSnapshot(Base):
    """One row per polled vehicle position update from GTFS-RT VehiclePositions.

    Never overwritten - each poll appends new rows so history is preserved
    for trend analysis.
    """

    __tablename__ = "vehicle_position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(String, index=True)
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.trip_id"), nullable=True, index=True)
    route_id: Mapped[str | None] = mapped_column(ForeignKey("routes.route_id"), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    bearing: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_stop_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_id: Mapped[str | None] = mapped_column(ForeignKey("stops.stop_id"), nullable=True)
    # GTFS-RT VehicleStopStatus: INCOMING_AT, STOPPED_AT, IN_TRANSIT_TO
    current_status: Mapped[str | None] = mapped_column(String, nullable=True)
    vehicle_timestamp: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    polled_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True
    )


class ScheduleDeviation(Base):
    """Computed delay (actual vs. scheduled) for a trip at a stop, derived from
    a vehicle position/trip update snapshot compared against static stop_times.

    One row per realized (trip, stop-visit, service_date, event_type) - NOT
    one row per poll. GTFS-RT TripUpdates sends predictions for every
    remaining stop on a trip, not just the next one, so without the unique
    constraint below this table would gain a near-duplicate row for every
    still-far-away stop on every ~45s poll (measured: ~12M rows/day from
    this alone). The upsert in app/gtfs/deviation.py keys on the constraint
    below, so a stop's row keeps refining as predictions firm up and then
    naturally stops changing once the vehicle passes it and VIA's feed stops
    sending predictions for it.

    stop_sequence (not stop_id) is part of the key deliberately - a loop
    route can revisit the same physical stop twice in one trip, and stop_id
    alone would incorrectly collapse those into a single row.
    """

    __tablename__ = "schedule_deviations"
    __table_args__ = (
        UniqueConstraint(
            "trip_id", "stop_sequence", "service_date", "event_type", name="uq_schedule_deviation_stop_visit"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.trip_id"), index=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"), index=True)
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.stop_id"), index=True)
    stop_sequence: Mapped[int] = mapped_column(Integer)
    service_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    scheduled_time_seconds: Mapped[int] = mapped_column(Integer)
    actual_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    delay_seconds: Mapped[int] = mapped_column(Integer)
    # "arrival" or "departure" - which scheduled event this deviation is measured against
    event_type: Mapped[str] = mapped_column(String)
    # "trip_update" or "vehicle_position" - which GTFS-RT feed this was derived from
    source: Mapped[str] = mapped_column(String)
    # "exact_sequence" / "exact_stop_id" / "nearest_geographic" - see app/gtfs/matching.py.
    # Kept so noisy nearest_geographic matches can be filtered out of reliability
    # stats later if they turn out to add too much noise.
    match_type: Mapped[str] = mapped_column(String)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class BunchingEvent(Base):
    """A detected bunching incident: two or more consecutive vehicles on the
    same route running closer together than the threshold headway.
    """

    __tablename__ = "bunching_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"), index=True)
    direction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    location_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    nearest_stop_id: Mapped[str | None] = mapped_column(ForeignKey("stops.stop_id"), nullable=True)
    lead_trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.trip_id"), nullable=True)
    follow_trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.trip_id"), nullable=True)
    vehicle_ids: Mapped[list] = mapped_column(JSON)
    observed_headway_seconds: Mapped[float] = mapped_column(Float)
    scheduled_headway_seconds: Mapped[float] = mapped_column(Float)
    # low / medium / high, based on how far under threshold the headway fell
    severity: Mapped[str] = mapped_column(String)
    detected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class DailyRouteStat(Base):
    """One row per (route, service_date) rollup of that day's schedule_deviations
    and bunching_events, computed once by the nightly rollup job.

    Exists so reliability queries reaching further back than
    settings.raw_data_retention_days can still answer without the raw rows -
    see app/rollup_service.py. sample_count is carried through so the same
    low-confidence-flag convention used elsewhere (settings.min_reliable_sample_count)
    still applies to rollup-sourced results.
    """

    __tablename__ = "daily_route_stats"
    __table_args__ = (UniqueConstraint("route_id", "service_date", name="uq_daily_route_stat_route_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"), index=True)
    service_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    on_time_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    bunching_event_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class HeadwaySample(Base):
    """Every consecutive-vehicle-pair headway comparison computed during
    bunching detection, whether or not it crossed the bunching threshold.

    Bunching thresholds (app/config.py) were picked before seeing what actual
    headway variance looks like on VIA's network. Keeping every sample - not
    just the ones that trip the threshold - means the cutoff can be tuned
    against real distribution data later instead of by guesswork.
    """

    __tablename__ = "headway_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"), index=True)
    direction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lead_trip_id: Mapped[str] = mapped_column(ForeignKey("trips.trip_id"))
    follow_trip_id: Mapped[str] = mapped_column(ForeignKey("trips.trip_id"))
    lead_vehicle_id: Mapped[str] = mapped_column(String)
    follow_vehicle_id: Mapped[str] = mapped_column(String)
    reference_stop_id: Mapped[str | None] = mapped_column(ForeignKey("stops.stop_id"), nullable=True)
    lead_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    follow_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_headway_seconds: Mapped[float] = mapped_column(Float)
    scheduled_headway_seconds: Mapped[float] = mapped_column(Float)
    crossed_threshold: Mapped[bool] = mapped_column(default=False, index=True)
    location_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    sampled_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)

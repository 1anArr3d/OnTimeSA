from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Route(Base):
    """From GTFS static routes.txt."""

    __tablename__ = "routes"

    route_id: Mapped[str] = mapped_column(String, primary_key=True)
    agency_id: Mapped[str | None] = mapped_column(String, nullable=True)
    route_short_name: Mapped[str | None] = mapped_column(String, nullable=True)
    route_long_name: Mapped[str | None] = mapped_column(String, nullable=True)
    route_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_color: Mapped[str | None] = mapped_column(String, nullable=True)
    route_text_color: Mapped[str | None] = mapped_column(String, nullable=True)

    trips: Mapped[list["Trip"]] = relationship(back_populates="route")


class Stop(Base):
    """From GTFS static stops.txt."""

    __tablename__ = "stops"

    stop_id: Mapped[str] = mapped_column(String, primary_key=True)
    stop_code: Mapped[str | None] = mapped_column(String, nullable=True)
    stop_name: Mapped[str | None] = mapped_column(String, nullable=True)
    stop_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    location_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_station: Mapped[str | None] = mapped_column(String, nullable=True)

    stop_times: Mapped[list["StopTime"]] = relationship(back_populates="stop")


class Calendar(Base):
    """From GTFS static calendar.txt. Defines which days a service_id runs."""

    __tablename__ = "calendar"

    service_id: Mapped[str] = mapped_column(String, primary_key=True)
    monday: Mapped[bool] = mapped_column(Boolean, default=False)
    tuesday: Mapped[bool] = mapped_column(Boolean, default=False)
    wednesday: Mapped[bool] = mapped_column(Boolean, default=False)
    thursday: Mapped[bool] = mapped_column(Boolean, default=False)
    friday: Mapped[bool] = mapped_column(Boolean, default=False)
    saturday: Mapped[bool] = mapped_column(Boolean, default=False)
    sunday: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[Date] = mapped_column(Date)
    end_date: Mapped[Date] = mapped_column(Date)

    trips: Mapped[list["Trip"]] = relationship(back_populates="calendar")


class Trip(Base):
    """From GTFS static trips.txt."""

    __tablename__ = "trips"

    trip_id: Mapped[str] = mapped_column(String, primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("calendar.service_id"), index=True)
    trip_headsign: Mapped[str | None] = mapped_column(String, nullable=True)
    direction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_id: Mapped[str | None] = mapped_column(String, nullable=True)
    shape_id: Mapped[str | None] = mapped_column(String, nullable=True)

    route: Mapped["Route"] = relationship(back_populates="trips")
    calendar: Mapped["Calendar"] = relationship(back_populates="trips")
    stop_times: Mapped[list["StopTime"]] = relationship(back_populates="trip")


class StopTime(Base):
    """From GTFS static stop_times.txt.

    arrival/departure are stored as seconds-since-midnight (GTFS allows values
    past 24:00:00 for trips that run past midnight, so a Time column can't hold them).
    """

    __tablename__ = "stop_times"

    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.trip_id"), primary_key=True)
    stop_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.stop_id"), index=True)
    arrival_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pickup_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drop_off_type: Mapped[int | None] = mapped_column(Integer, nullable=True)

    trip: Mapped["Trip"] = relationship(back_populates="stop_times")
    stop: Mapped["Stop"] = relationship(back_populates="stop_times")

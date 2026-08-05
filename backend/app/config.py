from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SATP_")

    database_url: str = ""

    gtfs_static_url: str = "https://www.viainfo.net/BusService/google_transit.zip"
    gtfs_rt_vehicle_positions_url: str = "http://gtfs.viainfo.net/vehicle/vehiclepositions.pb"
    gtfs_rt_trip_updates_url: str = "http://gtfs.viainfo.net/tripupdate/tripupdates.pb"
    gtfs_rt_alerts_url: str = "http://gtfs.viainfo.net/alert/alerts.pb"

    gtfs_static_refresh_hours: int = 24
    # 2 minutes, not 45s - see README changelog re: Neon free-tier storage
    # cap. Still frequent enough to catch bunching events, which in practice
    # have played out over multiple minutes in every real event detected so far.
    gtfs_rt_poll_seconds: int = 120

    # How many days of raw vehicle_position_snapshots / schedule_deviations
    # to keep before the daily pruning job deletes them. Older history lives
    # only in the daily_route_stats rollup after this window.
    raw_data_retention_days: int = 5

    # Bunching detection thresholds
    bunching_headway_threshold_minutes: float = 3.0
    bunching_min_scheduled_headway_minutes: float = 15.0

    # A trip counts "on time" if its delay falls within
    # [on_time_early_threshold_seconds, on_time_late_threshold_seconds].
    # Negative = early. Defaults follow the common industry convention of
    # "up to 1 min early, up to 5 min late".
    on_time_early_threshold_seconds: int = -60
    on_time_late_threshold_seconds: int = 300

    # Below this many samples, reliability stats are flagged low-confidence
    # rather than hidden - the number is still returned, just labeled.
    min_reliable_sample_count: int = 20

    # Vite falls back to the next free port (5174, 5175, ...) if 5173 is
    # already taken by another process, so allow a small range of dev ports
    # rather than requiring an exact match.
    cors_allow_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
    ]


settings = Settings()

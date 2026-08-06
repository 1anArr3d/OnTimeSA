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

    # How often buffered poll data (snapshots, deviations, headway samples,
    # bunching events) is written to Postgres - see app/live_state.py and
    # app/poller.py's flush_state_to_db(). The poller itself keeps polling
    # VIA's feeds every gtfs_rt_poll_seconds regardless; this only controls
    # how often that buffered data actually reaches the database.
    flush_interval_seconds: int = 3600
    # Safety cap on unflushed data held in memory (pending_snapshots,
    # pending_headway_samples - see app/live_state.py), expressed as a
    # multiple of flush_interval_seconds so it scales automatically if that's
    # ever retuned instead of needing separate adjustment. A couple of missed
    # flushes (normal retry-every-poll-cycle behavior) never trips this; a
    # sustained outage degrades by dropping the oldest buffered data instead
    # of growing unbounded - see the 2026-08-06 incident, where a stale DB
    # credential left flushes failing for 9+ hours and the unbounded buffers
    # were a direct contributor to the box being OOM-killed repeatedly.
    pending_buffer_retention_multiplier: float = 4.0
    # How often the in-memory static schedule cache (routes/stops/trips/
    # stop_times) is reloaded from Postgres - the static feed itself only
    # changes about once a day, so this doesn't need to be frequent.
    static_cache_refresh_hours: int = 24

    # Per-IP rate limits (slowapi syntax, e.g. "60/minute"). vehicles_live is
    # looser since /api/vehicles/live now serves from memory (see
    # app/live_state.py) rather than querying Postgres; reliability/bunching
    # stay tighter since those still hit Postgres and can span wide date
    # ranges. default applies to every other /api/ route (catalog lookups).
    rate_limit_default: str = "60/minute"
    rate_limit_vehicles_live: str = "30/minute"
    rate_limit_reliability: str = "10/minute"


settings = Settings()

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SATP_")

    database_url: str = ""

    gtfs_static_url: str = "https://www.viainfo.net/BusService/google_transit.zip"
    gtfs_rt_vehicle_positions_url: str = "http://gtfs.viainfo.net/vehicle/vehiclepositions.pb"
    gtfs_rt_trip_updates_url: str = "http://gtfs.viainfo.net/tripupdate/tripupdates.pb"
    gtfs_rt_alerts_url: str = "http://gtfs.viainfo.net/alert/alerts.pb"

    gtfs_static_refresh_hours: int = 24
    gtfs_rt_poll_seconds: int = 45

    # Bunching detection thresholds
    bunching_headway_threshold_minutes: float = 3.0
    bunching_min_scheduled_headway_minutes: float = 15.0


settings = Settings()

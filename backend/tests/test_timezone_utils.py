import datetime

from app.gtfs.timezone_utils import infer_service_date, parse_rt_start_date, scheduled_seconds_to_utc


def test_scheduled_seconds_to_utc_cdt_offset():
    # Aug 3 2026 is CDT (UTC-5). 08:15:00 local -> 13:15:00 UTC.
    result = scheduled_seconds_to_utc(datetime.date(2026, 8, 3), 8 * 3600 + 15 * 60)
    assert result == datetime.datetime(2026, 8, 3, 13, 15, 0, tzinfo=datetime.timezone.utc)


def test_scheduled_seconds_to_utc_cst_offset():
    # Jan 15 2026 is CST (UTC-6). 08:15:00 local -> 14:15:00 UTC.
    result = scheduled_seconds_to_utc(datetime.date(2026, 1, 15), 8 * 3600 + 15 * 60)
    assert result == datetime.datetime(2026, 1, 15, 14, 15, 0, tzinfo=datetime.timezone.utc)


def test_scheduled_seconds_to_utc_past_midnight_spillover():
    # GTFS "25:30:00" on service_date Aug 3 means 01:30 local on Aug 4.
    result = scheduled_seconds_to_utc(datetime.date(2026, 8, 3), 25 * 3600 + 30 * 60)
    local = result.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
    assert local.date() == datetime.date(2026, 8, 4)
    assert local.hour == 1 and local.minute == 30


def test_infer_service_date_matches_same_day_observation():
    # Scheduled 08:15 local, observed almost exactly on time on Aug 3.
    scheduled_seconds = 8 * 3600 + 15 * 60
    actual_utc = scheduled_seconds_to_utc(datetime.date(2026, 8, 3), scheduled_seconds) + datetime.timedelta(minutes=2)
    assert infer_service_date(actual_utc, scheduled_seconds) == datetime.date(2026, 8, 3)


def test_infer_service_date_handles_post_midnight_trip():
    # Trip scheduled as "25:30:00" (1:30am) on service_date Aug 3 - observed
    # shortly after 1:30am local on the calendar day of Aug 4. Should still
    # resolve back to service_date Aug 3, not Aug 4.
    scheduled_seconds = 25 * 3600 + 30 * 60
    actual_utc = scheduled_seconds_to_utc(datetime.date(2026, 8, 3), scheduled_seconds) + datetime.timedelta(minutes=3)
    assert infer_service_date(actual_utc, scheduled_seconds) == datetime.date(2026, 8, 3)


def test_parse_rt_start_date():
    assert parse_rt_start_date("20260803") == datetime.date(2026, 8, 3)
    assert parse_rt_start_date(None) is None
    assert parse_rt_start_date("") is None
    assert parse_rt_start_date("not-a-date") is None

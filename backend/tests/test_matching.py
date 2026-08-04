from app.gtfs.matching import haversine_meters, match_stop_time

STOP_TIMES = [
    {"stop_sequence": 1, "stop_id": "A", "arrival_time_seconds": 100, "departure_time_seconds": 100, "stop_lat": 29.50, "stop_lon": -98.50},
    {"stop_sequence": 2, "stop_id": "B", "arrival_time_seconds": 200, "departure_time_seconds": 200, "stop_lat": 29.51, "stop_lon": -98.49},
    {"stop_sequence": 3, "stop_id": "C", "arrival_time_seconds": 300, "departure_time_seconds": 300, "stop_lat": 29.52, "stop_lon": -98.48},
]


def test_exact_sequence_match_wins():
    matched, match_type = match_stop_time(STOP_TIMES, stop_sequence=2, stop_id="C")
    assert match_type == "exact_sequence"
    assert matched["stop_id"] == "B"


def test_falls_back_to_stop_id_when_sequence_unknown():
    matched, match_type = match_stop_time(STOP_TIMES, stop_sequence=99, stop_id="B")
    assert match_type == "exact_stop_id"
    assert matched["stop_id"] == "B"


def test_falls_back_to_nearest_geographic_within_tolerance():
    # Very close to stop B's coordinates, no sequence/stop_id given.
    matched, match_type = match_stop_time(STOP_TIMES, lat=29.5101, lon=-98.4901, max_distance_meters=500)
    assert match_type == "nearest_geographic"
    assert matched["stop_id"] == "B"


def test_no_match_when_nothing_within_tolerance():
    # Far from every stop in the list.
    matched, match_type = match_stop_time(STOP_TIMES, lat=30.5, lon=-99.5, max_distance_meters=500)
    assert matched is None
    assert match_type is None


def test_no_match_returns_none_none_for_empty_stop_times():
    matched, match_type = match_stop_time([], stop_sequence=1, stop_id="A", lat=29.5, lon=-98.5)
    assert matched is None
    assert match_type is None


def test_haversine_zero_distance():
    assert haversine_meters(29.5, -98.5, 29.5, -98.5) == 0


def test_haversine_known_distance_roughly_correct():
    # ~0.01 degrees latitude apart at this latitude is roughly 1.1km.
    distance = haversine_meters(29.50, -98.50, 29.51, -98.50)
    assert 1000 < distance < 1200

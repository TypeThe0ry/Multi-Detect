from multidetect.search_coverage import (
    CoverageCamera,
    CoverageConfig,
    current_expanding_square,
    lawnmower,
    simulate_pattern,
)


def test_current_pattern_starts_at_centre_and_stays_in_zone() -> None:
    path = current_expanding_square(25.0, CoverageCamera(), 35.0)
    assert path[0] == (0.0, 0.0)
    assert all((n * n + e * e) ** 0.5 <= 25.0 + 1e-6 for n, e in path)


def test_lawnmower_spacing_uses_camera_footprint() -> None:
    camera = CoverageCamera()
    path = lawnmower(25.0, camera, 20.0)
    assert len(path) > 3
    assert path[0] == (0.0, 0.0)


def test_small_object_is_floor_limited_at_high_altitude() -> None:
    config = CoverageConfig(radius_m=25.0, altitude_m=35.0, object_count=32)
    result = simulate_pattern("current", object_size_m=0.5, config=config)
    assert result.objects_seen_well == 0
    assert result.time_to_cover_90_s is None


def test_two_altitude_can_recover_small_objects() -> None:
    config = CoverageConfig(radius_m=25.0, altitude_m=35.0, low_altitude_m=8.0, object_count=32)
    result = simulate_pattern("two_altitude", object_size_m=0.5, config=config)
    assert result.total_time_s > 0
    assert result.expected_slow_calls == result.objects_seen_well

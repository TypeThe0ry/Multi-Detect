from multidetect.search_coverage import (
    CoverageCamera,
    CoverageConfig,
    current_expanding_square,
    lawnmower,
    simulate_pattern,
)


def test_current_pattern_starts_at_centre_and_stays_in_zone() -> None:
    path = current_expanding_square(25.0, CoverageCamera(), 35.0)
    # ExpertSearchPolicy stores waypoints after home; the first leg starts at
    # the historical spacing rather than adding an artificial centre waypoint.
    assert path[0] == (12.5, 0.0)
    assert path[1] == (12.5, 12.5)
    assert path[2] == (-12.5, 12.5)
    assert all((n * n + e * e) ** 0.5 <= 25.0 + 1e-6 for n, e in path)


def test_lawnmower_spacing_uses_camera_footprint() -> None:
    camera = CoverageCamera()
    path = lawnmower(25.0, camera, 20.0)
    assert len(path) > 3
    assert path[0] == (0.0, 0.0)


def test_small_object_is_floor_limited_at_high_altitude() -> None:
    config = CoverageConfig(radius_m=25.0, altitude_m=35.0, object_count=32)
    result = simulate_pattern("current", object_size_m=0.5, config=config)
    assert result.candidates_seen > 0
    assert result.slow_verdict_eligible == 0
    assert result.confirmed == 0
    assert result.candidate_fraction > result.slow_verdict_fraction
    assert result.time_to_cover_90_s is None


def test_slant_range_gate_rejects_objects_outside_configured_limit() -> None:
    config = CoverageConfig(
        radius_m=25.0,
        altitude_m=35.0,
        max_detection_range_m=35.1,
        proposal_floor_px=1.0,
        object_count=32,
    )
    result = simulate_pattern("lawnmower", object_size_m=4.5, config=config)
    assert result.objects_seen_well < result.object_count


def test_camera_default_matches_recorded_1280x960_principal_point() -> None:
    camera = CoverageCamera()
    assert (camera.width_px, camera.height_px) == (1280, 960)
    assert (camera.cx, camera.cy) == (640.0, 480.0)


def test_two_altitude_can_recover_small_objects() -> None:
    config = CoverageConfig(radius_m=25.0, altitude_m=35.0, low_altitude_m=8.0, object_count=32)
    result = simulate_pattern("two_altitude", object_size_m=0.5, config=config)
    assert result.total_time_s > 0
    assert result.candidates_seen > 0
    assert result.slow_verdict_eligible > 0
    assert result.expected_slow_calls == result.slow_verdict_eligible


def test_runtime_threshold_names_are_accepted_as_aliases() -> None:
    config = CoverageConfig(
        radius_m=25.0,
        min_box_side_px=5.0,
        min_verdict_side_px=11.0,
        confirm_min_side_px=21.0,
    )
    assert (config.proposal_floor_px, config.slow_verdict_min_px, config.confirmation_min_px) == (
        5.0,
        11.0,
        21.0,
    )

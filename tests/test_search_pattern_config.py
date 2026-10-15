from __future__ import annotations

import math

import pytest

from multidetect.search_coverage import CoverageCamera, current_expanding_square
from multidetect.sim_teacher.policies import ExpertSearchPolicy

INTRINSICS = {"fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0}


def _policy(pattern: str = "current") -> ExpertSearchPolicy:
    return ExpertSearchPolicy(
        target_ned=(1e7, 1e7, 0.0),
        intrinsics=INTRINSICS,
        image_size=(1280, 960),
        altitude_m=30.0,
        home_ned=(10.0, -4.0, 0.0),
        search_radius_m=55.0,
        search_pattern=pattern,
    )


def test_search_pattern_defaults_to_historical_route() -> None:
    policy = _policy()
    assert policy.search_pattern == "current"
    assert policy._waypoints == policy._build_spiral()


@pytest.mark.parametrize(
    "radius, altitude, pitch",
    [(25.0, 35.0, 0.18), (55.0, 30.0, 0.2), (100.0, 18.0, 0.12)],
)
def test_current_route_helper_matches_runtime_for_multiple_zones(radius, altitude, pitch) -> None:
    policy = ExpertSearchPolicy(
        target_ned=(1e7, 1e7, 0.0), intrinsics=INTRINSICS,
        altitude_m=altitude, camera_pitch_rad=pitch, search_radius_m=radius,
    )
    camera = CoverageCamera(fx=540.0, fy=540.0, cx=640.0, cy=480.0,
                            width_px=1280, height_px=960,
                            pitch_down_deg=math.degrees(pitch))
    assert current_expanding_square(radius, camera, altitude) == policy._waypoints


@pytest.mark.parametrize("pattern", ["lawnmower", "spiral", "two_altitude"])
def test_alternate_zone_routes_are_built_around_zone_centre(pattern: str) -> None:
    policy = _policy(pattern)
    assert policy._waypoints
    assert all(
        ((north - 10.0) ** 2 + (east + 4.0) ** 2) ** 0.5 <= 55.0 + 1e-6
        for north, east in policy._waypoints
    )
    assert policy._waypoints != _policy("current")._waypoints


def test_unknown_search_pattern_is_rejected() -> None:
    with pytest.raises(ValueError, match="search_pattern"):
        _policy("unknown")


def test_two_altitude_descends_only_after_high_route_and_then_uses_low_route() -> None:
    policy = _policy("two_altitude")
    policy.low_altitude_m = 10.0
    policy._low_waypoints = policy._build_coverage_route(10.0)
    # Skip the initial four-camera scan and put the policy at the final high
    # waypoint.  The next command is a bounded vertical descent.
    policy._initial_scan_remaining = 0
    policy._waypoint_index = len(policy._waypoints)
    action = policy.next_action(pose_ned=(10.0, -4.0, -30.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.forward_m == 0
    assert action.right_m == 0 and action.up_m == -8.0
    assert policy._search_phase == "high"

    # Once the host reports the lower altitude, the policy switches to the
    # second route and never emits another descent command.
    action = policy.next_action(pose_ned=(10.0, -4.0, -10.0), yaw_deg=0.0)
    assert policy._search_phase == "low"
    assert action.up_m == 0


def test_two_altitude_rejects_low_height_above_cruise() -> None:
    with pytest.raises(ValueError, match="low_altitude_m"):
        ExpertSearchPolicy(
            target_ned=(1e7, 1e7, 0.0),
            intrinsics=INTRINSICS,
            altitude_m=20.0,
            search_pattern="two_altitude",
            low_altitude_m=21.0,
        )

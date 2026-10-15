from __future__ import annotations

import pytest

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

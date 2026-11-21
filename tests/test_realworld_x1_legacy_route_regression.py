"""Freeze the shipped X1 routes independently of the new-site preflight."""
from __future__ import annotations

import math

import pytest

from scripts.realworld import long_range_flight as flight


LEGACY_NORTH_EAST_M = (
    (850, -850),
    (2450, -2200),
    (2890, -2890),
    (2250, -2710),
    (1510, -1900),
    (740, -900),
    (300, -360),
)


@pytest.mark.parametrize(
    ("site", "relative_home_altitudes_m", "agl_m"),
    [
        (
            "rw_farm_iowa",
            (82.11, 82.42, 84.8812, 88.142, 85.028, 82.498, 82.688),
            (61.696547, 89.881531),
        ),
        (
            "rw_city_zurich",
            (67.97, 64, 62.1424, 63.992, 68.606, 67, 85.526),
            (54.941964, 103.526),
        ),
    ],
)
def test_shipped_x1_route_and_dem_audit_are_unchanged(site, relative_home_altitudes_m, agl_m):
    profile = f"{site}_w6km__clear_noon"
    scenario = flight.SCENARIO_ROOT / flight.DELTA / f"{profile}.scenario.json"
    dem = flight.ROOT / "simulation/terrain" / f"{profile}.dem.json"

    mission = flight.load_mission(scenario, dem_path=dem)

    assert mission["profile"] == profile
    assert mission["airframe"] == flight.DELTA
    assert len(mission["targets"]) == len(LEGACY_NORTH_EAST_M)
    expected_ned = [
        (north, east, -up)
        for (north, east), up in zip(LEGACY_NORTH_EAST_M, relative_home_altitudes_m)
    ]
    for target, expected in zip(mission["targets"], expected_ned):
        assert target == pytest.approx(expected, abs=1e-6)
    assert mission["minimum_max_distance_m"] == 4000
    assert mission["planned_max_distance_m"] == pytest.approx(math.hypot(2890, 2890), abs=1e-6)
    assert mission["planned_agl_envelope_m"] == pytest.approx(agl_m, abs=1e-5)

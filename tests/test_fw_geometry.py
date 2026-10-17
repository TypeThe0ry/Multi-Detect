"""Focused pure-function tests for fixed-wing geometry."""

from __future__ import annotations

import math

import pytest

from multidetect.autonomy.fw_geometry import (
    PassObservation,
    check_curvature,
    confirm_fixed_wing_passes,
    dubins_path,
    dubins_path_lengths,
    estimate_coverage,
    fly_by_passes,
    lawnmower_search,
    max_path_curvature,
    observation_orbit,
    rounded_square_path,
    thinking_loiter,
)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (((0.0, 0.0), 2.003), ((6.34, 19.75), -0.965)),  # LRL
        (((0.0, 0.0), -1.19), ((4.69, 4.06), -1.259)),  # LSL
        (((0.0, 0.0), -2.72), ((-2.62, -17.69), 0.085)),  # RLR
        (((0.0, 0.0), 1.86), ((-6.0, 28.9), 2.10)),  # RSL
        (((0.0, 0.0), -2.15), ((25.3, -25.3), 3.04)),  # LSR
        (((0.0, 0.0), -1.07), ((-26.7, -25.0), -3.07)),  # RSR
    ],
)
def test_dubins_selected_length_matches_analytic(start, end):
    path = dubins_path(start, end, 10.0, sample_step_m=4.0)
    lengths = dubins_path_lengths(start, end, 10.0)
    assert path.word in lengths
    assert path.length_m == pytest.approx(lengths[path.word], rel=1e-9)
    assert path.waypoints[-1].north == pytest.approx(end[0][0], abs=1e-7)
    assert path.waypoints[-1].east == pytest.approx(end[0][1], abs=1e-7)
    assert path.waypoints[-1].heading_rad == pytest.approx(end[1], abs=1e-7)
    assert check_curvature(path, 10.0)


def test_dubins_curvature_never_exceeds_limit_and_degenerate_is_finite():
    path = dubins_path(((0, 0), 0), ((0, 100), math.pi / 2), 12, sample_step_m=20)
    assert max_path_curvature(path) <= 1 / 12 + 1e-6
    zero = dubins_path(((0, 0), 0), ((0, 0), math.pi / 2), 1e-12)
    assert zero.waypoints and all(math.isfinite(p.cumulative_length_m) for p in zero)


def test_lawnmower_coverage_and_narrow_lane_skip_mode():
    wide = lawnmower_search((0, 100, 0, 100), 20, 0.2, 5, sample_step_m=5)
    assert wide.coverage_fraction >= 0.95
    assert wide.mode == "semicircle"
    assert check_curvature(wide, 5)
    narrow = lawnmower_search((0, 50, 0, 10), 4, 0.2, 5)
    assert narrow.mode == "skip-lane"
    assert narrow.lane_spacing_m < 10
    assert estimate_coverage((0, 100, 0, 100), [(50, 20), (50, 40)], 20) == pytest.approx(0.4)


def test_rounded_square_is_nonempty():
    points = rounded_square_path((0, 50, 0, 50), 5)
    assert len(points) > 4
    assert all(math.isfinite(p.north) for p in points)


def test_observation_orbit_respects_requested_and_camera_geometry():
    orbit = observation_orbit((10, 20, 0), 8, 12, math.radians(45), math.radians(30))
    assert orbit.radius_m >= 12
    expected_height = orbit.radius_m * math.tan(orbit.view_angle_rad)
    assert orbit.height_m == pytest.approx(expected_height)
    assert orbit.center_ned[2] == pytest.approx(-orbit.height_m)
    assert len(orbit.waypoints) >= 72


def test_flyby_has_separated_headings_and_visible_windows():
    plan = fly_by_passes((0, 0, 0), ((-100, -100), 0), 3, 10, altitude_m=30, airspeed_mps=15)
    assert len(plan.passes) == 3
    headings = [p.heading_rad for p in plan.passes]
    assert all(
        abs(((headings[i] - headings[j] + math.pi) % (2 * math.pi)) - math.pi)
        >= math.radians(60) - 1e-7
        for i in range(3)
        for j in range(i)
    )
    assert all(p.visible_time_s > 0 for p in plan.passes)


def _obs(heading: float, position=(50.0, 60.0, 0.0), vote=True, sigma=1.0):
    return PassObservation(1.0, (0, 0, -30), (1, 0, 1), vote, heading, position, sigma)


def test_confirmation_branches():
    assert confirm_fixed_wing_passes([_obs(0), _obs(0.5)]).status == "need_more_passes"
    assert confirm_fixed_wing_passes([_obs(0), _obs(1.1), _obs(2.2)]).status == "confirm"
    assert confirm_fixed_wing_passes([_obs(0), _obs(1.1), _obs(2.2, vote=False)]).status == "reject"
    assert confirm_fixed_wing_passes([_obs(0), _obs(1.1), _obs(2.2, sigma=9)]).status == "reject"
    assert (
        confirm_fixed_wing_passes([_obs(0), _obs(1.1), _obs(2.2, position=(100, 100, 0))]).status
        == "reject"
    )


def test_thinking_loiter_chooses_clear_side_and_falls_back_when_blocked():
    def clear(start, end):
        return end[1] <= 0

    plan = thinking_loiter(((0, 0, -30), 0), 15, 20, 10, clear, points=24)
    assert plan.clear and plan.side == "right"
    blocked = thinking_loiter(((0, 0, -30), 0), 15, 20, 10, lambda _a, _b: False)
    assert not blocked.clear and blocked.side == "none"

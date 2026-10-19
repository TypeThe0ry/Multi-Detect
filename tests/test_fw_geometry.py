"""Focused pure-function tests for fixed-wing geometry."""

from __future__ import annotations

import math

import pytest

from multidetect.autonomy.fw_geometry import (
    PassObservation,
    check_curvature,
    circle_points,
    confirm_fixed_wing_passes,
    dubins_path,
    dubins_path_lengths,
    estimate_coverage,
    fly_by_passes,
    forward_camera_pass_orbit,
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


# rc_cessna mono camera: 640x480, fx = fy = 269.97, level (pitch 0) forward mount.
_FX, _CX, _CY = 269.968, 320.0, 240.0
_HALF_H, _HALF_V = math.atan2(_CX, _FX), math.atan2(_CY, _FX)


def _level_camera_sees(aircraft, heading, target, altitude):
    dn, de = target[0] - aircraft[0], target[1] - aircraft[1]
    forward = dn * math.cos(heading) + de * math.sin(heading)
    right = -dn * math.sin(heading) + de * math.cos(heading)
    if forward <= 0:
        return False
    return abs(math.atan2(right, forward)) < _HALF_H and math.atan2(altitude, forward) < _HALF_V


def _rc_cessna_pass(target=(0.0, 0.0), aircraft=(-150.0, 0.0)):
    return forward_camera_pass_orbit(
        target,
        aircraft,
        altitude_m=30.0,
        half_hfov_rad=_HALF_H,
        half_vfov_rad=_HALF_V,
        camera_pitch_down_rad=0.0,
        min_turn_radius_m=21.18,
        view_range_m=120.0,
    )


def test_pass_orbit_puts_the_target_on_the_rim_right_of_the_line_of_sight():
    orbit = _rc_cessna_pass(target=(10.0, 5.0), aircraft=(-140.0, 5.0))
    # rc_cessna at 30 m: R = 120 / (2 sin(49.85 - 8 deg)) ~ 90 m, bank ~14 deg.
    assert orbit.radius_m == pytest.approx(89.9, abs=0.5)
    assert orbit.far_range_m == pytest.approx(120.0)
    assert orbit.near_range_m == pytest.approx(45.1, abs=0.3)
    assert math.dist(orbit.centre_ne, orbit.target_ne) == pytest.approx(orbit.radius_m)
    # Flying north at the target, the centre is due east of it (to the right):
    # a clockwise orbit crosses the target on the current heading.
    assert orbit.centre_ne[0] == pytest.approx(10.0)
    assert orbit.centre_ne[1] == pytest.approx(5.0 + orbit.radius_m)
    assert orbit.in_view_arc_m == pytest.approx(85.8, abs=1.0)


def test_pass_orbit_shows_a_forward_camera_the_target_once_per_lap_a_centred_orbit_never():
    orbit = _rc_cessna_pass()
    cn, ce = orbit.centre_ne
    count, seen = 720, []
    for index in range(count):
        azimuth = 2 * math.pi * index / count
        point = (cn + orbit.radius_m * math.cos(azimuth), ce + orbit.radius_m * math.sin(azimuth))
        if _level_camera_sees(point, azimuth + math.pi / 2, orbit.target_ne, 30.0):
            seen.append(index)
    # One contiguous inbound arc per lap, at least as long as the planned window
    # (the plan keeps an 8 deg FOV margin).
    assert seen and seen == list(range(seen[0], seen[-1] + 1))
    arc = 2 * math.pi * orbit.radius_m * len(seen) / count
    assert orbit.in_view_arc_m <= arc <= 1.6 * orbit.in_view_arc_m
    # The same radius centred on the target holds it 90 deg off the nose.
    assert not any(
        _level_camera_sees(
            (orbit.radius_m * math.cos(a), orbit.radius_m * math.sin(a)),
            a + math.pi / 2,
            (0.0, 0.0),
            30.0,
        )
        for a in (2 * math.pi * i / count for i in range(count))
    )


def test_pass_orbit_respects_the_turn_radius_and_an_explicit_radius():
    wide = forward_camera_pass_orbit(
        (0.0, 0.0),
        (-100.0, 0.0),
        altitude_m=30.0,
        half_hfov_rad=_HALF_H,
        half_vfov_rad=_HALF_V,
        camera_pitch_down_rad=0.0,
        min_turn_radius_m=150.0,
        view_range_m=120.0,
    )
    assert wide.radius_m == pytest.approx(150.0)
    small = forward_camera_pass_orbit(
        (0.0, 0.0),
        (-100.0, 0.0),
        altitude_m=30.0,
        half_hfov_rad=_HALF_H,
        half_vfov_rad=_HALF_V,
        camera_pitch_down_rad=0.0,
        min_turn_radius_m=21.0,
        view_range_m=120.0,
        radius_m=10.0,
    )
    assert small.radius_m == pytest.approx(21.0)
    rim = circle_points((0.0, 0.0), 10.0, 8)
    assert len(rim) == 9 and math.dist(rim[0], rim[-1]) < 1e-9

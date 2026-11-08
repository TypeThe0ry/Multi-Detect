from __future__ import annotations

import math
from pathlib import Path

import pytest

from multidetect.autonomy.fw_search import FixedWingSearch, camera_footprint_width_m
from multidetect.autonomy.motion_intent import AirframeMotionAdapter, dynamics_from_profile
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig
from multidetect.camera_mount import CameraMount, compose_camera_pose
from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned

ROOT = Path(__file__).resolve().parents[1]


def _fly(search: FixedWingSearch, steps: int, yaw_deg: float = 90.0):
    pose = [0.0, 0.0, -30.0]
    visited = []
    for _ in range(steps):
        action = search.next_action(pose_ned=pose, yaw_deg=yaw_deg)
        assert action.kind == "translate"  # never an in-place hold or turn
        north, east, _ = body_to_ned(action.forward_m, action.right_m, 0.0, yaw_deg)
        assert math.hypot(north, east) <= search.max_leg_m
        pose = [pose[0] + north, pose[1] + east, pose[2]]
        visited.append((pose[0], pose[1]))
    return visited


def test_fixed_wing_search_sweeps_the_region_in_full_legs() -> None:
    search = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0),
        radius_m=150.0,
        footprint_width_m=80.0,
        turn_radius_m=21.0,
        max_leg_m=30.0,
    )
    visited = _fly(search, 120)
    norths = [p[0] for p in visited]
    easts = [p[1] for p in visited]
    # Lanes span the whole square, not a slow drift near the start.
    assert max(norths) - min(norths) > 250.0 and max(easts) - min(easts) > 150.0
    assert all(abs(n) <= 150.0 + 21.0 * 2 and abs(e) <= 150.0 + 21.0 * 2 for n, e in visited)


def test_camera_footprint_grows_with_altitude() -> None:
    intrinsics = {"fx": 640.0, "cx": 320.0}
    assert camera_footprint_width_m(intrinsics, 60.0, 0.35) > camera_footprint_width_m(
        intrinsics, 30.0, 0.35
    )


def test_fixed_wing_turn_heads_off_on_the_new_bearing_and_keeps_the_primitive() -> None:
    adapter = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT)
    turn = ActionPrimitive.turn(90.0)
    command = adapter.from_primitive(turn, pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
    assert command.kind == "reposition" and command.action == turn
    # Facing north, a +90 deg turn flies east, one leg away (not "reposition here").
    assert command.target_ned[1] > 20.0 and abs(command.target_ned[0]) < 1e-6
    hold = adapter.from_primitive(
        ActionPrimitive.hold(2.0), pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0
    )
    assert hold.kind == "loiter" and hold.action.kind == "hold"


def test_fixed_wing_search_legs_pass_the_adapter_envelope() -> None:
    # The 15 m multirotor step bound clipped every fixed-wing leg into a loiter.
    adapter = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT)
    search = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0),
        radius_m=150.0,
        footprint_width_m=80.0,
        turn_radius_m=21.0,
        max_leg_m=adapter.profile.limits.max_step_distance_m,
    )
    pose = (0.0, 0.0, -30.0)
    leg = search.next_action(pose_ned=pose, yaw_deg=0.0)
    command = adapter.from_primitive(leg, pose_ned=pose, yaw_deg=0.0)
    command, safety = adapter.guard_command(command, pose_ned=pose, yaw_deg=0.0)
    assert command.kind == "reposition", safety


def test_fixed_wing_steps_hold_the_commanded_altitude_while_the_plane_sinks() -> None:
    # SITL: commanding the current altitude every step let banked-turn losses
    # accumulate (30 m -> 18 m).  The bridge keeps the commanded altitude.
    adapter = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT)
    first = adapter.from_primitive(
        ActionPrimitive.translate(25.0, 0.0, 0.0), pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0
    )
    assert first.target_ned[2] == pytest.approx(-30.0)
    sunk = adapter.from_primitive(
        ActionPrimitive.translate(25.0, 0.0, 0.0), pose_ned=(25.0, 0.0, -27.5), yaw_deg=0.0
    )
    assert sunk.target_ned[2] == pytest.approx(-30.0)
    loiter = adapter.from_primitive(
        ActionPrimitive.hold(2.0), pose_ned=(50.0, 0.0, -26.0), yaw_deg=0.0
    )
    assert loiter.target_ned[2] == pytest.approx(-30.0)
    # A requested descent is measured from the altitude the loop sees.
    descend = adapter.from_primitive(
        ActionPrimitive.translate(20.0, 0.0, -4.0), pose_ned=(50.0, 0.0, -26.0), yaw_deg=0.0
    )
    assert descend.target_ned[2] == pytest.approx(-22.0)
    after = adapter.from_primitive(
        ActionPrimitive.translate(20.0, 0.0, 0.0), pose_ned=(70.0, 0.0, -24.0), yaw_deg=0.0
    )
    assert after.target_ned[2] == pytest.approx(-22.0)


def _orbit_hold(adapter: AirframeMotionAdapter, pose):
    """The loop's fly-by pass step: a hold carrying the orbit plan."""
    return adapter.from_primitive(
        ActionPrimitive.hold(1.0),
        pose_ned=pose,
        yaw_deg=0.0,
        metadata={"onboard": {"fw_orbit": {"centre_ne": [200.0, 0.0], "radius_m": 90.0}}},
    )


def test_fixed_wing_repeated_climb_requests_do_not_wind_up_the_commanded_altitude() -> None:
    # SITL rc_cessna w0031: the plane at 25.6 m asked for +4.4 m (back to 30 m)
    # on 8 steps while it climbed slowly; adding each request to the held
    # altitude commanded 54 m, then the descents back ran it below ground.
    adapter = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT)
    adapter.from_primitive(
        ActionPrimitive.translate(25.0, 0.0, 0.0), pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0
    )
    targets = []
    for step, altitude in enumerate((25.6, 25.6, 25.7, 26.3, 26.9, 27.8, 28.6)):
        command = adapter.from_primitive(
            ActionPrimitive.translate(0.0, 0.0, 30.0 - altitude),
            pose_ned=(float(step), 0.0, -altitude),
            yaw_deg=0.0,
        )
        targets.append(-command.target_ned[2])
    assert targets == pytest.approx([30.0] * len(targets))
    orbit = _orbit_hold(adapter, (10.0, 0.0, -29.0))
    assert orbit.kind == "orbit" and orbit.target_ned[2] == pytest.approx(-30.0)


def test_fixed_wing_refused_descents_never_become_the_orbit_altitude() -> None:
    # SITL rc_cessna w0031: every refused descent still moved the held altitude,
    # which walked to 153 m below home; the next fly-by orbit flew into the ground.
    profile = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT).profile
    safety = SafetyEnvelope(
        SafetyEnvelopeConfig(
            vehicle=dynamics_from_profile(profile),
            min_altitude_m=20.0,
            max_translation_m=profile.limits.max_step_distance_m,
        )
    )
    adapter = AirframeMotionAdapter(profile, safety=safety)
    pose = (0.0, 0.0, -24.0)
    adapter.from_primitive(ActionPrimitive.translate(25.0, 0.0, 0.0), pose_ned=pose, yaw_deg=0.0)
    for _ in range(6):
        command = adapter.from_primitive(
            ActionPrimitive.translate(0.0, 0.0, -8.0), pose_ned=pose, yaw_deg=0.0
        )
        command, safety_info = adapter.guard_command(command, pose_ned=pose, yaw_deg=0.0)
        assert command.kind == "loiter" and "min_altitude" in safety_info["reasons"]
    orbit = _orbit_hold(adapter, pose)
    assert orbit.kind == "orbit" and orbit.target_ned[2] == pytest.approx(-24.0)
    guarded, _ = adapter.guard_command(orbit, pose_ned=pose, yaw_deg=0.0)
    assert guarded.kind == "orbit"


# SITL rc_cessna w0016 (e4ecaaf): the search-leg poses (north, east, yaw) of
# steps 40-63.  Every reposition 28 m ahead became an autopilot loiter, and the
# plane circled the first lane start (-150, -75) at 64-92 m, abeam, for ~100 steps.
_W0016_RING = (
    (-140.8, -11.6, 3.7),
    (-222.0, -17.0, -105.8),
    (-227.6, -107.0, -45.7),
    (-161.0, -159.3, 6.5),
    (-78.3, -117.4, 85.4),
    (-76.6, -43.2, 113.8),
    (-145.5, 7.6, -170.2),
    (-215.6, -26.3, -121.9),
    (-225.9, -107.9, -56.2),
    (-160.8, -155.4, -0.1),
    (-85.3, -112.4, 72.8),
    (-90.9, -32.8, 127.3),
)


def _w0016_search() -> FixedWingSearch:
    # The run's search: home (0, 0), 150 m radius, rc_cessna turn radius and leg.
    return FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0),
        radius_m=150.0,
        footprint_width_m=camera_footprint_width_m({"fx": 539.94, "cx": 640.0}, 30.0, 0.18),
        turn_radius_m=21.2,
        max_leg_m=30.0,
    )


def test_fixed_wing_search_passes_a_waypoint_the_plane_keeps_circling() -> None:
    search = _w0016_search()
    first = search.waypoints[0]
    assert first == pytest.approx((-150.0, -75.0), abs=1.0)
    indices = []
    for north, east, yaw in _W0016_RING:
        search.next_action(pose_ned=(north, east, -30.0), yaw_deg=yaw)
        indices.append(search._index)
    # Within a lap the circled lane start counts as passed and the lane is flown.
    assert indices[-1] >= 1 and indices.index(1) <= 4, indices
    leg = search.next_action(pose_ned=(-150.0, -75.0, -30.0), yaw_deg=0.0)
    north, east, _ = body_to_ned(leg.forward_m, leg.right_m, 0.0, 0.0)
    assert north > 20.0 and abs(east) < 1.0  # up the first lane, towards (150, -75)


def test_fixed_wing_search_does_not_skip_a_far_or_closing_waypoint() -> None:
    search = _w0016_search()
    # Far away and abeam (e.g. back from an approach elsewhere): never "passed".
    for _ in range(10):
        search.next_action(pose_ned=(100.0, -75.0, -30.0), yaw_deg=90.0)
    assert search._index == 0
    # Inside the capture bound but closing on it every leg: still approaching.
    for north in (-60.0, -80.0, -100.0, -120.0):
        search.next_action(pose_ned=(north, -75.0, -30.0), yaw_deg=180.0)
    assert search._index == 0


def test_fixed_wing_search_spacing_respects_turn_diameter() -> None:
    search = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0),
        radius_m=55.0,
        footprint_width_m=30.0,
        turn_radius_m=21.2,
        max_leg_m=30.0,
    )
    # A 27 m sampling stride would put successive fixed-wing targets inside
    # the 42.4 m turn diameter and make every short reposition a loiter.
    assert search.waypoint_spacing_m == pytest.approx(42.4)


def test_fixed_wing_search_advances_a_stalled_capture() -> None:
    search = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0),
        radius_m=55.0,
        footprint_width_m=30.0,
        turn_radius_m=21.2,
        max_leg_m=30.0,
        stalled_calls=3,
    )
    target = search.waypoints[0]
    for _ in range(4):
        search.next_action(
            pose_ned=(target[0] + 50.0, target[1], -30.0),
            yaw_deg=0.0,
        )
    assert search._index == 1


def test_fixed_wing_coverage_search_tracks_cells_and_reserves_return_budget() -> None:
    search = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0), radius_m=55.0,
        footprint_width_m=100.0, turn_radius_m=21.2, max_leg_m=30.0,
        camera_pitch_rad=math.radians(45), coverage_enabled=True,
        max_search_calls=3, return_reserve_calls=40, coverage_threshold=0.99,
        min_coverage_calls=2,
    )
    for _ in range(2):
        action = search.next_action(pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
        assert action.kind == "translate"
    final = search.next_action(pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
    assert final.kind == "stop"
    assert search.search_outcome == "budget_reserve"


def test_fixed_wing_gimbal_coverage_requires_a_fresh_composed_pose() -> None:
    search = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0), radius_m=55.0,
        footprint_width_m=100.0, turn_radius_m=21.2, max_leg_m=30.0,
        coverage_enabled=True, camera_is_gimballed=True,
        camera_intrinsics={"fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
        max_search_calls=10, return_reserve_calls=2,
    )
    search._record_camera_coverage((0.0, 0.0, -30.0), 0.0)
    assert search.coverage_fraction == 0.0
    composed = compose_camera_pose(
        (0.0, 0.0, -30.0), vehicle_roll_rad=0.0, vehicle_pitch_rad=0.0,
        vehicle_yaw_rad=0.0, mount=CameraMount.fixed(pitch_rad=math.radians(45.0)),
    )
    search._record_camera_coverage(
        (0.0, 0.0, -30.0), 0.0,
        {"center_ned": composed.center_ned, "optical_to_world": composed.optical_to_world,
         "gimbal_feedback_fresh": False},
    )
    assert search.coverage_fraction == 0.0
    search._record_camera_coverage(
        (0.0, 0.0, -30.0), 0.0,
        {"center_ned": composed.center_ned, "optical_to_world": composed.optical_to_world,
         "gimbal_feedback_fresh": True},
    )
    assert 0.0 < search.coverage_fraction < 0.99

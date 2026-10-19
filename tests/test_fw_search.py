from __future__ import annotations

import math
from pathlib import Path

import pytest

from multidetect.autonomy.fw_search import FixedWingSearch, camera_footprint_width_m
from multidetect.autonomy.motion_intent import AirframeMotionAdapter
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
    descend = adapter.from_primitive(
        ActionPrimitive.translate(20.0, 0.0, -4.0), pose_ned=(50.0, 0.0, -26.0), yaw_deg=0.0
    )
    assert descend.target_ned[2] == pytest.approx(-26.0)
    after = adapter.from_primitive(
        ActionPrimitive.translate(20.0, 0.0, 0.0), pose_ned=(70.0, 0.0, -24.0), yaw_deg=0.0
    )
    assert after.target_ned[2] == pytest.approx(-26.0)

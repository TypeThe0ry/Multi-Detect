import math
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.mission_plan import adapt_skill_for_airframe, validate_mission_plan
from multidetect.autonomy.motion_intent import (
    AirframeMotionAdapter,
    MotionCommand,
    MotionIntent,
    camera_ray_from_profile,
)
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig
from multidetect.sim_teacher.primitives import ActionPrimitive


ROOT = Path(__file__).resolve().parents[1]


def test_x500_legacy_primitive_is_byte_identical():
    adapter = AirframeMotionAdapter("x500_mono_cam", base_dir=ROOT)
    action = ActionPrimitive.translate(10, 0, 0)
    assert adapter.from_primitive(action, pose_ned=(0, 0, -20), yaw_deg=0) == action


@pytest.mark.parametrize("name", ["advanced_plane", "standard_vtol", "quadtailsitter", "tiltrotor"])
def test_non_multirotor_wait_is_loiter_with_profile_radius(name):
    adapter = AirframeMotionAdapter(name, base_dir=ROOT)
    command = adapter.from_primitive(ActionPrimitive.hold(2), pose_ned=(0, 0, -30), yaw_deg=0)
    assert isinstance(command, MotionCommand)
    assert command.kind == "loiter"
    assert command.radius_m == adapter.profile.orbit_radius()
    assert command.speed_mps == adapter.profile.cruise_airspeed_mps


def test_fixed_wing_translate_is_reposition_when_profile_speed_is_valid():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    command = adapter.from_primitive(
        ActionPrimitive.translate(20, 0), pose_ned=(0, 0, -30), yaw_deg=0
    )
    assert command.kind == "reposition"
    assert command.target_ned == (20.0, 0.0, -30.0)


def test_vtol_mc_mode_uses_mc_commands():
    adapter = AirframeMotionAdapter("standard_vtol", base_dir=ROOT)
    adapter.set_mode(3)
    command = adapter.from_primitive(ActionPrimitive.translate(5, 0), pose_ned=(0, 0, -20), yaw_deg=0)
    assert command.kind == "mc_reposition"
    assert command.mode == "MC"
    adapter.set_mode(4)
    command = adapter.from_primitive(ActionPrimitive.hold(1), pose_ned=(0, 0, -20), yaw_deg=0)
    assert command.kind == "loiter"


def test_vtol_mc_recovery_keeps_mc_hold():
    adapter = AirframeMotionAdapter("standard_vtol", base_dir=ROOT, mode="MC")
    adapter.safety = SafetyEnvelope(
        SafetyEnvelopeConfig(
            operating_polygon=((-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)),
            vehicle=adapter.safety.vehicle,
        )
    )
    command, safety = adapter.guard_command(
        MotionCommand("mc_reposition", target_ned=(50.0, 0.0, -20.0), mode="MC"),
        pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0,
    )
    assert command.kind == "mc_hold"
    assert command.mode == "MC"
    assert safety["rejected"] is True


def test_fixed_wing_deliver_fails_closed():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    command = adapter.from_primitive(
        ActionPrimitive.hold(1), pose_ned=(0, 0, -30), yaw_deg=0,
        metadata={"onboard": {"state": "deliver"}},
    )
    assert command.rejected
    guarded, safety = adapter.guard_command(command, pose_ned=(0, 0, -30), yaw_deg=0)
    assert guarded.rejected
    assert safety["rejected"] is True


def test_fixed_wing_turn_carries_profile_turn_radius():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    command = adapter.from_primitive(
        ActionPrimitive.turn(45), pose_ned=(0, 0, -30), yaw_deg=0
    )
    assert command.kind == "reposition"
    assert command.radius_m == adapter.profile.min_turn_radius_m


def test_fixed_wing_geometry_and_energy_use_profile():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    route = adapter.search_path((-50, 50, -30, 30), footprint_width_m=20, overlap=0.4)
    assert route.waypoints
    assert route.mode in {"semicircle", "skip-lane"}
    orbit = adapter.observation_orbit((0, 0, 0), 2, altitude_m=30, views=8)
    assert orbit.radius_m == adapter.profile.orbit_radius(2)
    intent = MotionIntent.goto((100, 0, -30))
    assert adapter.intent_to_command(intent, pose_ned=(0, 0, -30)).kind == "reposition"
    assert adapter.estimated_duration_s(intent, (0, 0, -30)) == pytest.approx(100 / 15)
    assert adapter.estimated_energy_wh(intent, (0, 0, -30)) > 0


def test_plan_prompt_contract_is_unchanged_and_host_adapts_skill():
    plan = validate_mission_plan({
        "plan_version": 1, "goal": "hover", "report": [],
        "steps": [
            {"id": "h", "skill": "hover", "params": {"duration_s": 2}, "on_success": "r", "on_fail": "r"},
            {"id": "r", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
    })
    assert adapt_skill_for_airframe(plan.steps[0], load_airframe_profile(ROOT / "configs/airframes/advanced_plane.json"))[0] == "loiter"


def test_tailsitter_camera_mc_fw_rotations_differ():
    profile = load_airframe_profile(ROOT / "configs/airframes/quadtailsitter.json")
    mc = camera_ray_from_profile(320, 240, fx=400, fy=400, cx=320, cy=240, attitude_deg=(0, 0, 0), profile=profile, mode="MC")
    fw = camera_ray_from_profile(320, 240, fx=400, fy=400, cx=320, cy=240, attitude_deg=(0, 0, 0), profile=profile, mode="FW")
    assert mc != fw
    assert math.isclose(math.sqrt(sum(v * v for v in mc)), 1.0, rel_tol=1e-9)
    assert math.isclose(math.sqrt(sum(v * v for v in fw)), 1.0, rel_tol=1e-9)


def test_unknown_profile_fails_closed():
    with pytest.raises(ValueError, match="unknown airframe profile"):
        AirframeMotionAdapter("does-not-exist", base_dir=ROOT)


def test_fixed_wing_command_safety_rejection_recovers_to_loiter():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    adapter.safety = SafetyEnvelope(
        SafetyEnvelopeConfig(
            operating_polygon=((-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)),
            vehicle=adapter.safety.vehicle,
        ),
        home_ned=(0.0, 0.0, -30.0),
    )
    command, safety = adapter.guard_command(
        MotionCommand(
            "reposition", target_ned=(50.0, 0.0, -30.0),
            speed_mps=adapter.profile.cruise_airspeed_mps, mode="FW",
        ),
        pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0,
    )
    assert command.kind == "loiter"
    assert safety["rejected"] is True
    assert "geofence" in safety["reasons"]

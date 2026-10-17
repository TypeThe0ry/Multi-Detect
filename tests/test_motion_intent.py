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
    command = adapter.from_primitive(
        ActionPrimitive.translate(5, 0), pose_ned=(0, 0, -20), yaw_deg=0
    )
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
        pose_ned=(0.0, 0.0, -20.0),
        yaw_deg=0.0,
    )
    assert command.kind == "mc_hold"
    assert command.mode == "MC"
    assert safety["rejected"] is True


def test_fixed_wing_deliver_fails_closed():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    command = adapter.from_primitive(
        ActionPrimitive.hold(1),
        pose_ned=(0, 0, -30),
        yaw_deg=0,
        metadata={"onboard": {"state": "deliver"}},
    )
    assert command.rejected
    guarded, safety = adapter.guard_command(command, pose_ned=(0, 0, -30), yaw_deg=0)
    assert guarded.rejected
    assert safety["rejected"] is True


def test_fixed_wing_turn_carries_profile_turn_radius():
    adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    command = adapter.from_primitive(ActionPrimitive.turn(45), pose_ned=(0, 0, -30), yaw_deg=0)
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
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "hover",
            "report": [],
            "steps": [
                {
                    "id": "h",
                    "skill": "hover",
                    "params": {"duration_s": 2},
                    "on_success": "r",
                    "on_fail": "r",
                },
                {
                    "id": "r",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
        }
    )
    assert (
        adapt_skill_for_airframe(
            plan.steps[0], load_airframe_profile(ROOT / "configs/airframes/advanced_plane.json")
        )[0]
        == "loiter"
    )


def test_tailsitter_camera_looks_up_in_hover_and_down_ahead_in_forward_flight():
    # PX4 reports tailsitter attitude in the multicopter frame; forward flight is a
    # pitch towards -90 deg, so one fixed mount covers both phases (checked against
    # rendered frames and real SITL attitude).
    profile = load_airframe_profile(ROOT / "configs/airframes/quadtailsitter.json")
    hover = camera_ray_from_profile(
        320, 240, fx=400, fy=400, cx=320, cy=240, attitude_deg=(0, 0, 0), profile=profile
    )
    assert hover[0] > 0.9 and hover[2] == pytest.approx(-math.sin(math.radians(20)))
    cruise = camera_ray_from_profile(
        320, 240, fx=400, fy=400, cx=320, cy=240, attitude_deg=(0, -80, 0), profile=profile
    )
    assert cruise[2] > 0.8 and cruise[0] > 0.3  # mostly down, somewhat ahead
    assert math.isclose(math.sqrt(sum(v * v for v in cruise)), 1.0, rel_tol=1e-9)


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
            "reposition",
            target_ned=(50.0, 0.0, -30.0),
            speed_mps=adapter.profile.cruise_airspeed_mps,
            mode="FW",
        ),
        pose_ned=(0.0, 0.0, -30.0),
        yaw_deg=0.0,
    )
    assert command.kind == "loiter"
    assert safety["rejected"] is True
    assert "geofence" in safety["reasons"]


class _GuidedVehicle:
    def __init__(self):
        self.calls = []

    def orbit(self, center, radius, speed, **kwargs):
        self.calls.append(("orbit", center, radius, speed, kwargs))
        return {"status": "accepted"}

    def reposition(self, point, alt, speed, **kwargs):
        self.calls.append(("reposition", point, alt, speed, kwargs))
        return {"status": "accepted"}


def _runtime_for(vehicle):
    from multidetect.onboard_runtime.runtime import OnboardRuntime

    runtime = OnboardRuntime.__new__(OnboardRuntime)
    runtime.vehicle = vehicle
    runtime.pump = None
    runtime.motion_adapter = AirframeMotionAdapter("advanced_plane", base_dir=ROOT)
    return runtime


def test_runtime_sends_guided_commands_as_wgs84_with_amsl_altitude():
    from multidetect.onboard_runtime.vehicle import VehicleState

    vehicle = _GuidedVehicle()
    runtime = _runtime_for(vehicle)
    state = VehicleState(
        position_ned=(0.0, 0.0, -40.0), home_ned=(0.0, 0.0, 0.0), home_geodetic=(31.0, 121.0, 12.0)
    )
    runtime._dispatch_motion_command(
        MotionCommand("reposition", target_ned=(100.0, 0.0, -40.0), speed_mps=15.0), state
    )
    _, point, alt, _, _ = vehicle.calls[-1]
    assert point[0] == pytest.approx(31.0 + math.degrees(100.0 / 6378137.0))
    assert point[1] == pytest.approx(121.0)
    assert alt == pytest.approx(52.0)  # 40 m above a 12 m AMSL home


def test_runtime_refuses_guided_commands_without_geodetic_home():
    from multidetect.onboard_runtime.vehicle import VehicleState

    vehicle = _GuidedVehicle()
    runtime = _runtime_for(vehicle)
    with pytest.raises(RuntimeError, match="geodetic home"):
        runtime._dispatch_motion_command(
            MotionCommand("loiter", target_ned=(0.0, 0.0, -40.0), radius_m=80.0),
            VehicleState(position_ned=(0.0, 0.0, -40.0), home_ned=(0.0, 0.0, 0.0)),
        )
    assert vehicle.calls == []


def test_fixed_wing_guard_accepts_the_mode_names_vehicle_state_reports():
    from multidetect.onboard_runtime.runtime import FIXED_WING_GUIDED_MODES
    from multidetect.onboard_runtime.vehicle import _mode_name

    def custom(main, sub):
        return (main << 16) | (sub << 24)

    # reposition -> AUTO.LOITER, orbit -> POSCTL.ORBIT, mission landing, RTL.
    for main, sub in ((4, 3), (3, 1), (4, 4), (4, 5), (4, 6), (6, 0)):
        assert _mode_name(custom(main, sub)) in FIXED_WING_GUIDED_MODES
    assert _mode_name(custom(3, 0)) not in FIXED_WING_GUIDED_MODES  # manual POSCTL

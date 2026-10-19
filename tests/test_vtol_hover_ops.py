"""A hover-capable VTOL flies the multirotor loop in MC and hands its return to PX4."""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest
from onboard_factories import make_policy
from PIL import Image

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.fw_search import FixedWingSearch
from multidetect.autonomy.motion_intent import (
    AirframeMotionAdapter,
    MotionCommand,
    MotionIntentError,
    profile_mount_pitch_down_rad,
)
from multidetect.sim_teacher.fw_executor import FixedWingVtolExecutor
from multidetect.sim_teacher.policies import ExpertSearchPolicy
from multidetect.sim_teacher.primitives import ActionPrimitive

ROOT = Path(__file__).resolve().parents[1]


def _frame(vtol_state: int) -> dict:
    payload = io.BytesIO()
    Image.new("RGB", (2, 2)).save(payload, format="PNG")
    return {
        "rgb": payload.getvalue(),
        "camera_info": {},
        "flight_state": {"vtol_state": vtol_state, "landed_state": 2},
    }


@pytest.mark.parametrize("airframe", ["standard_vtol", "quadtailsitter"])
def test_vtol_policy_plans_like_a_multirotor_and_returns_with_the_autopilot(
    tmp_path: Path, airframe: str
) -> None:
    policy = make_policy(work_dir=tmp_path / airframe, config={"airframe": airframe})
    assert policy.motion_adapter.hover_ops
    # Multirotor search and approach (turn/step, closest view), no fly-by passes.
    assert isinstance(policy._search, ExpertSearchPolicy)
    assert not policy.loop.config.fixed_wing_pass
    assert policy.loop.config.autopilot_return
    # The loop's level-camera model is the MC mount (pitch from the profile).
    profile = policy.airframe_profile
    matrix = profile.camera.attitude_rotations.get("mc", profile.camera.body_to_camera_rotation)
    assert policy.camera_pitch_down_rad == pytest.approx(math.atan2(matrix[6], matrix[0]))
    assert policy.loop.camera.pitch_down_rad == pytest.approx(policy.camera_pitch_down_rad)


def test_vtol_hover_ops_can_be_disabled_back_to_fixed_wing_search(tmp_path: Path) -> None:
    policy = make_policy(
        work_dir=tmp_path, config={"airframe": "standard_vtol", "vtol_hover_ops": False}
    )
    assert not policy.motion_adapter.hover_ops
    assert isinstance(policy._search, FixedWingSearch)
    with pytest.raises(ValueError, match="vtol_hover_ops"):
        make_policy(
            work_dir=tmp_path / "bad",
            config={"airframe": "standard_vtol", "vtol_hover_ops": "maybe"},
        )


def test_multirotor_and_fixed_wing_are_not_hover_ops(tmp_path: Path) -> None:
    assert not make_policy(work_dir=tmp_path / "x500").motion_adapter.hover_ops
    cessna = make_policy(work_dir=tmp_path / "fw", config={"airframe": "rc_cessna_runway"})
    assert not cessna.motion_adapter.hover_ops and cessna.loop.config.fixed_wing_pass
    with pytest.raises(MotionIntentError):
        AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT, preferred_mode="MC")


def test_vtol_in_forward_flight_first_transitions_to_mc_without_stepping_the_loop(
    tmp_path: Path,
) -> None:
    policy = make_policy(work_dir=tmp_path, config={"airframe": "standard_vtol"})
    command, labels = policy.act(_frame(4), pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
    assert isinstance(command, MotionCommand)
    assert (command.kind, command.mode) == ("transition", "MC")
    assert labels["onboard"]["state"] == "transition"
    assert labels["motion"]["kind"] == "transition"
    assert policy.loop.step_index == 0


def test_vtol_in_mc_flies_the_loop_with_hover_commands(tmp_path: Path) -> None:
    policy = make_policy(work_dir=tmp_path, config={"airframe": "standard_vtol"})
    # At the mission altitude (20 m in the factory) the search starts at once.
    command, labels = policy.act(_frame(3), pose_ned=(0.0, 0.0, -20.0), yaw_deg=10.0)
    assert policy.loop.step_index == 1
    # The multirotor search opens with an in-place scan turn: a hover yaw, not a
    # 22 m fixed-wing leg on the new bearing.
    assert command.kind == "mc_reposition" and command.mode == "MC"
    assert command.target_ned == pytest.approx((0.0, 0.0, -20.0))
    assert command.yaw_deg == pytest.approx(100.0)
    assert command.speed_mps == AirframeMotionAdapter.MC_SPEED_MPS
    assert labels["motion"]["kind"] == "mc_reposition"


def test_confirmed_stop_hands_the_return_to_the_autopilot_not_a_landing_at_the_target(
    tmp_path: Path, monkeypatch
) -> None:
    policy = make_policy(work_dir=tmp_path, config={"airframe": "quadtailsitter"})
    monkeypatch.setattr(
        policy,
        "_act_impl",
        lambda observation, **kwargs: (
            ActionPrimitive.stop(),
            {"onboard": {"state": "done", "return": "autopilot"}},
        ),
    )
    command, _ = policy.act(_frame(3), pose_ned=(40.0, 10.0, -30.0), yaw_deg=0.0)
    assert command.kind == "stop"
    # A supervisor's land-now still lands where it is.
    monkeypatch.setattr(
        policy,
        "_act_impl",
        lambda observation, **kwargs: (
            ActionPrimitive.stop(),
            {"onboard": {"state": "supervised", "supervisor_action": "land_now"}},
        ),
    )
    command, _ = policy.act(_frame(3), pose_ned=(40.0, 10.0, -30.0), yaw_deg=0.0)
    assert command.kind == "mc_land"


def test_mc_adapter_holds_heading_and_steps_at_multirotor_speed() -> None:
    adapter = AirframeMotionAdapter("standard_vtol", base_dir=ROOT, mode="MC")
    hold = adapter.from_primitive(ActionPrimitive.hold(2.0), pose_ned=(1, 2, -30), yaw_deg=33.0)
    assert (hold.kind, hold.yaw_deg, hold.duration_s) == ("mc_hold", 33.0, 2.0)
    step = adapter.from_primitive(
        ActionPrimitive.translate(10.0, 0.0, -4.0), pose_ned=(0, 0, -30), yaw_deg=90.0
    )
    assert step.kind == "mc_reposition"
    assert step.target_ned == pytest.approx((0.0, 10.0, -26.0))
    assert step.speed_mps == AirframeMotionAdapter.MC_SPEED_MPS
    adapter.preferred_mode = "MC"
    assert adapter.mode_change_command() is None
    adapter.set_mode(4)
    assert adapter.mode_change_command().mode == "MC"


def test_tailsitter_mc_mount_pitch_is_read_for_the_requested_mode() -> None:
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/quadtailsitter.json")
    mc = profile.camera.attitude_rotations["mc"]
    assert profile_mount_pitch_down_rad(profile, 0.25, mode="mc") == pytest.approx(
        math.atan2(mc[6], mc[0])
    )
    assert profile_mount_pitch_down_rad(profile, 0.25) == 0.25


class _Vehicle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return type("R", (), {"status": "sent", "ok": True})()

        return record


def test_sim_executor_holds_a_hovering_vtol_in_place_instead_of_orbiting() -> None:
    vehicle = _Vehicle()
    executor = FixedWingVtolExecutor(
        vehicle, airframe="vtol", position=lambda: (5.0, 6.0, -30.0), yaw_deg=lambda: 45.0
    )
    result = executor.execute_command(
        MotionCommand("mc_hold", target_ned=(5.0, 6.0, -30.0), yaw_deg=45.0, duration_s=1.0)
    )
    assert result.status == "success"
    name, args, kwargs = vehicle.calls[-1]
    assert name == "reposition"
    assert tuple(args[0]) == (5.0, 6.0) and args[1] == pytest.approx(30.0)
    assert args[2] == FixedWingVtolExecutor.MC_HOLD_SPEED_MPS and kwargs["yaw_deg"] == 45.0
    assert all(call[0] != "orbit" for call in vehicle.calls)


def test_hover_mission_vtol_takes_off_as_a_multicopter_and_stays_in_mc() -> None:
    vehicle = _Vehicle()
    executor = FixedWingVtolExecutor(vehicle, airframe="vtol", transition_after_takeoff=False)
    assert executor.arm_and_takeoff(30.0).status == "success"
    # NAV_VTOL_TAKEOFF would transition to FW by itself after the climb.
    assert [name for name, _, _ in vehicle.calls] == ["arm", "fw_takeoff"]
    assert vehicle.calls[1][1][0] == "multirotor"
    assert executor.mode == "MC"
    default = _Vehicle()
    FixedWingVtolExecutor(default, airframe="vtol").arm_and_takeoff(30.0)
    assert [name for name, _, _ in default.calls] == ["arm", "fw_takeoff", "vtol_transition"]
    assert default.calls[1][1][0] == "vtol"


def test_hover_mode_vtol_search_settles_back_to_the_mission_altitude(tmp_path: Path) -> None:
    vtol = make_policy(work_dir=tmp_path / "vtol", config={"airframe": "standard_vtol"})
    assert vtol.loop.config.search_altitude_m == pytest.approx(20.0)
    # After a back-transition climb the first search step descends.
    action, _ = vtol.loop.next_action([], pose_ned=(0.0, 0.0, -45.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.up_m < 0
    assert make_policy(work_dir=tmp_path / "x500").loop.config.search_altitude_m is None


def test_tailsitter_hover_frames_project_onto_the_ground(tmp_path: Path, monkeypatch) -> None:
    """The full-attitude ray path hands the ground model an N/E/up origin.

    SITL tailsitter ep2 confirmed the fire truck with position None: the origin's
    NED down value (negative) was taken for the height above ground.
    """
    from multidetect.autonomy.onboard_loop import OnboardLoop

    camera = {"fx": 270.0, "fy": 270.0, "cx": 320.0, "cy": 240.0}
    policy = make_policy(
        work_dir=tmp_path,
        config={"airframe": "quadtailsitter"},
        camera_info=camera,
        image_size=(640, 480),
    )
    seen = []

    def capture(self, detections, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(OnboardLoop, "update_tracks", capture)
    payload = io.BytesIO()
    Image.new("RGB", (640, 480)).save(payload, format="PNG")
    observation = {
        "rgb": payload.getvalue(),
        "camera_info": camera,
        "pose": {"local_ned_m": [0.0, 0.0, -30.0]},
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
        "flight_state": {"vtol_state": 3},
    }
    policy.act(observation, pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
    ground_point = seen[0]["ground_point"]
    # The camera looks 20 deg up: a pixel 50 deg below the axis is 30 deg below the
    # horizon, 30 / tan(30 deg) = 52 m ahead (camera offset of the x500 mount aside).
    north, east = ground_point(320.0, 240.0 + 270.0 * math.tan(math.radians(50.0)))
    assert north == pytest.approx(30.0 / math.tan(math.radians(30.0)), abs=1.0)
    assert east == pytest.approx(0.0, abs=1e-6)
    # Above the horizon there is no ground.
    assert ground_point(320.0, 240.0) is None


def test_tailsitter_camera_looks_above_the_horizon_in_hover_and_down_ahead_in_cruise() -> None:
    from multidetect.autonomy.motion_intent import camera_ray_from_profile

    profile = AirframeProfile.from_json(ROOT / "configs/airframes/quadtailsitter.json")
    centre = {"fx": 270.0, "fy": 270.0, "cx": 320.0, "cy": 240.0}
    hover = camera_ray_from_profile(
        320.0, 240.0, attitude_deg=(0, 0, 0), profile=profile, mode="mc", **centre
    )
    # SITL hover frames show the horizon below the image centre: 20 deg up.
    assert math.degrees(math.asin(-hover[2])) == pytest.approx(20.0, abs=1e-6)
    assert hover[0] > 0.9
    # Forward flight: PX4 reports the MC frame pitched -90 deg, nose (-z) ahead (north).
    cruise = camera_ray_from_profile(
        320.0, 240.0, attitude_deg=(0, -90, 0), profile=profile, mode="fw", **centre
    )
    assert cruise[2] == pytest.approx(math.cos(math.radians(20.0)), abs=1e-6)
    assert cruise[0] == pytest.approx(math.sin(math.radians(20.0)), abs=1e-6)

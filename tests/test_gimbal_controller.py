from __future__ import annotations

import math

import pytest

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.camera_mount import CameraMount, GimbalLimits, compose_camera_pose
from multidetect.gimbal_controller import (
    GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME,
    GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME,
    MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
    FixedGimbalController,
    Px4GimbalController,
    SimGimbalController,
    parse_gimbal_device_attitude_status,
)
from multidetect.onboard_runtime.vehicle import PymavlinkVehicle
from multidetect.sim_teacher.primitives import ActionPrimitive


def test_fixed_controller_is_noop() -> None:
    calls: list[tuple[float, float]] = []
    FixedGimbalController().command_pitch_yaw(1.0, 2.0)
    assert calls == []


def test_sim_controller_clamps_and_forwards_angles() -> None:
    calls: list[tuple[float, float]] = []
    mount = CameraMount.gimbal(
        limits=GimbalLimits(pitch_min_rad=-0.5, pitch_max_rad=0.5, yaw_min_rad=-1.0, yaw_max_rad=1.0)
    )
    SimGimbalController(mount, lambda pitch, yaw: calls.append((pitch, yaw))).command_pitch_yaw(2.0, -2.0)
    assert calls == [(0.5, -1.0)]


def test_px4_controller_emits_pitch_yaw_command_long() -> None:
    sent: list[tuple[int, tuple[float, ...]]] = []
    mount = CameraMount.gimbal()
    Px4GimbalController(mount, lambda command, params: sent.append((command, params))).command_pitch_yaw(
        math.radians(20.0), math.radians(-30.0)
    )
    assert sent[0][0] == MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW
    assert sent[0][1][:2] == pytest.approx((-20.0, -30.0))


def test_gimbal_attitude_feedback_parses_quaternion() -> None:
    state = parse_gimbal_device_attitude_status({"q": [math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4)]})
    assert state is not None
    assert state.yaw_rad == pytest.approx(math.pi / 2)


def test_gimbal_feedback_preserves_yaw_frame_flags() -> None:
    earth = parse_gimbal_device_attitude_status(
        {"q": [1.0, 0.0, 0.0, 0.0], "flags": GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME}
    )
    vehicle = parse_gimbal_device_attitude_status(
        {"q": [1.0, 0.0, 0.0, 0.0], "flags": GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME}
    )
    assert earth is not None and earth.yaw_frame == "world"
    assert vehicle is not None and vehicle.yaw_frame == "body"


@pytest.mark.parametrize("down_deg", (45.0, 90.0))
def test_gimbal_feedback_pitch_is_positive_down_and_rotation_points_down(down_deg: float) -> None:
    # MAVLink quaternion for a nose-down attitude has standard Euler pitch
    # negative; CameraMount intentionally exposes the opposite, positive-down
    # convention.  The matrix check catches the nadir sign error that Euler-only
    # tests miss.
    pitch_mavlink = -math.radians(down_deg)
    state = parse_gimbal_device_attitude_status(
        {
            "q": [math.cos(pitch_mavlink / 2.0), 0.0, math.sin(pitch_mavlink / 2.0), 0.0]
        }
    )
    assert state is not None
    assert state.pitch_rad == pytest.approx(math.radians(down_deg))
    assert state.rotation is not None
    optical_axis_down = state.rotation[6]
    assert optical_axis_down == pytest.approx(math.sin(math.radians(down_deg)), abs=1e-6)


def test_composed_nonidentity_vehicle_and_gimbal_pose_keeps_downward_axis() -> None:
    pitch_mavlink = -math.pi / 4.0
    state = parse_gimbal_device_attitude_status(
        {
            "q": [math.cos(pitch_mavlink / 2.0), 0.0, math.sin(pitch_mavlink / 2.0), 0.0],
            "flags": GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME,
        }
    )
    assert state is not None and state.rotation is not None
    pose = compose_camera_pose(
        (0.0, 0.0, -10.0),
        vehicle_roll_rad=math.radians(10.0),
        vehicle_pitch_rad=math.radians(-8.0),
        vehicle_yaw_rad=math.radians(45.0),
        mount=CameraMount.gimbal(),
        rotation_override=state.rotation,
        rotation_frame="body",
    )
    assert pose.optical_to_world[6] > 0.0


def test_x500_profile_limits_are_converted_to_camera_mount_positive_down() -> None:
    profile = load_airframe_profile("configs/airframes/x500_gimbal.json")
    assert profile.camera_mount is not None
    mount = profile.camera_mount.to_camera_mount()
    assert mount.limits.pitch_min_rad == pytest.approx(-math.radians(45.0))
    assert mount.limits.pitch_max_rad == pytest.approx(math.radians(135.0))


def test_airframe_camera_mount_profile_is_optional_and_selectable() -> None:
    profile = load_airframe_profile("configs/airframes/x500_mono_cam.json")
    assert profile.camera_mount is None
    raw = profile.to_mapping()
    raw["camera_mount"] = {
        "type": "gimbal",
        "axes": ["pitch", "yaw"],
        "limits": {"pitch": [-1.0, 0.2], "yaw": [-3.14, 3.14]},
        "rates": {"pitch": 1.0, "yaw": 1.5},
        "default_mode": "lock-horizon",
    }
    selected = type(profile).from_mapping(raw)
    assert selected.camera_mount is not None
    assert selected.camera_mount.to_camera_mount().kind == "gimbal"


def test_px4_vehicle_exposes_gimbal_send_and_state_feedback() -> None:
    vehicle = PymavlinkVehicle("udp://127.0.0.1:14550")
    sent: list[tuple[int, tuple[float, ...]]] = []
    vehicle._command = lambda command, *params: sent.append((command, tuple(params)))
    vehicle.send_gimbal_pitch_yaw(math.radians(15), math.radians(45))
    assert sent[0][0] == MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW
    assert sent[0][1][:2] == pytest.approx((-15.0, 45.0))
    vehicle.ingest(
        {
            "message_type": "GIMBAL_DEVICE_ATTITUDE_STATUS",
            "q": [math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4)],
        },
        received_monotonic=1.0,
    )
    assert vehicle.snapshot().gimbal_attitude_rad is not None
    assert vehicle.snapshot().gimbal_attitude_rad[2] == pytest.approx(math.pi / 2)


def test_survey_commands_nadir_on_gimbal_without_changing_fixed_path() -> None:
    calls: list[tuple[float, float]] = []
    mount = CameraMount.gimbal()
    controller = SimGimbalController(mount, lambda pitch, yaw: calls.append((pitch, yaw)))
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "survey",
            "steps": [
                {"id": "survey", "skill": "survey", "params": {"region": "zone:a", "overlap": 0.7}, "on_success": "home", "on_fail": "home"},
                {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
            ],
            "report": ["coverage"],
        }
    )
    factory = lambda task, region=None, **overrides: OnboardLoop(
        camera=CameraModel(fx=540, fy=540, cx=640, cy=480, width=1280, height=960),
        search_action=lambda pose, yaw: ActionPrimitive.translate(1.0, 0.0),
        config=LoopConfig(**overrides),
    )
    executor = MissionExecutor(plan, loop_factory=factory, refs={"zone:a": (0.0, 0.0, 100.0)}, gimbal_controller=controller)
    action, info = executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    assert calls and calls[-1][0] == pytest.approx(math.pi / 2)
    fixed = MissionExecutor(
        plan, loop_factory=factory, refs={"zone:a": (0.0, 0.0, 100.0)}
    )
    no_op_fixed = MissionExecutor(
        plan,
        loop_factory=factory,
        refs={"zone:a": (0.0, 0.0, 100.0)},
        gimbal_controller=FixedGimbalController(),
    )
    fixed_result = fixed.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    no_op_result = no_op_fixed.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    assert no_op_result == fixed_result


def test_inspect_gimbal_points_at_target_instead_of_turning_vehicle() -> None:
    calls: list[tuple[float, float]] = []
    controller = SimGimbalController(CameraMount.gimbal(), lambda pitch, yaw: calls.append((pitch, yaw)))
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "inspect target",
            "steps": [
                {
                    "id": "inspect",
                    "skill": "inspect",
                    "params": {"target_ref": "poi:target", "query": "target", "views": 1, "standoff_m": 12.0},
                    "on_success": "home",
                    "on_fail": "home",
                },
                {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
            ],
            "report": ["inspection"],
        }
    )
    factory = lambda task, region=None, **overrides: OnboardLoop(
        camera=CameraModel(fx=540, fy=540, cx=640, cy=480, width=1280, height=960),
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        config=LoopConfig(**overrides),
    )
    executor = MissionExecutor(
        plan,
        loop_factory=factory,
        refs={"poi:target": (0.0, 20.0)},
        gimbal_controller=controller,
    )
    run = executor._run
    assert run is not None
    action_info = executor._command_gimbal(run, (0.0, 0.0, -20.0), 0.0)
    assert action_info is not None
    assert calls[-1][0] == pytest.approx(math.pi / 4)
    assert calls[-1][1] == pytest.approx(math.pi / 2)

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.motion_intent import camera_ray_from_profile
from multidetect.autonomy.capability_contract import capabilities_for_airframe
from multidetect.autonomy.motion_intent import AirframeMotionAdapter, MotionIntent
from multidetect.sim_teacher import ActionPrimitive, execute_gimbal_action
from multidetect.sim_teacher.streams import MavlinkState, SensorSample, build_observation
from multidetect.simulator.camera_projection import pixel_camera_ray
from scripts import collect_sim_teacher_episodes as collector

ROOT = Path(__file__).resolve().parents[1]


def test_delta_model_exposes_three_gimbal_joints_and_standard_sensor_links():
    root = ET.parse(ROOT / "simulation/gazebo/models/delta_tailsitter_gimbal_cam/model.sdf").getroot()
    model = root.find("model")
    assert model is not None
    assert {model.findtext(f"joint[@name='c11_{axis}_joint']/parent") for axis in ("yaw", "roll", "pitch")} == {
        "c11_mount_link", "c11_yaw_link", "c11_roll_link"
    }
    assert model.find("link[@name='camera_link']/sensor[@name='camera']") is not None
    assert model.find("link[@name='depth_camera_link']/sensor[@name='depth_camera']") is not None
    topics = {plugin.findtext("sub_topic") for plugin in model.findall("plugin") if plugin.findtext("sub_topic")}
    assert {"command/gimbal_yaw", "command/gimbal_roll", "command/gimbal_pitch"} <= topics


def test_profile_camera_ray_applies_live_gimbal_pitch_only_when_sampled():
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    nominal = camera_ray_from_profile(
        320, 240, fx=270, fy=270, cx=320, cy=240,
        attitude_deg=(0, 0, 0), profile=profile, mode="mc",
    )
    live = camera_ray_from_profile(
        320, 240, fx=270, fy=270, cx=320, cy=240,
        attitude_deg=(0, 0, 0), profile=profile, mode="mc",
        gimbal_angles_deg={"yaw": 0, "roll": 0, "pitch": -45},
    )
    assert math.asin(nominal[2]) == pytest.approx(math.radians(-20), abs=1e-6)
    assert math.asin(live[2]) < math.asin(nominal[2])


@pytest.mark.parametrize(
    "angles",
    [
        {"yaw": 0.0, "roll": 0.0, "pitch": -45.0},
        {"yaw": 179.0, "roll": -12.0, "pitch": 30.0},
    ],
)
def test_gimbal_attitude_judgement_decodes_device_quaternion(angles):
    yaw, roll, pitch = (math.radians(angles[key]) for key in ("yaw", "roll", "pitch"))
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    q = (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )
    actual = collector._gimbal_attitude_angles_deg({"q": q})
    error = collector._gimbal_attitude_error_deg({"q": q}, angles)
    assert actual == pytest.approx(angles, abs=1e-6)
    assert error == pytest.approx({"yaw": 0, "roll": 0, "pitch": 0}, abs=1e-6)


def test_gimbal_command_wait_uses_fresh_fake_device_telemetry(monkeypatch):
    import time

    state = {
        "GIMBAL_DEVICE_ATTITUDE_STATUS": None,
        "received_monotonic_ns": {},
    }

    class Reader:
        def __init__(self):
            self.state = state

        def raise_if_failed(self):
            return None

    class Mav:
        def command_long_send(self, *args):
            assert args[:4] == (1, 1, 205, 0)
            pitch = math.radians(float(args[4]))
            state["GIMBAL_DEVICE_ATTITUDE_STATUS"] = {
                "q": (math.cos(pitch / 2), 0.0, math.sin(pitch / 2), 0.0)
            }
            state["received_monotonic_ns"]["GIMBAL_DEVICE_ATTITUDE_STATUS"] = time.monotonic_ns()

    class Connection:
        target_system = 1
        target_component = 1
        mav = Mav()

    monkeypatch.setattr(collector._ReaderAckLink, "wait_command_ack", lambda *args: {"result": 0})
    result = collector._command_and_wait_gimbal(
        Connection(), Reader(), {"yaw": 0.0, "roll": 0.0, "pitch": -45.0}, timeout_s=0.2
    )
    assert result["status"] == "reached"
    assert max(result["error_deg"].values()) <= 4.0


def test_fixed_camera_smoke_plan_does_not_request_gimbal_control():
    from scripts.smoke_airframe_flight import build_plan, default_scenario, profile_for

    scenario = default_scenario("delta_tailsitter_mono_cam")
    assert scenario is not None
    plan = build_plan("delta_tailsitter_mono_cam", profile_for("delta_tailsitter_mono_cam"), scenario, None)
    assert plan["judgement"]["gimbal_command_success"] is True
    assert "gimbal_command_success" not in plan["pass_fields"]


def test_sim_projection_accepts_runtime_camera_extrinsics():
    pose = {
        "local_ned_m": (0.0, 0.0, -10.0),
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
    }
    rotation = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    result = pixel_camera_ray(
        320, 240, pose=pose, camera_info={"fx": 270, "fy": 270, "cx": 320, "cy": 240},
        image_size=(640, 480),
        camera_extrinsics={"position_body_ned": (0.2, 0.0, -0.3), "rotation_body_from_camera": rotation},
    )
    assert result is not None
    centre, ray = result
    assert centre == pytest.approx((0.2, 0.0, -10.3))
    assert ray == pytest.approx((1.0, 0.0, 0.0))


def test_gimbal_action_rejects_fixed_camera_before_sending():
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")

    class Mav:
        def __init__(self):
            self.calls = []

        def command_long_send(self, *args):
            self.calls.append(args)

    class Vehicle:
        target_system = 1
        target_component = 1

        def __init__(self):
            self.mav = Mav()

    vehicle = Vehicle()
    result = execute_gimbal_action(
        vehicle,
        ActionPrimitive.gimbal(pitch_deg=-20),
        profile=profile,
        reader=object(),
        timeout_s=0.01,
    )
    assert result.status == "rejected"
    assert result.ack["reason"] == "gimbal_capability_required"
    assert vehicle.mav.calls == []


def test_gimbal_action_sends_and_reads_fresh_attitude():
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    state = MavlinkState()

    class Mav:
        def command_long_send(self, *args):
            assert args[:4] == (1, 1, 205, 0)
            pitch = math.radians(float(args[4]))
            state.command_acks[205] = ({"result": 0}, __import__("time").monotonic_ns())
            state.messages["GIMBAL_DEVICE_ATTITUDE_STATUS"] = {
                "q": (math.cos(pitch / 2), 0.0, math.sin(pitch / 2), 0.0)
            }
            state.received_monotonic_ns["GIMBAL_DEVICE_ATTITUDE_STATUS"] = __import__("time").monotonic_ns()

    class Vehicle:
        target_system = 1
        target_component = 1
        mav = Mav()

    class Reader:
        def __init__(self):
            self.state = state

    result = execute_gimbal_action(
        Vehicle(),
        ActionPrimitive.gimbal(pitch_deg=-45),
        profile=profile,
        reader=Reader(),
        timeout_s=0.2,
    )
    assert result.status == "success"
    assert result.ack["actual_deg"]["pitch"] == pytest.approx(-45.0, abs=1e-5)


def test_gimbal_action_rejects_profile_range_and_non_numeric_intent():
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    result = execute_gimbal_action(
        object(),
        ActionPrimitive.gimbal(pitch_deg=60),
        profile=profile,
        reader=object(),
    )
    assert result.status == "rejected"
    assert result.ack["reason"] == "gimbal_angle_out_of_range"
    assert result.ack["axis"] == "pitch"

    with pytest.raises(ValueError, match="finite numbers"):
        MotionIntent.gimbal(pitch_deg="-45")


def test_gimbal_intent_and_capability_description_are_typed():
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    assert "gimbal_control" in capabilities_for_airframe(profile)
    description = profile.thinking_behavior()["gimbal"]
    assert description["axes"] == ["yaw", "roll", "pitch"]
    command = AirframeMotionAdapter(profile).intent_to_command(
        MotionIntent.gimbal(pitch_deg=-45, yaw_deg=20), pose_ned=(0, 0, -30)
    )
    assert command.kind == "gimbal"
    assert command.action is not None and command.action.kind == "gimbal"


def test_onboard_dispatch_rejects_gimbal_without_profile_capability():
    from multidetect.onboard_runtime.runtime import OnboardRuntime

    runtime = object.__new__(OnboardRuntime)
    runtime.vehicle = object()
    runtime.motion_adapter = AirframeMotionAdapter(
        AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")
    )
    command = runtime.motion_adapter.intent_to_command(
        MotionIntent.gimbal(pitch_deg=-20), pose_ned=(0, 0, -30)
    )
    with pytest.raises(RuntimeError, match="gimbal_capability_required"):
        runtime._dispatch_motion_command(command, object())

    runtime.motion_adapter = AirframeMotionAdapter(
        AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    )
    command = runtime.motion_adapter.intent_to_command(
        MotionIntent.gimbal(pitch_deg=60), pose_ned=(0, 0, -30)
    )
    with pytest.raises(RuntimeError, match="gimbal_angle_out_of_range"):
        runtime._dispatch_motion_command(command, object())


def test_observation_exposes_live_gimbal_angles():
    sensor = SensorSample(
        rgb=b"",
        width=1,
        height=1,
        depth=None,
        camera_info={},
        pose={},
        velocity=[],
        sensor_stamp_ns=1,
        received_monotonic_ns=10,
        received_wall_time=0.0,
    )
    state = MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"x": 0, "y": 0, "z": -10},
            "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
            "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 10000},
            "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10, "eph": 100},
            "GIMBAL_DEVICE_ATTITUDE_STATUS": {"q": (1, 0, 0, 0)},
        },
        received_monotonic_ns={"LOCAL_POSITION_NED": 10, "GIMBAL_DEVICE_ATTITUDE_STATUS": 20},
    )
    observation = build_observation(sensor, state)
    assert observation["gimbal"]["available"] is True
    assert observation["gimbal"]["angles_deg"] == pytest.approx(
        {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    )

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.motion_intent import camera_ray_from_profile
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


def test_gimbal_earth_frame_yaw_is_compared_in_vehicle_frame():
    q = (math.cos(math.radians(40) / 2), 0.0, 0.0, math.sin(math.radians(40) / 2))
    status = {"q": q, "flags": 64}
    actual = collector._gimbal_attitude_angles_deg(status, vehicle_yaw_deg=40.0)
    assert actual == pytest.approx({"yaw": 0.0, "roll": 0.0, "pitch": 0.0}, abs=1e-6)


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

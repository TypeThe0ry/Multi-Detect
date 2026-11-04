from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.motion_intent import camera_ray_from_profile
from multidetect.simulator.camera_projection import pixel_camera_ray

ROOT = Path(__file__).resolve().parents[1]


def test_delta_model_exposes_three_gimbal_joints_and_standard_sensor_links():
    root = ET.parse(ROOT / "simulation/gazebo/models/delta_tailsitter/model.sdf").getroot()
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
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")
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

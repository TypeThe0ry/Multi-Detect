"""Offline camera/overlay contract for the airframes regression suite."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.simulator.stack import _vehicle_spec

pytestmark = pytest.mark.onboard
ROOT = Path(__file__).resolve().parents[1]


def _missions():
    suite = json.loads((ROOT / "configs/onboard/suite-airframes.json").read_text("utf-8"))
    for mission in suite["missions"]:
        runtime = ROOT / mission["index"].replace("index.json", f"{mission['world']}.runtime_input.json")
        yield mission, json.loads(runtime.read_text("utf-8"))


@pytest.mark.parametrize("mission,runtime", list(_missions()), ids=lambda value: value.get("id", "runtime"))
def test_suite_airframe_has_rgb_depth_and_segmentation_assets(mission, runtime):
    _, model = _vehicle_spec(
        runtime["vehicle"]["model"], runtime["vehicle"]["airframe_profile_id"]
    )
    sdf_path = ROOT / "simulation/gazebo/models" / model / "model.sdf"
    sdf = ET.parse(sdf_path).getroot().find("model")
    text = sdf_path.read_text("utf-8")
    assert "model://mono_cam" in text
    assert sdf is not None
    assert sdf.find("joint[@name='CameraJoint']/child").text == "camera_link"
    assert sdf.find("link[@name='depth_camera_link']/sensor[@name='depth_camera']") is not None
    overlay = ROOT / "simulation/gazebo/model-overlays" / f"{model}_seg"
    overlay_root = ET.parse(overlay / "model.sdf").getroot().find("model")
    assert overlay_root is not None
    assert overlay_root.find("link[@name='segmentation_camera_link']") is not None
    assert overlay_root.findtext(
        "link[@name='segmentation_camera_link']/sensor[@name='segmentation_camera']/topic"
    ) == "/multidetect/teacher/segmentation"
    image_topic = f"/world/default/model/{model}_0/link/camera_link/sensor/camera/image"
    depth_topic = (
        f"/world/default/model/{model}_0/link/depth_camera_link/"
        "sensor/depth_camera/depth_image"
    )
    assert image_topic.startswith("/world/default/model/")
    assert depth_topic.endswith("/sensor/depth_camera/depth_image")

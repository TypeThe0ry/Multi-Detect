"""Offline contracts for the PX4 x500 gimbal segmentation overlay."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.collect_sim_teacher_episodes import _airframe_profile

ROOT = Path(__file__).resolve().parents[1]
AIRFRAME_ROOT = ROOT / "configs" / "airframes"
OVERLAY_ROOT = ROOT / "simulation" / "gazebo" / "model-overlays"

_OVERLAY_MODEL_BY_PROFILE = {
    "advanced_plane": "advanced_plane_mono_cam",
    "quadtailsitter": "quadtailsitter_mono_cam",
    "rc_cessna_catapult": "rc_cessna_mono_cam",
    "rc_cessna_hand": "rc_cessna_mono_cam",
    "rc_cessna_runway": "rc_cessna_mono_cam",
    "standard_vtol": "standard_vtol_mono_cam",
    "tiltrotor": "tiltrotor_mono_cam",
    "x500_gimbal": "x500_gimbal",
    "x500_mono_cam": "x500_mono_cam",
}


@pytest.mark.parametrize(
    "profile_path",
    sorted(AIRFRAME_ROOT.glob("*.json")),
    ids=lambda path: path.stem,
)
def test_every_collector_selectable_airframe_has_segmentation_overlay(profile_path: Path) -> None:
    profile_id = json.loads(profile_path.read_text(encoding="utf-8"))["id"]
    # Keep this check tied to the collector's actual profile resolver.  A new
    # profile must be selectable there before its overlay is required here.
    _airframe_profile(profile_id)
    model_name = _OVERLAY_MODEL_BY_PROFILE[profile_id]
    overlay = OVERLAY_ROOT / f"{model_name}_seg"
    assert (overlay / "model.sdf").is_file(), f"missing segmentation overlay for {profile_id}"
    config = ET.parse(overlay / "model.config").getroot()
    assert config.findtext("name") == model_name
    assert config.findtext("sdf") == "model.sdf"


def test_x500_gimbal_segmentation_camera_follows_gimbal_camera_link() -> None:
    overlay = OVERLAY_ROOT / "x500_gimbal_seg"
    root = ET.parse(overlay / "model.sdf").getroot()
    assert root.attrib == {"version": "1.9"}
    model = root.find("model")
    assert model is not None and model.attrib == {"name": "x500_gimbal"}
    includes = model.findall("include")
    assert [include.findtext("uri") for include in includes] == [
        "model://x500",
        "model://gimbal",
    ]
    assert includes[1].findtext("pose") == "0 0 0.26 0 0 3.14"
    attach = model.find("joint[@name='GimbalAttachJoint']")
    assert attach is not None
    assert attach.findtext("parent") == "base_link"
    assert attach.findtext("child") == "cgo3_mount_link"

    depth_link = model.find("link[@name='depth_camera_link']")
    assert depth_link is not None
    depth_sensor = depth_link.find("sensor[@name='depth_camera']")
    assert depth_sensor is not None and depth_sensor.attrib.get("type") == "depth_camera"
    assert depth_sensor.findtext("gz_frame_id") == "depth_camera_link"
    assert depth_sensor.findtext("pose") == "-0.0412 0 -0.162 0 0 3.14"
    assert depth_sensor.findtext("camera/horizontal_fov") == "2.0"
    assert depth_sensor.findtext("camera/image/width") == "1280"
    assert depth_sensor.findtext("camera/image/height") == "720"
    assert depth_sensor.findtext("camera/image/format") == "R_FLOAT32"

    link = model.find("link[@name='segmentation_camera_link']")
    assert link is not None
    assert link.find("pose").attrib == {"relative_to": "camera_link"}
    sensor = link.find("sensor[@name='segmentation_camera']")
    assert sensor is not None and sensor.attrib.get("type") == "segmentation"
    assert sensor.findtext("gz_frame_id") == "segmentation_camera_link"
    assert sensor.findtext("topic") == "/multidetect/teacher/segmentation"
    assert sensor.findtext("camera/segmentation_type") == "instance"
    assert sensor.findtext("pose") == "-0.0412 0 -0.162 0 0 3.14"
    assert sensor.findtext("camera/horizontal_fov") == "2.0"
    assert sensor.findtext("camera/image/width") == "1280"
    assert sensor.findtext("camera/image/height") == "720"
    assert sensor.findtext("camera/clip/near") == "0.05"
    assert sensor.findtext("camera/clip/far") == "15000"

    joint = model.find("joint[@name='SegmentationCameraJoint']")
    assert joint is not None and joint.attrib.get("type") == "fixed"
    assert joint.findtext("parent") == "camera_link"
    assert joint.findtext("child") == "segmentation_camera_link"
    assert joint.find("pose").attrib == {"relative_to": "camera_link"}

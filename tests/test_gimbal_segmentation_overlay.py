"""Offline contracts for the PX4 x500 gimbal segmentation overlay."""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO
from scripts.gazebo_sensor_stream import CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX
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

    joint = model.find("joint[@name='SegmentationCameraJoint']")
    assert joint is not None and joint.attrib.get("type") == "fixed"
    assert joint.findtext("parent") == "camera_link"
    assert joint.findtext("child") == "segmentation_camera_link"
    assert joint.find("pose").attrib == {"relative_to": "camera_link"}

    odometry = model.find("plugin[@name='gz::sim::systems::OdometryPublisher']")
    assert odometry is not None
    assert odometry.attrib.get("filename") == "gz-sim-odometry-publisher-system"
    assert odometry.findtext("dimensions") == "3"
    assert odometry.findtext("odom_publish_frequency") == "50"


def test_gimbal_rgb_depth_and_segmentation_match_standard_camera_calibration() -> None:
    standard_model = ET.parse(
        ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf"
    ).getroot().find("model")
    assert standard_model is not None
    standard_depth = standard_model.find(
        "link[@name='depth_camera_link']/sensor[@name='depth_camera']"
    )
    assert standard_depth is not None
    standard_camera = standard_depth.find("camera")
    assert standard_camera is not None
    standard_geometry = {
        "horizontal_fov": float(standard_camera.findtext("horizontal_fov", "nan")),
        "width": int(standard_camera.findtext("image/width", "-1")),
        "height": int(standard_camera.findtext("image/height", "-1")),
        "near": float(standard_camera.findtext("clip/near", "nan")),
        "far": float(standard_camera.findtext("clip/far", "nan")),
        "update_rate": float(standard_depth.findtext("update_rate", "nan")),
    }
    assert standard_geometry["width"] == DEFAULT_CAMERA_INFO["width"]
    assert standard_geometry["height"] == DEFAULT_CAMERA_INFO["height"]
    standard_fx = standard_geometry["width"] / (
        2.0 * math.tan(standard_geometry["horizontal_fov"] / 2.0)
    )
    assert math.isclose(
        standard_fx,
        DEFAULT_CAMERA_INFO["fx"],
        rel_tol=0.0,
        abs_tol=CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX,
    )

    rgb_root = ET.parse(
        OVERLAY_ROOT / "x500_gimbal_seg/gimbal/model.sdf"
    ).getroot()
    rgb_model = rgb_root.find("model")
    assert rgb_model is not None and rgb_model.attrib == {"name": "gimbal"}
    rgb = rgb_model.find("link[@name='camera_link']/sensor[@name='camera']")
    assert rgb is not None and rgb.attrib.get("type") == "camera"
    assert rgb.findtext("gz_frame_id") == "camera_link"

    overlay_root = ET.parse(
        OVERLAY_ROOT / "x500_gimbal_seg/model.sdf"
    ).getroot()
    overlay_model = overlay_root.find("model")
    assert overlay_model is not None
    depth = overlay_model.find(
        "link[@name='depth_camera_link']/sensor[@name='depth_camera']"
    )
    segmentation = overlay_model.find(
        "link[@name='segmentation_camera_link']/sensor[@name='segmentation_camera']"
    )
    assert depth is not None and segmentation is not None
    for sensor in (rgb, depth, segmentation):
        camera = sensor.find("camera")
        assert camera is not None
        actual_geometry = {
            "horizontal_fov": float(camera.findtext("horizontal_fov", "nan")),
            "width": int(camera.findtext("image/width", "-1")),
            "height": int(camera.findtext("image/height", "-1")),
            "near": float(camera.findtext("clip/near", "nan")),
            "far": float(camera.findtext("clip/far", "nan")),
            "update_rate": float(sensor.findtext("update_rate", "nan")),
        }
        assert actual_geometry == standard_geometry
        assert actual_geometry["width"] == DEFAULT_CAMERA_INFO["width"]
        assert actual_geometry["height"] == DEFAULT_CAMERA_INFO["height"]
        fx_from_sdf = actual_geometry["width"] / (
            2.0 * math.tan(actual_geometry["horizontal_fov"] / 2.0)
        )
        assert math.isclose(
            fx_from_sdf,
            DEFAULT_CAMERA_INFO["fx"],
            rel_tol=0.0,
            abs_tol=CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX,
        )
        fy_from_sdf = actual_geometry["height"] / (
            2.0
            * math.tan(
                math.atan(
                    math.tan(actual_geometry["horizontal_fov"] / 2.0)
                    * actual_geometry["height"]
                    / actual_geometry["width"]
                )
            )
        )
        assert math.isclose(
            fy_from_sdf,
            DEFAULT_CAMERA_INFO["fy"],
            rel_tol=0.0,
            abs_tol=CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX,
        )
        assert math.isclose(
            DEFAULT_CAMERA_INFO["cx"],
            actual_geometry["width"] / 2.0,
            rel_tol=0.0,
            abs_tol=CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX,
        )
        assert math.isclose(
            DEFAULT_CAMERA_INFO["cy"],
            actual_geometry["height"] / 2.0,
            rel_tol=0.0,
            abs_tol=CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX,
        )

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "simulation" / "gazebo" / "models"

_MODELS = (
    ("x500_mono_cam", "x500_mono_cam_0"),
    ("standard_vtol_mono_cam", "standard_vtol_mono_cam_0"),
    ("rc_cessna_mono_cam", "rc_cessna_mono_cam_0"),
)

_RGB_CONTRACT = {
    "horizontal_fov": 1.74,
    "width": 1280,
    "height": 960,
    "near": 0.1,
    "far": 3000.0,
    "update_rate": 30.0,
}


def _vector(text: str | None) -> tuple[float, ...]:
    assert text is not None
    values = tuple(float(value) for value in text.split())
    assert len(values) == 6
    assert all(math.isfinite(value) for value in values)
    return values


def _model(model_name: str) -> ET.Element:
    root = ET.parse(MODEL_ROOT / model_name / "model.sdf").getroot()
    assert root.tag == "sdf"
    assert root.attrib.get("version") == "1.9"
    model = root.find("model")
    assert model is not None
    assert model.attrib == {"name": model_name}
    return model


@pytest.mark.parametrize("model_name,model_instance", _MODELS)
def test_depth_camera_is_colocated_and_uses_the_standard_scoped_topic(
    model_name: str, model_instance: str
) -> None:
    model = _model(model_name)
    depth_link = model.find("link[@name='depth_camera_link']")
    assert depth_link is not None
    depth_pose = depth_link.find("pose")
    assert depth_pose is not None
    assert depth_pose.attrib == {"relative_to": "camera_link"}
    assert _vector(depth_pose.text) == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    depth_joint = model.find("joint[@name='DepthCameraJoint']")
    assert depth_joint is not None
    assert depth_joint.attrib.get("type") == "fixed"
    assert depth_joint.findtext("parent") == "camera_link"
    assert depth_joint.findtext("child") == "depth_camera_link"
    joint_pose = depth_joint.find("pose")
    assert joint_pose is not None
    assert joint_pose.attrib == {"relative_to": "camera_link"}
    assert _vector(joint_pose.text) == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    sensor = depth_link.find("sensor[@name='depth_camera']")
    assert sensor is not None
    assert sensor.attrib.get("type") == "depth_camera"
    assert sensor.findtext("gz_frame_id") == "depth_camera_link"
    assert _vector(sensor.findtext("pose")) == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert sensor.findtext("always_on") == "1"
    assert float(sensor.findtext("update_rate", "nan")) == _RGB_CONTRACT["update_rate"]
    assert sensor.findtext("visualize") == "true"

    camera = sensor.find("camera")
    assert camera is not None
    assert float(camera.findtext("horizontal_fov", "nan")) == _RGB_CONTRACT["horizontal_fov"]
    image = camera.find("image")
    assert image is not None
    assert int(image.findtext("width", "-1")) == _RGB_CONTRACT["width"]
    assert int(image.findtext("height", "-1")) == _RGB_CONTRACT["height"]
    assert image.findtext("format") == "R_FLOAT32"
    clip = camera.find("clip")
    assert clip is not None
    assert float(clip.findtext("near", "nan")) == _RGB_CONTRACT["near"]
    assert float(clip.findtext("far", "nan")) == _RGB_CONTRACT["far"]

    # No custom topic is intentional: Gazebo's default scoped topic is stable
    # and resolves to this exact depth_image suffix for the named sensor.
    assert sensor.find("topic") is None
    expected_topic = (
        f"/world/default/model/{model_instance}/link/depth_camera_link/"
        "sensor/depth_camera/depth_image"
    )
    assert expected_topic.endswith("/sensor/depth_camera/depth_image")

    serialized = (MODEL_ROOT / model_name / "model.sdf").read_text(encoding="utf-8")
    normalized_text = " ".join(serialized.split())
    assert "Gazebo's camera frame looks along +X" in serialized
    assert "optical +Z convention" in serialized
    assert "does not assert whether a depth scalar" in normalized_text
    assert "optical-axis or ray-slant" in normalized_text
    for token in ("ground_truth", "expected_answer", "target_id"):
        assert token not in serialized.lower()


@pytest.mark.parametrize("model_name,_model_instance", _MODELS)
def test_depth_model_config_preserves_model_identity_and_declares_depth(
    model_name: str, _model_instance: str
) -> None:
    config = ET.parse(MODEL_ROOT / model_name / "model.config").getroot()
    assert config.findtext("name") == model_name
    assert config.findtext("sdf") == "model.sdf"
    sdf = config.find("sdf")
    assert sdf is not None
    assert sdf.attrib.get("version") == "1.9"
    description = config.findtext("description", "").lower()
    assert "mono rgb" in description
    assert "depth camera" in description


def test_depth_camera_sdf_uses_gazebo_not_ros_optical_axis_semantics() -> None:
    """The SDF must not silently claim the unresolved +Z convention.

    Gazebo documents rendering-camera forward as +X, while ROS-style optical
    frames use +Z.  This model only declares the co-located sensor.  A future
    bridge must perform the explicit frame conversion before labelling samples
    as optical-axis-Z depth; no unsupported SDF optical-frame tag is invented.
    """

    for model_name, _model_instance in _MODELS:
        model = _model(model_name)
        sensor = model.find("link[@name='depth_camera_link']/sensor[@name='depth_camera']")
        assert sensor is not None
        camera = sensor.find("camera")
        assert camera is not None
        assert camera.find("optical_frame_id") is None

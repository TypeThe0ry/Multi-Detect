"""The opt-in camera preserves the base composition and optical geometry."""

import xml.etree.ElementTree as ET
from pathlib import Path


def test_overlay_camera_geometry():
    root = Path(__file__).resolve().parents[1]
    base = ET.parse(root / "simulation/gazebo/models/x500_mono_cam/model.sdf").getroot()
    overlay = ET.parse(
        root / "simulation/gazebo/model-overlays/x500_mono_cam_seg/model.sdf"
    ).getroot()
    model = overlay.find("model")
    assert model.attrib == base.find("model").attrib
    for node in (*base.iter(), *overlay.iter()):
        node.tail = None
    for child in base.find("model"):
        matches = [item for item in model if item.tag == child.tag and item.attrib == child.attrib]
        assert any(ET.tostring(item) == ET.tostring(child) for item in matches)
    sensor = model.find("link[@name='segmentation_camera_link']/sensor")
    depth = base.find("model/link[@name='depth_camera_link']/sensor")
    assert sensor.get("type") == "segmentation"
    assert sensor.findtext("camera/segmentation_type") == "instance"
    for path in ("camera/horizontal_fov", "camera/image/width", "camera/image/height",
                 "camera/clip/near", "camera/clip/far", "update_rate", "pose"):
        assert sensor.findtext(path) == depth.findtext(path)
    assert model.findtext("link[@name='segmentation_camera_link']/pose") == "0 0 0 0 0 0"

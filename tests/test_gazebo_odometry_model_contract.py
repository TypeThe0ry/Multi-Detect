from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "simulation" / "gazebo" / "models"
MODEL_NAMES = (
    "x500_mono_cam",
    "standard_vtol_mono_cam",
    "rc_cessna_mono_cam",
)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_repository_airframes_publish_required_full_3d_odometry(model_name: str) -> None:
    model = ET.parse(MODEL_ROOT / model_name / "model.sdf").getroot().find("model")
    assert model is not None

    plugins = [
        plugin
        for plugin in model.findall("plugin")
        if plugin.attrib.get("name") == "gz::sim::systems::OdometryPublisher"
    ]
    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin.attrib.get("filename") == "gz-sim-odometry-publisher-system"
    assert plugin.findtext("dimensions") == "3"
    assert plugin.findtext("odom_publish_frequency") == "50"
    # Keep Gazebo's canonical model-instance-qualified topic names so the same
    # helper contract works for multirotor, VTOL, and fixed-wing scenes.
    assert plugin.find("odom_topic") is None
    assert plugin.find("odom_covariance_topic") is None
    assert plugin.find("tf_topic") is None

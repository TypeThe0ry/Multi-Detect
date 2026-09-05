from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.multimodal_ranging import load_camera_calibration
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_ROOT = ROOT / "simulation/gazebo/calibration"
MODEL_ROOT = ROOT / "simulation/gazebo/models"

_CASES = (
    (
        "configs/scenarios/validation/terrain_ranging_validation_001.scenario.json",
        "x500_mono_cam",
        "gazebo-multirotor-front-camera-v1",
    ),
    (
        "configs/scenarios/river_rescue_vtol_001.scenario.json",
        "standard_vtol_mono_cam",
        "gazebo-front-camera-v1",
    ),
    (
        "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json",
        "rc_cessna_mono_cam",
        "gazebo-fixed-wing-front-camera-v1",
    ),
)


def _camera_pose(model_name: str) -> tuple[float, ...]:
    root = ET.parse(MODEL_ROOT / model_name / "model.sdf").getroot()
    model = root.find("model")
    assert model is not None
    include = next(
        item for item in model.findall("include") if item.findtext("uri") == "model://mono_cam"
    )
    pose = include.find("pose")
    assert pose is not None
    assert pose.attrib == {"relative_to": "base_link"}
    return tuple(float(value) for value in (pose.text or "").split())


@pytest.mark.parametrize("scenario_relative,model_name,calibration_id", _CASES)
def test_gazebo_camera_calibration_matches_scenario_and_composite_model(
    scenario_relative: str,
    model_name: str,
    calibration_id: str,
) -> None:
    scenario = load_runtime_scenario(ROOT / scenario_relative, mode=RuntimeMode.SIM)
    rgb_sensors = tuple(sensor for sensor in scenario.sensors if sensor.kind == "rgb_camera")
    assert len(rgb_sensors) == 1
    assert rgb_sensors[0].calibration_id == calibration_id

    calibration = load_camera_calibration(CALIBRATION_ROOT / f"{calibration_id}.json")
    assert calibration.calibration_id == calibration_id
    assert (calibration.width_px, calibration.height_px) == (1280, 960)
    expected_focal_px = 1280.0 / (2.0 * math.tan(1.74 / 2.0))
    assert calibration.fx_px == pytest.approx(expected_focal_px, abs=1e-12)
    assert calibration.fy_px == pytest.approx(expected_focal_px, abs=1e-12)
    assert (calibration.cx_px, calibration.cy_px) == (640.0, 480.0)
    assert (calibration.k1, calibration.k2, calibration.p1, calibration.p2, calibration.k3) == (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )

    x_m, y_m, z_m, roll_rad, pitch_rad, yaw_rad = _camera_pose(model_name)
    assert calibration.camera_offset_forward_m == pytest.approx(x_m)
    assert calibration.camera_offset_right_m == pytest.approx(-y_m)
    assert calibration.camera_offset_down_m == pytest.approx(-z_m)
    assert calibration.mount_roll_clockwise_deg == pytest.approx(math.degrees(roll_rad))
    assert calibration.mount_pitch_down_deg == pytest.approx(math.degrees(pitch_rad))
    assert calibration.mount_yaw_right_deg == pytest.approx(math.degrees(yaw_rad))


def test_gazebo_camera_calibration_contract_is_not_field_acceptance() -> None:
    text = (CALIBRATION_ROOT / "README.md").read_text(encoding="utf-8")
    assert "cannot satisfy the REAL or outdoor-GNSS release gates" in text
    assert "bind the\ncalibration file SHA-256 into the integrated run identity" in text

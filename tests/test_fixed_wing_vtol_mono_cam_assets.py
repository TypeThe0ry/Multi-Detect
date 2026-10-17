from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.simulator.stack import stack_plan_from_scenario


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = ROOT / "configs" / "scenarios"
MODEL_ROOT = ROOT / "simulation" / "gazebo" / "models"
OVERLAY_ROOT = ROOT / "simulation" / "gazebo" / "model-overlays"


@pytest.mark.parametrize(
    ("scenario", "vehicle_class", "model_name", "autostart"),
    [
        ("urban_inspection_multirotor_001", "multirotor", "x500_mono_cam", "4001"),
        ("river_rescue_vtol_001", "vtol", "standard_vtol_mono_cam", "4004"),
        ("coastal_patrol_fixed_wing_001", "fixed-wing", "rc_cessna_mono_cam", "4003"),
        ("airframes/advanced_plane_mono_cam_001", "fixed-wing", "advanced_plane_mono_cam", "4008"),
        ("airframes/quadtailsitter_mono_cam_001", "vtol", "quadtailsitter_mono_cam", "4018"),
        ("airframes/tiltrotor_mono_cam_001", "vtol", "tiltrotor_mono_cam", "4020"),
    ],
)
def test_new_airframe_environment_mapping(
    scenario: str, vehicle_class: str, model_name: str, autostart: str
) -> None:
    plan = stack_plan_from_scenario(
        SCENARIO_ROOT / f"{scenario}.scenario.json",
        repo_root=ROOT,
        tool_capabilities={"gz": False, "px4": False},
    )
    assert plan.vehicle_class == vehicle_class
    assert plan.model_name == model_name
    assert plan.environment_mapping["PX4_SYS_AUTOSTART"] == autostart
    assert plan.environment_mapping["PX4_SIM_MODEL"] == f"gz_{model_name}"
    if scenario.startswith("airframes/"):
        assert plan.environment_mapping["PX4_GZ_MODEL_POSE"]


@pytest.mark.parametrize(
    ("model_name", "px4_model", "pitch_rad"),
    [
        ("advanced_plane_mono_cam", "advanced_plane", math.radians(25)),
        ("quadtailsitter_mono_cam", "quadtailsitter", math.radians(45)),
        ("tiltrotor_mono_cam", "tiltrotor", math.radians(25)),
    ],
)
def test_new_models_and_segmentation_overlays_are_local_compositions(
    model_name: str, px4_model: str, pitch_rad: float
) -> None:
    for directory in (MODEL_ROOT / model_name, OVERLAY_ROOT / f"{model_name}_seg"):
        config = ET.parse(directory / "model.config").getroot()
        assert config.findtext("name") == model_name
        root = ET.parse(directory / "model.sdf").getroot()
        model = root.find("model")
        assert model is not None and model.attrib["name"] == model_name
        includes = {item.findtext("uri") for item in model.findall("include")}
        assert includes == {f"model://{px4_model}", "model://mono_cam"}
        assert "http://" not in (directory / "model.sdf").read_text(encoding="utf-8")
        assert "https://" not in (directory / "model.sdf").read_text(encoding="utf-8")

    base_model = ET.parse(MODEL_ROOT / model_name / "model.sdf").getroot().find("model")
    assert base_model is not None
    camera = base_model.find("include[uri='model://mono_cam']/pose")
    assert camera is not None
    values = tuple(float(value) for value in (camera.text or "").split())
    assert len(values) == 6 and math.isclose(values[4], pitch_rad, abs_tol=1e-9)

    overlay_text = (OVERLAY_ROOT / f"{model_name}_seg" / "model.sdf").read_text(encoding="utf-8")
    assert "name=\"segmentation_camera\"" in overlay_text
    assert "/multidetect/teacher/segmentation" in overlay_text


def test_quadtailsitter_camera_axis_is_body_forward_down_and_rotates_with_airframe() -> None:
    """The fixed belly mount is 45 deg below +X in body coordinates.

    At hover the tailsitter body is rotated 90 deg relative to forward flight,
    so the same rigid optical axis becomes a forward/downward ray in world
    coordinates instead of silently changing the sensor pose at runtime.
    """
    pitch = math.radians(45)
    # The mount has yaw=pi, so its body-frame ray is rearward/downward.
    body_axis = (-math.cos(pitch), 0.0, -math.sin(pitch))
    assert body_axis == pytest.approx((-math.sqrt(0.5), 0.0, -math.sqrt(0.5)))
    # R_y(-90 deg) is the hover attitude (body nose points up).  In this
    # convention the same rigid ray is world-forward/downward.
    hover_axis = (-body_axis[2], 0.0, body_axis[0])
    assert hover_axis[0] > 0.0 and hover_axis[2] < 0.0

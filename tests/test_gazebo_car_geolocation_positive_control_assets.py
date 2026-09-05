from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.runtime import RuntimeMode
from multidetect.simulation import VehicleSpawnPose, load_runtime_scenario, load_scenario_manifest
from multidetect.simulator.scene_stream_set import SCENARIO_MODES
from multidetect.simulator.stack import stack_plan_from_scenario
from multidetect.simulator.three_d_control import AIRFRAME_SCENARIOS
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import load_local_dem_grid
from scripts.run_simulator_docker import build_docker_simulator_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "gazebo_car_geolocation_positive_control_001"
SCENARIO_ROOT = ROOT / "configs/scenarios/validation"
MANIFEST = SCENARIO_ROOT / f"{SCENARIO_ID}.scenario.json"
RUNTIME = SCENARIO_ROOT / f"{SCENARIO_ID}.runtime.json"
TRUTH = SCENARIO_ROOT / f"{SCENARIO_ID}.truth.json"
EXPECTATIONS = SCENARIO_ROOT / f"{SCENARIO_ID}.expectations.json"
WORLD = ROOT / f"simulation/gazebo/worlds/{SCENARIO_ID}.sdf"
MAP = ROOT / f"simulation/maps/{SCENARIO_ID}.geojson"
DEM = ROOT / f"simulation/terrain/{SCENARIO_ID}.dem.json"
CALIBRATION = ROOT / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json"
TARGET_ID = "gazebo-car-geolocation-positive-control-billboard-v1"
PX4_POSE = "0,0,6,0,0,0"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _vector(text: str | None, length: int) -> tuple[float, ...]:
    assert text is not None
    result = tuple(float(item) for item in text.split())
    assert len(result) == length
    assert all(math.isfinite(item) for item in result)
    return result


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key).lower() for key in value),
            *(key for child in value.values() for key in _walk_keys(child)),
        }
    if isinstance(value, list):
        return {key for child in value for key in _walk_keys(child)}
    return set()


def _walk_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {item for child in value.values() for item in _walk_strings(child)}
    if isinstance(value, list):
        return {item for child in value for item in _walk_strings(child)}
    return set()


def test_scenario_channels_are_strict_hash_bound_and_truth_separated() -> None:
    documents = {
        "manifest": _load_json(MANIFEST),
        "runtime_input": _load_json(RUNTIME),
        "truth": _load_json(TRUTH),
        "expectations": _load_json(EXPECTATIONS),
    }
    schema_names = {
        "manifest": "scenario-manifest.schema.json",
        "runtime_input": "scenario-runtime-input.schema.json",
        "truth": "scenario-truth.schema.json",
        "expectations": "scenario-expectations.schema.json",
    }
    for name, document in documents.items():
        schema = _load_json(ROOT / "configs/schemas" / schema_names[name])
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)

    for channel_name, reference in documents["manifest"]["channels"].items():
        assert canonical_sha256(documents[channel_name]) == reference["sha256"]

    manifest = load_scenario_manifest(MANIFEST)
    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    assert runtime.scenario_manifest_hash == manifest.canonical_hash
    assert runtime.vehicle.spawn_pose == VehicleSpawnPose(
        x_east_m=0.0,
        y_north_m=0.0,
        z_up_m=6.0,
        roll_rad=0.0,
        pitch_rad=0.0,
        yaw_rad=0.0,
    )

    forbidden_keys = {
        "target",
        "targets",
        "target_id",
        "position",
        "bbox",
        "bounding_box",
        "normalized_bbox",
        "expected_label",
        "expected_confidence",
        "annotation",
        "ground_truth",
        "evaluation_truth",
    }
    assert _walk_keys(documents["runtime_input"]).isdisjoint(forbidden_keys)
    assert TARGET_ID not in _walk_strings(documents["runtime_input"])


def test_world_keeps_vehicle_support_invisible_and_target_on_dem_ground() -> None:
    root = ET.parse(WORLD).getroot()
    assert root.tag == "sdf" and root.attrib == {"version": "1.9"}
    world = root.find("world")
    assert world is not None and world.attrib == {"name": "default"}
    assert world.findall(".//include") == []
    models = {model.attrib["name"]: model for model in world.findall("model")}
    assert set(models) == {
        "multidetect_dem_truth_surface",
        "multidetect_geolocation_vehicle_support",
        "gazebo_car_geolocation_positive_control_billboard",
        "home_marker",
    }

    support = models["multidetect_geolocation_vehicle_support"]
    assert support.findtext("static") == "true"
    assert _vector(support.findtext("pose"), 6) == (0.0, 0.0, 5.4, 0.0, 0.0, 0.0)
    assert _vector(support.findtext("link/collision/geometry/box/size"), 3) == (
        2.5,
        2.5,
        0.2,
    )
    assert support.findall(".//visual") == []
    assert 5.4 + 0.2 / 2.0 == pytest.approx(5.5)

    billboard = models["gazebo_car_geolocation_positive_control_billboard"]
    assert billboard.findtext("static") == "true"
    assert _vector(billboard.findtext("pose"), 6) == (8.0, 0.0, 1.15, 0.0, 0.0, 0.0)
    assert _vector(billboard.findtext("link/collision/geometry/box/size"), 3) == (
        0.08,
        3.3,
        2.2,
    )
    assert (
        billboard.findtext("link/visual/geometry/mesh/uri")
        == "model://multidetect_coco_car_billboard_v1/meshes/car_billboard.obj"
    )

    serialized = WORLD.read_text(encoding="utf-8").lower()
    for token in (
        "target_id",
        "normalized_bbox",
        "bounding_box",
        "expected_label",
        "expected_confidence",
        "detector_output",
    ):
        assert token not in serialized
    assert "software-only" in serialized
    assert "not qgc-rendering" in serialized
    assert "not" in serialized and "flight" in serialized


def test_elevated_geometry_places_complete_billboard_inside_calibrated_fov() -> None:
    calibration = _load_json(CALIBRATION)["calibration"]
    width_px = float(calibration["width_px"])
    height_px = float(calibration["height_px"])
    fx_px = float(calibration["fx_px"])
    fy_px = float(calibration["fy_px"])
    cy_px = float(calibration["cy_px"])
    horizontal_half_fov_rad = math.atan(width_px / (2.0 * fx_px))
    vertical_half_fov_rad = math.atan(height_px / (2.0 * fy_px))

    nominal_navsat_agl_m = 5.5 + 0.231
    lens_up_m = nominal_navsat_agl_m - float(calibration["camera_offset_down_m"])
    horizontal_range_m = 8.0 - float(calibration["camera_offset_forward_m"])
    target_bottom_up_m = 1.15 - 2.2 / 2.0
    target_top_up_m = 1.15 + 2.2 / 2.0
    optical_pitch_down_rad = math.radians(float(calibration["mount_pitch_down_deg"]))

    bottom_down_rad = math.atan2(lens_up_m - target_bottom_up_m, horizontal_range_m)
    top_down_rad = math.atan2(lens_up_m - target_top_up_m, horizontal_range_m)
    bottom_pixel_y = cy_px + fy_px * math.tan(bottom_down_rad - optical_pitch_down_rad)
    top_pixel_y = cy_px + fy_px * math.tan(top_down_rad - optical_pitch_down_rad)
    horizontal_half_angle_rad = math.atan2(3.3 / 2.0, horizontal_range_m)
    projected_width_px = 2.0 * fx_px * math.tan(horizontal_half_angle_rad)

    assert horizontal_half_angle_rad < horizontal_half_fov_rad
    assert bottom_down_rad - optical_pitch_down_rad < vertical_half_fov_rad
    assert top_down_rad - optical_pitch_down_rad > -vertical_half_fov_rad
    assert 0.0 < top_pixel_y < bottom_pixel_y < height_px
    assert bottom_pixel_y > cy_px
    assert bottom_pixel_y - top_pixel_y >= 100.0
    assert projected_width_px >= 200.0


def test_truth_coordinate_reconciles_with_eight_metre_east_world_offset() -> None:
    manifest = load_scenario_manifest(MANIFEST)
    dem = load_local_dem_grid(DEM)
    expected_latitude, expected_longitude = wgs84_forward_from_ned(
        latitude_deg=dem.origin_latitude_deg,
        longitude_deg=dem.origin_longitude_deg,
        north_offset_m=0.0,
        east_offset_m=8.0,
    )
    target = manifest.truth.targets[0]

    assert target.target_id == TARGET_ID
    assert target.label == "car"
    assert target.position.latitude_deg == pytest.approx(expected_latitude, abs=1e-12)
    assert target.position.longitude_deg == pytest.approx(expected_longitude, abs=1e-12)
    assert target.position.altitude_reference == "MSL"
    assert target.position.altitude_m == pytest.approx(26.15)
    assert dem.sample_local(0.0, 8.0).elevation_m == pytest.approx(25.0)


def test_stack_and_docker_forward_exact_hash_bound_spawn_pose(tmp_path: Path) -> None:
    plan = stack_plan_from_scenario(MANIFEST, repo_root=ROOT)
    assert plan.scenario_id == SCENARIO_ID
    assert plan.world_path == WORLD.resolve()
    assert plan.map_path == MAP.resolve()
    assert plan.dem_path == DEM.resolve()
    assert plan.environment_mapping["PX4_GZ_MODEL_POSE"] == PX4_POSE

    docker_plan = build_docker_simulator_plan(
        MANIFEST,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
        container_name="multidetect-geolocation-positive-test",
    )
    assert f"PX4_GZ_MODEL_POSE={PX4_POSE}" in docker_plan.command
    assert docker_plan.scenario_manifest_hash == plan.scenario_manifest_hash
    assert SCENARIO_ID not in AIRFRAME_SCENARIOS
    assert SCENARIO_ID not in SCENARIO_MODES

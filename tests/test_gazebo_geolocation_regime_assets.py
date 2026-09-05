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
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest
from multidetect.simulator.stack import stack_plan_from_scenario
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import load_local_dem_grid
from scripts.run_simulator_docker import build_docker_simulator_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = ROOT / "configs/scenarios/validation"
CASES = (
    {
        "scenario_id": "gazebo_car_geolocation_mid_slope_001",
        "spawn_z_m": 12.0,
        "target_east_m": 15.0,
        "target_center_up_m": 2.1625,
        "target_height_m": 4.125,
        "target_width_m": 6.1875,
        "slope": 0.07,
        "normal": (0.0, -0.0698291277, 0.9975589671),
    },
    {
        "scenario_id": "gazebo_car_geolocation_high_slope_001",
        "spawn_z_m": 20.0,
        "target_east_m": 25.0,
        "target_center_up_m": 3.5375,
        "target_height_m": 6.875,
        "target_width_m": 10.3125,
        "slope": 0.14,
        "normal": (0.0, -0.1386478445, 0.9903417467),
    },
)


def _load(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


def _vector(text: str | None) -> tuple[float, ...]:
    assert text is not None
    return tuple(float(item) for item in text.split())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario_id"])
def test_regime_scenario_contract_and_hash_binding(case: dict[str, Any]) -> None:
    scenario_id = case["scenario_id"]
    paths = {
        "manifest": SCENARIO_ROOT / f"{scenario_id}.scenario.json",
        "runtime_input": SCENARIO_ROOT / f"{scenario_id}.runtime.json",
        "truth": SCENARIO_ROOT / f"{scenario_id}.truth.json",
        "expectations": SCENARIO_ROOT / f"{scenario_id}.expectations.json",
    }
    documents = {name: _load(path) for name, path in paths.items()}
    schema_names = {
        "manifest": "scenario-manifest.schema.json",
        "runtime_input": "scenario-runtime-input.schema.json",
        "truth": "scenario-truth.schema.json",
        "expectations": "scenario-expectations.schema.json",
    }
    for name, document in documents.items():
        validator = Draft202012Validator(_load(ROOT / "configs/schemas" / schema_names[name]))
        validator.validate(document)
    for channel, reference in documents["manifest"]["channels"].items():
        assert canonical_sha256(documents[channel]) == reference["sha256"]

    manifest = load_scenario_manifest(paths["manifest"])
    runtime = load_runtime_scenario(paths["manifest"], mode=RuntimeMode.SIM)
    assert runtime.scenario_manifest_hash == manifest.canonical_hash
    assert runtime.vehicle.spawn_pose.z_up_m == pytest.approx(case["spawn_z_m"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario_id"])
def test_dem_world_and_truth_reconcile(case: dict[str, Any]) -> None:
    scenario_id = case["scenario_id"]
    manifest_path = SCENARIO_ROOT / f"{scenario_id}.scenario.json"
    manifest = load_scenario_manifest(manifest_path)
    dem = load_local_dem_grid(ROOT / f"simulation/terrain/{scenario_id}.dem.json")
    sample = dem.sample_local(0.0, case["target_east_m"])
    assert sample.elevation_m == pytest.approx(25.0)
    assert sample.north_slope_m_per_m == pytest.approx(case["slope"])
    assert sample.east_slope_m_per_m == pytest.approx(0.0)
    slope_m_per_m = math.hypot(
        sample.north_slope_m_per_m,
        sample.east_slope_m_per_m,
    )
    assert math.degrees(math.atan(slope_m_per_m)) >= 3.0

    latitude, longitude = wgs84_forward_from_ned(
        latitude_deg=dem.origin_latitude_deg,
        longitude_deg=dem.origin_longitude_deg,
        north_offset_m=0.0,
        east_offset_m=case["target_east_m"],
    )
    target = manifest.truth.targets[0]
    assert target.position.latitude_deg == pytest.approx(latitude, abs=1e-12)
    assert target.position.longitude_deg == pytest.approx(longitude, abs=1e-12)
    assert target.position.altitude_m == pytest.approx(
        sample.elevation_m + case["target_center_up_m"]
    )

    root = ET.parse(ROOT / f"simulation/gazebo/worlds/{scenario_id}.sdf").getroot()
    world = root.find("world")
    assert world is not None and world.findall(".//include") == []
    terrain = world.find("model[@name='multidetect_dem_truth_surface']")
    assert terrain is not None
    normal = _vector(terrain.findtext("link/collision/geometry/plane/normal"))
    assert normal == pytest.approx(case["normal"], abs=1e-9)
    assert math.sqrt(sum(value * value for value in normal)) == pytest.approx(1.0, abs=1e-9)
    assert -normal[1] / normal[2] == pytest.approx(case["slope"], abs=1e-9)

    billboard = next(
        model for model in world.findall("model") if model.attrib["name"].endswith("billboard")
    )
    pose = _vector(billboard.findtext("pose"))
    size = _vector(billboard.findtext("link/collision/geometry/box/size"))
    assert pose[:3] == pytest.approx((case["target_east_m"], 0.0, case["target_center_up_m"]))
    assert size[1:] == pytest.approx((case["target_width_m"], case["target_height_m"]))
    assert pose[2] - size[2] / 2.0 == pytest.approx(0.1)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario_id"])
def test_scaled_billboard_remains_inside_camera_fov(case: dict[str, Any]) -> None:
    calibration = _load(
        ROOT / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json"
    )["calibration"]
    lens_up_m = case["spawn_z_m"] + 0.231 - calibration["camera_offset_down_m"]
    horizontal_m = case["target_east_m"] - calibration["camera_offset_forward_m"]
    bottom_m = case["target_center_up_m"] - case["target_height_m"] / 2.0
    top_m = case["target_center_up_m"] + case["target_height_m"] / 2.0
    optical_pitch = math.radians(calibration["mount_pitch_down_deg"])
    vertical_half_fov = math.atan(calibration["height_px"] / (2.0 * calibration["fy_px"]))
    bottom_angle = math.atan2(lens_up_m - bottom_m, horizontal_m) - optical_pitch
    top_angle = math.atan2(lens_up_m - top_m, horizontal_m) - optical_pitch
    projected_width_px = (
        2.0
        * calibration["fx_px"]
        * math.tan(math.atan2(case["target_width_m"] / 2.0, horizontal_m))
    )
    assert -vertical_half_fov < top_angle < bottom_angle < vertical_half_fov
    assert projected_width_px >= 200.0


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario_id"])
def test_stack_and_docker_plan_bind_each_regime(case: dict[str, Any], tmp_path: Path) -> None:
    scenario_id = case["scenario_id"]
    manifest = SCENARIO_ROOT / f"{scenario_id}.scenario.json"
    stack = stack_plan_from_scenario(manifest, repo_root=ROOT)
    assert stack.environment_mapping["PX4_GZ_MODEL_POSE"] == (f"0,0,{int(case['spawn_z_m'])},0,0,0")
    docker = build_docker_simulator_plan(
        manifest,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
        container_name=f"multidetect-{scenario_id}",
    )
    assert docker.scenario_manifest_hash == stack.scenario_manifest_hash
    world_path = str(ROOT / f"simulation/gazebo/worlds/{scenario_id}.sdf")
    assert any(world_path in argument for argument in docker.command)
    assert docker.dem_path == ROOT / f"simulation/terrain/{scenario_id}.dem.json"

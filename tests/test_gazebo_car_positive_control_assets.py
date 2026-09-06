from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy.serialization import AutonomyContractError, canonical_sha256
from multidetect.model_manifest import sha256_file
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest
from multidetect.simulator.scene_stream_set import SCENARIO_MODES
from multidetect.simulator.stack import stack_plan_from_scenario
from multidetect.simulator.three_d_control import AIRFRAME_SCENARIOS
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import load_local_dem_grid

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "gazebo_car_positive_control_001"
SCENARIO_ROOT = ROOT / "configs/scenarios/validation"
MANIFEST = SCENARIO_ROOT / f"{SCENARIO_ID}.scenario.json"
RUNTIME = SCENARIO_ROOT / f"{SCENARIO_ID}.runtime.json"
TRUTH = SCENARIO_ROOT / f"{SCENARIO_ID}.truth.json"
EXPECTATIONS = SCENARIO_ROOT / f"{SCENARIO_ID}.expectations.json"
WORLD = ROOT / f"simulation/gazebo/worlds/{SCENARIO_ID}.sdf"
MAP = ROOT / f"simulation/maps/{SCENARIO_ID}.geojson"
DEM = ROOT / f"simulation/terrain/{SCENARIO_ID}.dem.json"
MODEL_NAME = "multidetect_coco_car_billboard_v1"
MODEL_ROOT = ROOT / "simulation/gazebo/models" / MODEL_NAME
MODEL_SDF = MODEL_ROOT / "model.sdf"
MODEL_CONFIG = MODEL_ROOT / "model.config"
MODEL_MESH = MODEL_ROOT / "meshes/car_billboard.obj"
MODEL_MATERIAL = MODEL_ROOT / "meshes/car_billboard.mtl"
SOURCE_TEXTURE = ROOT / "simulation/gazebo/textures/coco-car-positive-control-v1.png"
MODEL_TEXTURE = MODEL_ROOT / "materials/textures/coco-car-positive-control-v1.png"
CALIBRATION = ROOT / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json"
TEXTURE_SHA256 = "474e41d52d62ee83c4f77d530b0591e5901869c374ac58313bdd6deb283fd031"
MESH_SHA256 = "be9ee4f005f1ad86218bfc18698f32f58fe4465dcdebd35e313bbc4bbc8aba8e"
MATERIAL_SHA256 = "45642ceb02a8a5f72fb4d9156ff552bbb2d32b123f6e1b9ca87e561b8fb33a34"
TEXTURE_URI = (
    "model://multidetect_coco_car_billboard_v1/materials/textures/coco-car-positive-control-v1.png"
)
MESH_URI = "model://multidetect_coco_car_billboard_v1/meshes/car_billboard.obj"
TARGET_ID = "gazebo-car-positive-control-billboard-v1"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _vector(text: str | None, length: int) -> tuple[float, ...]:
    assert text is not None
    values = tuple(float(value) for value in text.split())
    assert len(values) == length
    assert all(math.isfinite(value) for value in values)
    return values


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


def test_scenario_channels_validate_hash_bind_and_load_only_in_sim() -> None:
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

    raw_manifest = documents["manifest"]
    for channel_name, channel in raw_manifest["channels"].items():
        assert canonical_sha256(documents[channel_name]) == channel["sha256"]

    manifest = load_scenario_manifest(MANIFEST)
    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    assert manifest.scenario_id == SCENARIO_ID
    assert manifest.scenario_version == 1
    assert runtime.scenario_manifest_hash == manifest.canonical_hash
    assert runtime.scenario_id == SCENARIO_ID
    assert runtime.vehicle.model == "px4_multirotor_standard"
    assert runtime.vehicle.airframe_profile_id == "px4_multirotor_gazebo_v1"
    with pytest.raises(AutonomyContractError, match="REAL"):
        load_runtime_scenario(MANIFEST, mode=RuntimeMode.REAL)


def test_runtime_channel_contains_no_truth_annotation_or_detector_answer() -> None:
    runtime = _load_json(RUNTIME)
    manifest = _load_json(MANIFEST)
    truth = _load_json(TRUTH)
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
    assert _walk_keys(runtime).isdisjoint(forbidden_keys)
    runtime_strings = _walk_strings(runtime)
    assert TARGET_ID not in runtime_strings
    assert MODEL_NAME not in runtime_strings
    assert MODEL_TEXTURE.name not in runtime_strings
    assert set(manifest["channels"]) == {"runtime_input", "truth", "expectations"}
    assert {channel["path"] for channel in manifest["channels"].values()} == {
        RUNTIME.name,
        TRUTH.name,
        EXPECTATIONS.name,
    }
    assert truth["targets"] == [
        {
            "target_id": TARGET_ID,
            "label": "car",
            "position": {
                "latitude_deg": 1.2999999999995135,
                "longitude_deg": 103.80054941997577,
                "altitude_m": 26.15,
                "altitude_reference": "MSL",
            },
            "behavior": "stationary_software_only_billboard",
        }
    ]


def test_texture_copy_is_exact_repository_owned_png_with_no_network_dependency() -> None:
    assert SOURCE_TEXTURE.is_file()
    assert MODEL_TEXTURE.is_file()
    assert not SOURCE_TEXTURE.is_symlink()
    assert not MODEL_TEXTURE.is_symlink()
    assert SOURCE_TEXTURE.stat().st_size == MODEL_TEXTURE.stat().st_size == 1_961_776
    assert sha256_file(SOURCE_TEXTURE) == TEXTURE_SHA256
    assert sha256_file(MODEL_TEXTURE) == TEXTURE_SHA256
    assert SOURCE_TEXTURE.read_bytes() == MODEL_TEXTURE.read_bytes()

    payload = MODEL_TEXTURE.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(payload[16:20], "big") == 1536
    assert int.from_bytes(payload[20:24], "big") == 1024

    serialized = MODEL_SDF.read_text(encoding="utf-8") + WORLD.read_text(encoding="utf-8")
    lower = serialized.lower()
    assert "http://" not in lower
    assert "https://" not in lower
    assert "fuel://" not in lower
    assert TEXTURE_URI in serialized
    assert MESH_URI in serialized
    assert "simulation/gazebo/textures" not in lower


def test_obj_mtl_bundle_is_exact_two_sided_three_to_two_uv_mesh() -> None:
    assert MODEL_MESH.is_file() and not MODEL_MESH.is_symlink()
    assert MODEL_MATERIAL.is_file() and not MODEL_MATERIAL.is_symlink()
    assert sha256_file(MODEL_MESH) == MESH_SHA256
    assert sha256_file(MODEL_MATERIAL) == MATERIAL_SHA256

    lines = [line.strip() for line in MODEL_MESH.read_text(encoding="utf-8").splitlines()]
    vertices = [tuple(map(float, line.split()[1:])) for line in lines if line.startswith("v ")]
    texcoords = [tuple(map(float, line.split()[1:])) for line in lines if line.startswith("vt ")]
    normals = [tuple(map(float, line.split()[1:])) for line in lines if line.startswith("vn ")]
    faces = [tuple(line.split()[1:]) for line in lines if line.startswith("f ")]
    assert vertices == [
        (0.0, -1.65, -1.1),
        (0.0, 1.65, -1.1),
        (0.0, 1.65, 1.1),
        (0.0, -1.65, 1.1),
    ]
    assert texcoords == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert normals == [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
    assert faces == [
        ("1/1/1", "4/4/1", "3/3/1"),
        ("1/1/1", "3/3/1", "2/2/1"),
        ("1/1/2", "2/2/2", "3/3/2"),
        ("1/1/2", "3/3/2", "4/4/2"),
    ]
    assert max(vertex[1] for vertex in vertices) - min(vertex[1] for vertex in vertices) == 3.3
    assert max(vertex[2] for vertex in vertices) - min(vertex[2] for vertex in vertices) == 2.2

    material_lines = [
        line.strip()
        for line in MODEL_MATERIAL.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert material_lines[0] == "newmtl multidetect_coco_car_positive_control_v1"
    texture_binding = next(line for line in material_lines if line.startswith("map_Kd "))
    texture_relative = texture_binding.removeprefix("map_Kd ")
    assert ":" not in texture_relative and not texture_relative.startswith(("/", "\\"))
    assert (MODEL_MATERIAL.parent / texture_relative).resolve() == MODEL_TEXTURE.resolve()
    assert MODEL_TEXTURE.is_relative_to(MODEL_ROOT)
    serialized = MODEL_MESH.read_text(encoding="utf-8") + MODEL_MATERIAL.read_text(encoding="utf-8")
    assert "http://" not in serialized.lower()
    assert "https://" not in serialized.lower()
    assert "truth" not in serialized.lower()


def test_versioned_model_has_static_three_to_two_textured_billboard() -> None:
    config_root = ET.parse(MODEL_CONFIG).getroot()
    assert config_root.tag == "model"
    assert config_root.findtext("name") == MODEL_NAME
    assert config_root.findtext("version") == "1.0"
    assert config_root.findtext("sdf") == "model.sdf"

    root = ET.parse(MODEL_SDF).getroot()
    assert root.tag == "sdf"
    assert root.attrib == {"version": "1.9"}
    model = root.find("model")
    assert model is not None and model.attrib == {"name": MODEL_NAME}
    assert model.findtext("static") == "true"
    collision_size = _vector(
        model.findtext("link/collision[@name='billboard_collision']/geometry/box/size"), 3
    )
    visual_scale = _vector(
        model.findtext("link/visual[@name='car_positive_control_visual']/geometry/mesh/scale"),
        3,
    )
    visual_pose_element = model.find("link/visual[@name='car_positive_control_visual']/pose")
    assert collision_size == (0.08, 3.3, 2.2)
    assert visual_scale == (1.0, 1.0, 1.0)
    assert visual_pose_element is None
    assert collision_size[1] / collision_size[2] == pytest.approx(1536 / 1024)
    assert model.find("link/visual[@name='car_positive_control_visual']/geometry/plane") is None
    assert model.find("link/visual[@name='car_positive_control_visual']/geometry/box") is None
    assert (
        model.findtext("link/visual[@name='car_positive_control_visual']/geometry/mesh/uri")
        == MESH_URI
    )
    assert (
        model.findtext("link/visual[@name='car_positive_control_visual']/material/double_sided")
        == "true"
    )
    assert (
        model.findtext(
            "link/visual[@name='car_positive_control_visual']/material/pbr/metal/albedo_map"
        )
        == TEXTURE_URI
    )


def test_world_embeds_one_east_facing_billboard_and_resolves_model_texture() -> None:
    root = ET.parse(WORLD).getroot()
    assert root.tag == "sdf"
    assert root.attrib == {"version": "1.9"}
    world = root.find("world")
    assert world is not None and world.attrib == {"name": "default"}
    assert world.findall(".//include") == []
    models = {model.attrib["name"]: model for model in world.findall("model")}
    assert set(models) == {
        "multidetect_dem_truth_surface",
        "gazebo_car_positive_control_billboard",
        "home_marker",
    }
    billboard = models["gazebo_car_positive_control_billboard"]
    assert billboard.findtext("static") == "true"
    assert _vector(billboard.findtext("pose"), 6) == (5.5, 0.0, 1.15, 0.0, 0.0, 0.0)
    assert _vector(
        billboard.findtext("link/visual[@name='car_positive_control_visual']/geometry/mesh/scale"),
        3,
    ) == (1.0, 1.0, 1.0)
    visual_pose_element = billboard.find("link/visual[@name='car_positive_control_visual']/pose")
    assert visual_pose_element is None
    assert billboard.find("link/visual[@name='car_positive_control_visual']/geometry/plane") is None
    assert billboard.find("link/visual[@name='car_positive_control_visual']/geometry/box") is None
    assert (
        billboard.findtext("link/visual[@name='car_positive_control_visual']/geometry/mesh/uri")
        == MESH_URI
    )
    assert (
        billboard.findtext("link/visual[@name='car_positive_control_visual']/material/double_sided")
        == "true"
    )
    assert (
        billboard.findtext(
            "link/visual[@name='car_positive_control_visual']/material/pbr/metal/albedo_map"
        )
        == TEXTURE_URI
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
    assert "does not join or expand" in serialized
    assert "not model-accuracy" in serialized


def test_billboard_full_face_is_inside_nominal_initial_camera_field_of_view() -> None:
    calibration = _load_json(CALIBRATION)["calibration"]
    horizontal_half_fov_deg = math.degrees(
        math.atan(float(calibration["width_px"]) / (2.0 * float(calibration["fx_px"])))
    )
    vertical_half_fov_deg = math.degrees(
        math.atan(float(calibration["height_px"]) / (2.0 * float(calibration["fy_px"])))
    )
    lens_east_m = float(calibration["camera_offset_forward_m"])
    nominal_base_agl_m = 0.23
    lens_up_m = nominal_base_agl_m - float(calibration["camera_offset_down_m"])
    range_m = 5.5 - lens_east_m
    bottom_up_m = 1.15 - 2.2 / 2.0
    top_up_m = 1.15 + 2.2 / 2.0
    left_right_angle_deg = math.degrees(math.atan((3.3 / 2.0) / range_m))
    optical_pitch_down_deg = float(calibration["mount_pitch_down_deg"])
    bottom_offset_deg = abs(
        math.degrees(math.atan2(bottom_up_m - lens_up_m, range_m)) + optical_pitch_down_deg
    )
    top_offset_deg = abs(
        math.degrees(math.atan2(top_up_m - lens_up_m, range_m)) + optical_pitch_down_deg
    )

    assert range_m == pytest.approx(5.32)
    assert bottom_up_m == pytest.approx(0.05)
    assert left_right_angle_deg < horizontal_half_fov_deg
    assert bottom_offset_deg < vertical_half_fov_deg
    assert top_offset_deg < vertical_half_fov_deg
    assert max(bottom_offset_deg, top_offset_deg) < vertical_half_fov_deg - 2.0


def test_truth_coordinate_and_flat_dem_reconcile_with_world_geometry() -> None:
    manifest = load_scenario_manifest(MANIFEST)
    dem = load_local_dem_grid(DEM)
    assert dem.scenario_id == SCENARIO_ID
    world = ET.parse(WORLD).getroot().find("world")
    assert world is not None
    terrain = world.find("model[@name='multidetect_dem_truth_surface']")
    assert terrain is not None
    assert _vector(terrain.findtext("pose"), 6) == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert _vector(terrain.findtext("link/collision/geometry/plane/normal"), 3) == (
        0.0,
        0.0,
        1.0,
    )
    assert _vector(terrain.findtext("link/collision/geometry/plane/size"), 2) == (
        200.0,
        200.0,
    )

    target = manifest.truth.targets[0]
    latitude_deg, longitude_deg = wgs84_forward_from_ned(
        latitude_deg=dem.origin_latitude_deg,
        longitude_deg=dem.origin_longitude_deg,
        north_offset_m=0.0,
        east_offset_m=5.5,
    )
    assert target.target_id == TARGET_ID
    assert target.position.latitude_deg == pytest.approx(latitude_deg, abs=1e-12)
    assert target.position.longitude_deg == pytest.approx(longitude_deg, abs=1e-12)
    assert target.position.altitude_reference == "MSL"
    assert target.position.altitude_m == pytest.approx(26.15)
    assert dem.sample_local(0.0, 5.5).elevation_m == pytest.approx(25.0)
    assert target.position.altitude_m - 25.0 == pytest.approx(1.15)


def test_stack_plan_resolves_all_assets_without_expanding_release_matrix() -> None:
    plan = stack_plan_from_scenario(MANIFEST, repo_root=ROOT)
    assert plan.scenario_id == SCENARIO_ID
    assert plan.world_path == WORLD.resolve()
    assert plan.map_path == MAP.resolve()
    assert plan.dem_path == DEM.resolve()
    assert plan.model_name == "x500_mono_cam"
    assert plan.model_path == ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf"
    assert (
        str(ROOT / "simulation/gazebo/models") in plan.environment_mapping["GZ_SIM_RESOURCE_PATH"]
    )
    assert MODEL_ROOT.is_relative_to(ROOT / "simulation/gazebo/models")
    model_root_files = {
        item.path.relative_to(plan.assets.model_root.root).as_posix(): item.sha256
        for item in plan.assets.model_root.files
    }
    assert plan.asset_sha256["model_root"] == plan.assets.model_root.aggregate_sha256
    assert plan.assets.model_root.file_count == len(model_root_files) >= 11
    assert model_root_files["multidetect_coco_car_billboard_v1/meshes/car_billboard.obj"] == (
        MESH_SHA256
    )
    assert model_root_files["multidetect_coco_car_billboard_v1/meshes/car_billboard.mtl"] == (
        MATERIAL_SHA256
    )
    assert (
        model_root_files[
            "multidetect_coco_car_billboard_v1/materials/textures/coco-car-positive-control-v1.png"
        ]
        == TEXTURE_SHA256
    )
    assert SCENARIO_ID not in AIRFRAME_SCENARIOS
    assert SCENARIO_ID not in SCENARIO_MODES
    assert set(AIRFRAME_SCENARIOS) == {
        "river_rescue_vtol_001",
        "coastal_patrol_fixed_wing_001",
        "urban_inspection_multirotor_001",
    }
    assert SCENARIO_MODES == {
        "coastal_patrol_fixed_wing_001": "fixed-wing",
        "urban_inspection_multirotor_001": "multirotor",
        "river_rescue_vtol_001": "vtol",
    }

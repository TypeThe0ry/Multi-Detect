from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy.serialization import AutonomyContractError, canonical_sha256
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest
from multidetect.simulator.scene_stream_set import SCENARIO_MODES
from multidetect.simulator.stack import stack_plan_from_scenario
from multidetect.simulator.terrain_truth import (
    compare_dem_to_sdf_truth,
    load_sdf_terrain_truth,
)
from multidetect.simulator.three_d_control import AIRFRAME_SCENARIOS
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import load_local_dem_grid

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "terrain_pixel_validation_001"
SCENARIO_ROOT = ROOT / "configs/scenarios/validation"
MANIFEST = SCENARIO_ROOT / f"{SCENARIO_ID}.scenario.json"
RUNTIME = SCENARIO_ROOT / f"{SCENARIO_ID}.runtime.json"
TRUTH = SCENARIO_ROOT / f"{SCENARIO_ID}.truth.json"
EXPECTATIONS = SCENARIO_ROOT / f"{SCENARIO_ID}.expectations.json"
FLIGHT_PLAN = SCENARIO_ROOT / f"{SCENARIO_ID}.flight-plan.json"
WORLD = ROOT / f"simulation/gazebo/worlds/{SCENARIO_ID}.sdf"
MAP = ROOT / f"simulation/maps/{SCENARIO_ID}.geojson"
DEM = ROOT / f"simulation/terrain/{SCENARIO_ID}.dem.json"
CALIBRATION = ROOT / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json"


@dataclass(frozen=True, slots=True)
class _Marker:
    target_id: str
    model_name: str
    north_m: float
    east_m: float
    local_center_z_m: float
    altitude_msl_m: float


@dataclass(frozen=True, slots=True)
class _Viewpoint:
    view_id: str
    north_m: float
    east_m: float
    agl_m: float
    local_z_m: float
    yaw_deg: float
    target_id: str


MARKERS = (
    _Marker("terrain-pixel-target-v1", "terrain_pixel_panel_a", 40.0, 8.0, 6.0, 31.0),
    _Marker("terrain-pixel-target-v2", "terrain_pixel_panel_b", 100.0, 8.0, 12.0, 37.0),
    _Marker("terrain-pixel-target-v3", "terrain_pixel_panel_c", 160.0, 8.0, 18.0, 43.0),
)


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _load_flight_plan() -> tuple[dict[str, Any], tuple[_Viewpoint, ...]]:
    document = _load_json(FLIGHT_PLAN)
    assert set(document) == {
        "schema_version",
        "scenario_id",
        "scenario_version",
        "evidence_scope",
        "runtime_loaded",
        "truth_channel_separate",
        "expectations_channel_separate",
        "viewpoints",
    }
    raw_viewpoints = document["viewpoints"]
    assert isinstance(raw_viewpoints, list)
    assert len(raw_viewpoints) == 3
    viewpoints: list[_Viewpoint] = []
    for raw in raw_viewpoints:
        assert isinstance(raw, dict)
        assert set(raw) == {
            "view_id",
            "north_m",
            "east_m",
            "agl_m",
            "local_z_m",
            "yaw_deg",
            "target_id",
        }
        numeric_names = ("north_m", "east_m", "agl_m", "local_z_m", "yaw_deg")
        assert all(
            not isinstance(raw[name], bool)
            and isinstance(raw[name], (int, float))
            and math.isfinite(float(raw[name]))
            for name in numeric_names
        )
        assert isinstance(raw["view_id"], str) and raw["view_id"]
        assert isinstance(raw["target_id"], str) and raw["target_id"]
        viewpoints.append(
            _Viewpoint(
                view_id=raw["view_id"],
                north_m=float(raw["north_m"]),
                east_m=float(raw["east_m"]),
                agl_m=float(raw["agl_m"]),
                local_z_m=float(raw["local_z_m"]),
                yaw_deg=float(raw["yaw_deg"]),
                target_id=raw["target_id"],
            )
        )
    return document, tuple(viewpoints)


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


def test_channels_validate_load_and_match_canonical_hashes() -> None:
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
        channel_document = documents[channel_name]
        assert canonical_sha256(channel_document) == channel["sha256"]

    manifest = load_scenario_manifest(MANIFEST)
    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    assert manifest.scenario_id == SCENARIO_ID
    assert manifest.scenario_version == 1
    assert runtime.scenario_manifest_hash == manifest.canonical_hash
    assert runtime.scenario_id == SCENARIO_ID
    assert runtime.vehicle.model == "px4_multirotor_standard"
    assert manifest.expectations.maximum_false_geolocations == 0

    with pytest.raises(AutonomyContractError, match="REAL"):
        load_runtime_scenario(MANIFEST, mode=RuntimeMode.REAL)


def test_runtime_channel_contains_no_target_or_evaluator_answers() -> None:
    runtime = _load_json(RUNTIME)
    raw_manifest = _load_json(MANIFEST)
    flight_plan, viewpoints = _load_flight_plan()
    assert set(runtime) == {
        "schema_version",
        "scenario_id",
        "scenario_version",
        "seed",
        "world",
        "vehicle",
        "sensors",
        "faults",
    }
    assert runtime["world"] == {
        "world_asset": f"builtin://gazebo/{SCENARIO_ID}",
        "map_asset": f"builtin://maps/{SCENARIO_ID}.geojson",
        "dem_asset": f"builtin://terrain/{SCENARIO_ID}.dem.json",
        "wind_north_mps": 0.0,
        "wind_east_mps": 0.0,
        "gust_mps": 0.0,
    }

    forbidden_keys = {
        "target",
        "targets",
        "target_id",
        "position",
        "bbox",
        "bounding_box",
        "latitude_deg",
        "longitude_deg",
        "observation_points",
        "viewpoints",
        "flight_plan",
        "expectations",
        "ground_truth",
        "evaluation_truth",
    }
    assert _walk_keys(runtime).isdisjoint(forbidden_keys)
    runtime_strings = _walk_strings(runtime)
    assert {view.view_id for view in viewpoints}.isdisjoint(runtime_strings)
    assert {marker.target_id for marker in MARKERS}.isdisjoint(runtime_strings)

    assert flight_plan["schema_version"] == 1
    assert flight_plan["scenario_id"] == SCENARIO_ID
    assert flight_plan["scenario_version"] == 1
    assert flight_plan["evidence_scope"] == "software_only_gazebo_pixel_validation"
    assert flight_plan["runtime_loaded"] is False
    assert flight_plan["truth_channel_separate"] is True
    assert flight_plan["expectations_channel_separate"] is True
    manifest_channel_paths = {channel["path"] for channel in raw_manifest["channels"].values()}
    assert manifest_channel_paths == {RUNTIME.name, TRUTH.name, EXPECTATIONS.name}
    assert FLIGHT_PLAN.name not in manifest_channel_paths

    truth = _load_json(TRUTH)
    assert {target["target_id"] for target in truth["targets"]} == {
        marker.target_id for marker in MARKERS
    }


def test_world_has_three_grounded_high_contrast_pattern_panels() -> None:
    root = ET.parse(WORLD).getroot()
    assert root.tag == "sdf"
    assert root.attrib.get("version") == "1.9"
    world = root.find("world")
    assert world is not None
    assert world.attrib == {"name": "default"}
    assert not world.findall(".//include")
    assert not world.findall(".//uri")

    spherical = world.find("spherical_coordinates")
    assert spherical is not None
    assert spherical.findtext("surface_model") == "EARTH_WGS84"
    assert spherical.findtext("world_frame_orientation") == "ENU"
    assert float(spherical.findtext("latitude_deg", "nan")) == 1.3
    assert float(spherical.findtext("longitude_deg", "nan")) == 103.8005
    assert float(spherical.findtext("elevation", "nan")) == 25.0

    models = {model.attrib["name"]: model for model in world.findall("model")}
    terrain = models["multidetect_dem_truth_surface"]
    assert _vector(terrain.findtext("pose"), 6) == pytest.approx(
        (0.0, 0.0, 0.0, math.atan(0.1), 0.0, 0.0), abs=1e-15
    )
    assert _vector(terrain.findtext("link/collision/geometry/plane/size"), 2) == (500.0, 500.0)

    panel_diffuses: list[tuple[float, ...]] = []
    for marker in MARKERS:
        model = models[marker.model_name]
        assert model.findtext("static") == "true"
        east_m, north_m, center_z_m, roll, pitch, yaw = _vector(model.findtext("pose"), 6)
        assert (east_m, north_m, center_z_m) == (
            marker.east_m,
            marker.north_m,
            marker.local_center_z_m,
        )
        assert (roll, pitch, yaw) == (0.0, 0.0, 0.0)
        assert center_z_m - 2.0 == pytest.approx(0.1 * north_m)
        assert _vector(
            model.findtext("link/collision[@name='panel_collision']/geometry/box/size"), 3
        ) == (6.0, 0.3, 4.0)
        assert _vector(
            model.findtext("link/visual[@name='panel_visual']/geometry/box/size"), 3
        ) == (6.0, 0.3, 4.0)
        panel_diffuses.append(
            _vector(model.findtext("link/visual[@name='panel_visual']/material/diffuse"), 4)
        )
    assert len(set(panel_diffuses)) == 3

    panel_a = models[MARKERS[0].model_name]
    assert _vector(
        panel_a.findtext("link/visual[@name='vertical_stripe_visual']/geometry/box/size"), 3
    ) == (0.9, 0.08, 3.2)

    panel_b = models[MARKERS[1].model_name]
    roundel = panel_b.find("link/visual[@name='roundel_visual']")
    assert roundel is not None
    assert _vector(roundel.findtext("pose"), 6) == pytest.approx(
        (0.0, -0.19, 0.0, math.pi / 2.0, 0.0, 0.0), abs=1e-15
    )
    assert float(roundel.findtext("geometry/cylinder/radius", "nan")) == 1.0
    assert float(roundel.findtext("geometry/cylinder/length", "nan")) == 0.08

    panel_c = models[MARKERS[2].model_name]
    cross_pitches = sorted(
        _vector(panel_c.findtext(f"link/visual[@name='{name}']/pose"), 6)[4]
        for name in ("cross_rising_visual", "cross_falling_visual")
    )
    assert cross_pitches == pytest.approx((-0.6435011087932844, 0.6435011087932844), abs=1e-15)
    for name in ("cross_rising_visual", "cross_falling_visual"):
        assert _vector(panel_c.findtext(f"link/visual[@name='{name}']/geometry/box/size"), 3) == (
            5.0,
            0.08,
            0.3,
        )

    serialized = WORLD.read_text(encoding="utf-8").lower()
    for token in (
        "http://",
        "https://",
        "fuel://",
        "model://",
        "target_id",
        "bounding_box",
        "expected_answer",
    ):
        assert token not in serialized
    assert "software-only" in serialized
    assert "does not expand the release-bound" in serialized


def test_truth_coordinates_and_altitudes_reconcile_with_dem() -> None:
    manifest = load_scenario_manifest(MANIFEST)
    dem = load_local_dem_grid(DEM)
    sdf_truth = load_sdf_terrain_truth(WORLD)
    terrain_comparison = compare_dem_to_sdf_truth(dem, sdf_truth)
    assert dem.scenario_id == SCENARIO_ID
    assert sdf_truth.scenario_id == SCENARIO_ID
    assert terrain_comparison["gate_passed"] is True
    assert terrain_comparison["failure_reasons"] == []
    assert all(terrain_comparison["checks"].values())
    assert dem.origin_latitude_deg == 1.3
    assert dem.origin_longitude_deg == 103.8005
    assert dem.origin_elevation_m == 25.0

    targets = {target.target_id: target for target in manifest.truth.targets}
    for marker in MARKERS:
        target = targets[marker.target_id]
        latitude_deg, longitude_deg = wgs84_forward_from_ned(
            latitude_deg=dem.origin_latitude_deg,
            longitude_deg=dem.origin_longitude_deg,
            north_offset_m=marker.north_m,
            east_offset_m=marker.east_m,
        )
        assert target.position.latitude_deg == pytest.approx(latitude_deg, abs=1e-12)
        assert target.position.longitude_deg == pytest.approx(longitude_deg, abs=1e-12)
        assert target.position.altitude_reference == "MSL"
        assert target.position.altitude_m == marker.altitude_msl_m

        surface = dem.sample_local(marker.north_m, marker.east_m)
        assert surface.elevation_m == pytest.approx(25.0 + 0.1 * marker.north_m)
        assert target.position.altitude_m - surface.elevation_m == pytest.approx(2.0)
        assert marker.local_center_z_m == pytest.approx(
            target.position.altitude_m - dem.origin_elevation_m
        )


def test_flight_plan_views_keep_each_full_panel_inside_camera_fov() -> None:
    _flight_plan, viewpoints = _load_flight_plan()
    calibration = _load_json(CALIBRATION)["calibration"]
    width_px = float(calibration["width_px"])
    height_px = float(calibration["height_px"])
    fx_px = float(calibration["fx_px"])
    fy_px = float(calibration["fy_px"])
    mount_pitch_down_deg = float(calibration["mount_pitch_down_deg"])
    horizontal_half_fov_deg = math.degrees(math.atan(width_px / (2.0 * fx_px)))
    vertical_half_fov_deg = math.degrees(math.atan(height_px / (2.0 * fy_px)))
    markers = {marker.target_id: marker for marker in MARKERS}

    assert [view.view_id for view in viewpoints] == ["V1", "V2", "V3"]
    for view in viewpoints:
        marker = markers[view.target_id]
        assert view.yaw_deg == 0.0
        assert view.agl_m == 30.0
        assert view.local_z_m == pytest.approx(0.1 * view.north_m + view.agl_m)

        north_delta_m = marker.north_m - view.north_m
        east_delta_m = marker.east_m - view.east_m
        down_delta_m = view.local_z_m - marker.local_center_z_m
        horizontal_range_m = math.hypot(north_delta_m, east_delta_m)
        center_bearing_deg = math.degrees(math.atan2(east_delta_m, north_delta_m))
        center_depression_deg = math.degrees(math.atan2(down_delta_m, horizontal_range_m))

        assert north_delta_m == 40.0
        assert east_delta_m == 8.0
        assert down_delta_m == 24.0
        assert abs(center_bearing_deg) < horizontal_half_fov_deg
        assert abs(center_depression_deg - mount_pitch_down_deg) < vertical_half_fov_deg

        # Check the extreme east/west and top/bottom panel points, not only its center.
        horizontal_edge_deg = max(
            abs(math.degrees(math.atan2(east_delta_m - 3.0, north_delta_m))),
            abs(math.degrees(math.atan2(east_delta_m + 3.0, north_delta_m))),
        )
        vertical_edge_deg = max(
            abs(
                math.degrees(math.atan2(down_delta_m - 2.0, horizontal_range_m))
                - mount_pitch_down_deg
            ),
            abs(
                math.degrees(math.atan2(down_delta_m + 2.0, horizontal_range_m))
                - mount_pitch_down_deg
            ),
        )
        assert horizontal_edge_deg < horizontal_half_fov_deg
        assert vertical_edge_deg < vertical_half_fov_deg


def test_stack_plan_resolves_new_assets_without_expanding_release_matrix() -> None:
    plan = stack_plan_from_scenario(
        MANIFEST,
        repo_root=ROOT,
        tool_capabilities={"gz": False, "px4": False},
    )
    document = plan.to_mapping()
    assert plan.scenario_id == SCENARIO_ID
    assert plan.vehicle_class == "multirotor"
    assert plan.model_name == "x500_mono_cam"
    assert plan.world_path == WORLD.resolve()
    assert plan.map_path == MAP.resolve()
    assert plan.dem_path == DEM.resolve()
    assert document["repository"] == {
        "root": str(ROOT.resolve()),
        "owned": True,
        "network_assets": False,
        "truth_loaded": False,
    }
    assert document["truth_free"] is True
    assert document["runtime"]["dry_run"] is True
    assert document["validation"]["gate_passed"] is True
    assert document["validation"]["sdf_validated"] is True
    assert document["validation"]["map_validated"] is True
    assert document["validation"]["dem_validated"] is True
    assert document["environment"]["PX4_SIM_MODEL"] == "gz_x500_mono_cam"

    assert SCENARIO_ID not in AIRFRAME_SCENARIOS
    assert SCENARIO_ID not in SCENARIO_MODES
    assert set(AIRFRAME_SCENARIOS.values()) == {"fixed-wing", "multirotor", "vtol"}
    assert set(SCENARIO_MODES.values()) == {"fixed-wing", "multirotor", "vtol"}

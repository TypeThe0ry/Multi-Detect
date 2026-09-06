from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_scenario_manifest
from multidetect.simulator.stack import stack_plan_from_scenario
from multidetect.simulator.terrain_truth import (
    SimulatorTerrainTruthError,
    compare_dem_to_sdf_truth,
    load_sdf_terrain_truth,
)
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import load_local_dem_grid
from scripts.run_simulator_terrain_ranging_acceptance import build_record, main

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "terrain_ranging_validation_001"
WORLD = ROOT / "simulation" / "gazebo" / "worlds" / f"{SCENARIO_ID}.sdf"
DEM = ROOT / "simulation" / "terrain" / f"{SCENARIO_ID}.dem.json"
MAP = ROOT / "simulation" / "maps" / f"{SCENARIO_ID}.geojson"
SCENARIO = ROOT / "configs" / "scenarios" / "validation" / f"{SCENARIO_ID}.scenario.json"
SCHEMA = ROOT / "configs" / "schemas" / "simulator-terrain-ranging-evidence.schema.json"
SCRIPT = ROOT / "scripts" / "run_simulator_terrain_ranging_acceptance.py"


def test_sdf_truth_parser_derives_non_flat_enu_plane() -> None:
    truth = load_sdf_terrain_truth(WORLD)

    assert truth.scenario_id == SCENARIO_ID
    assert truth.sdf_version == "1.9"
    assert truth.world_name == "default"
    assert truth.surface_model == "EARTH_WGS84"
    assert truth.world_frame_orientation == "ENU"
    assert truth.origin_latitude_deg == 1.3
    assert truth.origin_longitude_deg == 103.8005
    assert truth.origin_elevation_m == 25.0
    assert truth.north_slope_m_per_m == pytest.approx(0.1, abs=1.0e-15)
    assert truth.east_slope_m_per_m == pytest.approx(0.0, abs=1.0e-15)
    assert truth.is_non_flat is True
    assert truth.elevation_msl(-200.0, 75.0) == pytest.approx(5.0)
    assert truth.elevation_msl(0.0, 0.0) == pytest.approx(25.0)
    assert truth.elevation_msl(200.0, -75.0) == pytest.approx(45.0)
    assert truth.covers(-200.0, -200.0) is True
    assert truth.covers(200.0, 200.0) is True
    assert truth.source_sha256 == hashlib.sha256(WORLD.read_bytes()).hexdigest()
    assert truth.source_bytes == WORLD.stat().st_size


def test_dem_samples_and_seven_rays_match_independent_sdf_truth() -> None:
    truth = load_sdf_terrain_truth(WORLD)
    grid = load_local_dem_grid(DEM)

    result = compare_dem_to_sdf_truth(grid, truth)

    assert result["gate_passed"] is True
    assert result["failure_reasons"] == []
    assert all(result["checks"].values())
    assert result["node_sample_count"] == 25
    assert result["cell_center_sample_count"] == 16
    assert result["total_sample_count"] == 41
    assert result["ray_probe_count"] == 7
    assert result["cross_cell_ray_probe_count"] >= 5
    assert len(result["rays"]) == 7
    assert all(ray["gate_passed"] for ray in result["rays"])
    assert sum(ray["traversed_cell_count"] >= 2 for ray in result["rays"]) >= 5
    assert result["max_errors"]["elevation_m"] <= 1.0e-9
    assert result["max_errors"]["north_slope_m_per_m"] <= 1.0e-12
    assert result["max_errors"]["ray_slant_range_m"] <= 1.0e-7
    assert json.dumps(result, allow_nan=False)


def test_non_flat_assets_are_bound_into_an_isolated_runtime_scenario() -> None:
    manifest = load_scenario_manifest(SCENARIO)
    runtime = manifest.runtime_input_for(RuntimeMode.SIM)
    plan = stack_plan_from_scenario(SCENARIO, repo_root=ROOT)

    assert manifest.scenario_id == SCENARIO_ID
    assert runtime.scenario_manifest_hash == manifest.canonical_hash
    assert len(runtime.scenario_manifest_hash) == 64
    assert runtime.vehicle.model == "px4_multirotor_standard"
    assert runtime.vehicle.airframe_profile_id == "px4_multirotor_gazebo_v1"
    assert runtime.faults == ()
    assert plan.vehicle_class == "multirotor"
    assert plan.world_path == WORLD
    assert plan.map_path == MAP
    assert plan.dem_path == DEM
    assert len(plan.assets_sha256) == 64

    targets = {target.target_id: target for target in manifest.truth.targets}
    north_latitude, north_longitude = wgs84_forward_from_ned(
        latitude_deg=1.3,
        longitude_deg=103.8005,
        north_offset_m=80.0,
        east_offset_m=70.0,
    )
    south_latitude, south_longitude = wgs84_forward_from_ned(
        latitude_deg=1.3,
        longitude_deg=103.8005,
        north_offset_m=-60.0,
        east_offset_m=-100.0,
    )
    assert targets["north-slope-target"].position.latitude_deg == pytest.approx(north_latitude)
    assert targets["north-slope-target"].position.longitude_deg == pytest.approx(north_longitude)
    assert targets["south-slope-target"].position.latitude_deg == pytest.approx(south_latitude)
    assert targets["south-slope-target"].position.longitude_deg == pytest.approx(south_longitude)


def test_hash_bound_evidence_validates_and_keeps_external_claims_closed() -> None:
    record = build_record(ROOT)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(record)
    assert record["gate_passed"] is True
    assert record["deterministic_gate_passed"] is True
    assert record["gate_scope"] == "deterministic_simulator_asset_truth"
    assert record["external_gazebo_runtime_gate_passed"] is False
    assert record["checks"]["release_parity_matches_current_sources"] is True
    assert record["inputs"]["world_sdf"]["path"] == (
        "simulation/gazebo/worlds/terrain_ranging_validation_001.sdf"
    )
    assert record["inputs"]["terrain_dem"]["path"] == (
        "simulation/terrain/terrain_ranging_validation_001.dem.json"
    )
    assert set(record["inputs"]["release_parity"]["hashes"]) == {
        "ui_contract_hash",
        "core_algorithm_hash",
        "task_contract_hash",
        "safety_policy_hash",
        "px4_mission_path_hash",
    }
    assert record["claims"]["non_flat_asset_geometry_truth"] is True
    assert record["claims"]["shared_dem_intersection_matches_sdf_analytic_truth"] is True
    assert record["claims"]["external_gazebo_runtime"] == "not_claimed"
    assert record["claims"]["qgc_visual_acceptance"] == "not_claimed"
    assert record["claims"]["hardware_acceptance"] == "not_claimed"
    assert record["claims"]["outdoor_gnss"] == "not_claimed"
    assert record["claims"]["hil_real_flight"] == "not_claimed"
    assert record["claims"]["final_v0_3_0"] == "not_released"


def test_modified_dem_elevation_fails_geometry_gate(tmp_path: Path) -> None:
    document = json.loads(DEM.read_text(encoding="utf-8"))
    document["elevation_m"][0][0] += 1.0
    modified = tmp_path / DEM.name
    modified.write_text(json.dumps(document), encoding="utf-8")

    result = compare_dem_to_sdf_truth(
        load_local_dem_grid(modified),
        load_sdf_terrain_truth(WORLD),
    )

    assert result["gate_passed"] is False
    assert result["checks"]["dem_elevation_within_tolerance"] is False
    assert result["max_errors"]["elevation_m"] == pytest.approx(1.0)
    assert result["failure_reasons"]


def test_modified_sdf_slope_fails_independent_dem_comparison(tmp_path: Path) -> None:
    text = WORLD.read_text(encoding="utf-8").replace(
        "<pose>0 0 0 0.09966865249116204 0 0</pose>",
        "<pose>0 0 0 0.19739555984988078 0 0</pose>",
        1,
    )
    modified = tmp_path / WORLD.name
    modified.write_text(text, encoding="utf-8")

    result = compare_dem_to_sdf_truth(
        load_local_dem_grid(DEM),
        load_sdf_terrain_truth(modified),
    )

    assert result["gate_passed"] is False
    assert result["checks"]["dem_slope_within_tolerance"] is False
    assert result["checks"]["all_ray_probes_within_tolerance"] is False


def test_modified_sdf_world_origin_fails_binding(tmp_path: Path) -> None:
    text = WORLD.read_text(encoding="utf-8").replace(
        "<latitude_deg>1.300000</latitude_deg>",
        "<latitude_deg>1.310000</latitude_deg>",
        1,
    )
    modified = tmp_path / WORLD.name
    modified.write_text(text, encoding="utf-8")

    result = compare_dem_to_sdf_truth(
        load_local_dem_grid(DEM),
        load_sdf_terrain_truth(modified),
    )

    assert result["gate_passed"] is False
    assert result["checks"]["world_origin_matches_dem"] is False


@pytest.mark.parametrize(
    ("replacement", "match"),
    (
        (
            '<!DOCTYPE sdf [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n<sdf version="1.9">',
            "DTD/entity",
        ),
        (
            '<sdf version="1.9"><world name="default"><include>'
            "<uri>https://example.invalid/model</uri></include></world>",
            "external asset URI",
        ),
    ),
)
def test_truth_loader_rejects_unsafe_xml(
    tmp_path: Path,
    replacement: str,
    match: str,
) -> None:
    modified = tmp_path / WORLD.name
    modified.write_text(replacement, encoding="utf-8")

    with pytest.raises(SimulatorTerrainTruthError, match=match):
        load_sdf_terrain_truth(modified)


def test_acceptance_cli_writes_once_and_refuses_implicit_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "terrain-truth.json"

    assert main(["--root", str(ROOT), "--out", str(output)]) == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == printed
    original = output.read_bytes()

    assert main(["--root", str(ROOT), "--out", str(output)]) == 2
    error = json.loads(capsys.readouterr().out)
    assert "File exists" in error["error"]
    assert output.read_bytes() == original

    assert main(["--root", str(ROOT), "--out", str(output), "--overwrite"]) == 0
    rewritten = json.loads(capsys.readouterr().out)
    assert rewritten == written


def test_acceptance_script_help_runs_without_pythonpath() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--world-sdf" in completed.stdout
    assert "--terrain-dem" in completed.stdout
    assert "--out" in completed.stdout

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.autonomy import AutonomyContractError
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.runtime import RuntimeMode
from multidetect.simulation import (
    ScenarioManifestIndex,
    ScenarioTruth,
    VehicleRuntimeInput,
    VehicleSpawnPose,
    load_runtime_scenario,
    load_scenario_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


def test_river_rescue_scenario_loads_as_strict_runtime_and_truth_channels() -> None:
    manifest = load_scenario_manifest(SCENARIO)
    runtime_input = manifest.runtime_input_for(RuntimeMode.SIM)

    assert manifest.scenario_id == "river_rescue_vtol_001"
    assert runtime_input.seed == 42017
    assert runtime_input.vehicle.airframe_profile_id == "px4_vtol_gazebo_v1"
    assert tuple(fault.event_id for fault in runtime_input.faults) == (
        "camera-loss-001",
        "battery-drop-001",
    )
    assert manifest.truth.targets[0].label == "red_life_jacket"
    assert manifest.expectations.allowed_terminal_states == ("completed", "safe_rtl")
    assert runtime_input.scenario_manifest_hash == manifest.canonical_hash
    assert load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM) == runtime_input


def test_scenario_canonical_hash_is_stable() -> None:
    first = load_scenario_manifest(SCENARIO)
    raw = json.loads(SCENARIO.read_text(encoding="utf-8"))
    reordered = dict(reversed(tuple(raw.items())))

    assert first.canonical_hash == ScenarioManifestIndex.from_mapping(reordered).canonical_hash


def test_vehicle_spawn_pose_is_optional_strict_and_hash_bound() -> None:
    legacy = VehicleRuntimeInput.from_mapping(
        {
            "model": "px4_multirotor_standard",
            "airframe_profile_id": "px4_multirotor_gazebo_v1",
            "spawn_id": "home",
        }
    )
    assert legacy.spawn_pose is None
    assert "spawn_pose" not in legacy.to_mapping()

    pose = VehicleSpawnPose.from_mapping(
        {
            "x_east_m": 1.25,
            "y_north_m": -2.5,
            "z_up_m": 6.0,
            "roll_rad": 0.0,
            "pitch_rad": 0.0,
            "yaw_rad": 1.5707963267948966,
        }
    )
    positioned = VehicleRuntimeInput(
        model=legacy.model,
        airframe_profile_id=legacy.airframe_profile_id,
        spawn_id=legacy.spawn_id,
        spawn_pose=pose,
    )
    assert VehicleRuntimeInput.from_mapping(positioned.to_mapping()) == positioned
    assert canonical_sha256(legacy.to_mapping()) != canonical_sha256(positioned.to_mapping())


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("x_east_m", 10_000.1, "x_east_m must be at most"),
        ("y_north_m", -10_000.1, "y_north_m must be at least"),
        ("z_up_m", -0.01, "z_up_m must be at least"),
        ("roll_rad", 3.15, "roll_rad must be at most"),
        ("pitch_rad", 1.58, "pitch_rad must be at most"),
        ("yaw_rad", -3.15, "yaw_rad must be at least"),
    ),
)
def test_vehicle_spawn_pose_rejects_out_of_contract_values(
    field: str, value: float, error: str
) -> None:
    raw = {
        "x_east_m": 0.0,
        "y_north_m": 0.0,
        "z_up_m": 0.0,
        "roll_rad": 0.0,
        "pitch_rad": 0.0,
        "yaw_rad": 0.0,
    }
    raw[field] = value
    with pytest.raises(AutonomyContractError, match=error):
        VehicleSpawnPose.from_mapping(raw)


def test_vehicle_spawn_pose_rejects_unknown_and_null_values() -> None:
    raw = {
        "model": "px4_multirotor_standard",
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "spawn_id": "home",
        "spawn_pose": None,
    }
    with pytest.raises(AutonomyContractError, match="VehicleSpawnPose must be an object"):
        VehicleRuntimeInput.from_mapping(raw)

    raw["spawn_pose"] = {
        "x_east_m": 0.0,
        "y_north_m": 0.0,
        "z_up_m": 1.0,
        "roll_rad": 0.0,
        "pitch_rad": 0.0,
        "yaw_rad": 0.0,
        "target_hint": 1.0,
    }
    with pytest.raises(AutonomyContractError, match="unknown field"):
        VehicleRuntimeInput.from_mapping(raw)


def test_real_runtime_cannot_load_scenario_or_faults() -> None:
    with pytest.raises(AutonomyContractError, match="REAL"):
        load_runtime_scenario(SCENARIO, mode=RuntimeMode.REAL)


def test_scenario_rejects_unknown_fields(tmp_path: Path) -> None:
    index = json.loads(SCENARIO.read_text(encoding="utf-8"))
    runtime_source = SCENARIO.with_name("river_rescue_vtol_001.runtime.json")
    raw = json.loads(runtime_source.read_text(encoding="utf-8"))
    raw["ground_truth_target"] = {"latitude_deg": 1.0}
    runtime_name = "invalid.runtime.json"
    (tmp_path / runtime_name).write_text(json.dumps(raw), encoding="utf-8")
    index["channels"]["runtime_input"] = {
        "path": runtime_name,
        "sha256": canonical_sha256(raw),
    }
    path = tmp_path / "invalid.scenario.json"
    path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(AutonomyContractError, match="unknown field"):
        load_runtime_scenario(path, mode=RuntimeMode.SIM)


def test_runtime_loader_never_instantiates_truth_or_expectations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("truth parser must not run")

    monkeypatch.setattr(ScenarioTruth, "from_mapping", explode)

    assert load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM).seed == 42017


def test_all_new_schemas_are_valid_strict_json_objects() -> None:
    names = (
        "autonomy-prompt-envelope.schema.json",
        "autonomy-context-snapshot.schema.json",
        "autonomy-resolved-task-spec.schema.json",
        "autonomy-platform-neutral-plan.schema.json",
        "autonomy-executable-mission.schema.json",
        "autonomy-validation-report.schema.json",
        "autonomy-operator-grant.schema.json",
        "autonomy-episode-bundle.schema.json",
        "runtime-descriptor.schema.json",
        "runtime-profile.schema.json",
        "raw-sensor-replay-manifest.schema.json",
        "replay-set-manifest.schema.json",
        "replay-set-calibration-report.schema.json",
        "field-replay-campaign-plan.schema.json",
        "scenario-manifest.schema.json",
        "scenario-runtime-input.schema.json",
        "scenario-truth.schema.json",
        "scenario-expectations.schema.json",
        "simulator-scenario-matrix-evidence.schema.json",
        "simulator-terrain-ranging-evidence.schema.json",
        "actuator-safety-evidence.schema.json",
        "simulator-scene-stream.schema.json",
    )
    for name in names:
        schema = json.loads((ROOT / "configs/schemas" / name).read_text(encoding="utf-8"))
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False

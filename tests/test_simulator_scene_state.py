from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy import AutonomyContractError
from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.mission_progress import MissionProgressPhase
from multidetect.simulator import (
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
    SimulatorSceneSnapshot,
    SimulatorSceneStream,
    scene_stream_from_result,
)
from multidetect.simulator.scene_state import main, verify_file, verify_main

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "configs" / "scenarios"
SCENE_SCHEMA = ROOT / "configs" / "schemas" / "simulator-scene-stream.schema.json"
VTOL_SCENARIO = SCENARIOS / "river_rescue_vtol_001.scenario.json"


def _recorded(path: Path = VTOL_SCENARIO, *, frames: int = 5):
    return SimulatorScenarioRunner(
        path,
        config=SimulatorScenarioRunnerConfig(
            frames=frames,
            dt_s=0.1,
            record_scene_stream=True,
        ),
    ).run()


def _mission_for_scene_stream() -> ExecutableMission:
    return ExecutableMission(
        mission_id="scene-stream-progress-fixture",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
        airframe_adapter_id="scene-stream-fixture-v1",
        airframe_adapter_hash="c" * 64,
        items=(
            MissionItem(
                sequence=0,
                command=MissionCommand.TAKEOFF,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=1.3,
                longitude_deg=103.8005,
                altitude_m=10.0,
            ),
        ),
    )


def test_recorded_scene_stream_binds_runtime_pose_and_truth_free_observation() -> None:
    result = _recorded(frames=5)

    stream = result.scene_stream
    assert stream is not None
    assert scene_stream_from_result(result) is stream
    assert stream.scenario_id == result.scenario_id
    base_result = result.to_mapping()
    base_result.pop("scene_stream")
    expected_source_hash = hashlib.sha256(
        json.dumps(base_result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()
    assert stream.source_evidence_hash == expected_source_hash
    assert len(stream.snapshots) == result.completed_frames == 5
    assert [item.sequence for item in stream.snapshots] == list(range(5))
    assert [item.simulation_time_s for item in stream.snapshots] == sorted(
        item.simulation_time_s for item in stream.snapshots
    )
    first, last = stream.snapshots[0], stream.snapshots[-1]
    assert first.truth_free is True
    assert first.observation["truth_free"] is True
    assert first.pose["latitude_deg"] == pytest.approx(first.telemetry["latitude_deg"])
    assert first.pose["longitude_deg"] == pytest.approx(first.telemetry["longitude_deg"])
    assert last.pose["north_m"] != first.pose["north_m"]
    assert last.pose["east_m"] != first.pose["east_m"]
    assert len(stream.sha256) == 64
    assert all(len(item.sha256) == 64 for item in stream.snapshots)


@pytest.mark.parametrize(
    "name",
    (
        "coastal_patrol_fixed_wing_001.scenario.json",
        "river_rescue_vtol_001.scenario.json",
        "urban_inspection_multirotor_001.scenario.json",
    ),
)
def test_scene_stream_uses_the_same_contract_for_each_airframe(name: str) -> None:
    result = _recorded(SCENARIOS / name, frames=2)
    stream = result.scene_stream
    assert stream is not None
    assert stream.mode == result.mode
    assert all(snapshot.mode == result.mode for snapshot in stream.snapshots)
    assert all(
        snapshot.authority_state in {"paused", "manual", "ai", "failsafe"}
        for snapshot in stream.snapshots
    )


def test_scene_stream_is_deterministic_and_round_trips() -> None:
    first = _recorded(frames=7).scene_stream
    second = _recorded(frames=7).scene_stream
    assert first is not None and second is not None
    assert first.canonical_hash == second.canonical_hash
    assert "mission_progress" not in first.snapshots[0].to_mapping()
    restored = SimulatorSceneStream.from_mapping(first.to_mapping_with_hash())
    assert restored.canonical_hash == first.canonical_hash
    assert restored.replay() == first.replay()


def test_scene_stream_carries_hash_bound_mission_progress() -> None:
    mission = _mission_for_scene_stream()
    runner = SimulatorScenarioRunner(
        VTOL_SCENARIO,
        config=SimulatorScenarioRunnerConfig(
            frames=2,
            dt_s=0.1,
            enable_ai=False,
            record_scene_stream=True,
        ),
        mission=mission,
    )
    assert runner.start_mission() is True
    result = runner.run()
    stream = result.scene_stream
    assert stream is not None
    assert all(snapshot.mission_progress is not None for snapshot in stream.snapshots)
    first = stream.snapshots[0].mission_progress
    assert first is not None
    assert first.mission_id == mission.mission_id
    assert first.mission_hash == mission.canonical_hash
    assert first.phase in {
        MissionProgressPhase.ACTIVE,
        MissionProgressPhase.COMPLETE,
    }

    restored = SimulatorSceneStream.from_mapping(stream.to_mapping_with_hash())
    assert restored.replay() == stream.replay()
    assert restored.snapshots[0].mission_progress == first

    tampered = stream.snapshots[0].to_mapping_with_hash()
    progress = tampered["mission_progress"]
    assert isinstance(progress, dict)
    progress["progress_fraction"] = 0.5
    tampered.pop("sha256")
    with pytest.raises(AutonomyContractError, match="progress_fraction"):
        SimulatorSceneSnapshot.from_mapping(tampered)

    explicit_null = stream.snapshots[0].to_mapping_with_hash()
    explicit_null["mission_progress"] = None
    explicit_null.pop("sha256")
    with pytest.raises(AutonomyContractError, match="MissionProgressSnapshot must be an object"):
        SimulatorSceneSnapshot.from_mapping(explicit_null)


def test_scene_stream_rejects_mixed_or_partial_mission_progress() -> None:
    mission = _mission_for_scene_stream()
    runner = SimulatorScenarioRunner(
        VTOL_SCENARIO,
        config=SimulatorScenarioRunnerConfig(
            frames=2,
            dt_s=0.1,
            enable_ai=False,
            record_scene_stream=True,
        ),
        mission=mission,
    )
    assert runner.start_mission() is True
    stream = runner.run().scene_stream
    assert stream is not None

    mixed = stream.to_mapping_with_hash()
    second = mixed["snapshots"][1]
    assert isinstance(second, dict)
    progress = second["mission_progress"]
    assert isinstance(progress, dict)
    progress["mission_id"] = "another-mission"
    second.pop("sha256")
    mixed.pop("sha256")
    with pytest.raises(AutonomyContractError, match="identity does not match stream"):
        SimulatorSceneStream.from_mapping(mixed)

    partial = stream.to_mapping_with_hash()
    second = partial["snapshots"][1]
    assert isinstance(second, dict)
    second.pop("mission_progress")
    second.pop("sha256")
    partial.pop("sha256")
    with pytest.raises(AutonomyContractError, match="all carry mission_progress"):
        SimulatorSceneStream.from_mapping(partial)


def test_scene_stream_rejects_tampered_snapshot_and_truth() -> None:
    stream = _recorded(frames=2).scene_stream
    assert stream is not None

    tampered = stream.to_mapping_with_hash()
    tampered["snapshots"][0]["pose"]["north_m"] += 1.0  # type: ignore[index]
    with pytest.raises(AutonomyContractError, match="snapshot sha256"):
        SimulatorSceneStream.from_mapping(tampered)

    invalid_observation = stream.snapshots[0].to_mapping_with_hash()
    invalid_observation["observation"]["truth_free"] = False  # type: ignore[index]
    invalid_observation.pop("sha256")
    with pytest.raises(AutonomyContractError, match="observation.truth_free"):
        SimulatorSceneSnapshot.from_mapping(invalid_observation)

    forbidden_truth = stream.snapshots[0].to_mapping_with_hash()
    forbidden_truth["telemetry"]["ground_truth"] = {}  # type: ignore[index]
    forbidden_truth.pop("sha256")
    with pytest.raises(AutonomyContractError, match="forbidden truth field"):
        SimulatorSceneSnapshot.from_mapping(forbidden_truth)

    forbidden_pose_truth = stream.snapshots[0].to_mapping_with_hash()
    forbidden_pose_truth["pose"]["truth"] = {}  # type: ignore[index]
    forbidden_pose_truth.pop("sha256")
    with pytest.raises(AutonomyContractError, match="forbidden truth field"):
        SimulatorSceneSnapshot.from_mapping(forbidden_pose_truth)

    unknown_pose_field = stream.snapshots[0].to_mapping_with_hash()
    unknown_pose_field["pose"]["future_pose_field"] = 1.0  # type: ignore[index]
    unknown_pose_field.pop("sha256")
    with pytest.raises(AutonomyContractError, match="pose has unknown field"):
        SimulatorSceneSnapshot.from_mapping(unknown_pose_field)

    forbidden_origin_truth = stream.to_mapping_with_hash()
    forbidden_origin_truth["world_origin"]["ground_truth"] = {}  # type: ignore[index]
    forbidden_origin_truth.pop("sha256")
    with pytest.raises(AutonomyContractError, match="forbidden truth field"):
        SimulatorSceneStream.from_mapping(forbidden_origin_truth)

    invalid_fault_array = stream.snapshots[0].to_mapping_with_hash()
    invalid_fault_array["active_fault_ids"] = "not-an-array"  # type: ignore[assignment]
    invalid_fault_array.pop("sha256")
    with pytest.raises(AutonomyContractError, match="active_fault_ids must be an array"):
        SimulatorSceneSnapshot.from_mapping(invalid_fault_array)

    duplicate_faults = stream.snapshots[0].to_mapping_with_hash()
    duplicate_faults["active_fault_ids"] = ["fault-a", "fault-a"]  # type: ignore[assignment]
    duplicate_faults.pop("sha256")
    with pytest.raises(AutonomyContractError, match="active_fault_ids must be unique"):
        SimulatorSceneSnapshot.from_mapping(duplicate_faults)

    invalid_release = stream.to_mapping_with_hash()
    invalid_release["release_label"] = "nightly"
    invalid_release.pop("sha256")
    with pytest.raises(AutonomyContractError, match="release_label"):
        SimulatorSceneStream.from_mapping(invalid_release)


def test_default_runner_does_not_change_legacy_result_shape() -> None:
    result = SimulatorScenarioRunner(
        VTOL_SCENARIO,
        config=SimulatorScenarioRunnerConfig(frames=2),
    ).run()
    assert result.scene_stream is None
    assert "scene_stream" not in result.to_mapping()


def test_scene_stream_schema_validates_generated_document() -> None:
    schema = json.loads(SCENE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    stream = _recorded(frames=3).scene_stream
    assert stream is not None
    validator = Draft202012Validator(schema)
    document = stream.to_mapping_with_hash()
    validator.validate(document)

    document["snapshots"][0]["telemetry"]["ground_truth"] = {}  # type: ignore[index]
    assert any(error.validator == "not" for error in validator.iter_errors(document))


def test_scene_stream_cli_writes_hash_bound_document(tmp_path: Path) -> None:
    output = tmp_path / "scene-stream.json"
    exit_code = main(
        [
            "--scenario",
            str(VTOL_SCENARIO),
            "--frames",
            "2",
            "--out",
            str(output),
        ]
    )
    assert exit_code == 0
    assert output.exists()
    loaded = SimulatorSceneStream.from_mapping(json.loads(output.read_text(encoding="utf-8")))
    assert len(loaded.snapshots) == 2
    assert SimulatorSceneStream.load_json(output).canonical_hash == loaded.canonical_hash


def test_scene_stream_cli_binds_and_starts_mission_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(
        json.dumps(_mission_for_scene_stream().to_mapping()), encoding="utf-8", newline="\n"
    )
    output = tmp_path / "mission-scene-stream.json"
    code = main(
        [
            "--scenario",
            str(VTOL_SCENARIO),
            "--mission",
            str(mission_path),
            "--start-mission",
            "--frames",
            "1",
            "--no-ai",
            "--out",
            str(output),
        ]
    )
    assert code == 0
    # ``main`` prints the stream as well as writing it, so consume the output
    # to keep the test runner's capture buffer deterministic.
    capsys.readouterr()
    loaded = SimulatorSceneStream.load_json(output)
    assert loaded.snapshots[0].mission_progress is not None
    assert loaded.snapshots[0].mission_progress.mission_id == ("scene-stream-progress-fixture")

    ready_output = tmp_path / "mission-scene-stream-ready.json"
    ready_code = main(
        [
            "--scenario",
            str(VTOL_SCENARIO),
            "--mission",
            str(mission_path),
            "--frames",
            "1",
            "--no-ai",
            "--out",
            str(ready_output),
        ]
    )
    capsys.readouterr()
    assert ready_code == 0
    ready = SimulatorSceneStream.load_json(ready_output)
    ready_progress = ready.snapshots[0].mission_progress
    assert ready_progress is not None
    assert ready_progress.phase is MissionProgressPhase.READY
    assert ready_progress.mission_started is False


def test_scene_stream_load_json_rejects_duplicate_keys_and_nonfinite_numbers(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="duplicate JSON key"):
        SimulatorSceneStream.load_json(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"simulation_time_s": NaN}', encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="non-finite JSON number"):
        SimulatorSceneStream.load_json(nonfinite)


def test_scene_stream_verifier_reports_file_and_canonical_hashes(tmp_path: Path) -> None:
    source = tmp_path / "stream.json"
    stream = _recorded(frames=3).scene_stream
    assert stream is not None
    stream.write_json(source)

    report = verify_file(source, expected_release_label="0.3.0-pre.8")
    assert report["gate_passed"] is True
    assert report["file_bytes"] == source.stat().st_size
    assert report["file_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["canonical_sha256"] == stream.canonical_hash
    assert report["checks"]["json_schema"] is True  # type: ignore[index]

    output = tmp_path / "verification.json"
    assert verify_main([str(source), "--out", str(output)]) == 0
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["gate_passed"] is True

    mismatch = verify_file(source, expected_release_label="0.3.0")
    assert mismatch["gate_passed"] is False
    assert any("does not match expected" in reason for reason in mismatch["reasons"])

    malformed_schema = tmp_path / "malformed-schema.json"
    malformed_schema.write_text('{"type": "not-a-json-schema-type"}', encoding="utf-8")
    schema_failure = verify_file(source, schema_path=malformed_schema)
    assert schema_failure["gate_passed"] is False
    assert schema_failure["checks"]["json_schema"] is False  # type: ignore[index]
    assert schema_failure["schema_errors"]  # type: ignore[index]

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema

from multidetect.release_gate import _px4_reported_position_response_matrix_result
from multidetect.release_gate_manifest import (
    _px4_reported_position_response_matrix_record,
)
from scripts.run_px4_reported_position_response_matrix import (
    EXPECTED_AIRFRAMES,
    EXPECTED_CONTROL_PATH,
    build_matrix,
    main,
)
from scripts.verify_px4_reported_position_response_matrix import verify_file

ROOT = Path(__file__).resolve().parents[1]


def _write_parity(root: Path) -> Path:
    path = root / "configs" / "release-parity.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0-pre.8",
                "parity": {
                    "ui_contract_hash": "1" * 64,
                    "core_algorithm_hash": "2" * 64,
                    "task_contract_hash": "3" * 64,
                    "safety_policy_hash": "4" * 64,
                    "px4_mission_path_hash": "5" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_sources(root: Path) -> list[Path]:
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in (
        "run_simulator_docker_ai_acceptance.py",
        "run_px4_reported_position_response_matrix.py",
        "verify_px4_reported_position_response_matrix.py",
    ):
        (scripts / name).write_text(f"# bound fixture {name}\n", encoding="utf-8")
    sources: list[Path] = []
    for index, (vehicle_class, expected) in enumerate(EXPECTED_AIRFRAMES.items()):
        scenario = root / expected["scenario_path"]
        scenario.parent.mkdir(parents=True, exist_ok=True)
        scenario.write_text(json.dumps({"vehicle_class": vehicle_class}), encoding="utf-8")
        scenario_sha = hashlib.sha256(scenario.read_bytes()).hexdigest()
        document = {
            "schema": "multidetect.simulator-docker-acceptance",
            "schema_version": 1,
            "execution_requested": True,
            "gate_passed": True,
            "runtime_gate_passed": True,
            "ai_policy_gate_passed": True,
            "target_policy_gate_passed": True,
            "flight_response_gate_passed": True,
            "flight_response_gate_status": "passed",
            "scenario": {
                "scenario_id": expected["scenario_id"],
                "airframe_profile_id": expected["airframe_profile_id"],
                "manifest_sha256": scenario_sha,
                "scenario_manifest_hash": str(index + 6) * 64,
                "truth_loaded_by_runtime": False,
                "expectations_loaded_by_runtime": False,
            },
            "plan": {
                "vehicle": {
                    "model": expected["vehicle_model"],
                    "model_name": expected["model_name"],
                },
                "environment": {"PX4_SYS_AUTOSTART": expected["px4_sys_autostart"]},
            },
            "container_runtime": {
                "status": "passed",
                "container_running_observed": True,
                "image": "px4@test@sha256:" + "a" * 64,
                "image_id": "sha256:" + "a" * 64,
                "cleanup": {"attempted": True, "removed": True, "errors": []},
                "inspect_checks": {"pinned_image": True, "not_privileged": True},
            },
            "flight_exercise": {
                "requested": True,
                "gate_passed": True,
                "status": "passed",
                "completed": True,
                "position_source": "MAVLink GLOBAL_POSITION_INT",
                "control_path": EXPECTED_CONTROL_PATH,
                "target_refresh_gate_passed": True,
                "heartbeat_continuity_gate_passed": True,
                "flight_mode_gate_passed": True,
                "flight_mode": {"observed": True, "loss_events": 0, "px4_main_mode": "POSCTL"},
                "gazebo_truth_consumed": False,
                "gazebo_pose_injection_used": False,
                "synthetic_perception_input": True,
                "target_updates_accepted": 60,
                "target_updates_rejected": 0,
                "maximum_horizontal_displacement_m": 2.0,
                "minimum_horizontal_displacement_m": 0.5,
                "elapsed_duration_s": 8.0,
                "maximum_target_refresh_gap_s": 0.2,
                "maximum_heartbeat_gap_s": 1.5,
                "position_sample_count": 120,
            },
            "hardware": {"hardware_control_enabled": False, "real_pixhawk_contacted": False},
            "claims": {"px4_sim_vehicle_motion": "passed", "hardware": "not_claimed"},
            "safety": {"hardware_control_enabled": False},
        }
        source = root / "artifacts" / f"{vehicle_class}.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(document), encoding="utf-8")
        sources.append(source)
    return sources


def test_three_airframe_response_matrix_is_source_bound_and_reproducible(tmp_path: Path) -> None:
    _write_parity(tmp_path)
    sources = _write_sources(tmp_path)
    document = build_matrix(sources, repository_root=tmp_path)
    assert document["gate_passed"] is True
    assert [entry["vehicle_class"] for entry in document["entries"]] == [
        "fixed-wing",
        "multirotor",
        "vtol",
    ]
    assert all(document["shared_checks"].values())
    output = tmp_path / "matrix.json"
    output.write_text(json.dumps(document), encoding="utf-8")
    assert verify_file(output, repository_root=tmp_path)["gate_passed"] is True
    schema = json.loads(
        (ROOT / "configs/schemas/px4-reported-position-response-matrix.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(document, schema)


def test_matrix_fails_closed_when_one_source_is_tampered(tmp_path: Path) -> None:
    _write_parity(tmp_path)
    sources = _write_sources(tmp_path)
    document = build_matrix(sources, repository_root=tmp_path)
    output = tmp_path / "matrix.json"
    output.write_text(json.dumps(document), encoding="utf-8")
    tampered = json.loads(sources[0].read_text(encoding="utf-8"))
    tampered["flight_exercise"]["gazebo_pose_injection_used"] = True
    sources[0].write_text(json.dumps(tampered), encoding="utf-8")
    result = verify_file(output, repository_root=tmp_path)
    assert result["gate_passed"] is False
    assert result["checks"]["entries"] is False


def test_matrix_rejects_duplicate_airframes_and_cli_writes_failure(tmp_path: Path) -> None:
    _write_parity(tmp_path)
    sources = _write_sources(tmp_path)
    duplicate = copy.deepcopy(json.loads(sources[0].read_text(encoding="utf-8")))
    sources[1].write_text(json.dumps(duplicate), encoding="utf-8")
    document = build_matrix(sources, repository_root=tmp_path)
    assert document["gate_passed"] is False
    assert document["shared_checks"]["vehicle_classes_complete"] is False
    output = tmp_path / "matrix.json"
    assert (
        main(
            [
                *(argument for source in sources for argument in ("--source", str(source))),
                "--out",
                str(output),
                "--repository-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert output.is_file()


def test_release_gate_binding_reopens_matrix_and_fails_closed_on_tamper(
    tmp_path: Path, monkeypatch
) -> None:
    _write_parity(tmp_path)
    sources = _write_sources(tmp_path)
    matrix = tmp_path / "matrix.json"
    document = build_matrix(sources, repository_root=tmp_path)
    matrix.write_text(json.dumps(document), encoding="utf-8")
    verification = verify_file(matrix, repository_root=tmp_path)
    monkeypatch.setattr(
        "scripts.verify_px4_reported_position_response_matrix.verify_file",
        lambda _path: verification,
    )
    manifest = tmp_path / "release-gate.json"
    record = _px4_reported_position_response_matrix_record(
        matrix,
        output_identity=manifest,
        manifest_directory=tmp_path,
        release_label="0.3.0-pre.8",
    )
    result = _px4_reported_position_response_matrix_result(
        record,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert result["passed"] is True
    matrix.write_text(matrix.read_text(encoding="utf-8") + " ", encoding="utf-8")
    tampered = _px4_reported_position_response_matrix_result(
        record,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert tampered["passed"] is False
    assert any("SHA-256 mismatch" in reason for reason in tampered["reasons"])

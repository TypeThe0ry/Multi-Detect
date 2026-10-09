from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema

from multidetect.release_gate import _px4_reported_position_response_campaign_result
from multidetect.release_gate_manifest import (
    _px4_reported_position_response_campaign_record,
)
from scripts.run_px4_reported_position_response_campaign import (
    EXPECTED_FAULT_STATUS,
    build_campaign,
)
from scripts.run_px4_reported_position_response_matrix import (
    EXPECTED_AIRFRAMES,
    EXPECTED_CONTROL_PATH,
)
from scripts.verify_px4_reported_position_response_campaign import verify_file

ROOT = Path(__file__).resolve().parents[1]


def _workspace(root: Path) -> tuple[list[Path], list[Path]]:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "run_simulator_docker_ai_acceptance.py",
        "run_px4_reported_position_response_campaign.py",
        "verify_px4_reported_position_response_campaign.py",
    ):
        (scripts / name).write_text(f"# {name}\n", encoding="utf-8")
    parity = root / "configs/release-parity.json"
    parity.parent.mkdir(parents=True)
    parity.write_text(
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
    positives: list[Path] = []
    faults: list[Path] = []
    for class_index, (vehicle_class, expected) in enumerate(EXPECTED_AIRFRAMES.items()):
        scenario = root / expected["scenario_path"]
        scenario.parent.mkdir(parents=True, exist_ok=True)
        scenario.write_text(json.dumps({"vehicle_class": vehicle_class}), encoding="utf-8")
        scenario_sha = hashlib.sha256(scenario.read_bytes()).hexdigest()
        base = {
            "schema": "multidetect.simulator-docker-acceptance",
            "schema_version": 1,
            "execution_requested": True,
            "runtime_gate_passed": True,
            "scenario": {
                "scenario_id": expected["scenario_id"],
                "airframe_profile_id": expected["airframe_profile_id"],
                "manifest_sha256": scenario_sha,
                "scenario_manifest_hash": str(class_index + 6) * 64,
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
                "image": "px4@sha256:" + "a" * 64,
                "image_id": "sha256:" + "a" * 64,
                "cleanup": {"attempted": True, "removed": True, "errors": []},
                "inspect_checks": {"pinned": True},
            },
            "hardware": {"hardware_control_enabled": False, "real_pixhawk_contacted": False},
            "safety": {"hardware_control_enabled": False},
        }
        for repeat in range(2):
            document = copy.deepcopy(base)
            document.update(
                {
                    "gate_passed": True,
                    "ai_policy_gate_passed": True,
                    "target_policy_gate_passed": True,
                    "flight_response_gate_passed": True,
                    "flight_response_gate_status": "passed",
                    "claims": {"px4_sim_vehicle_motion": "passed", "hardware": "not_claimed"},
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
                        "flight_mode": {
                            "observed": True,
                            "loss_events": 0,
                            "px4_main_mode": "POSCTL",
                        },
                        "gazebo_truth_consumed": False,
                        "gazebo_pose_injection_used": False,
                        "synthetic_perception_input": True,
                        "target_updates_accepted": 60 + repeat,
                        "target_updates_rejected": 0,
                        "maximum_horizontal_displacement_m": 2.0 + repeat,
                        "minimum_horizontal_displacement_m": 0.5,
                        "elapsed_duration_s": 8.0 + repeat / 10,
                        "maximum_target_refresh_gap_s": 0.2,
                        "maximum_heartbeat_gap_s": 1.2,
                        "position_sample_count": 100 + repeat,
                        "fault_injection": {"requested": False, "name": "none"},
                    },
                }
            )
            path = root / "artifacts" / f"{vehicle_class}-positive-{repeat}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document), encoding="utf-8")
            positives.append(path)
        for fault_name, status in EXPECTED_FAULT_STATUS.items():
            document = copy.deepcopy(base)
            document.update(
                {
                    "gate_passed": False,
                    "ai_policy_gate_passed": False,
                    "target_policy_gate_passed": True,
                    "flight_response_gate_passed": False,
                    "flight_response_gate_status": status,
                    "claims": {"px4_sim_vehicle_motion": "not_observed", "hardware": "not_claimed"},
                    "flight_exercise": {
                        "requested": True,
                        "gate_passed": False,
                        "status": status,
                        "position_source": "MAVLink GLOBAL_POSITION_INT",
                        "control_path": EXPECTED_CONTROL_PATH,
                        "gazebo_truth_consumed": False,
                        "gazebo_pose_injection_used": False,
                        "fault_injection": {
                            "requested": True,
                            "name": fault_name,
                            "activated": True,
                            "observed": True,
                            "fail_closed": True,
                            "scope": "disposable_loopback_px4_sitl_only",
                        },
                    },
                }
            )
            path = root / "artifacts" / f"{vehicle_class}-fault-{fault_name}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            faults.append(path)
    return positives, faults


def test_campaign_requires_repeatability_and_complete_fault_coverage(tmp_path: Path) -> None:
    positives, faults = _workspace(tmp_path)
    document = build_campaign(positives, faults, repository_root=tmp_path)
    assert document["gate_passed"] is True
    assert len(document["positive_runs"]) == 6
    assert len(document["fault_runs"]) == 9
    assert all(document["shared_checks"].values())
    assert all(row["repeat_count"] == 2 for row in document["statistics"].values())
    assert document["statistics"]["fixed-wing"]["metrics"]["maximum_horizontal_displacement_m"] == {
        "minimum": 2.0,
        "p50": 2.5,
        "p95": 2.95,
        "maximum": 3.0,
    }
    schema = json.loads(
        (ROOT / "configs/schemas/px4-reported-position-response-campaign.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(document, schema)
    output = tmp_path / "campaign.json"
    output.write_text(json.dumps(document), encoding="utf-8")
    assert verify_file(output, repository_root=tmp_path)["gate_passed"] is True


def test_campaign_fails_when_repeat_is_duplicated_or_fault_is_missing(tmp_path: Path) -> None:
    positives, faults = _workspace(tmp_path)
    document = build_campaign(
        [positives[0], positives[0], *positives[2:]],
        faults[:-1],
        repository_root=tmp_path,
    )
    assert document["gate_passed"] is False
    assert document["shared_checks"]["positive_sources_unique"] is False
    assert document["shared_checks"]["fault_coverage_complete"] is False


def test_campaign_verifier_reopens_every_source(tmp_path: Path) -> None:
    positives, faults = _workspace(tmp_path)
    output = tmp_path / "campaign.json"
    output.write_text(
        json.dumps(build_campaign(positives, faults, repository_root=tmp_path)),
        encoding="utf-8",
    )
    changed = json.loads(faults[0].read_text(encoding="utf-8"))
    changed["flight_exercise"]["fault_injection"]["fail_closed"] = False
    faults[0].write_text(json.dumps(changed), encoding="utf-8")
    verification = verify_file(output, repository_root=tmp_path)
    assert verification["gate_passed"] is False
    assert verification["checks"]["fault_runs"] is False


def test_campaign_verifier_rejects_fault_source_bound_to_stale_scenario_manifest(
    tmp_path: Path,
) -> None:
    positives, faults = _workspace(tmp_path)
    output = tmp_path / "campaign.json"
    output.write_text(
        json.dumps(build_campaign(positives, faults, repository_root=tmp_path)),
        encoding="utf-8",
    )
    scenario_path = tmp_path / EXPECTED_AIRFRAMES["fixed-wing"]["scenario_path"]
    scenario_path.write_text('{"vehicle_class":"fixed-wing","changed":true}', encoding="utf-8")
    verification = verify_file(output, repository_root=tmp_path)
    assert verification["gate_passed"] is False
    assert verification["checks"]["fault_runs"] is False


def test_release_gate_binds_and_reverifies_campaign(tmp_path: Path, monkeypatch) -> None:
    positives, faults = _workspace(tmp_path)
    campaign = tmp_path / "campaign.json"
    campaign.write_text(
        json.dumps(build_campaign(positives, faults, repository_root=tmp_path)),
        encoding="utf-8",
    )
    verification = verify_file(campaign, repository_root=tmp_path)
    monkeypatch.setattr(
        "scripts.verify_px4_reported_position_response_campaign.verify_file",
        lambda _path: verification,
    )
    manifest = tmp_path / "release-gate.json"
    record = _px4_reported_position_response_campaign_record(
        campaign,
        output_identity=manifest,
        manifest_directory=tmp_path,
        release_label="0.3.0-pre.8",
    )
    result = _px4_reported_position_response_campaign_result(
        record,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert result["passed"] is True
    campaign.write_text(campaign.read_text(encoding="utf-8") + " ", encoding="utf-8")
    tampered = _px4_reported_position_response_campaign_result(
        record,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert tampered["passed"] is False
    assert any("SHA-256 mismatch" in reason for reason in tampered["reasons"])

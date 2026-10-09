from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from scripts.create_simulator_ai_flight_parity_receipt import build_receipt

_HASHES = {
    "core_algorithm_hash": "a" * 64,
    "px4_mission_path_hash": "b" * 64,
    "safety_policy_hash": "c" * 64,
    "task_contract_hash": "d" * 64,
    "ui_contract_hash": "e" * 64,
}
_AIRFRAME_HASH = "f" * 64
_SCENARIO_MANIFEST_HASH = "1" * 64
_POLICY_HASH = "2" * 64
_RANKER_HASH = "3" * 64


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create a complete, self-contained synthetic receipt chain."""

    runtime = _write_json(
        tmp_path / "runtime-evidence.json",
        {
            "schema": "multidetect.vtol-selected-campaign",
            "runtime_identity": {
                "airframe_profile_sha256": _AIRFRAME_HASH,
                "airframe_profile": {
                    "airframe_profile_id": "fixture_vtol_profile",
                    "model_name": "fixture-vtol",
                    "vehicle_model": "vtol",
                },
            },
        },
    )
    parity = _write_json(
        tmp_path / "release-parity.json",
        {"release_label": "0.3.0-pre.8", "parity": _HASHES},
    )
    policy_unsigned = {
        "schema": "multidetect.shared-policy-campaign-receipt",
        "schema_version": 1,
        "producer": "create_shared_policy_campaign_receipt",
        "evidence_scope": "retained_files_audit",
        "runtime_source_parity_verified": False,
        "external_sim_rerun_performed": False,
        "release_parity": {
            "verified": True,
            "file_sha256": hashlib.sha256(parity.read_bytes()).hexdigest(),
            "hashes": _HASHES,
            "release_label": "0.3.0-pre.8",
        },
        "runtime_binding": {
            "verified": True,
            "runtime_mode": "SIM",
            "scenario_id": "fixture_scenario",
            "scenario_manifest_sha256": _SCENARIO_MANIFEST_HASH,
            "airframe_profile_sha256": _AIRFRAME_HASH,
        },
        "cleanup": {
            "verified": True,
            "runtime_evidence_path": str(runtime),
            "runtime_evidence_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        },
        "session_id": "fixture-session",
        "shared_policy_used": True,
        "policy_artifact_sha256": _POLICY_HASH,
        "ranker_sha256": _RANKER_HASH,
        "planner_revalidated": True,
        "truth_free": True,
        "policy_applied_to_actuator": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    policy_unsigned["receipt_sha256"] = canonical_sha256(policy_unsigned)
    policy = _write_json(tmp_path / "shared-policy.receipt.json", policy_unsigned)

    ai = _write_json(
        tmp_path / "ai-flight.json",
        {
            "schema": "multidetect.simulator-docker-acceptance",
            "schema_version": 1,
            "gate_passed": True,
            "preflight_gate_passed": True,
            "runtime_gate_passed": True,
            "ai_policy_gate_passed": True,
            "target_policy_gate_passed": True,
            "flight_response_gate_passed": True,
            "scenario": {
                "scenario_id": "fixture_scenario",
                "manifest_sha256": _SCENARIO_MANIFEST_HASH,
                "airframe_profile_id": "fixture_vtol_profile",
                "model_name": "fixture-vtol",
                "vehicle_model": "vtol",
            },
            "flight_exercise": {
                "status": "passed",
                "gate_passed": True,
                "position_sample_count": 3,
                "minimum_horizontal_displacement_m": 1.0,
                "final_horizontal_displacement_m": 2.0,
                "maximum_horizontal_displacement_m": 2.5,
                "gazebo_truth_consumed": False,
                "gazebo_pose_injection_used": False,
                "target_updates_accepted": 1,
            },
            "target_exercise": {"accepted": True, "truth_free": True},
            "safety": {
                "truth_free": True,
                "hardware_control_enabled": False,
                "real_pixhawk_contacted": False,
            },
            "hardware": {"status": "not_claimed", "real_pixhawk_contacted": False},
            "docker": {"cleanup": {"attempted": True, "removed": True, "errors": []}},
            "container_runtime": {
                "cleanup": {"attempted": True, "removed": True, "errors": []}
            },
            "ai_steps": 3,
            "ai_policy_steps": 2,
        },
    )
    return ai, policy, parity, runtime


def test_binds_synthetic_layers_without_claiming_runtime_parity(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    receipt = build_receipt(ai, policy, parity, tmp_path / "composite.receipt.json")

    assert receipt["schema_version"] == 1
    assert receipt["evidence_scope"] == "retained_layers_file_audit"
    assert receipt["release_parity"]["scope"] == "receipt_generation_file_binding"
    assert receipt["bindings"]["same_release_parity"] is False
    assert receipt["bindings"]["same_audit_reference_parity"] is True
    assert receipt["bindings"]["current_runtime_parity_proven"] is False
    assert receipt["bindings"]["external_sim_rerun_performed"] is False
    assert receipt["shared_policy_mission_layer"]["runtime_source_parity_verified"] is False
    assert receipt["receipt_sha256"] == canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def test_missing_runtime_source_evidence_cannot_be_upgraded_to_true(tmp_path: Path) -> None:
    ai, policy, parity, runtime = _fixture(tmp_path)
    runtime.unlink()

    with pytest.raises(ValueError, match="runtime evidence SHA-256 binding"):
        build_receipt(ai, policy, parity, tmp_path / "missing-runtime.receipt.json")


def test_current_audit_parity_rebuild_does_not_claim_runtime_parity(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    receipt = build_receipt(ai, policy, parity, tmp_path / "audit.receipt.json")

    assert receipt["release_parity"]["scope"] == "receipt_generation_file_binding"
    assert receipt["bindings"]["same_release_parity"] is False
    assert receipt["bindings"]["current_runtime_parity_proven"] is False


def test_stable_ai_runtime_parity_snapshot_is_recorded(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    document = json.loads(ai.read_text(encoding="utf-8"))
    parity_snapshot = {
        "bytes": parity.stat().st_size,
        "file_sha256": hashlib.sha256(parity.read_bytes()).hexdigest(),
        "hashes": _HASHES,
        "release_label": "0.3.0-pre.8",
    }
    snapshot = {
        "requested": True,
        "stable": True,
        "error": None,
        "start": parity_snapshot,
    }
    snapshot["end"] = dict(snapshot["start"])
    document["release_parity_snapshot"] = snapshot
    ai.write_text(json.dumps(document), encoding="utf-8")

    receipt = build_receipt(ai, policy, parity, tmp_path / "snapshot.receipt.json")
    assert receipt["bindings"]["simulator_runtime_parity_snapshot_verified"] is True
    assert receipt["simulator_ai_flight_layer"]["runtime_parity_snapshot_verified"] is True


def test_stable_but_stale_ai_runtime_parity_snapshot_is_not_upgraded(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    document = json.loads(ai.read_text(encoding="utf-8"))
    stale_snapshot = {
        "bytes": parity.stat().st_size,
        "file_sha256": "0" * 64,
        "hashes": _HASHES,
        "release_label": "0.3.0-pre.8",
    }
    document["release_parity_snapshot"] = {
        "requested": True,
        "stable": True,
        "error": None,
        "start": stale_snapshot,
        "end": dict(stale_snapshot),
    }
    ai.write_text(json.dumps(document), encoding="utf-8")

    receipt = build_receipt(ai, policy, parity, tmp_path / "stale-snapshot.receipt.json")

    assert receipt["bindings"]["simulator_runtime_parity_snapshot_verified"] is False
    assert receipt["bindings"]["same_release_parity"] is False
    assert receipt["bindings"]["current_runtime_parity_proven"] is False
    assert receipt["simulator_ai_flight_layer"]["runtime_parity_snapshot_verified"] is False


def test_stable_but_stale_policy_runtime_parity_snapshot_is_not_upgraded(
    tmp_path: Path,
) -> None:
    ai, policy, parity, runtime = _fixture(tmp_path)
    runtime_document = json.loads(runtime.read_text(encoding="utf-8"))
    stale_snapshot = {
        "bytes": parity.stat().st_size,
        "file_sha256": "0" * 64,
        "hashes": _HASHES,
        "release_label": "0.3.0-pre.8",
    }
    runtime_document["campaign"] = {
        "release_parity_snapshot": {
            "requested": True,
            "stable": True,
            "error": None,
            "start": stale_snapshot,
            "end": dict(stale_snapshot),
        }
    }
    runtime.write_text(json.dumps(runtime_document), encoding="utf-8")

    policy_document = json.loads(policy.read_text(encoding="utf-8"))
    policy_document["runtime_parity_snapshot_verified"] = True
    policy_document["cleanup"]["runtime_evidence_sha256"] = hashlib.sha256(
        runtime.read_bytes()
    ).hexdigest()
    policy_document["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in policy_document.items() if key != "receipt_sha256"}
    )
    policy.write_text(json.dumps(policy_document), encoding="utf-8")

    receipt = build_receipt(ai, policy, parity, tmp_path / "stale-policy-snapshot.receipt.json")

    assert receipt["shared_policy_mission_layer"]["runtime_parity_snapshot_verified"] is False
    assert receipt["bindings"]["shared_policy_runtime_parity_snapshot_verified"] is False
    assert receipt["bindings"]["same_release_parity"] is False
    assert receipt["bindings"]["current_runtime_parity_proven"] is False


def test_unstable_ai_runtime_parity_snapshot_is_rejected(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    document = json.loads(ai.read_text(encoding="utf-8"))
    document["release_parity_snapshot"] = {
        "requested": True,
        "stable": False,
        "error": "parity drift",
        "start": {"file_sha256": "a" * 64},
        "end": {"file_sha256": "b" * 64},
    }
    ai.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="release parity snapshot"):
        build_receipt(ai, policy, parity, tmp_path / "unstable-snapshot.receipt.json")


def test_runtime_evidence_tamper_is_rejected_by_file_hash(tmp_path: Path) -> None:
    ai, policy, parity, runtime = _fixture(tmp_path)
    tampered = json.loads(runtime.read_text(encoding="utf-8"))
    tampered["tampered"] = True
    runtime.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime evidence SHA-256 binding"):
        build_receipt(ai, policy, parity, tmp_path / "tampered-runtime.receipt.json")


def test_rejects_ai_report_with_different_scenario(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    tampered = json.loads(ai.read_text(encoding="utf-8"))
    tampered["scenario"]["scenario_id"] = "different_scenario"
    ai.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ValueError, match="scenario ID"):
        build_receipt(ai, policy, parity, tmp_path / "different-scenario.receipt.json")


def test_rejects_policy_receipt_with_invalid_hash(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    tampered = json.loads(policy.read_text(encoding="utf-8"))
    tampered["planner_revalidated"] = False
    policy.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ValueError, match="receipt_sha256"):
        build_receipt(ai, policy, parity, tmp_path / "tampered-policy.receipt.json")

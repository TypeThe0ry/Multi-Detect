from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.task_ranker import TaskRanker
from scripts.verify_shared_policy_runtime_handoff import (
    SharedPolicyRuntimeHandoffError,
    build_handoff,
)


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> dict[str, Path | str]:
    airframe = "a" * 64
    ranker = TaskRanker((0, 0, 0), "handoff fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="handoff",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=airframe,
        mission_contract_hash="b" * 64,
        safety_policy_hash="c" * 64,
    )
    artifact_path = _write(tmp_path / "policy.json", artifact.to_mapping())
    parity_hashes = {
        "core_algorithm_hash": "1" * 64,
        "px4_mission_path_hash": "2" * 64,
        "safety_policy_hash": "3" * 64,
        "task_contract_hash": "4" * 64,
        "ui_contract_hash": "5" * 64,
    }
    parity_path = _write(
        tmp_path / "release-parity.json",
        {
            "release_label": "0.3.0-pre.8",
            "schema_version": 1,
            "parity": parity_hashes,
        },
    )
    receipt_unsigned = {
        "schema": "multidetect.shared-policy-campaign-receipt",
        "schema_version": 1,
        "shared_policy_used": True,
        "truth_free": True,
        "planner_revalidated": True,
        "policy_applied_to_actuator": False,
        "final_release_allowed": False,
        "policy_artifact_sha256": artifact.canonical_hash,
        "ranker_sha256": ranker.canonical_hash,
        "runtime_binding": {
            "verified": True,
            "runtime_mode": "SIM",
            "airframe_profile_sha256": airframe,
        },
        "release_parity": {
            "verified": True,
            "release_label": "0.3.0-pre.8",
            "hashes": parity_hashes,
        },
    }
    receipt = {**receipt_unsigned, "receipt_sha256": canonical_sha256(receipt_unsigned)}
    receipt_path = _write(tmp_path / "receipt.json", receipt)
    binding_unsigned = {
        "schema": "multidetect.live-runtime-observation-mission-binding",
        "schema_version": 1,
        "event": "live_runtime_observation_mission_binding_verified",
        "runtime_mode": "hil",
        "mission": {"mission_hash": "6" * 64, "airframe_profile_hash": airframe},
        "live_observation": {
            "runtime_descriptor_hash": "7" * 64,
            "runtime_input_hash": "8" * 64,
        },
        "checks": {"all_outputs_locked": True, "mode_matches": True},
        "gate_passed": True,
        "claims": {
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "flight_acceptance": False,
            "operator_visual_acceptance": False,
        },
        "claim_boundary": {"release_eligible": False},
    }
    binding = {
        **binding_unsigned,
        "evidence_sha256": canonical_sha256(binding_unsigned),
    }
    binding_path = _write(tmp_path / "binding.json", binding)
    return {
        "receipt": receipt_path,
        "binding": binding_path,
        "artifact": artifact_path,
        "artifact_file_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "parity": parity_path,
    }


def test_handoff_binds_policy_parity_airframe_and_fail_closed_target(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = build_handoff(
        fixture["receipt"],
        fixture["binding"],
        fixture["artifact"],
        fixture["parity"],
        policy_artifact_file_sha256=fixture["artifact_file_sha256"],
    )
    assert result["schema"] == "multidetect.shared-policy-runtime-handoff"
    assert result["gate_passed"] is True
    assert result["source"]["runtime_mode"] == "SIM"
    assert result["target"]["runtime_mode"] == "hil"
    assert result["checks"]["policy_artifact_identity_matches"] is True
    assert result["checks"]["release_parity_identity_matches"] is True
    assert result["claims"]["hil_acceptance"] is False
    assert result["claims"]["final_release_allowed"] is False


@pytest.mark.parametrize("tamper", ["receipt", "binding", "artifact"])
def test_handoff_rejects_identity_tampering(tmp_path: Path, tamper: str) -> None:
    fixture = _fixture(tmp_path)
    target = Path(fixture[tamper])
    raw = json.loads(target.read_text(encoding="utf-8"))
    if tamper == "receipt":
        raw["policy_artifact_sha256"] = "f" * 64
        raw["receipt_sha256"] = canonical_sha256(
            {key: value for key, value in raw.items() if key != "receipt_sha256"}
        )
    elif tamper == "binding":
        raw["mission"]["airframe_profile_hash"] = "e" * 64
        raw["evidence_sha256"] = canonical_sha256(
            {key: value for key, value in raw.items() if key != "evidence_sha256"}
        )
    else:
        raw["airframe_profile_hash"] = "d" * 64
    target.write_text(json.dumps(raw), encoding="utf-8")
    if tamper == "artifact":
        with pytest.raises(SharedPolicyRuntimeHandoffError):
            build_handoff(
                fixture["receipt"],
                fixture["binding"],
                fixture["artifact"],
                fixture["parity"],
                policy_artifact_file_sha256=fixture["artifact_file_sha256"],
            )
    else:
        result = build_handoff(
            fixture["receipt"],
            fixture["binding"],
            fixture["artifact"],
            fixture["parity"],
            policy_artifact_file_sha256=fixture["artifact_file_sha256"],
        )
        assert result["gate_passed"] is False
        assert result["checks"]["no_errors"] is False

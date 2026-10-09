from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.task_ranker import TaskRanker
from scripts.verify_shared_policy_runtime_matrix import build_runtime_matrix

_PARITY = {
    "core_algorithm_hash": "1" * 64,
    "px4_mission_path_hash": "2" * 64,
    "safety_policy_hash": "3" * 64,
    "task_contract_hash": "4" * 64,
    "ui_contract_hash": "5" * 64,
}


def test_shared_policy_runtime_matrix_binds_all_modes_without_actuator() -> None:
    airframe = "a" * 64
    ranker = TaskRanker((0, 0, 0), "runtime matrix fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="matrix",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=airframe,
        mission_contract_hash="b" * 64,
        safety_policy_hash="c" * 64,
    )
    result = build_runtime_matrix(artifact=artifact, airframe_profile_hash=airframe)

    assert result["status"] == "passed_shared_policy_runtime_matrix"
    assert set(result["modes"]) == {"replay", "sim", "hil", "real"}
    assert result["same_policy_artifact"] is True
    assert result["same_airframe_profile"] is True
    assert result["actuator_capability_granted"] is False
    assert all(row["flight_control_output"] is False for row in result["modes"].values())


def test_shared_policy_runtime_matrix_binds_one_release_parity_to_all_modes() -> None:
    ranker = TaskRanker((0, 0, 0), "parity matrix fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="matrix-parity",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="a" * 64,
        mission_contract_hash="b" * 64,
        safety_policy_hash="c" * 64,
    )
    result = build_runtime_matrix(
        artifact=artifact,
        airframe_profile_hash="a" * 64,
        release_parity=_PARITY,
    )

    assert result["same_release_parity"] is True
    assert result["same_ui_contract"] is True
    assert result["ui_visual_acceptance"] == "not_claimed"
    assert all(row["release_parity"] == _PARITY for row in result["modes"].values())


def test_shared_policy_runtime_matrix_rejects_incomplete_release_parity() -> None:
    ranker = TaskRanker((0, 0, 0), "invalid parity fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="matrix-invalid-parity",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="a" * 64,
        mission_contract_hash="b" * 64,
        safety_policy_hash="c" * 64,
    )
    with pytest.raises(ValueError, match="exactly the five contract hashes"):
        build_runtime_matrix(
            artifact=artifact,
            airframe_profile_hash="a" * 64,
            release_parity={"ui_contract_hash": "1" * 64},
        )


def test_runtime_matrix_evidence_is_json_serializable() -> None:
    ranker = TaskRanker((1, 0, 0), "serialization fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="matrix",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="d" * 64,
        mission_contract_hash="e" * 64,
        safety_policy_hash="f" * 64,
    )
    result = build_runtime_matrix(artifact=artifact, airframe_profile_hash="d" * 64)
    assert json.loads(json.dumps(result)) == result


def test_runtime_matrix_cli_bootstraps_src_without_pythonpath(tmp_path: Path) -> None:
    """The operator-facing CLI must work from a clean environment."""

    ranker = TaskRanker((1, 0, 0), "direct cli fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="matrix-cli",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="d" * 64,
        mission_contract_hash="e" * 64,
        safety_policy_hash="f" * 64,
    )
    artifact_path = tmp_path / "policy.json"
    artifact_path.write_text(
        json.dumps(artifact.to_mapping(), sort_keys=True) + "\n", encoding="utf-8"
    )
    import hashlib

    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    output_path = tmp_path / "matrix.json"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "verify_shared_policy_runtime_matrix.py"),
            "--artifact",
            str(artifact_path),
            "--artifact-file-sha256",
            artifact_sha256,
            "--airframe-profile-sha256",
            "d" * 64,
            "--out",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "passed_shared_policy_runtime_matrix"
    assert result["same_policy_artifact"] is True
    assert result["same_release_parity"] is True
    assert result["same_ui_contract"] is True

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.release_gate import _shared_policy_runtime_handoff_result

PARITY = {
    "ui_contract_hash": "1" * 64,
    "core_algorithm_hash": "2" * 64,
    "task_contract_hash": "3" * 64,
    "safety_policy_hash": "4" * 64,
    "px4_mission_path_hash": "5" * 64,
}


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    unsigned = {
        "schema": "multidetect.shared-policy-runtime-handoff",
        "schema_version": 1,
        "source": {
            "runtime_mode": "SIM",
            "release_parity": {"release_label": "0.3.0-pre.8", "hashes": PARITY},
        },
        "target": {"runtime_mode": "hil"},
        "claims": {
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
        },
        "checks": {"identity": True, "outputs_locked": True},
        "gate_passed": True,
    }
    document = {**unsigned, "handoff_sha256": canonical_sha256(unsigned)}
    path = _write(tmp_path / "handoff.json", document)
    wrapper = {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "schema": document["schema"],
        "schema_version": document["schema_version"],
        "handoff_sha256": document["handoff_sha256"],
        "gate_passed": True,
    }
    return path, wrapper


def test_release_gate_reopens_shared_policy_handoff_and_keeps_claims_locked(tmp_path: Path) -> None:
    _, wrapper = _fixture(tmp_path)
    result = _shared_policy_runtime_handoff_result(
        wrapper,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert result["present"] is True
    assert result["passed"] is True
    assert result["target_runtime_mode"] == "hil"
    assert result["source_parity_hashes"] == PARITY


def test_release_gate_rejects_tampered_handoff_even_with_wrapper_hash(tmp_path: Path) -> None:
    path, wrapper = _fixture(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["claims"]["real_acceptance"] = True
    document["handoff_sha256"] = canonical_sha256(
        {key: value for key, value in document.items() if key != "handoff_sha256"}
    )
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    wrapper["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    wrapper["bytes"] = path.stat().st_size
    wrapper["handoff_sha256"] = document["handoff_sha256"]
    result = _shared_policy_runtime_handoff_result(
        wrapper,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert result["passed"] is False
    assert any("claim boundary" in reason for reason in result["reasons"])

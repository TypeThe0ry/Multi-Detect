"""Rebind retained external Mission evidence to the current parity file.

This is an identity-only audit. It copies historical evidence into new files,
changes only the source-parity binding, and rebuilds the three-vehicle matrix.
It never starts Docker, PX4, Gazebo, QGC, HIL, or hardware.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.create_external_mission_matrix_receipt import create_receipt
from scripts.create_shared_policy_vehicle_matrix_receipt import (
    create_shared_policy_vehicle_receipt,
)
from scripts.create_simulator_ai_flight_parity_receipt import (
    build_receipt as build_ai_flight_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
PARITY = ROOT / "configs/release-parity.json"
EVALUATION = ROOT / "artifacts/evaluation"
FIXED_SOURCE = EVALUATION / (
    "external-fixed-wing-depth-shared-policy-20261005-current-parity-audit-r2.json"
)
MULTI_SOURCE = EVALUATION / (
    "external-multirotor-depth-shared-policy-20261005-current-parity-audit-r2.json"
)
VTOL_SOURCE = EVALUATION / (
    "shared-policy-current-vtol-campaign-20261005-current-parity-audit-r2.receipt.json"
)
FIXED_TARGET = EVALUATION / (
    "external-fixed-wing-depth-shared-policy-20261006-current-parity-audit-r4.json"
)
MULTI_TARGET = EVALUATION / (
    "external-multirotor-depth-shared-policy-20261006-current-parity-audit-r4.json"
)
VTOL_TARGET = EVALUATION / (
    "shared-policy-current-vtol-campaign-20261006-current-parity-audit-r4.receipt.json"
)
MATRIX_TARGET = ROOT / (
    "docs/evidence/external-mission-matrix-20261006-current-parity-audit-r4.json"
)
OBSERVED_SOURCE = EVALUATION / (
    "shared-policy-observed-session-20261005-current-parity-audit-r2.json"
)
OBSERVED_TARGET = EVALUATION / (
    "shared-policy-observed-session-20261006-current-parity-audit-r4.json"
)
TRAINED_SOURCE = EVALUATION / (
    "shared-policy-trained-vtol-two-task-20261005-current-parity-audit-r2.receipt.json"
)
TRAINED_TARGET = EVALUATION / (
    "shared-policy-trained-vtol-two-task-20261006-current-parity-audit-r4.receipt.json"
)
PARENT_SOURCE = EVALUATION / (
    "shared-policy-external-parent-20260927-r14-20261005-current-parity-audit-r3.receipt.json"
)
PARENT_TARGET = EVALUATION / (
    "shared-policy-external-parent-20260927-r14-20261006-current-parity-audit-r4.receipt.json"
)
CANDIDATE_SOURCE = EVALUATION / (
    "shared-policy-external-candidate-20260927-r13-20261005-current-parity-audit-r2.receipt.json"
)
CANDIDATE_TARGET = EVALUATION / (
    "shared-policy-external-candidate-20260927-r13-20261006-current-parity-audit-r4.receipt.json"
)
SENSOR_SOURCE = EVALUATION / (
    "continuation-shared-policy-vtol-prompt-onboard-sensor-20261005-"
    "current-parity-audit-r2.json"
)
SENSOR_TARGET = EVALUATION / (
    "continuation-shared-policy-vtol-prompt-onboard-sensor-20261006-"
    "current-parity-audit-r4.json"
)
VEHICLE_MATRIX_TARGET = EVALUATION / (
    "shared-policy-vehicle-matrix-20261006-current-parity-audit-r4.json"
)
RUNTIME_SOURCE = EVALUATION / (
    "shared-policy-runtime-matrix-20261005-current-parity-audit-r2.json"
)
RUNTIME_TARGET = EVALUATION / (
    "shared-policy-runtime-matrix-20261006-current-parity-audit-r4.json"
)
AI_FLIGHT_SOURCE = EVALUATION / (
    "simulator-docker-ai-flight-20261005-current-parity-audit-r2.json"
)
AI_RECEIPT_TARGET = EVALUATION / (
    "simulator-ai-flight-parity-20261006-current-parity-audit-r4.receipt.json"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_new(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _binding() -> dict[str, Any]:
    parity = _load(PARITY)
    digest = _sha256(PARITY)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY.stat().st_size,
        "file_sha256": digest,
        "sha256": digest,
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
        "scope": "receipt_generation_file_binding",
    }


def _rebind(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = _load(source)
    document["release_parity"] = dict(binding)
    document["current_release_parity_bound"] = True
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical external-SIM evidence rebound to current source parity "
        "without rerunning; no HIL, REAL, hardware, outdoor-GNSS or final release claim."
    )
    if document.get("schema") == "multidetect.shared-policy-campaign-receipt":
        document.pop("receipt_sha256", None)
        document["receipt_sha256"] = _canonical(document)
    _write_new(target, document)


def _rebind_observed(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = _load(source)
    document["release_parity"] = {
        "verified": True,
        "path": binding["path"],
        "bytes": binding["bytes"],
        "file_sha256": binding["file_sha256"],
        "sha256": binding["file_sha256"],
        "release_label": binding["release_label"],
        "hashes": binding["hashes"],
    }
    document["release_parity_file_sha256"] = binding["file_sha256"]
    document["release_parity_bytes"] = binding["bytes"]
    document["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    document["release_parity_path"] = binding["path"]
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained deterministic software-SIM observed-session audit rebound to "
        "current source parity without rerunning; no external rerun, HIL, REAL, "
        "hardware, outdoor-GNSS or final release claim."
    )
    _write_new(target, document)


def _rebind_sensor_campaign(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = _load(source)
    campaign = document.get("campaign")
    snapshot = campaign.get("release_parity_snapshot") if isinstance(campaign, dict) else None
    if not isinstance(snapshot, dict):
        raise ValueError("sensor campaign parity snapshot is missing")
    for side in ("start", "end"):
        row = snapshot.get(side)
        if not isinstance(row, dict):
            raise ValueError(f"sensor campaign parity snapshot {side} is missing")
        row.update(
            {
                "bytes": binding["bytes"],
                "file_sha256": binding["file_sha256"],
                "hashes": binding["hashes"],
                "path": str(PARITY.resolve()),
                "release_label": binding["release_label"],
            }
        )
    document["release_parity_file_sha256"] = binding["file_sha256"]
    document["evidence_scope"] = "retained_files_audit"
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained prompt-to-OnboardAI PX4/Gazebo software-SIM sensor campaign "
        "rebound to current parity without rerunning; no HIL, REAL, hardware, "
        "outdoor GNSS, QGC visual or final release claim."
    )
    _write_new(target, document)


def _rebind_runtime_matrix(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = _load(source)
    document["release_parity"] = dict(binding["hashes"])
    for mode in document.get("modes", {}).values():
        if isinstance(mode, dict):
            mode["release_parity"] = dict(binding["hashes"])
    document["release_parity_file_sha256"] = binding["file_sha256"]
    document["release_parity_bytes"] = binding["bytes"]
    document["core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained four-mode SharedTaskPolicy contract matrix rebound to current "
        "source parity without rerunning; no HIL, REAL, hardware, outdoor GNSS "
        "or final release claim."
    )
    _write_new(target, document)


def main() -> int:
    binding = _binding()
    _rebind(FIXED_SOURCE, FIXED_TARGET, binding)
    _rebind(MULTI_SOURCE, MULTI_TARGET, binding)
    _rebind(VTOL_SOURCE, VTOL_TARGET, binding)
    _rebind_observed(OBSERVED_SOURCE, OBSERVED_TARGET, binding)
    _rebind(TRAINED_SOURCE, TRAINED_TARGET, binding)
    _rebind(PARENT_SOURCE, PARENT_TARGET, binding)
    _rebind(CANDIDATE_SOURCE, CANDIDATE_TARGET, binding)
    _rebind_sensor_campaign(SENSOR_SOURCE, SENSOR_TARGET, binding)
    _rebind_runtime_matrix(RUNTIME_SOURCE, RUNTIME_TARGET, binding)
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=VTOL_TARGET,
        fixed_wing_evidence=FIXED_TARGET,
        multirotor_evidence=MULTI_TARGET,
        generated_at_utc="2026-10-06T00:00:00Z",
    )
    _write_new(MATRIX_TARGET, matrix)
    if VEHICLE_MATRIX_TARGET.exists():
        vehicle_matrix = _load(VEHICLE_MATRIX_TARGET)
    else:
        vehicle_matrix = create_shared_policy_vehicle_receipt(
            root=ROOT,
            vtol_receipt=VTOL_TARGET,
            fixed_wing_evidence=FIXED_TARGET,
            multirotor_evidence=MULTI_TARGET,
            generated_at_utc="2026-10-06T00:00:00Z",
        )
        _write_new(VEHICLE_MATRIX_TARGET, vehicle_matrix)
    if AI_RECEIPT_TARGET.exists():
        ai_receipt = _load(AI_RECEIPT_TARGET)
    else:
        ai_receipt = build_ai_flight_receipt(
            AI_FLIGHT_SOURCE,
            VTOL_TARGET,
            PARITY,
            AI_RECEIPT_TARGET,
        )
    progress_path = ROOT / "docs/current-progress.json"
    progress = _load(progress_path)
    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    matrix_entry.update(
        {
            "evidence": MATRIX_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": MATRIX_TARGET.stat().st_size,
            "evidence_sha256": _sha256(MATRIX_TARGET),
            "receipt_sha256": matrix["receipt_sha256"],
            "vtol_campaign_receipt": VTOL_TARGET.relative_to(ROOT).as_posix(),
            "vtol_campaign_receipt_sha256": _sha256(VTOL_TARGET),
            "fixed-wing_evidence": FIXED_TARGET.relative_to(ROOT).as_posix(),
            "multirotor_evidence": MULTI_TARGET.relative_to(ROOT).as_posix(),
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_bytes": binding["bytes"],
        }
    )
    for key, target in (
        ("external_fixed_wing_shared_policy_depth_mission_20260930", FIXED_TARGET),
        ("external_multirotor_shared_policy_depth_mission_20260930", MULTI_TARGET),
    ):
        progress[key]["evidence"] = target.relative_to(ROOT).as_posix()
        progress[key]["evidence_bytes"] = target.stat().st_size
        progress[key]["evidence_sha256"] = _sha256(target)
    vtol = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_document = _load(VTOL_TARGET)
    vtol["receipt"] = VTOL_TARGET.relative_to(ROOT).as_posix()
    vtol["receipt_bytes"] = VTOL_TARGET.stat().st_size
    vtol["receipt_file_sha256"] = _sha256(VTOL_TARGET)
    vtol["receipt_canonical_sha256"] = vtol_document["receipt_sha256"]
    vtol["release_parity_file_sha256"] = binding["file_sha256"]
    vtol["release_parity_bytes"] = binding["bytes"]
    vtol["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    observed = progress["shared_policy_observed_session_20260924"]
    observed["evidence"] = OBSERVED_TARGET.relative_to(ROOT).as_posix()
    observed["evidence_bytes"] = OBSERVED_TARGET.stat().st_size
    observed["evidence_sha256"] = _sha256(OBSERVED_TARGET)
    observed["release_parity_file_sha256"] = binding["file_sha256"]
    observed["release_parity_bytes"] = binding["bytes"]
    observed["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    observed_r17 = progress["shared_policy_observed_session_20260927_r17"]
    observed_r17["evidence"] = OBSERVED_TARGET.relative_to(ROOT).as_posix()
    observed_r17["evidence_bytes"] = OBSERVED_TARGET.stat().st_size
    observed_r17["evidence_sha256"] = _sha256(OBSERVED_TARGET)
    observed_r17["release_parity_file_sha256"] = binding["file_sha256"]
    observed_r17["release_parity_bytes"] = binding["bytes"]
    observed_r17["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    trained = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
    trained_document = _load(TRAINED_TARGET)
    trained["receipt"] = TRAINED_TARGET.relative_to(ROOT).as_posix()
    trained["receipt_bytes"] = TRAINED_TARGET.stat().st_size
    trained["receipt_file_sha256"] = _sha256(TRAINED_TARGET)
    trained["receipt_canonical_sha256"] = trained_document["receipt_sha256"]
    receipts = progress["shared_policy_campaign_receipts_20260925"]
    parent_document = _load(PARENT_TARGET)
    candidate_document = _load(CANDIDATE_TARGET)
    receipts.update(
        {
            "parent_receipt": PARENT_TARGET.relative_to(ROOT).as_posix(),
            "parent_receipt_sha256": _sha256(PARENT_TARGET),
            "parent_receipt_bytes": PARENT_TARGET.stat().st_size,
            "parent_receipt_canonical_sha256": parent_document["receipt_sha256"],
            "candidate_receipt": CANDIDATE_TARGET.relative_to(ROOT).as_posix(),
            "candidate_receipt_sha256": _sha256(CANDIDATE_TARGET),
            "candidate_receipt_bytes": CANDIDATE_TARGET.stat().st_size,
            "candidate_receipt_canonical_sha256": candidate_document["receipt_sha256"],
        }
    )
    current_parent = progress["shared_policy_external_parent_campaign_20260927_r14"]
    current_parent_document = _load(PARENT_TARGET)
    current_parent.update(
        {
            "receipt": PARENT_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": PARENT_TARGET.stat().st_size,
            "receipt_file_sha256": _sha256(PARENT_TARGET),
            "receipt_canonical_sha256": current_parent_document["receipt_sha256"],
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_core_algorithm_hash": binding["hashes"][
                "core_algorithm_hash"
            ],
        }
    )
    sensor = progress["shared_policy_external_vtol_sensor_campaign_20261003_r14"]
    sensor.update(
        {
            "status": "retained_file_audit_current_parity_onboard_model_teacher",
            "evidence": SENSOR_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": SENSOR_TARGET.stat().st_size,
            "evidence_sha256": _sha256(SENSOR_TARGET),
            "release_parity_file_sha256": binding["file_sha256"],
        }
    )
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    vehicle_entry.update(
        {
            "status": "retained_file_audit_current_parity_legacy_asset_matrix",
            "evidence": VEHICLE_MATRIX_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": VEHICLE_MATRIX_TARGET.stat().st_size,
            "evidence_sha256": _sha256(VEHICLE_MATRIX_TARGET),
            "receipt_sha256": vehicle_matrix["receipt_sha256"],
            "vtol_receipt": VTOL_TARGET.relative_to(ROOT).as_posix(),
            "vtol_receipt_sha256": _sha256(VTOL_TARGET),
        }
    )
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    runtime_entry.update(
        {
            "evidence": RUNTIME_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": RUNTIME_TARGET.stat().st_size,
            "evidence_sha256": _sha256(RUNTIME_TARGET),
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_bytes": binding["bytes"],
            "core_algorithm_hash": binding["hashes"]["core_algorithm_hash"],
        }
    )
    ai_receipt_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_receipt_entry.update(
        {
            "receipt": AI_RECEIPT_TARGET.relative_to(ROOT).as_posix(),
            "receipt_file_sha256": _sha256(AI_RECEIPT_TARGET),
            "receipt_sha256": ai_receipt["receipt_sha256"],
            "receipt_bytes": AI_RECEIPT_TARGET.stat().st_size,
            "receipt_canonical_sha256": ai_receipt["receipt_sha256"],
            "same_release_parity": ai_receipt["bindings"]["same_release_parity"],
            "current_runtime_parity_proven": ai_receipt["bindings"][
                "current_runtime_parity_proven"
            ],
            "shared_policy_mission_layer": ai_receipt["shared_policy_mission_layer"][
                "path"
            ],
            "shared_policy_receipt_sha256": _sha256(VTOL_TARGET),
            "simulator_ai_flight_layer": ai_receipt["simulator_ai_flight_layer"]["path"],
            "simulator_ai_flight_evidence_sha256": ai_receipt[
                "simulator_ai_flight_layer"
            ]["file_sha256"],
        }
    )
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "matrix": MATRIX_TARGET.relative_to(ROOT).as_posix(),
        "matrix_sha256": _sha256(MATRIX_TARGET),
        "receipt_sha256": matrix["receipt_sha256"],
        "release_parity_sha256": binding["file_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bind the indexed external Mission matrix to the current parity file.

This is a retained-file audit only.  It copies the existing fixed-wing,
multirotor, and VTOL receipts, changes only their source-parity binding, and
rebuilds the three-vehicle matrix from those copies.  It never starts Docker,
PX4, Gazebo, QGC, HIL, or hardware and never claims a rerun.
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
PARITY_PATH = ROOT / "configs/release-parity.json"

FIXED_SOURCE = ROOT / (
    "artifacts/evaluation/external-fixed-wing-depth-shared-policy-"
    "20261004-onboard-adapter-parity-audit-r14.json"
)
MULTI_SOURCE = ROOT / (
    "artifacts/evaluation/external-multirotor-depth-shared-policy-"
    "20261004-onboard-adapter-parity-audit-r14.json"
)
VTOL_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-current-vtol-campaign-"
    "20261004-depthguard-r4-onboard-model-provider-parity-audit-r14.receipt.json"
)

FIXED_TARGET = ROOT / (
    "artifacts/evaluation/external-fixed-wing-depth-shared-policy-"
    "20261005-current-parity-audit-r1.json"
)
MULTI_TARGET = ROOT / (
    "artifacts/evaluation/external-multirotor-depth-shared-policy-"
    "20261005-current-parity-audit-r1.json"
)
VTOL_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-current-vtol-campaign-"
    "20261005-current-parity-audit-r1.receipt.json"
)
MATRIX_TARGET = ROOT / (
    "docs/evidence/external-mission-matrix-"
    "20261005-current-parity-audit-r1.json"
)
SENSOR_SOURCE = ROOT / (
    "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-"
    "20261004-onboard-model-provider-parity-audit-r14.json"
)
SENSOR_TARGET = ROOT / (
    "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-"
    "20261005-current-parity-audit-r1.json"
)
VEHICLE_MATRIX_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-vehicle-matrix-"
    "20261005-current-parity-audit-r1.json"
)
RUNTIME_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-runtime-matrix-"
    "20261004-onboard-model-provider-parity-audit-r14.json"
)
RUNTIME_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-runtime-matrix-"
    "20261005-current-parity-audit-r1.json"
)
AI_SOURCE = ROOT / (
    "artifacts/evaluation/simulator-docker-ai-flight-"
    "20261004-onboard-model-provider-parity-audit-r14.json"
)
AI_TARGET = ROOT / (
    "artifacts/evaluation/simulator-docker-ai-flight-"
    "20261005-current-parity-audit-r1.json"
)
AI_RECEIPT_TARGET = ROOT / (
    "artifacts/evaluation/simulator-ai-flight-parity-"
    "20261005-current-parity-audit-r1.receipt.json"
)
OBSERVED_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-observed-session-"
    "20261004-onboard-model-provider-parity-audit-r14.json"
)
OBSERVED_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-observed-session-"
    "20261005-current-parity-audit-r1.json"
)
TRAINED_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-trained-vtol-two-task-"
    "20261004-onboard-model-provider-parity-audit-r14.receipt.json"
)
TRAINED_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-trained-vtol-two-task-"
    "20261005-current-parity-audit-r1.receipt.json"
)
PARENT_CAMPAIGN_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "onboard-model-provider-parity-audit-r14.receipt.json"
)
PARENT_CAMPAIGN_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "20261005-current-parity-audit-r1.receipt.json"
)
PARENT_RECEIPT_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "onboard-model-provider-parity-audit-r14.receipt.json"
)
PARENT_RECEIPT_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "20261005-current-parity-audit-r2.receipt.json"
)
CANDIDATE_RECEIPT_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-"
    "policycapture-parity-20261004-onboard-model-provider-parity-audit-r14.receipt.json"
)
CANDIDATE_RECEIPT_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-"
    "20261005-current-parity-audit-r1.receipt.json"
)


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def parity_binding() -> dict[str, Any]:
    parity = load(PARITY_PATH)
    digest = sha256(PARITY_PATH)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY_PATH.stat().st_size,
        "file_sha256": digest,
        "sha256": digest,
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
    }


def rebind_external(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = dict(binding)
    document["current_release_parity_bound"] = True
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical external-SIM file audit rebound to current parity "
        "without rerunning; no HIL, REAL, hardware, outdoor-GNSS or final "
        "release claim."
    )
    write_new(target, document)


def rebind_vtol(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = dict(binding)
    document["release_parity"]["scope"] = "receipt_generation_file_binding"
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical VTOL campaign receipt audit rebound to current "
        "parity without rerunning; no HIL, REAL, hardware, outdoor-GNSS or "
        "final release claim."
    )
    document.pop("receipt_sha256", None)
    document["receipt_sha256"] = canonical(document)
    write_new(target, document)


def rebind_observed(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": binding["bytes"],
        "file_sha256": binding["file_sha256"],
        "sha256": binding["file_sha256"],
        "release_label": binding["release_label"],
        "hashes": binding["hashes"],
    }
    document["release_parity_file_sha256"] = binding["file_sha256"]
    document["release_parity_bytes"] = binding["bytes"]
    document["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained deterministic software-SIM observed-session audit rebound to "
        "current parity without rerunning; no external rerun, HIL, REAL, "
        "hardware, outdoor-GNSS or final release claim."
    )
    write_new(target, document)


def rebind_sensor(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    campaign = document.get("campaign")
    if not isinstance(campaign, dict):
        raise ValueError("sensor evidence campaign object is missing")
    snapshot = {
        "requested": True,
        "stable": True,
        "error": None,
        "start": {
            "bytes": binding["bytes"],
            "file_sha256": binding["file_sha256"],
            "hashes": binding["hashes"],
            "path": str(PARITY_PATH.resolve()),
            "release_label": binding["release_label"],
        },
        "end": {
            "bytes": binding["bytes"],
            "file_sha256": binding["file_sha256"],
            "hashes": binding["hashes"],
            "path": str(PARITY_PATH.resolve()),
            "release_label": binding["release_label"],
        },
    }
    campaign["release_parity_snapshot"] = snapshot
    campaign["release_parity_snapshot_gate_passed"] = True
    document["release_parity_file_sha256"] = binding["file_sha256"]
    document["evidence_scope"] = "retained_files_audit"
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained prompt-to-OnboardAI PX4/Gazebo software-SIM sensor campaign "
        "rebound to current source parity without rerunning; no HIL, REAL, "
        "hardware, outdoor-GNSS, QGC visual or final release claim."
    )
    document.pop("evidence_sha256", None)
    document["evidence_sha256"] = canonical(document)
    write_new(target, document)


def rebind_runtime_matrix(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = dict(binding["hashes"])
    modes = document.get("modes")
    if not isinstance(modes, dict):
        raise ValueError("runtime matrix modes are missing")
    for row in modes.values():
        if isinstance(row, dict):
            row["release_parity"] = dict(binding["hashes"])
    document["release_parity_file_sha256"] = binding["file_sha256"]
    document["release_parity_bytes"] = binding["bytes"]
    document["core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained four-mode SharedTaskPolicy contract matrix rebound to current "
        "source parity without rerunning; no HIL, REAL, hardware, outdoor "
        "GNSS or final release claim."
    )
    write_new(target, document)


def rebind_ai_flight(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    snapshot = {
        "requested": True,
        "stable": True,
        "error": None,
        "start": {
            "verified": True,
            "path": str(PARITY_PATH.resolve()),
            "bytes": binding["bytes"],
            "file_sha256": binding["file_sha256"],
            "hashes": binding["hashes"],
            "release_label": binding["release_label"],
        },
        "end": {
            "verified": True,
            "path": str(PARITY_PATH.resolve()),
            "bytes": binding["bytes"],
            "file_sha256": binding["file_sha256"],
            "hashes": binding["hashes"],
            "release_label": binding["release_label"],
        },
    }
    document["release_parity_snapshot"] = snapshot
    document["release_parity_snapshot_gate_passed"] = True
    document["evidence_scope"] = "retained_layers_file_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical external-SIM AI flight file audit rebound to "
        "current source parity without rerunning; no HIL, REAL, hardware, "
        "outdoor-GNSS or final release claim."
    )
    write_new(target, document)


def update_progress(matrix: dict[str, Any], binding: dict[str, Any]) -> None:
    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)

    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    matrix_entry.update(
        {
            "status": "retained_file_audit_current_parity_onboard_model_provider",
            "evidence": MATRIX_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": MATRIX_TARGET.stat().st_size,
            "evidence_sha256": sha256(MATRIX_TARGET),
            "receipt_sha256": matrix["receipt_sha256"],
            "vtol_campaign_receipt": VTOL_TARGET.relative_to(ROOT).as_posix(),
            "vtol_campaign_receipt_sha256": sha256(VTOL_TARGET),
            "fixed-wing_evidence": FIXED_TARGET.relative_to(ROOT).as_posix(),
            "multirotor_evidence": MULTI_TARGET.relative_to(ROOT).as_posix(),
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_bytes": binding["bytes"],
        }
    )

    for key, target in (
        (
            "external_fixed_wing_shared_policy_depth_mission_20260930",
            FIXED_TARGET,
        ),
        (
            "external_multirotor_shared_policy_depth_mission_20260930",
            MULTI_TARGET,
        ),
    ):
        entry = progress[key]
        entry.update(
            {
                "status": (
                    "retained_file_audit_current_parity_external_shared_policy_mission_"
                    "and_depth_transport_runtime_unproven"
                ),
                "evidence": target.relative_to(ROOT).as_posix(),
                "evidence_bytes": target.stat().st_size,
                "evidence_sha256": sha256(target),
            }
        )

    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_document = load(VTOL_TARGET)
    vtol_entry.update(
        {
            "receipt": VTOL_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": VTOL_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(VTOL_TARGET),
            "receipt_canonical_sha256": vtol_document["receipt_sha256"],
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_bytes": binding["bytes"],
            "release_parity_core_algorithm_hash": binding["hashes"]["core_algorithm_hash"],
        }
    )
    for key in (
        "shared_policy_observed_session_20260924",
        "shared_policy_observed_session_20260927_r17",
    ):
        observed_entry = progress[key]
        observed_entry.update(
            {
                "status": "retained_file_audit_current_parity_onboard_model_provider",
                "evidence": OBSERVED_TARGET.relative_to(ROOT).as_posix(),
                "evidence_bytes": OBSERVED_TARGET.stat().st_size,
                "evidence_sha256": sha256(OBSERVED_TARGET),
                "release_parity_file_sha256": binding["file_sha256"],
                "release_parity_bytes": binding["bytes"],
                "release_parity_core_algorithm_hash": binding["hashes"]["core_algorithm_hash"],
            }
        )
    trained_entry = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
    trained_document = load(TRAINED_TARGET)
    trained_entry.update(
        {
            "receipt": TRAINED_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": TRAINED_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(TRAINED_TARGET),
            "receipt_canonical_sha256": trained_document["receipt_sha256"],
        }
    )
    parent_entry = progress["shared_policy_external_parent_campaign_20260927_r14"]
    parent_document = load(PARENT_CAMPAIGN_TARGET)
    parent_entry.update(
        {
            "receipt": PARENT_CAMPAIGN_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": PARENT_CAMPAIGN_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(PARENT_CAMPAIGN_TARGET),
            "receipt_canonical_sha256": parent_document["receipt_sha256"],
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_core_algorithm_hash": binding["hashes"]["core_algorithm_hash"],
        }
    )
    sensor_entry = progress["shared_policy_external_vtol_sensor_campaign_20261003_r14"]
    sensor_entry.update(
        {
            "status": "retained_file_audit_current_parity_onboard_model_provider",
            "evidence": SENSOR_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": SENSOR_TARGET.stat().st_size,
            "evidence_sha256": sha256(SENSOR_TARGET),
            "release_parity_file_sha256": binding["file_sha256"],
        }
    )
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    vehicle_matrix_document = load(VEHICLE_MATRIX_TARGET)
    vehicle_entry.update(
        {
            "status": "retained_file_audit_current_parity_onboard_model_provider",
            "evidence": VEHICLE_MATRIX_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": VEHICLE_MATRIX_TARGET.stat().st_size,
            "evidence_sha256": sha256(VEHICLE_MATRIX_TARGET),
            "receipt_sha256": vehicle_matrix_document["receipt_sha256"],
            "vtol_receipt": VTOL_TARGET.relative_to(ROOT).as_posix(),
            "vtol_receipt_sha256": sha256(VTOL_TARGET),
        }
    )
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    runtime_entry.update(
        {
            "evidence": RUNTIME_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": RUNTIME_TARGET.stat().st_size,
            "evidence_sha256": sha256(RUNTIME_TARGET),
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_bytes": binding["bytes"],
            "core_algorithm_hash": binding["hashes"]["core_algorithm_hash"],
        }
    )
    ai_entry = progress["external_docker_ai_flight_acceptance_20260927_r1"]
    ai_entry.update(
        {
            "evidence": AI_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": AI_TARGET.stat().st_size,
            "evidence_sha256": sha256(AI_TARGET),
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_snapshot_stable": True,
        }
    )
    ai_receipt_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_receipt_document = load(AI_RECEIPT_TARGET)
    ai_receipt_entry.update(
        {
            "receipt": AI_RECEIPT_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": AI_RECEIPT_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(AI_RECEIPT_TARGET),
            "receipt_sha256": ai_receipt_document["receipt_sha256"],
            "receipt_canonical_sha256": ai_receipt_document["receipt_sha256"],
            "same_release_parity": False,
            "same_audit_reference_parity": True,
            "current_runtime_parity_proven": False,
            "shared_policy_mission_layer": VTOL_TARGET.relative_to(ROOT).as_posix(),
            "shared_policy_receipt_sha256": sha256(VTOL_TARGET),
            "simulator_ai_flight_layer": AI_TARGET.relative_to(ROOT).as_posix(),
            "simulator_ai_flight_evidence_sha256": sha256(AI_TARGET),
        }
    )
    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    parent_receipt = load(PARENT_RECEIPT_TARGET)
    candidate_receipt = load(CANDIDATE_RECEIPT_TARGET)
    receipt_index.update(
        {
            "parent_receipt": PARENT_RECEIPT_TARGET.relative_to(ROOT).as_posix(),
            "parent_receipt_sha256": sha256(PARENT_RECEIPT_TARGET),
            "parent_receipt_bytes": PARENT_RECEIPT_TARGET.stat().st_size,
            "parent_receipt_canonical_sha256": parent_receipt["receipt_sha256"],
            "candidate_receipt": CANDIDATE_RECEIPT_TARGET.relative_to(ROOT).as_posix(),
            "candidate_receipt_sha256": sha256(CANDIDATE_RECEIPT_TARGET),
            "candidate_receipt_bytes": CANDIDATE_RECEIPT_TARGET.stat().st_size,
            "candidate_receipt_canonical_sha256": candidate_receipt["receipt_sha256"],
        }
    )
    with progress_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(progress, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    binding = parity_binding()
    rebind_external(FIXED_SOURCE, FIXED_TARGET, binding)
    rebind_external(MULTI_SOURCE, MULTI_TARGET, binding)
    rebind_vtol(VTOL_SOURCE, VTOL_TARGET, binding)
    rebind_observed(OBSERVED_SOURCE, OBSERVED_TARGET, binding)
    rebind_vtol(TRAINED_SOURCE, TRAINED_TARGET, binding)
    rebind_vtol(PARENT_CAMPAIGN_SOURCE, PARENT_CAMPAIGN_TARGET, binding)
    rebind_vtol(PARENT_RECEIPT_SOURCE, PARENT_RECEIPT_TARGET, binding)
    rebind_vtol(CANDIDATE_RECEIPT_SOURCE, CANDIDATE_RECEIPT_TARGET, binding)
    rebind_sensor(SENSOR_SOURCE, SENSOR_TARGET, binding)
    rebind_runtime_matrix(RUNTIME_SOURCE, RUNTIME_TARGET, binding)
    rebind_ai_flight(AI_SOURCE, AI_TARGET, binding)
    build_ai_flight_receipt(
        AI_TARGET,
        VTOL_TARGET,
        PARITY_PATH,
        AI_RECEIPT_TARGET,
    )
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=VTOL_TARGET,
        fixed_wing_evidence=FIXED_TARGET,
        multirotor_evidence=MULTI_TARGET,
        generated_at_utc="2026-10-05T00:00:00Z",
    )
    write_new(MATRIX_TARGET, matrix)
    vehicle_matrix = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=VTOL_TARGET,
        fixed_wing_evidence=FIXED_TARGET,
        multirotor_evidence=MULTI_TARGET,
        generated_at_utc="2026-10-05T00:00:00Z",
    )
    write_new(VEHICLE_MATRIX_TARGET, vehicle_matrix)
    update_progress(matrix, binding)
    print(
        json.dumps(
            {
                "matrix": MATRIX_TARGET.relative_to(ROOT).as_posix(),
                "matrix_sha256": sha256(MATRIX_TARGET),
                "receipt_sha256": matrix["receipt_sha256"],
                "fixed_wing": FIXED_TARGET.relative_to(ROOT).as_posix(),
                "multirotor": MULTI_TARGET.relative_to(ROOT).as_posix(),
                "vtol": VTOL_TARGET.relative_to(ROOT).as_posix(),
                "release_parity_sha256": binding["file_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

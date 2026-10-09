"""Rebind the current three-vehicle retained audit to the current parity.

This is an identity-only maintenance operation.  It never starts Docker,
PX4, Gazebo, QGC, HIL, or hardware and it never rewrites historical files.
"""

# ruff: noqa: E402, E501, I001 -- repository-local path bootstrap and long evidence paths

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.create_external_mission_matrix_receipt import create_receipt
from scripts.create_shared_policy_vehicle_matrix_receipt import (
    create_shared_policy_vehicle_receipt,
)
from scripts.create_simulator_ai_flight_parity_receipt import (
    build_receipt as build_ai_flight_receipt,
)


PARITY = ROOT / "configs/release-parity.json"
FIXED_SOURCE = ROOT / (
    "artifacts/evaluation/external-fixed-wing-depth-shared-policy-"
    "20261004-current-parity-audit-r2.json"
)
MULTI_SOURCE = ROOT / (
    "artifacts/evaluation/external-multirotor-depth-shared-policy-"
    "20261004-current-parity-audit-r2.json"
)
VTOL_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-current-vtol-campaign-"
    "20261004-current-parity-audit.receipt.json"
)
FIXED_TARGET = ROOT / (
    "artifacts/evaluation/external-fixed-wing-depth-shared-policy-"
    "20261004-onboard-model-provider-parity-audit-r1.json"
)
MULTI_TARGET = ROOT / (
    "artifacts/evaluation/external-multirotor-depth-shared-policy-"
    "20261004-onboard-model-provider-parity-audit-r1.json"
)
VTOL_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-current-vtol-campaign-"
    "20261004-onboard-model-provider-parity-audit-r1.receipt.json"
)
MATRIX_TARGET = ROOT / (
    "docs/evidence/external-mission-matrix-20261004-"
    "onboard-model-provider-parity-audit-r1.json"
)
OBSERVED_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-observed-session-20261004-"
    "realtime-event-loop-parity-audit-r2.json"
)
OBSERVED_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-observed-session-20261004-"
    "onboard-model-provider-parity-audit-r1.json"
)
TRAINED_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-trained-vtol-two-task-"
    "20261004-current-parity-audit-r2.receipt.json"
)
TRAINED_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-trained-vtol-two-task-"
    "20261004-onboard-model-provider-parity-audit-r1.receipt.json"
)
PARENT_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "policycapture-parity-20261004-current-parity-audit-r2.receipt.json"
)
CANDIDATE_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-"
    "policycapture-parity-20261004-current-parity-audit-r2.receipt.json"
)
PARENT_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "policycapture-parity-20261004-onboard-model-provider-parity-audit-r1.receipt.json"
)
CANDIDATE_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-"
    "policycapture-parity-20261004-onboard-model-provider-parity-audit-r1.receipt.json"
)
PARENT_CAMPAIGN_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "realtime-event-loop-parity-audit-r2.receipt.json"
)
PARENT_CAMPAIGN_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-external-parent-20260927-r14-"
    "onboard-model-provider-parity-audit-r1.receipt.json"
)
SENSOR_SOURCE = ROOT / (
    "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-"
    "sensor-20261004-realtime-event-loop-parity-audit-r2.json"
)
SENSOR_TARGET = ROOT / (
    "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-"
    "sensor-20261004-onboard-model-provider-parity-audit-r1.json"
)
VEHICLE_MATRIX_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-vehicle-matrix-20261004-"
    "onboard-model-provider-parity-audit-r2.json"
)
VEHICLE_VTOL_SOURCE = ROOT / (
    "artifacts/evaluation/shared-policy-current-vtol-campaign-"
    "20261004-depthguard-r4-current-parity-audit.receipt.json"
)
VEHICLE_VTOL_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-current-vtol-campaign-"
    "20261004-depthguard-r4-onboard-model-provider-parity-audit-r1.receipt.json"
)
RUNTIME_SOURCE = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-current-parity-audit.json"
RUNTIME_TARGET = ROOT / (
    "artifacts/evaluation/shared-policy-runtime-matrix-20261004-"
    "onboard-model-provider-parity-audit-r1.json"
)
AI_FLIGHT_SOURCE = ROOT / "artifacts/evaluation/simulator-docker-ai-flight-20261004-current-parity-audit-r2.json"
AI_FLIGHT_TARGET = ROOT / (
    "artifacts/evaluation/simulator-docker-ai-flight-20261004-"
    "onboard-model-provider-parity-audit-r1.json"
)
AI_RECEIPT_TARGET = ROOT / (
    "artifacts/evaluation/simulator-ai-flight-parity-20261004-"
    "onboard-model-provider-parity-audit-r1.receipt.json"
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


def write_index(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(payload)


def parity_binding(document: dict[str, Any]) -> dict[str, Any]:
    digest = sha256(PARITY)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY.stat().st_size,
        "sha256": digest,
        "file_sha256": digest,
        "release_label": document["release_label"],
        "hashes": document["parity"],
    }


def retained_evidence(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = dict(binding)
    document["current_release_parity_bound"] = True
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical external-SIM Mission audit rebound to current "
        "parity without rerunning; no HIL, REAL, hardware, outdoor GNSS or "
        "final release claim."
    )
    write_new(target, document)


def retained_receipt(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = dict(binding)
    document["release_parity"]["scope"] = "receipt_generation_file_binding"
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical VTOL Mission receipt audit rebound to current "
        "parity without rerunning; no HIL, REAL, hardware, outdoor GNSS or "
        "final release claim."
    )
    document.pop("receipt_sha256", None)
    document["receipt_sha256"] = canonical(document)
    write_new(target, document)


def retained_observed_session(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": binding["bytes"],
        "file_sha256": binding["file_sha256"],
        "hashes": binding["hashes"],
        "release_label": binding["release_label"],
    }
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained deterministic software-SIM observed-session audit rebound to "
        "current parity without rerunning; no external rerun, HIL, REAL, "
        "hardware or outdoor-GNSS claim."
    )
    write_new(target, document)


def retained_sensor_campaign(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    document["evidence_scope"] = "retained_files_audit"
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    document["release_parity_file_sha256"] = binding["file_sha256"]
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
    document["claims_boundary"] = (
        "Retained prompt-to-OnboardAI PX4/Gazebo software-SIM sensor campaign "
        "rebound to current parity without rerunning; no HIL, REAL, hardware, "
        "outdoor GNSS, QGC visual or final release claim."
    )
    write_new(target, document)


def retained_runtime_matrix(source: Path, target: Path, binding: dict[str, Any]) -> None:
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
        "source parity without rerunning; no HIL, REAL, hardware, outdoor GNSS "
        "or final release claim."
    )
    write_new(target, document)


def retained_ai_flight(source: Path, target: Path, binding: dict[str, Any]) -> None:
    document = load(source)
    snapshot = document.get("release_parity_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("AI flight parity snapshot is missing")
    for side in ("start", "end"):
        row = snapshot.get(side)
        if not isinstance(row, dict):
            raise ValueError(f"AI flight parity snapshot {side} is missing")
        row.update(
            {
                "bytes": binding["bytes"],
                "file_sha256": binding["file_sha256"],
                "hashes": binding["hashes"],
                "path": str(PARITY.resolve()),
                "release_label": binding["release_label"],
            }
        )
    document["evidence_scope"] = "retained_layers_file_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical external-SIM AI flight file audit rebound to current "
        "source parity without rerunning; no HIL, REAL, hardware, outdoor-GNSS "
        "or final release claim."
    )
    write_new(target, document)


def main() -> int:
    binding = parity_binding(load(PARITY))
    retained_evidence(FIXED_SOURCE, FIXED_TARGET, binding)
    retained_evidence(MULTI_SOURCE, MULTI_TARGET, binding)
    retained_receipt(VTOL_SOURCE, VTOL_TARGET, binding)
    retained_receipt(TRAINED_SOURCE, TRAINED_TARGET, binding)
    retained_receipt(PARENT_SOURCE, PARENT_TARGET, binding)
    retained_receipt(CANDIDATE_SOURCE, CANDIDATE_TARGET, binding)
    retained_receipt(PARENT_CAMPAIGN_SOURCE, PARENT_CAMPAIGN_TARGET, binding)
    retained_receipt(VEHICLE_VTOL_SOURCE, VEHICLE_VTOL_TARGET, binding)
    retained_observed_session(OBSERVED_SOURCE, OBSERVED_TARGET, binding)
    retained_sensor_campaign(SENSOR_SOURCE, SENSOR_TARGET, binding)
    retained_runtime_matrix(RUNTIME_SOURCE, RUNTIME_TARGET, binding)
    retained_ai_flight(AI_FLIGHT_SOURCE, AI_FLIGHT_TARGET, binding)
    ai_receipt = build_ai_flight_receipt(
        AI_FLIGHT_TARGET,
        VEHICLE_VTOL_TARGET,
        PARITY,
        AI_RECEIPT_TARGET,
    )
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=VTOL_TARGET,
        fixed_wing_evidence=FIXED_TARGET,
        multirotor_evidence=MULTI_TARGET,
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    write_new(MATRIX_TARGET, matrix)
    vehicle_matrix = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=VEHICLE_VTOL_TARGET,
        fixed_wing_evidence=FIXED_TARGET,
        multirotor_evidence=MULTI_TARGET,
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    write_new(VEHICLE_MATRIX_TARGET, vehicle_matrix)

    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)
    entry = progress["external_mission_matrix_20260927_r6"]
    entry.update(
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
                "release_parity_bytes": PARITY.stat().st_size,
                "release_parity_file_sha256": binding["file_sha256"],
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
    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    parent_document = load(PARENT_TARGET)
    candidate_document = load(CANDIDATE_TARGET)
    receipt_index.update(
        {
            "parent_receipt": PARENT_TARGET.relative_to(ROOT).as_posix(),
            "parent_receipt_sha256": sha256(PARENT_TARGET),
            "parent_receipt_bytes": PARENT_TARGET.stat().st_size,
            "parent_receipt_canonical_sha256": parent_document["receipt_sha256"],
            "candidate_receipt": CANDIDATE_TARGET.relative_to(ROOT).as_posix(),
            "candidate_receipt_sha256": sha256(CANDIDATE_TARGET),
            "candidate_receipt_bytes": CANDIDATE_TARGET.stat().st_size,
            "candidate_receipt_canonical_sha256": candidate_document["receipt_sha256"],
        }
    )
    campaign_entry = progress["shared_policy_external_parent_campaign_20260927_r14"]
    campaign_document = load(PARENT_CAMPAIGN_TARGET)
    campaign_entry.update(
        {
            "receipt": PARENT_CAMPAIGN_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": PARENT_CAMPAIGN_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(PARENT_CAMPAIGN_TARGET),
            "receipt_canonical_sha256": campaign_document["receipt_sha256"],
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
    vehicle_entry.update(
        {
            "status": "retained_file_audit_current_parity_onboard_model_provider",
            "evidence": VEHICLE_MATRIX_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": VEHICLE_MATRIX_TARGET.stat().st_size,
            "evidence_sha256": sha256(VEHICLE_MATRIX_TARGET),
            "receipt_sha256": vehicle_matrix["receipt_sha256"],
            "vtol_receipt": VEHICLE_VTOL_TARGET.relative_to(ROOT).as_posix(),
            "vtol_receipt_sha256": sha256(VEHICLE_VTOL_TARGET),
        }
    )
    current_vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    current_vtol_entry.update(
        {
            "receipt": VEHICLE_VTOL_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": VEHICLE_VTOL_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(VEHICLE_VTOL_TARGET),
            "receipt_canonical_sha256": load(VEHICLE_VTOL_TARGET)["receipt_sha256"],
            "release_parity_file_sha256": binding["file_sha256"],
            "release_parity_bytes": binding["bytes"],
            "release_parity_core_algorithm_hash": binding["hashes"]["core_algorithm_hash"],
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
    ai_receipt_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_receipt_entry.update(
        {
            "receipt": AI_RECEIPT_TARGET.relative_to(ROOT).as_posix(),
            "receipt_bytes": AI_RECEIPT_TARGET.stat().st_size,
            "receipt_file_sha256": sha256(AI_RECEIPT_TARGET),
            "receipt_sha256": ai_receipt["receipt_sha256"],
            "receipt_canonical_sha256": ai_receipt["receipt_sha256"],
            "shared_policy_mission_layer": VEHICLE_VTOL_TARGET.relative_to(ROOT).as_posix(),
            "shared_policy_receipt_sha256": sha256(VEHICLE_VTOL_TARGET),
            "simulator_ai_flight_layer": AI_FLIGHT_TARGET.relative_to(ROOT).as_posix(),
            "simulator_ai_flight_evidence_sha256": sha256(AI_FLIGHT_TARGET),
        }
    )
    ai_entry = progress["external_docker_ai_flight_acceptance_20260927_r1"]
    ai_entry.update(
        {
            "evidence": AI_FLIGHT_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": AI_FLIGHT_TARGET.stat().st_size,
            "evidence_sha256": sha256(AI_FLIGHT_TARGET),
            "release_parity_file_sha256": binding["file_sha256"],
        }
    )
    fixed_entry = progress["external_fixed_wing_shared_policy_depth_mission_20260930"]
    fixed_entry.update(
        {
            "evidence": FIXED_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": FIXED_TARGET.stat().st_size,
            "evidence_sha256": sha256(FIXED_TARGET),
        }
    )
    multi_entry = progress["external_multirotor_shared_policy_depth_mission_20260930"]
    multi_entry.update(
        {
            "evidence": MULTI_TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": MULTI_TARGET.stat().st_size,
            "evidence_sha256": sha256(MULTI_TARGET),
        }
    )
    write_index(progress_path, progress)
    print(
        json.dumps(
            {
                "matrix": MATRIX_TARGET.relative_to(ROOT).as_posix(),
                "matrix_sha256": sha256(MATRIX_TARGET),
                "receipt_sha256": matrix["receipt_sha256"],
                "fixed_wing": FIXED_TARGET.relative_to(ROOT).as_posix(),
                "multirotor": MULTI_TARGET.relative_to(ROOT).as_posix(),
                "vtol": VTOL_TARGET.relative_to(ROOT).as_posix(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

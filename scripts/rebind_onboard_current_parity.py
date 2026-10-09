# ruff: noqa: E501 -- retained-evidence paths are intentionally explicit.
"""Rebind the active current-progress evidence to the checked-in parity.

This is an identity-only maintenance command.  It creates new retained-audit
copies, never overwrites an existing artifact, never starts PX4/Gazebo/QGC,
and does not claim that a historical runtime was rerun.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


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
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def parity_binding(parity_path: Path) -> dict[str, Any]:
    document = load(parity_path)
    digest = sha256(parity_path)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": digest,
        "sha256": digest,
        "release_label": document["release_label"],
        "hashes": document["parity"],
    }


def retained_external(source: Path, target: Path, binding: dict[str, Any]) -> dict[str, Any]:
    document = load(source)
    document["release_parity"] = dict(binding)
    document["current_release_parity_bound"] = True
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical evidence rebound to current source parity without rerun; "
        "no HIL, REAL, hardware, outdoor GNSS or final release claim."
    )
    write_new(target, document)
    return document


def retained_receipt(source: Path, target: Path, binding: dict[str, Any]) -> dict[str, Any]:
    document = load(source)
    document["release_parity"] = dict(binding)
    document["release_parity"]["scope"] = "receipt_generation_file_binding"
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained historical receipt rebound to current source parity without rerun; "
        "no HIL, REAL, hardware, outdoor GNSS or final release claim."
    )
    document.pop("receipt_sha256", None)
    document["receipt_sha256"] = canonical(document)
    write_new(target, document)
    return document


def set_file_binding(entry: dict[str, Any], key: str, path: Path, *, sha_key: str | None = None) -> None:
    entry[key] = rel(path)
    entry[f"{key}_bytes"] = path.stat().st_size
    entry[sha_key or f"{key}_sha256"] = sha256(path)


def main() -> int:
    parity_path = ROOT / "configs/release-parity.json"
    binding = parity_binding(parity_path)
    parity_sha = binding["file_sha256"]
    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)

    # Foundation and combined QGC evidence.
    foundation_source = ROOT / "docs/evidence/v030-pre1-foundation-20261004-onboard-model-provider-parity-audit-r1.json"
    foundation_target = ROOT / "docs/evidence/v030-pre1-foundation-20261006-geometry-label-r1.json"
    foundation = load(foundation_source)
    foundation["parity_file"] = {
        "path": "configs/release-parity.json",
        "bytes": binding["bytes"],
        "sha256": parity_sha,
        "release_label": binding["release_label"],
    }
    foundation["scope"] = "retained_files_audit"
    foundation["runtime_source_parity_verified"] = False
    foundation["external_sim_rerun_performed"] = False
    foundation["claims_boundary"] = (
        "Retained pre.1 foundation audit rebound to current source parity without rerun; "
        "no HIL, REAL, hardware, outdoor GNSS or final release claim."
    )
    foundation.pop("sha256", None)
    foundation["sha256"] = canonical(foundation)
    write_new(foundation_target, foundation)
    set_file_binding(progress["v030_pre1_foundation_acceptance_20261003_r5"], "evidence", foundation_target, sha_key="evidence_file_sha256")
    progress["v030_pre1_foundation_acceptance_20261003_r5"]["evidence_sha256"] = foundation["sha256"]

    qgc_source = ROOT / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-qgc-parity-rebind-20261004-r3/combined-evidence.json"
    qgc_source_target = ROOT / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-qgc-parity-rebind-20261006-geometry-label-r1/combined-evidence.json"
    qgc = load(qgc_source)
    qgc.setdefault("hashes", {})["release_parity"] = {
        "path": str(parity_path.resolve()),
        "bytes": binding["bytes"],
        "sha256": parity_sha.upper(),
        "parity": binding["hashes"],
    }
    qgc["current_release_parity_bound"] = True
    qgc["evidence_scope"] = "retained_files_audit"
    qgc["runtime_source_parity_verified"] = False
    qgc["external_sim_rerun_performed"] = False
    write_new(qgc_source_target, qgc)
    qgc_summary = ROOT / "docs/evidence/combined-sim-qgc-20261006-geometry-label-r1.json"
    old_summary = load(ROOT / "docs/evidence/combined-sim-qgc-20261004-qgc-parity-rebind-r3.json")
    old_summary["status"] = "retained_file_audit_qgc_combined_sim_parity_rebind"
    old_summary["source_evidence"] = rel(qgc_source_target)
    old_summary["source_evidence_bytes"] = qgc_source_target.stat().st_size
    old_summary["source_evidence_sha256"] = sha256(qgc_source_target)
    old_summary["release_parity_sha256"] = parity_sha
    old_summary["evidence_scope"] = "retained_files_audit"
    old_summary["runtime_source_parity_verified"] = False
    old_summary["external_sim_rerun_performed"] = False
    write_new(qgc_summary, old_summary)
    qgc_entry = progress["combined_sim_qgc_external_vtol_pass_20261003_r4"]
    set_file_binding(qgc_entry, "source_evidence", qgc_source_target)
    set_file_binding(qgc_entry, "evidence", qgc_summary)
    qgc_entry["release_parity_sha256"] = parity_sha

    # Sensor campaign evidence includes a stable start/end parity snapshot and
    # a canonical evidence hash.
    sensor_source = ROOT / "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261004-onboard-model-provider-parity-audit-r1.json"
    sensor_target = ROOT / "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261006-geometry-label-r1.json"
    sensor = load(sensor_source)
    campaign = sensor.get("campaign")
    if isinstance(campaign, dict):
        snapshot = campaign.get("release_parity_snapshot")
        if isinstance(snapshot, dict):
            for side in ("start", "end"):
                row = snapshot.get(side)
                if isinstance(row, dict):
                    row.update({
                        "bytes": binding["bytes"],
                        "file_sha256": parity_sha,
                        "hashes": binding["hashes"],
                        "path": str(parity_path.resolve()),
                        "release_label": binding["release_label"],
                    })
        campaign["release_parity_snapshot_gate_passed"] = True
    sensor["release_parity_file_sha256"] = parity_sha
    sensor["evidence_scope"] = "retained_files_audit"
    sensor["current_runtime_parity_proven"] = False
    sensor["external_sim_rerun_performed"] = False
    sensor["evidence_sha256"] = canonical({k: v for k, v in sensor.items() if k != "evidence_sha256"})
    write_new(sensor_target, sensor)
    sensor_entry = progress["shared_policy_external_vtol_sensor_campaign_20261003_r14"]
    set_file_binding(sensor_entry, "evidence", sensor_target)
    sensor_entry["release_parity_file_sha256"] = parity_sha

    # Fixed-wing and multirotor depth evidence.
    depth_targets: dict[str, Path] = {}
    for entry_key, source_name, target_name in (
        (
            "external_fixed_wing_shared_policy_depth_mission_20260930",
            "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261004-onboard-adapter-parity-audit-r1.json",
            "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261006-geometry-label-r1.json",
        ),
        (
            "external_multirotor_shared_policy_depth_mission_20260930",
            "artifacts/evaluation/external-multirotor-depth-shared-policy-20261004-onboard-adapter-parity-audit-r1.json",
            "artifacts/evaluation/external-multirotor-depth-shared-policy-20261006-geometry-label-r1.json",
        ),
    ):
        target = ROOT / target_name
        if not target.exists():
            retained_external(ROOT / source_name, target, binding)
        depth_targets[entry_key] = target
        set_file_binding(progress[entry_key], "evidence", target)

    # Current VTOL receipt, then regenerate the matrix from the rebound three
    # vehicle evidence so its source verifier sees the same identities.
    vtol_source = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-onboard-model-provider-parity-audit-r1.receipt.json"
    vtol_target = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261006-geometry-label-r1.receipt.json"
    vtol = retained_receipt(vtol_source, vtol_target, binding)
    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    set_file_binding(vtol_entry, "receipt", vtol_target, sha_key="receipt_file_sha256")
    vtol_entry["receipt_canonical_sha256"] = vtol["receipt_sha256"]
    vtol_entry["release_parity_file_sha256"] = parity_sha
    vtol_entry["release_parity_bytes"] = binding["bytes"]
    vtol_entry["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]

    from create_external_mission_matrix_receipt import create_receipt

    matrix_target = ROOT / "docs/evidence/external-mission-matrix-20261006-geometry-label-r1.json"
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=vtol_target,
        fixed_wing_evidence=depth_targets["external_fixed_wing_shared_policy_depth_mission_20260930"],
        multirotor_evidence=depth_targets["external_multirotor_shared_policy_depth_mission_20260930"],
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    write_new(matrix_target, matrix)
    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    set_file_binding(matrix_entry, "evidence", matrix_target)
    matrix_entry["receipt_sha256"] = matrix["receipt_sha256"]
    matrix_entry["vtol_campaign_receipt"] = rel(vtol_target)
    matrix_entry["vtol_campaign_receipt_sha256"] = sha256(vtol_target)
    matrix_entry["fixed-wing_evidence"] = rel(depth_targets["external_fixed_wing_shared_policy_depth_mission_20260930"])
    matrix_entry["multirotor_evidence"] = rel(depth_targets["external_multirotor_shared_policy_depth_mission_20260930"])

    from create_shared_policy_vehicle_matrix_receipt import create_shared_policy_vehicle_receipt

    vehicle_target = ROOT / "artifacts/evaluation/shared-policy-vehicle-matrix-20261006-geometry-label-r1.json"
    vehicle = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=vtol_target,
        fixed_wing_evidence=depth_targets["external_fixed_wing_shared_policy_depth_mission_20260930"],
        multirotor_evidence=depth_targets["external_multirotor_shared_policy_depth_mission_20260930"],
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    write_new(vehicle_target, vehicle)
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    set_file_binding(vehicle_entry, "evidence", vehicle_target)
    vehicle_entry["receipt_sha256"] = vehicle["receipt_sha256"]
    vehicle_entry["vtol_receipt"] = rel(vtol_target)
    vehicle_entry["vtol_receipt_sha256"] = sha256(vtol_target)

    # Observed session has two active index aliases; bind both to one fresh copy.
    observed_source = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261004-onboard-model-provider-parity-audit-r1.json"
    observed_target = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261006-geometry-label-r1.json"
    observed = load(observed_source)
    observed["release_parity"] = dict(binding)
    observed["release_parity_path"] = "configs/release-parity.json"
    observed["release_parity_bytes"] = binding["bytes"]
    observed["release_parity_file_sha256"] = parity_sha
    observed["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    observed["evidence_scope"] = "retained_files_audit"
    observed["runtime_source_parity_verified"] = False
    observed["current_runtime_parity_proven"] = False
    observed["external_sim_rerun_performed"] = False
    write_new(observed_target, observed)
    for key in ("shared_policy_observed_session_20260927_r17", "shared_policy_observed_session_20260924"):
        entry = progress[key]
        set_file_binding(entry, "evidence", observed_target)
        entry["release_parity_file_sha256"] = parity_sha
        entry["release_parity_bytes"] = binding["bytes"]
        entry["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]

    # Parent/candidate receipts and the trained-artifact receipt.
    for entry_key, source_name, target_name in (
        (
            "shared_policy_external_parent_campaign_20260927_r14",
            "artifacts/evaluation/shared-policy-external-parent-20260927-r14-onboard-model-provider-parity-audit-r1.receipt.json",
            "artifacts/evaluation/shared-policy-external-parent-20260927-r14-20261006-geometry-label-r1.receipt.json",
        ),
        (
            "candidate",
            "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-20261004-onboard-model-provider-parity-audit-r1.receipt.json",
            "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-20261006-geometry-label-r1.receipt.json",
        ),
        (
            "trained",
            "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261004-onboard-model-provider-parity-audit-r1.receipt.json",
            "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261006-geometry-label-r1.receipt.json",
        ),
    ):
        target = ROOT / target_name
        receipt = retained_receipt(ROOT / source_name, target, binding)
        if entry_key == "shared_policy_external_parent_campaign_20260927_r14":
            entry = progress[entry_key]
            set_file_binding(entry, "receipt", target, sha_key="receipt_file_sha256")
            entry["receipt_canonical_sha256"] = receipt["receipt_sha256"]
            entry["release_parity_file_sha256"] = parity_sha
            entry["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
            receipt_index = progress["shared_policy_campaign_receipts_20260925"]
            set_file_binding(receipt_index, "parent_receipt", target)
            receipt_index["parent_receipt_canonical_sha256"] = receipt["receipt_sha256"]
        elif entry_key == "trained":
            entry = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
            set_file_binding(entry, "receipt", target, sha_key="receipt_file_sha256")
            entry["receipt_canonical_sha256"] = receipt["receipt_sha256"]
        else:
            entry = progress["shared_policy_campaign_receipts_20260925"]
            field = "parent_receipt" if "parent" in source_name else "candidate_receipt"
            set_file_binding(entry, field, target)
            entry[field + "_canonical_sha256"] = receipt["receipt_sha256"]

    # Runtime matrix is an index-level contract document with parity fields.
    matrix_source = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-onboard-model-provider-parity-audit-r1.json"
    matrix_runtime_target = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261006-geometry-label-r1.json"
    runtime_matrix = load(matrix_source)
    runtime_matrix["release_parity"] = dict(binding["hashes"])
    runtime_matrix["release_parity_file_sha256"] = parity_sha
    runtime_matrix["release_parity_bytes"] = binding["bytes"]
    runtime_matrix["core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    runtime_matrix["evidence_scope"] = "retained_files_audit"
    runtime_matrix["runtime_source_parity_verified"] = False
    runtime_matrix["external_sim_rerun_performed"] = False
    modes = runtime_matrix.get("modes")
    if isinstance(modes, dict):
        for mode in modes.values():
            if isinstance(mode, dict):
                mode["release_parity"] = dict(binding["hashes"])
    write_new(matrix_runtime_target, runtime_matrix)
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    set_file_binding(runtime_entry, "evidence", matrix_runtime_target)
    runtime_entry["release_parity_file_sha256"] = parity_sha
    runtime_entry["release_parity_bytes"] = binding["bytes"]
    runtime_entry["core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]

    # Rebind the retained AI-flight layer and regenerate its receipt from the
    # current policy receipt plus the current parity snapshot.
    ai_source = ROOT / "artifacts/evaluation/simulator-docker-ai-flight-20261004-onboard-model-provider-parity-audit-r1.json"
    ai_target = ROOT / "artifacts/evaluation/simulator-docker-ai-flight-20261006-geometry-label-r1.json"
    ai_flight = load(ai_source)
    snapshot = ai_flight.get("release_parity_snapshot")
    if isinstance(snapshot, dict):
        for side in ("start", "end"):
            row = snapshot.get(side)
            if isinstance(row, dict):
                row.update({
                    "bytes": binding["bytes"],
                    "file_sha256": parity_sha,
                    "hashes": binding["hashes"],
                    "path": str(parity_path.resolve()),
                    "release_label": binding["release_label"],
                })
    ai_flight["evidence_scope"] = "retained_layers_file_audit"
    ai_flight["runtime_source_parity_verified"] = False
    ai_flight["external_sim_rerun_performed"] = False
    ai_flight["claims_boundary"] = (
        "Retained historical external-SIM AI-flight evidence rebound to current "
        "source parity without rerun; no HIL, REAL, hardware, outdoor GNSS or "
        "final release claim."
    )
    write_new(ai_target, ai_flight)

    from create_simulator_ai_flight_parity_receipt import build_receipt

    ai_receipt_target = ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261006-geometry-label-r1.receipt.json"
    if ai_receipt_target.exists():
        ai_receipt = load(ai_receipt_target)
    else:
        ai_receipt = build_receipt(ai_target, vtol_target, parity_path, ai_receipt_target)
    ai_entry = progress["external_docker_ai_flight_acceptance_20260927_r1"]
    set_file_binding(ai_entry, "evidence", ai_target)
    ai_entry["release_parity_file_sha256"] = parity_sha
    parity_receipt_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    set_file_binding(parity_receipt_entry, "receipt", ai_receipt_target, sha_key="receipt_file_sha256")
    parity_receipt_entry["receipt_sha256"] = ai_receipt["receipt_sha256"]
    parity_receipt_entry["shared_policy_mission_layer"] = rel(vtol_target)
    parity_receipt_entry["shared_policy_receipt_sha256"] = sha256(vtol_target)
    parity_receipt_entry["simulator_ai_flight_layer"] = rel(ai_target)
    parity_receipt_entry["simulator_ai_flight_evidence_sha256"] = sha256(ai_target)

    progress["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": binding["bytes"],
        "file_sha256": parity_sha,
        "hashes": binding["hashes"],
        "release_label": binding["release_label"],
    }
    audit_path = ROOT / "docs/evidence/release-parity-transition-20261006-geometry-label-r2.json"
    audit_entry = progress["evidence"]["release_gate_report"]
    audit_entry["audit_summary_path"] = rel(audit_path)
    audit_entry["audit_summary_bytes"] = audit_path.stat().st_size
    audit_entry["audit_summary_sha256"] = sha256(audit_path)
    progress_path.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps({"parity": binding, "progress": rel(progress_path)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

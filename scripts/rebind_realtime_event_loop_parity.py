"""Rebind the active prerelease evidence index after the realtime event-loop change.

The event loop changes the shared core parity, but the external PX4/Gazebo and
QGC runs were not rerun in this maintenance pass.  This script creates new,
immutable retained-file-audit copies, updates only the active progress-index
references, and leaves every historical artifact untouched.
"""

# Evidence paths are intentionally descriptive and exceed the default line
# length in a few places; keep the audit filenames stable.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.create_external_mission_matrix_receipt import create_receipt  # noqa: E402,I001
from scripts.create_shared_policy_vehicle_matrix_receipt import create_shared_policy_vehicle_receipt  # noqa: E402,I001


PARITY_KEYS = {
    "core_algorithm_hash",
    "px4_mission_path_hash",
    "safety_policy_hash",
    "task_contract_hash",
    "ui_contract_hash",
}


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


def write_new(path: Path, value: dict[str, Any], *, overwrite: bool = False) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        if not overwrite:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def parity_binding() -> dict[str, Any]:
    path = ROOT / "configs/release-parity.json"
    document = load(path)
    digest = sha256(path)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": path.stat().st_size,
        "file_sha256": digest,
        "hashes": document["parity"],
        "release_label": document["release_label"],
    }


def _is_hash_mapping(value: dict[str, Any]) -> bool:
    return PARITY_KEYS <= set(value) and all(
        isinstance(value[key], str) and len(value[key]) == 64 for key in PARITY_KEYS
    )


def _patch_parity_mapping(value: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    output = dict(value)
    hashes = dict(binding["hashes"])
    for key in PARITY_KEYS:
        if key in output:
            output[key] = hashes[key]
    if "path" in output:
        output["path"] = binding["path"]
    if "bytes" in output:
        output["bytes"] = binding["bytes"]
    if "sha256" in output:
        output["sha256"] = binding["file_sha256"]
    if "file_sha256" in output:
        output["file_sha256"] = binding["file_sha256"]
    if "release_label" in output:
        output["release_label"] = binding["release_label"]
    if "parity" in output:
        output["parity"] = hashes
    if "hashes" in output and isinstance(output["hashes"], dict):
        output["hashes"] = hashes
    return output


def transform(value: Any, binding: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [transform(item, binding) for item in value]
    if not isinstance(value, dict):
        return value
    output = {key: transform(item, binding) for key, item in value.items()}
    parity_path = output.get("path")
    if _is_hash_mapping(output) or (
        isinstance(parity_path, str)
        and parity_path.replace("\\", "/").endswith("configs/release-parity.json")
        and any(key in output for key in ("sha256", "file_sha256", "bytes"))
    ):
        output = _patch_parity_mapping(output, binding)
    release = output.get("release_parity")
    if isinstance(release, dict):
        output["release_parity"] = _patch_parity_mapping(release, binding)
    if "release_parity_file_sha256" in output:
        output["release_parity_file_sha256"] = binding["file_sha256"]
    if "release_parity_sha256" in output:
        output["release_parity_sha256"] = binding["file_sha256"]
    if "release_parity_bytes" in output:
        output["release_parity_bytes"] = binding["bytes"]
    if "release_parity_core_algorithm_hash" in output:
        output["release_parity_core_algorithm_hash"] = binding["hashes"][
            "core_algorithm_hash"
        ]
    if "release_parity_px4_mission_path_hash" in output:
        output["release_parity_px4_mission_path_hash"] = binding["hashes"][
            "px4_mission_path_hash"
        ]
    return output


def clone_json(
    source: Path,
    target: Path,
    binding: dict[str, Any],
    *,
    receipt: bool = False,
    overwrite: bool = True,
) -> dict[str, Any]:
    document = transform(load(source), binding)
    if isinstance(document, dict):
        if "evidence_scope" in document:
            document["evidence_scope"] = (
                "retained_layers_file_audit"
                if document["evidence_scope"] == "retained_layers_file_audit"
                else "retained_files_audit"
            )
        if "runtime_source_parity_verified" in document:
            document["runtime_source_parity_verified"] = False
        if "current_runtime_parity_proven" in document:
            document["current_runtime_parity_proven"] = False
        if "external_sim_rerun_performed" in document:
            document["external_sim_rerun_performed"] = False
        if receipt or "receipt_sha256" in document:
            document.pop("receipt_sha256", None)
            document["receipt_sha256"] = canonical(document)
    write_new(target, document, overwrite=overwrite)
    return document


def ref(path: Path) -> tuple[str, int, str]:
    return path.relative_to(ROOT).as_posix(), path.stat().st_size, sha256(path)


def update_ref(entry: dict[str, Any], path_key: str, path: Path, *, sha_key: str | None = None) -> None:
    relative, size, digest = ref(path)
    entry[path_key] = relative
    entry[f"{path_key}_bytes"] = size
    entry[sha_key or f"{path_key}_sha256"] = digest


def main() -> int:
    binding = parity_binding()
    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)
    progress["release_parity"] = binding

    qgc = progress["combined_sim_qgc_external_vtol_pass_20261003_r4"]
    qgc_source = ROOT / qgc["source_evidence"]
    qgc_source_target = ROOT / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-realtime-event-loop-parity-audit-r1/combined-evidence.json"
    clone_json(qgc_source, qgc_source_target, binding)
    qgc_summary = ROOT / qgc["evidence"]
    qgc_summary_target = ROOT / "docs/evidence/combined-sim-qgc-20261004-realtime-event-loop-parity-audit-r1.json"
    qgc_summary_document = clone_json(qgc_summary, qgc_summary_target, binding)
    qgc_summary_document["source_evidence"] = qgc_source_target.relative_to(ROOT).as_posix()
    qgc_summary_document["source_evidence_bytes"] = qgc_source_target.stat().st_size
    qgc_summary_document["source_evidence_sha256"] = sha256(qgc_source_target)
    write_new(qgc_summary_target, qgc_summary_document, overwrite=True)
    update_ref(qgc, "source_evidence", qgc_source_target)
    update_ref(qgc, "evidence", qgc_summary_target)
    qgc["release_parity_sha256"] = binding["file_sha256"]

    fixed_entry = progress["external_fixed_wing_shared_policy_depth_mission_20260930"]
    multi_entry = progress["external_multirotor_shared_policy_depth_mission_20260930"]
    fixed_target = ROOT / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261004-realtime-event-loop-parity-audit-r1.json"
    multi_target = ROOT / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261004-realtime-event-loop-parity-audit-r1.json"
    clone_json(ROOT / fixed_entry["evidence"], fixed_target, binding)
    clone_json(ROOT / multi_entry["evidence"], multi_target, binding)
    update_ref(fixed_entry, "evidence", fixed_target)
    update_ref(multi_entry, "evidence", multi_target)
    fixed_entry["current_release_parity_bound"] = True
    multi_entry["current_release_parity_bound"] = True

    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_source = ROOT / vtol_entry["receipt"]
    vtol_target = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-realtime-event-loop-parity-audit-r1.receipt.json"
    vtol_receipt = clone_json(vtol_source, vtol_target, binding, receipt=True)
    update_ref(vtol_entry, "receipt", vtol_target, sha_key="receipt_file_sha256")
    vtol_entry["receipt_canonical_sha256"] = vtol_receipt["receipt_sha256"]
    vtol_entry["release_parity_file_sha256"] = binding["file_sha256"]
    vtol_entry["release_parity_bytes"] = binding["bytes"]
    vtol_entry["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]

    # The cross-vehicle matrix intentionally uses its own VTOL receipt (the
    # airframe-bound trained artifact), while the current campaign entry above
    # points at the depth-guard campaign receipt.  Preserve that distinction.
    matrix_vtol_source = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-px4-path-rebind-r1.receipt.json"
    matrix_vtol_target = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-matrix-realtime-event-loop-parity-audit-r1.receipt.json"
    clone_json(matrix_vtol_source, matrix_vtol_target, binding, receipt=True)

    # The current parent/candidate receipts are retained-file audits too.
    parent_entry = progress["shared_policy_external_parent_campaign_20260927_r14"]
    parent_target = ROOT / "artifacts/evaluation/shared-policy-external-parent-20260927-r14-realtime-event-loop-parity-audit-r1.receipt.json"
    parent_receipt = clone_json(ROOT / parent_entry["receipt"], parent_target, binding, receipt=True)
    update_ref(parent_entry, "receipt", parent_target, sha_key="receipt_file_sha256")
    parent_entry["receipt_canonical_sha256"] = parent_receipt["receipt_sha256"]
    parent_entry["release_parity_file_sha256"] = binding["file_sha256"]
    parent_entry["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]
    candidate_target = ROOT / "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-realtime-event-loop-parity-audit-r1.receipt.json"
    candidate_source = ROOT / progress["shared_policy_campaign_receipts_20260925"]["candidate_receipt"]
    candidate_receipt = clone_json(candidate_source, candidate_target, binding, receipt=True)
    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    update_ref(receipt_index, "candidate_receipt", candidate_target, sha_key="candidate_receipt_sha256")
    receipt_index["candidate_receipt_canonical_sha256"] = candidate_receipt["receipt_sha256"]
    update_ref(receipt_index, "parent_receipt", parent_target, sha_key="parent_receipt_sha256")
    receipt_index["parent_receipt_canonical_sha256"] = parent_receipt["receipt_sha256"]
    receipt_index["release_parity_bound"] = False
    receipt_index["audit_release_parity_bound"] = True
    receipt_index["current_runtime_parity_proven"] = False

    observed_target = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261004-realtime-event-loop-parity-audit-r1.json"
    clone_json(ROOT / progress["shared_policy_observed_session_20260927_r17"]["evidence"], observed_target, binding)
    for key in ("shared_policy_observed_session_20260927_r17", "shared_policy_observed_session_20260924"):
        entry = progress[key]
        update_ref(entry, "evidence", observed_target)
        entry["release_parity_file_sha256"] = binding["file_sha256"]
        entry["release_parity_bytes"] = binding["bytes"]
        entry["release_parity_core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]

    runtime_target = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-realtime-event-loop-parity-audit-r1.json"
    clone_json(ROOT / progress["shared_policy_runtime_matrix_20260927_r3"]["evidence"], runtime_target, binding)
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    update_ref(runtime_entry, "evidence", runtime_target)
    runtime_entry["release_parity_file_sha256"] = binding["file_sha256"]
    runtime_entry["release_parity_bytes"] = binding["bytes"]
    runtime_entry["core_algorithm_hash"] = binding["hashes"]["core_algorithm_hash"]

    trained_entry = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
    trained_target = ROOT / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261004-realtime-event-loop-parity-audit-r1.receipt.json"
    trained_receipt = clone_json(ROOT / trained_entry["receipt"], trained_target, binding, receipt=True)
    update_ref(trained_entry, "receipt", trained_target, sha_key="receipt_file_sha256")
    trained_entry["receipt_canonical_sha256"] = trained_receipt["receipt_sha256"]

    sensor_entry = progress["shared_policy_external_vtol_sensor_campaign_20261003_r14"]
    sensor_target = ROOT / "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261004-realtime-event-loop-parity-audit-r1.json"
    clone_json(ROOT / sensor_entry["evidence"], sensor_target, binding)
    update_ref(sensor_entry, "evidence", sensor_target)
    sensor_entry["release_parity_file_sha256"] = binding["file_sha256"]

    # Rebuild both current three-vehicle matrix receipts from the new source copies.
    matrix_target = ROOT / "docs/evidence/external-mission-matrix-20261004-realtime-event-loop-parity-audit-r1.json"
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=matrix_vtol_target,
        fixed_wing_evidence=fixed_target,
        multirotor_evidence=multi_target,
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    write_new(matrix_target, matrix, overwrite=True)
    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    update_ref(matrix_entry, "evidence", matrix_target)
    matrix_entry["receipt_sha256"] = matrix["receipt_sha256"]

    vehicle_target = ROOT / "artifacts/evaluation/shared-policy-vehicle-matrix-20261004-realtime-event-loop-parity-audit-r1.json"
    vehicle = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=matrix_vtol_target,
        fixed_wing_evidence=fixed_target,
        multirotor_evidence=multi_target,
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    write_new(vehicle_target, vehicle, overwrite=True)
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    update_ref(vehicle_entry, "evidence", vehicle_target)
    vehicle_entry["receipt_sha256"] = vehicle["receipt_sha256"]
    vehicle_entry["vtol_receipt"] = matrix_vtol_target.relative_to(ROOT).as_posix()
    vehicle_entry["vtol_receipt_sha256"] = sha256(matrix_vtol_target)

    # The AI parity receipt validates its two source layers independently.
    ai_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_source = ROOT / "artifacts/evaluation/simulator-docker-ai-flight-20261004-onboard-adapter-parity-audit-r2.json"
    ai_target = ROOT / "artifacts/evaluation/simulator-docker-ai-flight-20261004-realtime-event-loop-parity-audit-r1.json"
    clone_json(ai_source, ai_target, binding)
    update_ref(progress["external_docker_ai_flight_acceptance_20260927_r1"], "evidence", ai_target)
    ai_receipt_target = ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261004-realtime-event-loop-parity-audit-r1.receipt.json"
    ai_receipt = clone_json(
        ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261004-px4-path-rebind-r1.receipt.json",
        ai_receipt_target,
        binding,
        receipt=True,
        overwrite=True,
    )
    policy_layer = ai_receipt.get("shared_policy_mission_layer")
    if isinstance(policy_layer, dict):
        policy_layer.update(
            {
                "path": str(vtol_target.resolve()),
                "bytes": vtol_target.stat().st_size,
                "file_sha256": sha256(vtol_target),
                "receipt_sha256": vtol_receipt["receipt_sha256"],
                "runtime_source_parity_verified": False,
                "runtime_parity_snapshot_verified": False,
            }
        )
    ai_layer = ai_receipt.get("simulator_ai_flight_layer")
    if isinstance(ai_layer, dict):
        ai_layer.update(
            {
                "path": str(ai_target.resolve()),
                "bytes": ai_target.stat().st_size,
                "file_sha256": sha256(ai_target),
                "runtime_parity_snapshot_verified": False,
            }
        )
    if "shared_policy_receipt_sha256" in ai_receipt:
        ai_receipt["shared_policy_receipt_sha256"] = vtol_receipt["receipt_sha256"]
    if "simulator_ai_flight_evidence_sha256" in ai_receipt:
        ai_receipt["simulator_ai_flight_evidence_sha256"] = sha256(ai_target)
    # Reuse the canonical layer auditors so path/byte/hash/claim fields are
    # byte-for-byte identical to what the progress verifier will recompute.
    from scripts.create_simulator_ai_flight_parity_receipt import (
        _parity,
        _validate_ai_report,
        _validate_policy_receipt,
    )

    audited_policy = _validate_policy_receipt(vtol_target, _parity(ROOT / "configs/release-parity.json"))
    audited_ai = _validate_ai_report(
        ai_target,
        audited_policy,
        _parity(ROOT / "configs/release-parity.json"),
    )
    ai_receipt["shared_policy_mission_layer"] = audited_policy
    ai_receipt["simulator_ai_flight_layer"] = audited_ai
    ai_receipt.pop("receipt_sha256", None)
    ai_receipt["receipt_sha256"] = canonical(ai_receipt)
    # The first clone is an intermediate file created by this same script;
    # replace that one intermediate with the final source-layer references.
    write_new(ai_receipt_target, ai_receipt, overwrite=True)
    update_ref(ai_entry, "receipt", ai_receipt_target, sha_key="receipt_file_sha256")
    ai_entry["receipt_sha256"] = ai_receipt["receipt_sha256"]
    ai_entry["same_release_parity"] = False
    ai_entry["current_runtime_parity_proven"] = False

    # Foundation and release-gate audits are source-bound reports, not reruns.
    foundation_entry = progress["v030_pre1_foundation_acceptance_20261003_r5"]
    foundation_target = ROOT / "docs/evidence/v030-pre1-foundation-20261004-realtime-event-loop-parity-audit-r1.json"
    foundation = transform(load(ROOT / foundation_entry["evidence"]), binding)
    foundation.pop("sha256", None)
    foundation["sha256"] = canonical(foundation)
    write_new(foundation_target, foundation, overwrite=True)
    update_ref(foundation_entry, "evidence", foundation_target, sha_key="evidence_file_sha256")
    foundation_entry["evidence_sha256"] = foundation["sha256"]

    gate_audit = ROOT / "docs/evidence/release-parity-transition-20261004-realtime-event-loop-parity-audit-r1.json"
    gate_source = ROOT / "docs/evidence/release-parity-transition-20261004-onboard-adapter-parity-audit-r3.json"
    gate_document = transform(load(gate_source), binding)
    gate_document["current_parity_file"] = {
        "path": binding["path"],
        "bytes": binding["bytes"],
        "sha256": binding["file_sha256"],
    }
    gate_document["current_parity"] = binding["hashes"]
    mismatches = gate_document.get("mode_parity_mismatches", {})
    if isinstance(mismatches, dict):
        # The historical mode snapshots intentionally remain mismatched; this
        # audit must not turn a source rebind into a runtime rerun claim.
        gate_document["current_release_parity_bound"] = not any(mismatches.values())
    write_new(gate_audit, gate_document, overwrite=True)
    gate_entry = progress["evidence"]["release_gate_report"]
    update_ref(gate_entry, "audit_summary_path", gate_audit, sha_key="audit_summary_sha256")
    gate_entry["audit_summary_bytes"] = gate_audit.stat().st_size
    gate_entry["current_release_parity_bound"] = gate_document["current_release_parity_bound"]

    progress["updated_at_utc"] = "2026-10-04T00:00:00Z"
    write_new(progress_path, progress, overwrite=True)
    print(json.dumps({"release_parity": binding, "updated_progress": str(progress_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rebind the current progress index to the checked-in release parity.

This maintenance command updates ``docs/current-progress.json`` and writes
new source-bound copies for the current index. It never edits historical
evidence and never claims that an existing runtime was rerun. The generated
copies remain byte-stable and refuse accidental overwrite; the source-bound
receipt generators own the evidence-specific transformations.
"""

# Evidence filenames are intentionally immutable, timestamped identities; the
# resulting path literals are occasionally longer than the repository's 100
# character formatting limit.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _rebind_external_evidence(source: Path, target: Path, parity_binding: dict[str, Any]) -> None:
    evidence = _load(source)
    evidence["release_parity"] = dict(parity_binding)
    evidence["current_release_parity_bound"] = True
    _write_new(target, evidence)


def _bind(path: Path) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _rebind_external_depth_entry(entry: dict[str, Any], target: Path) -> None:
    """Point a current-progress depth entry at a fresh external run.

    The run itself is immutable evidence.  Only its derived index fields are
    updated here so the verifier can re-open the same Mission, sensor,
    airframe, cleanup, and safety identities instead of trusting a filename.
    """

    evidence = _load(target)
    sensor = evidence["sensor_transport"]
    transport = sensor["transport"]
    mission = evidence["mission_transaction"]
    cleanup = evidence["container_runtime"]["cleanup"]
    entry.update(
        {
            "evidence": target.relative_to(ROOT).as_posix(),
            "evidence_bytes": target.stat().st_size,
            "evidence_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "current_release_parity_bound": evidence["current_release_parity_bound"],
            "gate_passed": evidence["gate_passed"],
            "runtime_gate_passed": evidence["runtime_gate_passed"],
            "mission_transaction_gate_passed": evidence["mission_transaction_gate_passed"],
            "sensor_transport_gate_passed": evidence["sensor_transport_gate_passed"],
            "shared_policy_used": evidence["shared_policy_used"],
            "policy_artifact_sha256": evidence["policy_artifact_sha256"],
            "ranker_sha256": evidence["ranker_sha256"],
            "planner_revalidated": evidence["task_binding"]["planner_revalidated"],
            "depth_enabled": transport["depth_enabled"],
            "depth_companion_count": transport["depth_companion_count"],
            "depth_calibration_id": transport.get("depth_calibration_id"),
            "depth_topic": transport.get("depth_topic"),
            "depth_calibration_sha256": transport.get("depth_calibration_sha256"),
            "external_airframe_profile_sha256": evidence[
                "external_airframe_profile_sha256"
            ],
            "received_sensor_frames": transport["received_frame_count"],
            "flight_started": mission["flight_started"],
            "physical_output": mission["physical_output_enabled"],
            "policy_applied_to_actuator": evidence["task_binding"][
                "policy_applied_to_actuator"
            ],
            "truth_free": evidence["task_binding"]["truth_free"],
            "container_cleanup": cleanup["removed"],
        }
    )


def _rebind_observed_session(source: Path, target: Path, parity: dict[str, Any]) -> None:
    document = _load(source)
    document["release_parity"] = {
        "path": "configs/release-parity.json",
        "bytes": parity["bytes"],
        "file_sha256": parity["file_sha256"],
        "hashes": parity["hashes"],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if target.exists():
        if target.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
    target.write_bytes(payload)


def _rebind_runtime_matrix(source: Path, target: Path, parity: dict[str, Any]) -> None:
    document = _load(source)
    document["release_parity"] = parity["hashes"]
    for row in document.get("modes", {}).values():
        if isinstance(row, dict):
            row["release_parity"] = parity["hashes"]
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if target.exists():
        if target.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
    target.write_bytes(payload)


def _rebind_receipt_release_parity(
    source: Path,
    target: Path,
    parity_binding: dict[str, Any],
) -> None:
    """Create a source-bound receipt copy without claiming a runtime rerun.

    Receipts whose scope is ``retained_files_audit`` may bind the current
    parity file while explicitly retaining ``runtime_source_parity_verified``
    as false.  This is the same identity-only operation used for other
    source-bound evidence: it changes neither the Mission reports nor the
    runtime claim.
    """

    receipt = _load(source)
    receipt["release_parity"] = dict(parity_binding)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_new(target, receipt)


def main() -> int:
    parity_path = ROOT / "configs/release-parity.json"
    progress_path = ROOT / "docs/current-progress.json"
    parity = _load(parity_path)
    progress = _load(progress_path)
    progress["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": hashlib.sha256(parity_path.read_bytes()).hexdigest(),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
    }
    parity_binding = progress["release_parity"]
    for source_name, target_name in (
        (
            "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261001-r21.json",
            "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261001-r23.json",
        ),
        (
            "artifacts/evaluation/external-multirotor-depth-shared-policy-20261001-r21.json",
            "artifacts/evaluation/external-multirotor-depth-shared-policy-20261001-r23.json",
        ),
    ):
        _rebind_external_evidence(
            ROOT / source_name,
            ROOT / target_name,
            parity_binding,
        )
    observed_target = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261001-r26.json"
    _rebind_observed_session(
        ROOT / "artifacts/evaluation/shared-policy-observed-session-20261001-r24.json",
        observed_target,
        parity_binding,
    )
    runtime_target = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261001-r26.json"
    _rebind_runtime_matrix(
        ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261001-depth-r24.json",
        runtime_target,
        parity_binding,
    )

    fixed_target = ROOT / "artifacts/evaluation/external-fixed-wing-shared-policy-20261001-r30.json"
    multi_target = ROOT / "artifacts/evaluation/external-multirotor-shared-policy-20261001-r30.json"
    fixed_entry = progress["external_fixed_wing_shared_policy_depth_mission_20260930"]
    multi_entry = progress["external_multirotor_shared_policy_depth_mission_20260930"]
    for entry, target in ((fixed_entry, fixed_target), (multi_entry, multi_target)):
        _rebind_external_depth_entry(entry, target)

    observed_ref = _bind(observed_target)
    for key in (
        "shared_policy_observed_session_20260927_r17",
        "shared_policy_observed_session_20260924",
    ):
        entry = progress[key]
        entry["evidence"] = observed_ref["path"]
        entry["evidence_bytes"] = observed_ref["bytes"]
        entry["evidence_sha256"] = observed_ref["sha256"]
        entry["release_parity_file_sha256"] = parity_binding["file_sha256"]
        entry["release_parity_bytes"] = parity_binding["bytes"]
        entry["release_parity_core_algorithm_hash"] = parity_binding["hashes"][
            "core_algorithm_hash"
        ]
    runtime_ref = _bind(runtime_target)
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    runtime_entry["evidence"] = runtime_ref["path"]
    runtime_entry["evidence_bytes"] = runtime_ref["bytes"]
    runtime_entry["evidence_sha256"] = runtime_ref["sha256"]
    runtime_entry["release_parity_file_sha256"] = parity_binding["file_sha256"]
    runtime_entry["release_parity_bytes"] = parity_binding["bytes"]
    runtime_entry["core_algorithm_hash"] = parity_binding["hashes"]["core_algorithm_hash"]

    vtol_evidence = (
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.json"
    )
    vtol_summary = (
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31-run/vtol-mission-acceptance.json"
    )
    vtol_receipt = (
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.receipt.json"
    )
    parent_receipt = (
        ROOT
        / "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-20261001-depth-r29.receipt.json"
    )
    candidate_receipt = (
        ROOT
        / "artifacts/evaluation/shared-policy-external-candidate-20260927-r14-policycapture-parity-20261001-depth-r29.receipt.json"
    )
    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_entry.update(
        {
            "evidence": vtol_evidence.relative_to(ROOT).as_posix(),
            "evidence_bytes": vtol_evidence.stat().st_size,
            "evidence_sha256": hashlib.sha256(vtol_evidence.read_bytes()).hexdigest(),
            "summary": vtol_summary.relative_to(ROOT).as_posix(),
            "summary_sha256": hashlib.sha256(vtol_summary.read_bytes()).hexdigest(),
            "receipt": vtol_receipt.relative_to(ROOT).as_posix(),
            "receipt_bytes": vtol_receipt.stat().st_size,
            "receipt_file_sha256": hashlib.sha256(vtol_receipt.read_bytes()).hexdigest(),
            "receipt_canonical_sha256": _load(vtol_receipt)["receipt_sha256"],
            "release_parity_file_sha256": parity_binding["file_sha256"],
            "release_parity_bytes": parity_binding["bytes"],
            "release_parity_core_algorithm_hash": parity_binding["hashes"]["core_algorithm_hash"],
        }
    )
    parent_entry = progress["shared_policy_external_parent_campaign_20260927_r14"]
    parent_entry.update(
        {
            "receipt": parent_receipt.relative_to(ROOT).as_posix(),
            "receipt_bytes": parent_receipt.stat().st_size,
            "receipt_file_sha256": hashlib.sha256(parent_receipt.read_bytes()).hexdigest(),
            "receipt_canonical_sha256": _load(parent_receipt)["receipt_sha256"],
            "release_parity_file_sha256": parity_binding["file_sha256"],
            "release_parity_core_algorithm_hash": parity_binding["hashes"]["core_algorithm_hash"],
        }
    )
    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    for prefix, path in (("parent", parent_receipt), ("candidate", candidate_receipt)):
        receipt_index[f"{prefix}_receipt"] = path.relative_to(ROOT).as_posix()
        receipt_index[f"{prefix}_receipt_bytes"] = path.stat().st_size
        receipt_index[f"{prefix}_receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt_index[f"{prefix}_receipt_canonical_sha256"] = _load(path)["receipt_sha256"]

    matrix_path = ROOT / "docs/evidence/external-mission-matrix-20261001-r32.json"
    vehicle_path = ROOT / "artifacts/evaluation/shared-policy-vehicle-matrix-20261001-r32.json"
    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    matrix_entry.update(
        {
            "evidence": matrix_path.relative_to(ROOT).as_posix(),
            "evidence_bytes": matrix_path.stat().st_size,
            "evidence_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
            "receipt_sha256": _load(matrix_path)["receipt_sha256"],
            "vtol_campaign_receipt": vtol_receipt.relative_to(ROOT).as_posix(),
            "vtol_campaign_receipt_sha256": hashlib.sha256(vtol_receipt.read_bytes()).hexdigest(),
        }
    )
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    vehicle_entry.update(
        {
            "evidence": vehicle_path.relative_to(ROOT).as_posix(),
            "evidence_bytes": vehicle_path.stat().st_size,
            "evidence_sha256": hashlib.sha256(vehicle_path.read_bytes()).hexdigest(),
            "receipt_sha256": _load(vehicle_path)["receipt_sha256"],
            "vtol_receipt": vtol_receipt.relative_to(ROOT).as_posix(),
            "vtol_receipt_sha256": hashlib.sha256(vtol_receipt.read_bytes()).hexdigest(),
            "vtol_runtime_evidence": vtol_evidence.relative_to(ROOT).as_posix(),
            "vtol_runtime_evidence_sha256": hashlib.sha256(
                vtol_evidence.read_bytes()
            ).hexdigest(),
        }
    )
    trained_receipt = (
        ROOT / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261001-r4.receipt.json"
    )
    trained_entry = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
    trained_entry.update(
        {
            "receipt": trained_receipt.relative_to(ROOT).as_posix(),
            "receipt_bytes": trained_receipt.stat().st_size,
            "receipt_file_sha256": hashlib.sha256(trained_receipt.read_bytes()).hexdigest(),
            "receipt_canonical_sha256": _load(trained_receipt)["receipt_sha256"],
        }
    )
    ai_receipt = (
        ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261001-depth-r29.receipt.json"
    )
    ai_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_entry.update(
        {
            "receipt": ai_receipt.relative_to(ROOT).as_posix(),
            "receipt_bytes": ai_receipt.stat().st_size,
            "receipt_file_sha256": hashlib.sha256(ai_receipt.read_bytes()).hexdigest(),
            "receipt_sha256": _load(ai_receipt)["receipt_sha256"],
            "shared_policy_mission_layer": parent_receipt.relative_to(ROOT).as_posix(),
            "shared_policy_receipt_sha256": _load(parent_receipt)["receipt_sha256"],
        }
    )

    response_root = (
        ROOT / "artifacts/evaluation/px4-reported-position-response-current-parity-20261001-r26"
    )
    response_matrix = response_root / "px4-reported-position-response-matrix-current-parity.json"
    response_campaign = (
        response_root / "px4-reported-position-response-campaign-current-parity.json"
    )
    response_matrix_ref = progress["evidence"]["px4_reported_position_response_matrix"]
    response_matrix_ref.update(
        {
            "local_path": response_matrix.relative_to(ROOT).as_posix(),
            "file_sha256": hashlib.sha256(response_matrix.read_bytes()).hexdigest(),
            "matrix_sha256": _load(response_matrix)["matrix_sha256"],
        }
    )
    response_campaign_ref = progress["evidence"]["px4_reported_position_response_campaign"]
    response_campaign_ref.update(
        {
            "local_path": response_campaign.relative_to(ROOT).as_posix(),
            "file_sha256": hashlib.sha256(response_campaign.read_bytes()).hexdigest(),
            "campaign_sha256": _load(response_campaign)["campaign_sha256"],
        }
    )
    external_join = (
        ROOT / "artifacts/evaluation/simulator-external-evidence-policy-helper-20261001-r26.json"
    )
    external_revalidation = (
        ROOT
        / "artifacts/evaluation/simulator-external-evidence-policy-helper-20261001-r26-revalidated.json"
    )
    external_ref = progress["evidence"]["external_three_airframe_rebind"]
    external_ref.update(
        {
            "local_path": external_join.relative_to(ROOT).as_posix(),
            "file_sha256": hashlib.sha256(external_join.read_bytes()).hexdigest(),
            "verification_local_path": external_revalidation.relative_to(ROOT).as_posix(),
            "verification_file_sha256": hashlib.sha256(
                external_revalidation.read_bytes()
            ).hexdigest(),
        }
    )

    gate_report = (
        ROOT
        / "artifacts/evaluation/release-gate-pre.8-current-20261001T172000Z-v6-bound-report.json"
    )
    gate_manifest = (
        ROOT / "artifacts/evaluation/release-gate-pre.8-current-20261001T172000Z-v6-bound.json"
    )
    gate_audit = (
        ROOT / "docs/evidence/shared-policy-release-parity-source-transition-20261001-r8.json"
    )
    gate_ref = progress["evidence"]["release_gate_report"]
    gate_ref.update(
        {
            "audit_summary_path": gate_audit.relative_to(ROOT).as_posix(),
            "manifest_local_path": gate_manifest.relative_to(ROOT).as_posix(),
            "local_path": gate_report.relative_to(ROOT).as_posix(),
            "file_bytes": gate_report.stat().st_size,
            "file_sha256": hashlib.sha256(gate_report.read_bytes()).hexdigest(),
            "manifest_bytes": gate_manifest.stat().st_size,
            "manifest_sha256": hashlib.sha256(gate_manifest.read_bytes()).hexdigest(),
            "current_release_parity_bound": True,
            "shared_policy_runtime_handoff": {
                "path": "artifacts/evaluation/shared-policy-runtime-handoff-20261001-r3/handoff.json",
                "file_bytes": (
                    ROOT
                    / "artifacts/evaluation/shared-policy-runtime-handoff-20261001-r3/handoff.json"
                )
                .stat()
                .st_size,
                "file_sha256": hashlib.sha256(
                    (
                        ROOT
                        / "artifacts/evaluation/shared-policy-runtime-handoff-20261001-r3/handoff.json"
                    ).read_bytes()
                ).hexdigest(),
                "handoff_sha256": _load(
                    ROOT
                    / "artifacts/evaluation/shared-policy-runtime-handoff-20261001-r3/handoff.json"
                )["handoff_sha256"],
                "gate_passed": True,
                "identity_handoff_only": True,
                "hil_acceptance": False,
                "real_acceptance": False,
                "hardware_acceptance": False,
                "flight_acceptance": False,
            },
        }
    )
    progress["updated_at_utc"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    progress_path.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

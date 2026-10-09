# ruff: noqa: E402, E501 -- repository-local path bootstrap and evidence paths
"""Rebind the current prerelease index to the checked-in release parity.

This maintenance command creates new source-bound copies and receipts.  It
never overwrites historical evidence and never starts an external simulator,
PX4, MAVLink transport, HIL session, or hardware.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: dict[str, Any]) -> None:
    # Write explicit LF bytes so a Windows rerun does not turn the entire
    # current-progress index into a CRLF-only diff or trigger false whitespace
    # failures in repository checks.
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def copy_new(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def parity_binding() -> dict[str, Any]:
    path = ROOT / "configs/release-parity.json"
    document = load(path)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": path.stat().st_size,
        "file_sha256": sha256(path),
        "hashes": document["parity"],
        "release_label": document["release_label"],
    }


def rebind_receipt(
    source: Path,
    target: Path,
    parity: dict[str, Any],
    *,
    retained_audit: bool = False,
) -> dict[str, Any]:
    receipt = load(source)
    receipt["release_parity"] = dict(parity)
    if retained_audit:
        receipt["release_parity"]["scope"] = "receipt_generation_file_binding"
        receipt["evidence_scope"] = "retained_files_audit"
        receipt["runtime_source_parity_verified"] = False
        receipt["external_sim_rerun_performed"] = False
    receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    write_new(target, receipt)
    return receipt


def rebind_external_evidence(source: Path, target: Path, parity: dict[str, Any]) -> dict[str, Any]:
    evidence = load(source)
    evidence["release_parity"] = dict(parity)
    evidence["current_release_parity_bound"] = True
    write_new(target, evidence)
    return evidence


def relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def bind_file(entry: dict[str, Any], key: str, path: Path, *, sha_key: str | None = None) -> None:
    entry[key] = relative(path)
    entry[f"{key}_bytes"] = path.stat().st_size
    entry[sha_key or f"{key}_sha256"] = sha256(path)


def main() -> int:
    parity = parity_binding()
    p = ROOT / "docs/current-progress.json"
    progress = load(p)

    # Rebind external campaign receipts without changing their immutable run
    # directories or summary/evidence content.
    parent_r12 = (
        ROOT
        / "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-20261003-qml-r1.receipt.json"
    )
    rebind_receipt(
        ROOT
        / "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-r12.receipt.json",
        parent_r12,
        parity,
        retained_audit=True,
    )
    parent_r11 = (
        ROOT
        / "artifacts/evaluation/shared-policy-external-parent-20260927-r13-policycapture-parity-20261003-qml-r1.receipt.json"
    )
    candidate_r11 = (
        ROOT
        / "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-20261003-qml-r1.receipt.json"
    )
    rebind_receipt(
        ROOT
        / "artifacts/evaluation/shared-policy-external-parent-20260927-r13-policycapture-parity-r11.receipt.json",
        parent_r11,
        parity,
        retained_audit=True,
    )
    rebind_receipt(
        ROOT
        / "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-r11.receipt.json",
        candidate_r11,
        parity,
        retained_audit=True,
    )

    # Rebind the fixed-wing/multirotor source evidence used by the current
    # pair and three-vehicle matrix.  The old files remain immutable.
    fixed = (
        ROOT
        / "artifacts/evaluation/external-fixed-wing-mission-20260927-r12-shared-policy-parity-20261003-qml-r1.json"
    )
    multi = (
        ROOT
        / "artifacts/evaluation/external-multirotor-mission-20260927-r6-shared-policy-parity-20261003-qml-r1.json"
    )
    rebind_external_evidence(
        ROOT / "artifacts/evaluation/external-fixed-wing-mission-20260927-r12-shared-policy.json",
        fixed,
        parity,
    )
    rebind_external_evidence(
        ROOT / "artifacts/evaluation/external-multirotor-mission-20260927-r6-shared-policy.json",
        multi,
        parity,
    )

    # The current depth-aware fixed-wing and multirotor index entries point to
    # richer same-runtime evidence than the compact matrix source rows.  Bind
    # those documents too; otherwise the index would mix the new matrix with
    # a stale parity identity.
    fixed_depth = (
        ROOT
        / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261003-qml-r1.json"
    )
    multi_depth = (
        ROOT
        / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261003-qml-r1.json"
    )
    rebind_external_evidence(
        ROOT / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261001-r12.json",
        fixed_depth,
        parity,
    )
    rebind_external_evidence(
        ROOT / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261001-r12.json",
        multi_depth,
        parity,
    )

    vtol = (
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261003-qml-r1.receipt.json"
    )
    if not vtol.exists():
        rebind_receipt(
            ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20260928-r2.receipt.json",
            vtol,
            parity,
        )
    elif load(vtol).get("release_parity", {}).get("file_sha256") != parity["file_sha256"]:
        raise ValueError("current VTOL receipt exists but is not bound to current parity")

    # Build new pair/matrix receipts from the rebound source copies.
    import subprocess

    def run(args: list[str]) -> None:
        if "--out" in args:
            output_arg = args[args.index("--out") + 1]
            output = Path(output_arg)
            if not output.is_absolute():
                output = ROOT / output
            if output.exists():
                return
        subprocess.run([sys.executable, *args], cwd=ROOT, check=True)

    pair = ROOT / "docs/evidence/external-shared-policy-pair-20261003-qml-r1.json"
    run(
        [
            "scripts/create_external_shared_policy_pair_receipt.py",
            "--root",
            str(ROOT),
            "--fixed-wing-evidence",
            relative(fixed),
            "--multirotor-evidence",
            relative(multi),
            "--out",
            relative(pair),
        ]
    )
    vehicle = ROOT / "artifacts/evaluation/shared-policy-vehicle-matrix-20261003-qml-r1.json"
    run(
        [
            "scripts/create_shared_policy_vehicle_matrix_receipt.py",
            "--root",
            str(ROOT),
            "--vtol-receipt",
            relative(vtol),
            "--fixed-wing-evidence",
            relative(fixed),
            "--multirotor-evidence",
            relative(multi),
            "--out",
            relative(vehicle),
        ]
    )
    matrix = ROOT / "docs/evidence/external-mission-matrix-20261003-qml-r1.json"
    run(
        [
            "scripts/create_external_mission_matrix_receipt.py",
            "--root",
            str(ROOT),
            "--vtol-receipt",
            relative(vtol),
            "--fixed-wing-evidence",
            relative(fixed),
            "--multirotor-evidence",
            relative(multi),
            "--out",
            relative(matrix),
        ]
    )

    # Rebind the AI-flight parity receipt to the rebound shared-policy layer.
    ai = load(ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20260927-r10.receipt.json")
    ai["release_parity"] = dict(parity)
    layer = ai["shared_policy_mission_layer"]
    layer["path"] = str(parent_r11)
    layer["bytes"] = parent_r11.stat().st_size
    layer["file_sha256"] = sha256(parent_r11)
    layer["receipt_sha256"] = load(parent_r11)["receipt_sha256"]
    ai["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in ai.items() if key != "receipt_sha256"}
    )
    ai_path = ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261003-qml-r1.receipt.json"
    write_new(ai_path, ai)

    # The observed session and runtime matrix are deterministic software
    # evidence, but their embedded parity identity must still be rebound when
    # a shared SIM/core file changes.  Preserve the old files and write new
    # source-bound copies.
    observed_source = ROOT / "artifacts/evaluation/shared-policy-observed-session-20260927-r19.json"
    observed = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261003-qml-r1.json"
    observed_doc = load(observed_source)
    observed_doc["release_parity"] = dict(parity)
    write_new(observed, observed_doc)
    for key in (
        "shared_policy_observed_session_20260927_r17",
        "shared_policy_observed_session_20260924",
    ):
        entry = progress[key]
        entry["evidence"] = relative(observed)
        entry["evidence_bytes"] = observed.stat().st_size
        entry["evidence_sha256"] = sha256(observed)
        entry["campaign"] = observed_doc["campaign"]
        entry["campaign_sha256"] = observed_doc["campaign_sha256"]
        entry["policy_artifact"] = observed_doc["policy_artifact"]
        entry["policy_artifact_file_sha256"] = observed_doc["policy_artifact_file_sha256"]
        entry["policy_artifact_canonical_sha256"] = observed_doc["policy_artifact_canonical_sha256"]
        entry["ranker_sha256"] = observed_doc["ranker_sha256"]
        entry["release_parity_file_sha256"] = parity["file_sha256"]
        entry["release_parity_bytes"] = parity["bytes"]
        entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
        for flag in (
            "all_tasks_completed",
            "deterministic_repeat_passed",
            "planner_revalidated",
            "truth_free",
            "backend_started",
            "mavlink_sent",
            "actuator_capability_granted",
            "hil_real_evidence",
            "outdoor_gnss_evidence",
            "final_release_allowed",
        ):
            entry[flag] = observed_doc[flag]
        entry["selected_task_ids"] = observed_doc["session"]["selected_task_ids"]
        entry["remaining_task_ids"] = observed_doc["session"]["remaining_task_ids"]
        entry["completed_task_count"] = len(observed_doc["session"]["selected_task_ids"])
    progress["shared_policy_observed_session_20260927_r18"] = {
        "status": "passed_current_shared_ranker_observed_session",
        "release_label": load(ROOT / "configs/product-version.json")["release_label"],
        "script": "scripts/run_shared_policy_observed_session.py",
        "evidence": relative(observed),
        "evidence_bytes": observed.stat().st_size,
        "evidence_sha256": sha256(observed),
        "campaign": observed_doc["campaign"],
        "campaign_sha256": observed_doc["campaign_sha256"],
        "policy_artifact": observed_doc["policy_artifact"],
        "policy_artifact_file_sha256": observed_doc["policy_artifact_file_sha256"],
        "policy_artifact_canonical_sha256": observed_doc["policy_artifact_canonical_sha256"],
        "ranker_sha256": observed_doc["ranker_sha256"],
        "release_parity_file_sha256": parity["file_sha256"],
        "release_parity_bytes": parity["bytes"],
        "release_parity_core_algorithm_hash": parity["hashes"]["core_algorithm_hash"],
        "selected_task_ids": observed_doc["session"]["selected_task_ids"],
        "completed_task_count": len(observed_doc["session"]["selected_task_ids"]),
        "remaining_task_ids": observed_doc["session"]["remaining_task_ids"],
        **{
            key: observed_doc[key]
            for key in (
                "all_tasks_completed",
                "deterministic_repeat_passed",
                "planner_revalidated",
                "truth_free",
                "backend_started",
                "mavlink_sent",
                "actuator_capability_granted",
                "hil_real_evidence",
                "outdoor_gnss_evidence",
                "final_release_allowed",
            )
        },
    }

    runtime_source = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20260927-r5.json"
    runtime = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261003-qml-r1.json"
    runtime_doc = load(runtime_source)
    runtime_doc["release_parity"] = dict(parity["hashes"])
    for mode in runtime_doc.get("modes", {}).values():
        if isinstance(mode, dict):
            mode["release_parity"] = dict(parity["hashes"])
    write_new(runtime, runtime_doc)
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    runtime_entry["evidence"] = relative(runtime)
    runtime_entry["evidence_sha256"] = sha256(runtime)
    runtime_entry["evidence_bytes"] = runtime.stat().st_size
    runtime_entry["policy_artifact_file_sha256"] = sha256(
        ROOT / runtime_entry["policy_artifact_file"]
    )
    runtime_entry["policy_artifact_canonical_sha256"] = runtime_doc["policy_artifact_sha256"]
    runtime_entry["release_parity_file_sha256"] = parity["file_sha256"]
    runtime_entry["release_parity_bytes"] = parity["bytes"]
    runtime_entry["core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    # Current campaign and campaign-receipt indexes.
    current = progress["shared_policy_external_parent_campaign_20260927_r14"]
    current_receipt = parent_r12
    current["receipt"] = relative(current_receipt)
    current["receipt_bytes"] = current_receipt.stat().st_size
    current["receipt_file_sha256"] = sha256(current_receipt)
    current["receipt_canonical_sha256"] = load(current_receipt)["receipt_sha256"]
    current["release_parity_file_sha256"] = parity["file_sha256"]
    current["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_entry["receipt"] = relative(vtol)
    vtol_entry["receipt_bytes"] = vtol.stat().st_size
    vtol_entry["receipt_file_sha256"] = sha256(vtol)
    vtol_entry["receipt_canonical_sha256"] = load(vtol)["receipt_sha256"]
    vtol_entry["release_parity_file_sha256"] = parity["file_sha256"]
    vtol_entry["release_parity_bytes"] = parity["bytes"]
    vtol_entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    receipts = progress["shared_policy_campaign_receipts_20260925"]
    for role, path in (("parent", parent_r11), ("candidate", candidate_r11)):
        receipts[f"{role}_receipt"] = relative(path)
        receipts[f"{role}_receipt_bytes"] = path.stat().st_size
        receipts[f"{role}_receipt_sha256"] = sha256(path)
        receipts[f"{role}_receipt_canonical_sha256"] = load(path)["receipt_sha256"]

    pair_entry = progress["external_shared_policy_pair_20260927_r1"]
    pair_entry["evidence"] = relative(pair)
    pair_entry["evidence_bytes"] = pair.stat().st_size
    pair_entry["evidence_sha256"] = sha256(pair)
    pair_entry["receipt_sha256"] = load(pair)["receipt_sha256"]
    pair_entry["current_release_parity_bound"] = True
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    vehicle_doc = load(vehicle)
    vehicle_entry["evidence"] = relative(vehicle)
    vehicle_entry["evidence_bytes"] = vehicle.stat().st_size
    vehicle_entry["evidence_sha256"] = sha256(vehicle)
    vehicle_entry["receipt_sha256"] = vehicle_doc["receipt_sha256"]
    vehicle_entry["vtol_receipt"] = relative(vtol)
    vehicle_entry["vtol_receipt_sha256"] = sha256(vtol)
    vehicle_entry["vtol_runtime_evidence_sha256"] = sha256(
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20260928-r2.json"
    )
    vehicle_entry["campaign_file_sha256"] = sha256(ROOT / vehicle_entry["campaign_file"])
    vehicle_entry["policy_artifact_file_sha256"] = sha256(
        ROOT / vehicle_entry["policy_artifact_file"]
    )
    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    matrix_doc = load(matrix)
    matrix_entry["evidence"] = relative(matrix)
    matrix_entry["evidence_bytes"] = matrix.stat().st_size
    matrix_entry["evidence_sha256"] = sha256(matrix)
    matrix_entry["receipt_sha256"] = matrix_doc["receipt_sha256"]
    matrix_entry["vtol_campaign_receipt"] = relative(vtol)
    matrix_entry["vtol_campaign_receipt_sha256"] = sha256(vtol)
    matrix_entry["current_release_parity_bound"] = matrix_doc["current_release_parity_bound"]

    for key, depth_path in (
        ("external_fixed_wing_shared_policy_depth_mission_20260930", fixed_depth),
        ("external_multirotor_shared_policy_depth_mission_20260930", multi_depth),
    ):
        depth_entry = progress[key]
        depth_entry["evidence"] = relative(depth_path)
        depth_entry["evidence_bytes"] = depth_path.stat().st_size
        depth_entry["evidence_sha256"] = sha256(depth_path)
        depth_entry["current_release_parity_bound"] = True

    ai_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_entry["receipt"] = relative(ai_path)
    ai_entry["receipt_bytes"] = ai_path.stat().st_size
    ai_entry["receipt_file_sha256"] = sha256(ai_path)
    ai_entry["receipt_sha256"] = ai["receipt_sha256"]
    ai_entry["shared_policy_mission_layer"] = relative(parent_r11)
    ai_entry["shared_policy_receipt_sha256"] = load(parent_r11)["receipt_sha256"]

    # Recreate the current fail-closed release audit against the new parity.
    audit = ROOT / "docs/evidence/shared-policy-release-parity-policy-helper-20261003-qml-r1.json"
    run(
        [
            "scripts/create_current_release_gate_audit.py",
            "--root",
            str(ROOT),
            "--report",
            "artifacts/evaluation/release-gate-current-20260927-r12/release-gate-pre.8-current-20260927T005000Z-v6-bound-report.json",
            "--parity",
            "configs/release-parity.json",
            "--current-field-link-preflight",
            "artifacts/evaluation/field-link-preflight-current-20260927-r2.json",
            "--out",
            relative(audit),
        ]
    )
    audit_doc = load(audit)
    release_ref = progress["evidence"]["release_gate_report"]
    release_ref["audit_summary_path"] = relative(audit)
    release_ref["current_release_parity_bound"] = audit_doc["current_release_parity_bound"]
    release_ref["file_bytes"] = audit_doc["report_bytes"]
    release_ref["file_sha256"] = audit_doc["report_sha256"]
    progress["release_parity"] = parity
    progress["updated_at_utc"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    p_rebound = ROOT / "docs/current-progress-rebound-policy-helper-20261003-qml-r1.json"
    write_new(p_rebound, progress)
    # Preserve the progress path as the sole current index; the old index is
    # backed up as a new immutable file before replacement.
    backup = ROOT / "docs/current-progress-pre-parity-rebind-20260927.json"
    if not backup.exists():
        shutil.copy2(p, backup)
    p_rebound.replace(p)
    print(
        json.dumps(
            {
                "parity_sha256": parity["file_sha256"],
                "observed": relative(observed),
                "runtime": relative(runtime),
                "pair": relative(pair),
                "vehicle_matrix": relative(vehicle),
                "mission_matrix": relative(matrix),
                "audit": relative(audit),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

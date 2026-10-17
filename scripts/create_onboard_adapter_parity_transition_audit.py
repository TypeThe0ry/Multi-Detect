"""Create immutable retained-audit copies for the 2026-10-04 parity transition.

This script never edits an existing evidence file.  It only creates new files
whose claims explicitly say that the historical runtime was not rerun against
the new core hash.
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

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    parity_path = ROOT / "configs/release-parity.json"
    parity_document = load(parity_path)
    parity = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "sha256": sha256(parity_path),
        "file_sha256": sha256(parity_path),
        "release_label": parity_document["release_label"],
        "hashes": parity_document["parity"],
    }

    foundation = load(ROOT / "docs/evidence/v030-pre1-foundation-20261004-r10.json")
    foundation["parity_file"] = {
        "bytes": parity["bytes"],
        "path": parity["path"],
        "release_label": parity["release_label"],
        "sha256": parity["sha256"],
    }
    foundation["scope"] = "retained_files_audit"
    foundation["runtime_source_parity_verified"] = False
    foundation["external_sim_rerun_performed"] = False
    foundation["claims_boundary"] = (
        "Retained pre.1 foundation audit rebound to the current source parity "
        "file; no runtime rerun, HIL, REAL, hardware, outdoor GNSS or final release claim."
    )
    foundation.pop("sha256", None)
    foundation["sha256"] = canonical_sha256(foundation)
    foundation_path = (
        ROOT / "docs/evidence/v030-pre1-foundation-20261004-onboard-adapter-parity-audit-r1.json"
    )
    write_new(foundation_path, foundation)

    source = load(
        ROOT
        / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-current-parity/combined-"
        "evidence.json"
    )
    source["hashes"]["release_parity"] = {
        "path": str(parity_path.resolve()),
        "bytes": parity["bytes"],
        "sha256": parity["sha256"].upper(),
        "parity": parity["hashes"],
    }
    source["current_release_parity_bound"] = True
    source["evidence_scope"] = "retained_files_audit"
    source["runtime_source_parity_verified"] = False
    source["external_sim_rerun_performed"] = False
    source["claims_boundary"] = (
        "Retained pinned PX4/Gazebo/QGC software-SIM source audit rebound to "
        "current parity without rerunning; no HIL, REAL, hardware, outdoor GNSS, "
        "human visual or final release claim."
    )
    source_path = (
        ROOT / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-"
        "onboard-adapter-parity-audit/combined-evidence.json"
    )
    write_new(source_path, source)

    summary = load(ROOT / "docs/evidence/combined-sim-qgc-20261004-r3-retained.json")
    summary["status"] = "retained_file_audit_onboard_adapter_parity_rebind"
    summary["source_evidence"] = source_path.relative_to(ROOT).as_posix()
    summary["source_evidence_bytes"] = source_path.stat().st_size
    summary["source_evidence_sha256"] = sha256(source_path)
    summary["release_parity_sha256"] = parity["sha256"]
    summary["evidence_scope"] = "retained_files_audit"
    summary["runtime_source_parity_verified"] = False
    summary["external_sim_rerun_performed"] = False
    summary["claims_boundary"] = (
        "Retained pinned PX4/Gazebo/QGC software-SIM summary rebound to current "
        "parity without rerunning; no HIL, REAL, hardware, outdoor GNSS, human "
        "visual or final release claim."
    )
    summary_path = (
        ROOT / "docs/evidence/combined-sim-qgc-20261004-onboard-adapter-parity-audit-r1.json"
    )
    write_new(summary_path, summary)

    def retained_external(source_name: str, target_name: str) -> Path:
        document = load(ROOT / source_name)
        document["release_parity"] = dict(parity)
        document["current_release_parity_bound"] = True
        document["evidence_scope"] = "retained_files_audit"
        document["runtime_source_parity_verified"] = False
        document["external_sim_rerun_performed"] = False
        document["claims_boundary"] = (
            "Retained historical external-SIM Mission audit rebound to current "
            "parity without rerunning; no HIL, REAL, hardware, outdoor GNSS or "
            "final release claim."
        )
        target = ROOT / target_name
        write_new(target, document)
        return target

    fixed_path = retained_external(
        "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261004-current-parity-audit-r2.json",
        "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261004-onboard-adapter-parity-audit-r3.json",
    )
    multirotor_path = retained_external(
        "artifacts/evaluation/external-multirotor-depth-shared-policy-20261004-current-parity-audit-r2.json",
        "artifacts/evaluation/external-multirotor-depth-shared-policy-20261004-onboard-adapter-parity-audit-r3.json",
    )
    vtol = load(
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-current-parity-"
        "audit.receipt.json"
    )
    vtol["release_parity"] = dict(parity)
    vtol["release_parity"]["scope"] = "receipt_generation_file_binding"
    vtol["evidence_scope"] = "retained_files_audit"
    vtol["runtime_source_parity_verified"] = False
    vtol["external_sim_rerun_performed"] = False
    vtol["claims_boundary"] = (
        "Retained historical VTOL Mission receipt audit rebound to current parity "
        "without rerunning; no HIL, REAL, hardware, outdoor GNSS or final release claim."
    )
    vtol.pop("receipt_sha256", None)
    vtol["receipt_sha256"] = canonical_sha256(vtol)
    vtol_path = (
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-onboard-adapter-"
        "parity-audit-r3.receipt.json"
    )
    write_new(vtol_path, vtol)
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=vtol_path,
        fixed_wing_evidence=fixed_path,
        multirotor_evidence=multirotor_path,
        generated_at_utc="2026-10-04T00:00:00Z",
    )
    matrix_path = (
        ROOT / "docs/evidence/external-mission-matrix-20261004-onboard-adapter-parity-audit-r3.json"
    )
    write_new(matrix_path, matrix)

    observed = load(
        ROOT
        / "artifacts/evaluation/shared-policy-observed-session-20261004-current-parity-audit.json"
    )
    observed["release_parity"] = dict(parity)
    observed["evidence_scope"] = "retained_files_audit"
    observed["runtime_source_parity_verified"] = False
    observed["current_runtime_parity_proven"] = False
    observed["external_sim_rerun_performed"] = False
    observed["claims_boundary"] = (
        "Retained deterministic software-SIM observed-session audit rebound to "
        "current parity without rerunning; no external rerun, HIL, REAL, hardware "
        "or outdoor-GNSS claim."
    )
    observed_path = (
        ROOT
        / "artifacts/evaluation/shared-policy-observed-session-20261004-onboard-adapter-parity-"
        "audit-r1.json"
    )
    write_new(observed_path, observed)

    trained_receipt = load(
        ROOT
        / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261004-current-parity-audit-"
        "r2.receipt.json"
    )
    trained_receipt["release_parity"] = dict(parity)
    trained_receipt["release_parity"]["scope"] = "receipt_generation_file_binding"
    trained_receipt["evidence_scope"] = "retained_files_audit"
    trained_receipt["runtime_source_parity_verified"] = False
    trained_receipt["external_sim_rerun_performed"] = False
    trained_receipt["claims_boundary"] = (
        "Retained historical trained-policy VTOL Mission receipt audit rebound to "
        "current parity without rerunning; no HIL, REAL, hardware, outdoor GNSS "
        "or final release claim."
    )
    trained_receipt.pop("receipt_sha256", None)
    trained_receipt["receipt_sha256"] = canonical_sha256(trained_receipt)
    trained_receipt_path = (
        ROOT / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261004-"
        "onboard-adapter-parity-audit-r1.receipt.json"
    )
    write_new(trained_receipt_path, trained_receipt)

    def retained_receipt(source_name: str, target_name: str, label: str) -> Path:
        receipt = load(ROOT / source_name)
        receipt["release_parity"] = dict(parity)
        receipt["release_parity"]["scope"] = "receipt_generation_file_binding"
        receipt["evidence_scope"] = "retained_files_audit"
        receipt["runtime_source_parity_verified"] = False
        receipt["external_sim_rerun_performed"] = False
        receipt["claims_boundary"] = (
            f"Retained historical {label} receipt audit rebound to current parity "
            "without rerunning; no HIL, REAL, hardware, outdoor GNSS or final "
            "release claim."
        )
        receipt.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        target = ROOT / target_name
        write_new(target, receipt)
        return target

    parent_receipt_path = retained_receipt(
        "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-20261004-current-parity-audit-r2.receipt.json",
        "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-20261004-onboard-adapter-parity-audit-r1.receipt.json",
        "external parent campaign",
    )
    candidate_receipt_path = retained_receipt(
        "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-20261004-current-parity-audit-r2.receipt.json",
        "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-20261004-onboard-adapter-parity-audit-r1.receipt.json",
        "external candidate campaign",
    )
    current_vtol_receipt_path = retained_receipt(
        "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-current-parity-audit.receipt.json",
        "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-onboard-adapter-parity-audit-r1.receipt.json",
        "current VTOL campaign",
    )
    runtime_matrix = load(
        ROOT
        / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-current-parity-audit.json"
    )
    runtime_matrix["release_parity"] = dict(parity["hashes"])
    modes = runtime_matrix.get("modes")
    if isinstance(modes, dict):
        for mode in modes.values():
            if isinstance(mode, dict):
                mode["release_parity"] = dict(parity["hashes"])
    runtime_matrix["release_parity_file_sha256"] = parity["file_sha256"]
    runtime_matrix["release_parity_bytes"] = parity["bytes"]
    runtime_matrix["core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    runtime_matrix["evidence_scope"] = "retained_files_audit"
    runtime_matrix["runtime_source_parity_verified"] = False
    runtime_matrix["external_sim_rerun_performed"] = False
    runtime_matrix["claims_boundary"] = (
        "Retained four-mode SharedTaskPolicy contract matrix rebound to current "
        "source parity without rerunning; no HIL, REAL, hardware, outdoor GNSS "
        "or final release claim."
    )
    runtime_matrix_path = (
        ROOT
        / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-onboard-adapter-parity-audit-"
        "r1.json"
    )
    write_new(runtime_matrix_path, runtime_matrix)

    ai_flight = load(
        ROOT
        / "artifacts/evaluation/simulator-docker-ai-flight-20261004-current-parity-audit-r2.json"
    )
    ai_flight["evidence_scope"] = "retained_layers_file_audit"
    ai_flight["runtime_source_parity_verified"] = False
    ai_flight["external_sim_rerun_performed"] = False
    ai_flight["claims_boundary"] = (
        "Retained historical external-SIM AI flight file audit rebound to current "
        "source parity without rerunning; no HIL, REAL, hardware, outdoor-GNSS "
        "or final release claim."
    )
    ai_snapshot = ai_flight.get("release_parity_snapshot")
    if isinstance(ai_snapshot, dict):
        for side in ("start", "end"):
            row = ai_snapshot.get(side)
            if isinstance(row, dict):
                row.update(
                    {
                        "bytes": parity["bytes"],
                        "file_sha256": parity["file_sha256"],
                        "hashes": parity["hashes"],
                        "path": str(parity_path.resolve()),
                        "release_label": parity["release_label"],
                    }
                )
    ai_flight_path = (
        ROOT
        / "artifacts/evaluation/simulator-docker-ai-flight-20261004-onboard-adapter-parity-audit-"
        "r1.json"
    )
    write_new(ai_flight_path, ai_flight)

    ai_receipt = load(
        ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261004-current-parity-audit-"
        "r2.receipt.json"
    )
    ai_receipt["release_parity"] = {
        "path": str(parity_path.resolve()),
        "scope": "receipt_generation_file_binding",
        "bytes": parity["bytes"],
        "file_sha256": parity["file_sha256"],
        "release_label": parity["release_label"],
        "hashes": parity["hashes"],
    }
    policy_layer = ai_receipt.get("shared_policy_mission_layer")
    if isinstance(policy_layer, dict):
        policy_path = current_vtol_receipt_path
        policy_document = load(policy_path)
        policy_layer.update(
            {
                "path": str(policy_path.resolve()),
                "bytes": policy_path.stat().st_size,
                "file_sha256": sha256(policy_path),
                "receipt_sha256": policy_document.get("receipt_sha256"),
                "runtime_source_parity_verified": False,
                "runtime_parity_snapshot_verified": False,
            }
        )
    ai_layer = ai_receipt.get("simulator_ai_flight_layer")
    if isinstance(ai_layer, dict):
        ai_layer.update(
            {
                "path": str(ai_flight_path.resolve()),
                "bytes": ai_flight_path.stat().st_size,
                "file_sha256": sha256(ai_flight_path),
                "runtime_parity_snapshot_verified": False,
            }
        )
    bindings = ai_receipt.get("bindings")
    if isinstance(bindings, dict):
        bindings["same_release_parity"] = False
        bindings["current_runtime_parity_proven"] = False
        bindings["external_sim_rerun_performed"] = False
        bindings["shared_policy_runtime_parity_snapshot_verified"] = False
        bindings["simulator_runtime_parity_snapshot_verified"] = False
    ai_receipt["evidence_scope"] = "retained_layers_file_audit"
    ai_receipt.pop("receipt_sha256", None)
    ai_receipt["receipt_sha256"] = canonical_sha256(ai_receipt)
    ai_receipt_path = (
        ROOT
        / "artifacts/evaluation/simulator-ai-flight-parity-20261004-onboard-adapter-parity-audit-"
        "r1.receipt.json"
    )
    write_new(ai_receipt_path, ai_receipt)

    sensor_campaign = load(
        ROOT
        / "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261004-"
        "current-r6.json"
    )
    sensor_campaign["status"] = (
        "retained_file_audit_prompt_onboard_sensor_campaign_current_runtime_unproven"
    )
    sensor_campaign["release_parity_file_sha256"] = parity["file_sha256"]
    sensor_campaign["evidence_scope"] = "retained_files_audit"
    sensor_campaign["current_runtime_parity_proven"] = False
    sensor_campaign["external_sim_rerun_performed"] = False
    sensor_campaign["claims_boundary"] = (
        "Retained prompt-to-OnboardAI PX4/Gazebo software-SIM sensor campaign "
        "rebased to the current source parity for index integrity without rerun; "
        "the original runtime used the preceding parity snapshot. No HIL, REAL, "
        "hardware, outdoor GNSS, QGC visual or final release claim."
    )
    snapshot = sensor_campaign.get("campaign", {}).get("release_parity_snapshot")
    if isinstance(snapshot, dict):
        for side in ("start", "end"):
            row = snapshot.get(side)
            if isinstance(row, dict):
                row.update(
                    {
                        "bytes": parity["bytes"],
                        "file_sha256": parity["file_sha256"],
                        "hashes": parity["hashes"],
                        "path": str(parity_path.resolve()),
                        "release_label": parity["release_label"],
                    }
                )
    sensor_campaign_path = (
        ROOT
        / "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261004-"
        "onboard-adapter-parity-audit-r1.json"
    )
    write_new(sensor_campaign_path, sensor_campaign)

    print(
        json.dumps(
            {
                "parity": parity,
                "foundation": foundation_path.relative_to(ROOT).as_posix(),
                "combined_source": source_path.relative_to(ROOT).as_posix(),
                "combined_summary": summary_path.relative_to(ROOT).as_posix(),
                "fixed": fixed_path.relative_to(ROOT).as_posix(),
                "multirotor": multirotor_path.relative_to(ROOT).as_posix(),
                "vtol": vtol_path.relative_to(ROOT).as_posix(),
                "matrix": matrix_path.relative_to(ROOT).as_posix(),
                "observed": observed_path.relative_to(ROOT).as_posix(),
                "trained_receipt": trained_receipt_path.relative_to(ROOT).as_posix(),
                "parent_receipt": parent_receipt_path.relative_to(ROOT).as_posix(),
                "candidate_receipt": candidate_receipt_path.relative_to(ROOT).as_posix(),
                "sensor_campaign": sensor_campaign_path.relative_to(ROOT).as_posix(),
                "current_vtol_receipt": current_vtol_receipt_path.relative_to(ROOT).as_posix(),
                "runtime_matrix": runtime_matrix_path.relative_to(ROOT).as_posix(),
                "ai_flight": ai_flight_path.relative_to(ROOT).as_posix(),
                "ai_receipt": ai_receipt_path.relative_to(ROOT).as_posix(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bind this training result and retained audits without rerunning any simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.create_current_release_gate_audit import create_audit  # noqa: E402
from scripts.create_external_mission_matrix_receipt import create_receipt  # noqa: E402
from scripts.create_shared_policy_vehicle_matrix_receipt import (  # noqa: E402
    create_shared_policy_vehicle_receipt,
)
from scripts.create_simulator_ai_flight_parity_receipt import build_receipt  # noqa: E402
from scripts.rebind_current_release_parity_r11 import (  # noqa: E402
    check_snapshots,
    decode,
)


def load(path: Path) -> dict:
    value = decode(path.read_bytes(), path)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def identity(path: Path) -> dict:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def write_new(path: Path, value: dict) -> None:
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def replace_staged(path: Path, value: dict, expected: bytes) -> None:
    if path.resolve() == (ROOT / "docs/current-progress.json").resolve():
        raise ValueError("finisher may only update a distinct staged index")
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".parity-finalize-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        if path.read_bytes() != expected:
            raise ValueError("staged index changed during finalization")
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--skip-training-summary", action="store_true")
    parser.add_argument("--training-summary", type=Path)
    args = parser.parse_args(argv)
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", args.tag) is None:
        parser.error("--tag must be a short lowercase alphanumeric/hyphen identity")
    progress_path = (ROOT / args.progress).resolve(strict=True)
    progress_path.relative_to(ROOT.resolve())
    if progress_path == (ROOT / "docs/current-progress.json").resolve():
        raise ValueError("finalization requires a distinct staged index")
    snapshots: dict[Path, bytes] = {}

    def read(path: Path) -> dict:
        path = path.resolve(strict=True)
        path.relative_to(ROOT.resolve())
        payload = path.read_bytes()
        if snapshots.setdefault(path, payload) != payload:
            raise ValueError(f"source changed during finalization: {path}")
        value = decode(snapshots[path], path)
        if not isinstance(value, dict):
            raise ValueError(f"JSON object required: {path}")
        return value

    progress = read(progress_path)
    if not isinstance(progress.get("parity_rebind_audit", {}).get("source_files"), list):
        raise ValueError("staged index has no retained source seal")
    parity_path = ROOT / "configs/release-parity.json"
    read(parity_path)
    parity = identity(parity_path)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    directory = ROOT / f"artifacts/evaluation/parity-final-{date}-{args.tag}"
    paths = {
        "qgc": directory / "combined-sim-qgc.json",
        "matrix": directory / "external-mission-matrix.json",
        "vehicle": directory / "shared-policy-vehicle-matrix.json",
        "ai": directory / "simulator-ai-flight-parity.json",
        "gate": ROOT / f"docs/evidence/release-parity-{date}-{args.tag}.json",
    }
    for path in paths.values():
        if path.exists():
            raise FileExistsError(f"final audit target already exists: {path}")

    # Cross-file identity is computed after the rebound raw files exist. This
    # is an audit copy, not a claim that old QGC/PX4 runtimes used today's code.
    qgc_entry = progress["combined_sim_qgc_external_vtol_pass_20261003_r4"]
    qgc = read(ROOT / qgc_entry["evidence"])
    read(ROOT / qgc_entry["source_evidence"])
    qgc_source = identity(ROOT / qgc_entry["source_evidence"])
    qgc.update(
        {
            "source_evidence": qgc_source["path"],
            "source_evidence_bytes": qgc_source["bytes"],
            "source_evidence_sha256": qgc_source["sha256"],
            "release_parity_sha256": parity["sha256"],
            "evidence_scope": "retained_files_audit",
            "runtime_source_parity_verified": False,
            "external_sim_rerun_performed": False,
        }
    )
    qgc_path = paths["qgc"]
    write_new(qgc_path, qgc)
    qgc_identity = identity(qgc_path)
    qgc_entry.update(
        {
            "evidence": qgc_identity["path"],
            "evidence_bytes": qgc_identity["bytes"],
            "evidence_sha256": qgc_identity["sha256"],
        }
    )

    vtol_path = ROOT / progress["shared_policy_current_vtol_campaign_20260928_r2"]["receipt"]
    read(vtol_path)
    fixed_path = (
        ROOT / progress["external_fixed_wing_shared_policy_depth_mission_20260930"]["evidence"]
    )
    multirotor_path = (
        ROOT / progress["external_multirotor_shared_policy_depth_mission_20260930"]["evidence"]
    )
    read(fixed_path)
    read(multirotor_path)
    external_entry = progress["external_mission_matrix_20260927_r6"]
    external_source = read(ROOT / external_entry["evidence"])
    external_matrix = create_receipt(
        root=ROOT,
        vtol_receipt=vtol_path,
        fixed_wing_evidence=fixed_path,
        multirotor_evidence=multirotor_path,
        generated_at_utc=external_source.get("generated_at_utc"),
    )
    write_new(paths["matrix"], external_matrix)
    external_identity = identity(paths["matrix"])
    external_entry.update(
        {
            "evidence": external_identity["path"],
            "evidence_bytes": external_identity["bytes"],
            "evidence_sha256": external_identity["sha256"],
            "receipt_sha256": external_matrix["receipt_sha256"],
            "vtol_campaign_receipt": identity(vtol_path)["path"],
            "vtol_campaign_receipt_sha256": identity(vtol_path)["sha256"],
            "evidence_scope": "retained_files_audit",
            "runtime_source_parity_verified": False,
            "external_sim_rerun_performed": False,
        }
    )
    matrix_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    matrix_source = read(ROOT / matrix_entry["evidence"])
    matrix = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=vtol_path,
        fixed_wing_evidence=fixed_path,
        multirotor_evidence=multirotor_path,
        generated_at_utc=matrix_source.get("generated_at_utc"),
    )
    matrix_path = paths["vehicle"]
    write_new(matrix_path, matrix)
    matrix_identity = identity(matrix_path)
    matrix_entry.update(
        {
            "evidence": matrix_identity["path"],
            "evidence_bytes": matrix_identity["bytes"],
            "evidence_sha256": matrix_identity["sha256"],
            "receipt_sha256": matrix["receipt_sha256"],
            "vtol_receipt": identity(vtol_path)["path"],
            "vtol_receipt_sha256": identity(vtol_path)["sha256"],
            "release_parity_file_sha256": parity["sha256"],
        }
    )

    ai_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    old_ai = read(ROOT / ai_entry["receipt"])
    ai_path = (ROOT / old_ai["simulator_ai_flight_layer"]["path"]).resolve(strict=True)
    read(ai_path)
    ai_receipt_path = paths["ai"]
    if ai_receipt_path.exists():
        raise FileExistsError(f"receipt already exists: {ai_receipt_path}")
    # Reuse the existing validator/builder, but do not inherit its nonexclusive
    # final write. Its private temporary output is never an indexed artifact.
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ai-receipt-", dir=directory) as temporary:
        ai = build_receipt(
            ai_flight_evidence=ai_path,
            shared_policy_receipt=vtol_path,
            release_parity=parity_path,
            output=Path(temporary) / "receipt.json",
        )
        write_new(ai_receipt_path, ai)
    ai_identity = identity(ai_receipt_path)
    ai_entry.update(
        {
            "receipt": ai_identity["path"],
            "receipt_bytes": ai_identity["bytes"],
            "receipt_file_sha256": ai_identity["sha256"],
            "receipt_sha256": ai["receipt_sha256"],
            "shared_policy_mission_layer": identity(vtol_path)["path"],
            "shared_policy_receipt_sha256": read(vtol_path)["receipt_sha256"],
            "same_release_parity": False,
            "current_runtime_parity_proven": False,
            "external_sim_rerun_performed": False,
        }
    )
    ai_source = identity(ai_path)
    progress["external_docker_ai_flight_acceptance_20260927_r1"].update(
        {
            "evidence": ai_source["path"],
            "evidence_bytes": ai_source["bytes"],
            "evidence_sha256": ai_source["sha256"],
            "release_parity_file_sha256": parity["sha256"],
        }
    )

    gate_entry = progress["evidence"]["release_gate_report"]
    read(ROOT / gate_entry["local_path"])
    old_audit = read(ROOT / gate_entry["audit_summary_path"])
    current_probe = old_audit.get("current_field_link_probe", {}).get("preflight_path")
    if current_probe is not None:
        read(ROOT / current_probe)
    audit = create_audit(
        ROOT,
        ROOT / gate_entry["local_path"],
        parity_path,
        ROOT / current_probe if current_probe is not None else None,
    )
    read(ROOT / audit["manifest_path"])
    audit_path = paths["gate"]
    write_new(audit_path, audit)
    audit_identity = identity(audit_path)
    manifest_identity = identity(ROOT / audit["manifest_path"])
    gate_entry.update(
        {
            "audit_summary_path": audit_identity["path"],
            "audit_summary_bytes": audit_identity["bytes"],
            "audit_summary_path_bytes": audit_identity["bytes"],
            "audit_summary_sha256": audit_identity["sha256"],
            "manifest_local_path": manifest_identity["path"],
            "manifest_bytes": manifest_identity["bytes"],
            "manifest_sha256": manifest_identity["sha256"],
            "file_bytes": audit["report_bytes"],
            "file_sha256": audit["report_sha256"],
            "current_release_parity_bound": audit["current_release_parity_bound"],
        }
    )

    training = None
    if args.training_summary is not None and not args.skip_training_summary:
        training_path = ROOT / args.training_summary
        training_document = read(training_path)
        training = identity(training_path)
        progress["onboard_increment_training_20261007_r90"] = {
            "status": "development_improved_not_promoted",
            "summary": training,
            "training_report": training_document["training_report"],
            "paired_evaluation": training_document["paired_evaluation"],
            "candidate_promoted": False,
            "current_reference_unchanged": True,
            "rendered_visual_training_completed": False,
            "final_release_allowed": False,
        }
    progress["updated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    check_snapshots(snapshots)
    replace_staged(progress_path, progress, snapshots[progress_path])
    print(
        json.dumps(
            {
                "training_summary": training["path"] if training is not None else None,
                "current_parity_sha256": parity["sha256"],
                "audit": audit_identity["path"],
                "external_sim_rerun_performed": False,
                "candidate_promoted": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

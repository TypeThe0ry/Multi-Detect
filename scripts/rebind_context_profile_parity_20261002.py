# ruff: noqa: E402, E501

"""Rebind retained prerelease evidence after the context profile guard.

This maintenance command never starts PX4, Gazebo, Docker, QGC, HIL, REAL or
hardware.  It creates new immutable, source-bound copies of the current
``gnss-display`` evidence and regenerates the derived receipts.  Existing
evidence is never overwritten.  The optional progress update is deliberately
explicit so a maintainer can inspect the new files before changing the
authoritative index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
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
from multidetect.simulator_external_evidence import (
    build_simulator_external_evidence,
    revalidate_simulator_external_evidence,
    write_simulator_external_evidence,
)


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def parity_binding() -> dict[str, Any]:
    path = ROOT / "configs/release-parity.json"
    document = load(path)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": path.stat().st_size,
        "file_sha256": sha(path),
        "hashes": document["parity"],
        "release_label": document["release_label"],
    }


def rebind(source: Path, target: Path, parity: dict[str, Any], *, receipt: bool = False) -> dict[str, Any]:
    value = load(source)
    release = dict(parity)
    previous = value.get("release_parity")
    if isinstance(previous, dict) and isinstance(previous.get("scope"), str):
        release["scope"] = previous["scope"]
    value["release_parity"] = release
    if "current_release_parity_bound" in value:
        value["current_release_parity_bound"] = True
    if receipt:
        value["receipt_sha256"] = canonical_sha256(
            {key: item for key, item in value.items() if key != "receipt_sha256"}
        )
    write_new(target, value)
    return value


def bind(entry: dict[str, Any], key: str, path: Path, *, sha_key: str | None = None) -> None:
    entry[key] = rel(path)
    entry[f"{key}_bytes"] = path.stat().st_size
    entry[sha_key or f"{key}_sha256"] = sha(path)


def run(args: list[str]) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def prepare(tag: str) -> dict[str, Path]:
    parity = parity_binding()
    suffix = f"20261002-{tag}"
    fixed_depth = ROOT / f"artifacts/evaluation/external-fixed-wing-shared-policy-{suffix}.json"
    multi_depth = ROOT / f"artifacts/evaluation/external-multirotor-shared-policy-{suffix}.json"
    fixed_mission = ROOT / f"artifacts/evaluation/external-fixed-wing-mission-{suffix}.json"
    multi_mission = ROOT / f"artifacts/evaluation/external-multirotor-mission-{suffix}.json"
    observed = ROOT / f"artifacts/evaluation/shared-policy-observed-session-{suffix}.json"
    vtol_runtime = ROOT / f"artifacts/evaluation/shared-policy-current-vtol-campaign-{suffix}-runtime.receipt.json"
    vtol_current = ROOT / f"artifacts/evaluation/shared-policy-current-vtol-campaign-{suffix}.receipt.json"
    current_campaign = ROOT / f"artifacts/evaluation/shared-policy-vtol-campaign-{suffix}-current.receipt.json"
    parent = ROOT / f"artifacts/evaluation/shared-policy-external-parent-{suffix}.receipt.json"
    candidate = ROOT / f"artifacts/evaluation/shared-policy-external-candidate-{suffix}.receipt.json"
    trained = ROOT / f"artifacts/evaluation/shared-policy-trained-vtol-two-task-{suffix}.receipt.json"
    runtime_matrix = ROOT / f"artifacts/evaluation/shared-policy-runtime-matrix-{suffix}.json"
    ai_receipt = ROOT / f"artifacts/evaluation/simulator-ai-flight-parity-{suffix}.receipt.json"

    rebind(
        ROOT / "artifacts/evaluation/external-fixed-wing-shared-policy-20261002-gnss-display-r2.json",
        fixed_depth,
        parity,
    )
    rebind(
        ROOT / "artifacts/evaluation/external-multirotor-shared-policy-20261002-gnss-display-r2.json",
        multi_depth,
        parity,
    )
    rebind(
        ROOT / "artifacts/evaluation/external-fixed-wing-mission-20261002-gnss-display-r1.json",
        fixed_mission,
        parity,
    )
    rebind(
        ROOT / "artifacts/evaluation/external-multirotor-mission-20261002-gnss-display-r1.json",
        multi_mission,
        parity,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-observed-session-20261002-gnss-display-r2.json",
        observed,
        parity,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261002-gnss-display-r3.receipt.json",
        vtol_runtime,
        parity,
        receipt=True,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261002-gnss-display-r4.receipt.json",
        vtol_current,
        parity,
        receipt=True,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-vtol-campaign-20261002-current-gnss-display-r2.receipt.json",
        current_campaign,
        parity,
        receipt=True,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-external-parent-20261002-gnss-display-r2.receipt.json",
        parent,
        parity,
        receipt=True,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-external-candidate-20261002-gnss-display-r2.receipt.json",
        candidate,
        parity,
        receipt=True,
    )
    rebind(
        ROOT / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261002-gnss-display-r2.receipt.json",
        trained,
        parity,
        receipt=True,
    )
    rebind_runtime = load(
        ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261002-gnss-display-r2.json"
    )
    rebind_runtime["release_parity"] = dict(parity["hashes"])
    for row in rebind_runtime.get("modes", {}).values():
        if isinstance(row, dict):
            row["release_parity"] = dict(parity["hashes"])
    write_new(runtime_matrix, rebind_runtime)

    # Rebuild the six-source external SIM join from the paths recorded by the
    # immutable r26 join.  This updates the parity projection and its source
    # hashes without pretending that a new external run happened.
    old_join = load(ROOT / "artifacts/evaluation/simulator-external-evidence-policy-helper-20261001-r26.json")
    runtime_sources = {
        vehicle: Path(old_join["vehicles"][vehicle]["runtime"]["path"])
        for vehicle in ("vtol", "fixed-wing", "multirotor")
    }
    ai_sources = {
        vehicle: Path(old_join["vehicles"][vehicle]["ai"]["path"])
        for vehicle in ("vtol", "fixed-wing", "multirotor")
    }
    external_join = ROOT / f"artifacts/evaluation/simulator-external-evidence-policy-helper-{suffix}.json"
    external_revalidation = ROOT / f"artifacts/evaluation/simulator-external-evidence-policy-helper-{suffix}-revalidated.json"
    fresh_join = build_simulator_external_evidence(
        runtime_sources,
        ai_sources,
        parity_path=ROOT / "configs/release-parity.json",
    )
    write_simulator_external_evidence(external_join, fresh_join, overwrite=False)
    fresh_revalidation = revalidate_simulator_external_evidence(
        external_join,
        expected_release_label=parity["release_label"],
    )
    write_simulator_external_evidence(external_revalidation, fresh_revalidation, overwrite=False)

    # Recompute the retained PX4 reported-position matrix and its fault
    # campaign against the new parity file.  Their runtime source JSON files
    # remain the same immutable external-SIM captures; only the derived parity
    # projection and source-tool hashes are regenerated.
    old_response_root = ROOT / "artifacts/evaluation/px4-reported-position-response-current-parity-20261001-r26"
    old_matrix = load(old_response_root / "px4-reported-position-response-matrix-current-parity.json")
    old_campaign = load(old_response_root / "px4-reported-position-response-campaign-current-parity.json")
    response_root = ROOT / f"artifacts/evaluation/px4-reported-position-response-current-parity-{suffix}"
    response_matrix = response_root / "px4-reported-position-response-matrix-current-parity.json"
    response_campaign = response_root / "px4-reported-position-response-campaign-current-parity.json"
    matrix_sources = [
        str(ROOT / entry["source_path"])
        for entry in old_matrix.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("source_path"), str)
    ]
    run_args = [
        "scripts/run_px4_reported_position_response_matrix.py",
        "--repository-root", str(ROOT),
        "--release-parity", str(ROOT / "configs/release-parity.json"),
        "--out", str(response_matrix),
    ]
    for source in matrix_sources:
        run_args.extend(["--source", source])
    run(run_args)
    positive_sources = [
        str(ROOT / entry["source_path"])
        for entry in old_campaign.get("positive_runs", [])
        if isinstance(entry, dict) and isinstance(entry.get("source_path"), str)
    ]
    fault_sources = [
        str(ROOT / entry["source_path"])
        for entry in old_campaign.get("fault_runs", [])
        if isinstance(entry, dict) and isinstance(entry.get("source_path"), str)
    ]
    campaign_args = [
        "scripts/run_px4_reported_position_response_campaign.py",
        "--repository-root", str(ROOT),
        "--release-parity", str(ROOT / "configs/release-parity.json"),
        "--out", str(response_campaign),
    ]
    for source in positive_sources:
        campaign_args.extend(["--positive", source])
    for source in fault_sources:
        campaign_args.extend(["--fault", source])
    run(campaign_args)

    pair = ROOT / f"docs/evidence/external-shared-policy-pair-{suffix}.json"
    run([
        "scripts/create_external_shared_policy_pair_receipt.py",
        "--root", str(ROOT),
        "--fixed-wing-evidence", rel(fixed_mission),
        "--multirotor-evidence", rel(multi_mission),
        "--out", rel(pair),
    ])
    vehicle = ROOT / f"artifacts/evaluation/shared-policy-vehicle-matrix-{suffix}.json"
    run([
        "scripts/create_shared_policy_vehicle_matrix_receipt.py",
        "--root", str(ROOT),
        "--vtol-receipt", rel(vtol_runtime),
        "--fixed-wing-evidence", rel(fixed_depth),
        "--multirotor-evidence", rel(multi_depth),
        "--out", rel(vehicle),
    ])
    matrix = ROOT / f"docs/evidence/external-mission-matrix-{suffix}.json"
    run([
        "scripts/create_external_mission_matrix_receipt.py",
        "--root", str(ROOT),
        "--vtol-receipt", rel(vtol_runtime),
        "--fixed-wing-evidence", rel(fixed_depth),
        "--multirotor-evidence", rel(multi_depth),
        "--out", rel(matrix),
    ])
    run([
        "scripts/create_simulator_ai_flight_parity_receipt.py",
        "--ai-flight-evidence",
        "artifacts/evaluation/simulator-docker-ai-flight-20261002-gnss-display-r6.json",
        "--shared-policy-receipt", rel(vtol_runtime),
        "--release-parity", "configs/release-parity.json",
        "--output", rel(ai_receipt),
    ])
    # The retained policy and AI runs each carried a stable snapshot at their
    # own runtime.  Changing the checked-in parity after those runs does not
    # make the old runtime execute against the new source identity.  Downgrade
    # only the composite claim to retained-layer audit; preserve the individual
    # snapshot facts and all source hashes.
    ai_document = load(ai_receipt)
    bindings = ai_document.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError("generated AI parity receipt has no bindings")
    bindings["same_release_parity"] = False
    bindings["current_runtime_parity_proven"] = False
    ai_document["receipt_sha256"] = canonical_sha256(
        {key: item for key, item in ai_document.items() if key != "receipt_sha256"}
    )
    ai_receipt.write_bytes(
        (json.dumps(ai_document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return {
        "fixed_depth": fixed_depth,
        "multi_depth": multi_depth,
        "fixed_mission": fixed_mission,
        "multi_mission": multi_mission,
        "observed": observed,
        "vtol_runtime": vtol_runtime,
        "vtol_current": vtol_current,
        "current_campaign": current_campaign,
        "parent": parent,
        "candidate": candidate,
        "trained": trained,
        "runtime_matrix": runtime_matrix,
        "pair": pair,
        "vehicle": vehicle,
        "matrix": matrix,
        "ai_receipt": ai_receipt,
        "external_join": external_join,
        "external_revalidation": external_revalidation,
        "response_matrix": response_matrix,
        "response_campaign": response_campaign,
    }


def update_progress(paths: dict[str, Path], report: Path, manifest: Path, audit: Path) -> None:
    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)
    parity = parity_binding()
    backup = ROOT / "docs/current-progress-pre-context-profile-parity-20261002.json"
    if not backup.exists():
        shutil.copy2(progress_path, backup)
    progress["release_parity"] = parity

    for key, path in (
        ("external_fixed_wing_shared_policy_depth_mission_20260930", paths["fixed_depth"]),
        ("external_multirotor_shared_policy_depth_mission_20260930", paths["multi_depth"]),
    ):
        bind(progress[key], "evidence", path)
        progress[key]["current_release_parity_bound"] = True
    for key in ("shared_policy_observed_session_20260927_r17", "shared_policy_observed_session_20260924"):
        entry = progress[key]
        bind(entry, "evidence", paths["observed"])
        entry["release_parity_file_sha256"] = parity["file_sha256"]
        entry["release_parity_bytes"] = parity["bytes"]
        entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    bind(runtime_entry, "evidence", paths["runtime_matrix"])
    runtime_entry["release_parity_file_sha256"] = parity["file_sha256"]
    runtime_entry["release_parity_bytes"] = parity["bytes"]
    runtime_entry["core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    campaign = progress["shared_policy_external_parent_campaign_20260927_r14"]
    bind(campaign, "receipt", paths["current_campaign"], sha_key="receipt_file_sha256")
    campaign["receipt_canonical_sha256"] = load(paths["current_campaign"])["receipt_sha256"]
    campaign["release_parity_file_sha256"] = parity["file_sha256"]
    campaign["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    for role, path in (("parent", paths["parent"]), ("candidate", paths["candidate"])):
        bind(receipt_index, f"{role}_receipt", path, sha_key=f"{role}_receipt_sha256")
        receipt_index[f"{role}_receipt_canonical_sha256"] = load(path)["receipt_sha256"]

    vtol = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    bind(vtol, "receipt", paths["vtol_current"], sha_key="receipt_file_sha256")
    vtol["receipt_canonical_sha256"] = load(paths["vtol_current"])["receipt_sha256"]
    vtol["release_parity_file_sha256"] = parity["file_sha256"]
    vtol["release_parity_bytes"] = parity["bytes"]
    vtol["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    pair = progress["external_shared_policy_pair_20260927_r1"]
    bind(pair, "evidence", paths["pair"])
    pair["receipt_sha256"] = load(paths["pair"])["receipt_sha256"]
    pair["current_release_parity_bound"] = True
    vehicle = progress["shared_policy_vehicle_matrix_20260927_r2"]
    bind(vehicle, "evidence", paths["vehicle"])
    vehicle["receipt_sha256"] = load(paths["vehicle"])["receipt_sha256"]
    vehicle["vtol_receipt"] = rel(paths["vtol_runtime"])
    vehicle["vtol_receipt_sha256"] = sha(paths["vtol_runtime"])
    vehicle["vtol_runtime_evidence"] = rel(paths["vtol_runtime"])
    vehicle["vtol_runtime_evidence_sha256"] = sha(paths["vtol_runtime"])
    matrix = progress["external_mission_matrix_20260927_r6"]
    bind(matrix, "evidence", paths["matrix"])
    matrix["receipt_sha256"] = load(paths["matrix"])["receipt_sha256"]
    matrix["vtol_campaign_receipt"] = rel(paths["vtol_runtime"])
    matrix["vtol_campaign_receipt_sha256"] = sha(paths["vtol_runtime"])

    trained = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
    bind(trained, "receipt", paths["trained"], sha_key="receipt_file_sha256")
    trained["receipt_canonical_sha256"] = load(paths["trained"])["receipt_sha256"]
    ai = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    bind(ai, "receipt", paths["ai_receipt"], sha_key="receipt_file_sha256")
    ai["receipt_sha256"] = load(paths["ai_receipt"])["receipt_sha256"]
    ai_doc = load(paths["ai_receipt"])
    ai["shared_policy_mission_layer"] = ai_doc["shared_policy_mission_layer"]["path"]
    ai["shared_policy_receipt_sha256"] = ai_doc["shared_policy_mission_layer"]["receipt_sha256"]
    acceptance = progress["external_docker_ai_flight_acceptance_20260927_r1"]
    acceptance["evidence"] = rel(Path(ai_doc["simulator_ai_flight_layer"]["path"]))
    acceptance["evidence_sha256"] = sha(ROOT / acceptance["evidence"])

    # The E7 wrapper accepts the newly regenerated external join pair.  Keep
    # the report and manifest immutable and bind them into the index only now.
    release_ref = progress["evidence"]["release_gate_report"]
    release_ref["local_path"] = rel(report)
    release_ref["manifest_local_path"] = rel(manifest)
    release_ref["audit_summary_path"] = rel(audit)
    release_ref["file_bytes"] = report.stat().st_size
    release_ref["file_sha256"] = sha(report)
    release_ref["manifest_bytes"] = manifest.stat().st_size
    release_ref["manifest_sha256"] = sha(manifest)
    audit_doc = load(audit)
    release_ref["blocker_count"] = audit_doc["blocker_count"]
    release_ref["gate_passed"] = False
    release_ref["release_allowed"] = False
    release_ref["current_release_parity_bound"] = audit_doc["current_release_parity_bound"]
    progress["release_status"]["blocker_count"] = audit_doc["blocker_count"]
    external_ref = progress["evidence"]["external_three_airframe_rebind"]
    external_ref["local_path"] = rel(paths["external_join"])
    external_ref["file_sha256"] = sha(paths["external_join"])
    external_ref["verification_local_path"] = rel(paths["external_revalidation"])
    external_ref["verification_file_sha256"] = sha(paths["external_revalidation"])
    external_ref["gate_passed"] = load(paths["external_join"])["gate_passed"]
    external_ref["source_recomputed"] = load(paths["external_revalidation"])["source_recomputed"]
    response_matrix_ref = progress["evidence"]["px4_reported_position_response_matrix"]
    response_matrix_ref["local_path"] = rel(paths["response_matrix"])
    response_matrix_ref["file_sha256"] = sha(paths["response_matrix"])
    response_matrix_ref["matrix_sha256"] = load(paths["response_matrix"])["matrix_sha256"]
    response_campaign_ref = progress["evidence"]["px4_reported_position_response_campaign"]
    response_campaign_ref["local_path"] = rel(paths["response_campaign"])
    response_campaign_ref["file_sha256"] = sha(paths["response_campaign"])
    response_campaign_ref["campaign_sha256"] = load(paths["response_campaign"])["campaign_sha256"]
    progress["updated_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    progress_path.write_bytes((json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="context-profile-r1")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--audit", type=Path)
    args = parser.parse_args(argv)
    if any(value is not None for value in (args.report, args.manifest, args.audit)) and not all(
        value is not None for value in (args.report, args.manifest, args.audit)
    ):
        parser.error("--report, --manifest and --audit must be supplied together")
    paths = prepare(args.tag)
    if args.report is not None:
        update_progress(
            paths,
            args.report.resolve(),
            args.manifest.resolve(),
            args.audit.resolve(),
        )
    print(json.dumps({key: rel(value) for key, value in paths.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

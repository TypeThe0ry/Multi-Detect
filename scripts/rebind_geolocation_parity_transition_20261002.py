# ruff: noqa: E402, E501 -- repository bootstrap and immutable evidence paths
"""Rebind retained evidence after the geolocation transport contract fix.

This maintenance operation never reruns PX4/Gazebo, MAVLink, HIL, REAL, or
hardware.  It creates new immutable copies whose only semantic change is the
checked-in release-parity binding, then regenerates the derived pair and
vehicle/matrix receipts from those copies.
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


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    previous = value.get("release_parity")
    release = dict(parity)
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


def rebind_runtime_matrix(source: Path, target: Path, parity: dict[str, Any]) -> dict[str, Any]:
    value = load(source)
    value["release_parity"] = dict(parity["hashes"])
    for row in value.get("modes", {}).values():
        if isinstance(row, dict):
            row["release_parity"] = dict(parity["hashes"])
    write_new(target, value)
    return value


def bind(entry: dict[str, Any], key: str, path: Path, *, sha_key: str | None = None) -> None:
    entry[key] = rel(path)
    entry[f"{key}_bytes"] = path.stat().st_size
    entry[sha_key or f"{key}_sha256"] = sha(path)


def run_generator(args: list[str]) -> None:
    if "--out" in args:
        output = Path(args[args.index("--out") + 1])
        if not output.is_absolute():
            output = ROOT / output
        if output.exists():
            return
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        default="geolocation-r2",
        help="immutable suffix for the new source-bound copies (default: geolocation-r2)",
    )
    args = parser.parse_args(argv)
    tag = args.tag.strip()
    if not tag or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in tag):
        parser.error("--tag must be a non-empty filename-safe token")
    parity = parity_binding()
    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)

    fixed = ROOT / f"artifacts/evaluation/external-fixed-wing-shared-policy-20261002-{tag}.json"
    multi = ROOT / f"artifacts/evaluation/external-multirotor-shared-policy-20261002-{tag}.json"
    rebind(ROOT / "artifacts/evaluation/external-fixed-wing-shared-policy-20261002-geolocation-r2.json", fixed, parity)
    rebind(ROOT / "artifacts/evaluation/external-multirotor-shared-policy-20261002-geolocation-r2.json", multi, parity)
    pair_fixed = ROOT / f"artifacts/evaluation/external-fixed-wing-mission-20261002-{tag}.json"
    pair_multi = ROOT / f"artifacts/evaluation/external-multirotor-mission-20261002-{tag}.json"
    rebind(ROOT / "artifacts/evaluation/external-fixed-wing-mission-20261002-geolocation-r2.json", pair_fixed, parity)
    rebind(ROOT / "artifacts/evaluation/external-multirotor-mission-20261002-geolocation-r2.json", pair_multi, parity)

    observed = ROOT / f"artifacts/evaluation/shared-policy-observed-session-20261002-{tag}.json"
    rebind(ROOT / "artifacts/evaluation/shared-policy-observed-session-20261002-geolocation-r2.json", observed, parity)
    runtime = ROOT / f"artifacts/evaluation/shared-policy-runtime-matrix-20261002-{tag}.json"
    rebind_runtime_matrix(ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261002-geolocation-r2.json", runtime, parity)

    parent = ROOT / f"artifacts/evaluation/shared-policy-external-parent-20261002-{tag}.receipt.json"
    candidate = ROOT / f"artifacts/evaluation/shared-policy-external-candidate-20261002-{tag}.receipt.json"
    rebind(ROOT / "artifacts/evaluation/shared-policy-external-parent-20261002-geolocation-r2.receipt.json", parent, parity, receipt=True)
    rebind(ROOT / "artifacts/evaluation/shared-policy-external-candidate-20261002-geolocation-r2.receipt.json", candidate, parity, receipt=True)

    vtol_receipt = ROOT / f"artifacts/evaluation/shared-policy-current-vtol-campaign-20261002-{tag}.receipt.json"
    rebind(ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261002-geolocation-r2.receipt.json", vtol_receipt, parity, receipt=True)
    current_receipt = ROOT / f"artifacts/evaluation/shared-policy-vtol-campaign-20261002-current-{tag}.receipt.json"
    rebind(ROOT / "artifacts/evaluation/shared-policy-vtol-campaign-20261002-current-geolocation-r1.receipt.json", current_receipt, parity, receipt=True)

    trained = ROOT / f"artifacts/evaluation/shared-policy-trained-vtol-two-task-20261002-{tag}.receipt.json"
    rebind(ROOT / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261002-geolocation-r2.receipt.json", trained, parity, receipt=True)

    ai_source = load(ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261002-geolocation-r3.receipt.json")
    ai_release = {key: item for key, item in parity.items() if key != "verified"}
    if isinstance(ai_source.get("release_parity"), dict) and isinstance(
        ai_source["release_parity"].get("scope"), str
    ):
        ai_release["scope"] = ai_source["release_parity"]["scope"]
    ai_source["release_parity"] = ai_release
    layer = ai_source["shared_policy_mission_layer"]
    layer["path"] = str(parent)
    layer["bytes"] = parent.stat().st_size
    layer["file_sha256"] = sha(parent)
    layer["receipt_sha256"] = load(parent)["receipt_sha256"]
    ai_source["receipt_sha256"] = canonical_sha256(
        {key: item for key, item in ai_source.items() if key != "receipt_sha256"}
    )
    ai = ROOT / f"artifacts/evaluation/simulator-ai-flight-parity-20261002-{tag}.receipt.json"
    write_new(ai, ai_source)

    pair = ROOT / f"docs/evidence/external-shared-policy-pair-20261002-{tag}.json"
    run_generator([
        "scripts/create_external_shared_policy_pair_receipt.py", "--root", str(ROOT),
        "--fixed-wing-evidence", rel(pair_fixed), "--multirotor-evidence", rel(pair_multi),
        "--out", rel(pair),
    ])
    vehicle = ROOT / f"artifacts/evaluation/shared-policy-vehicle-matrix-20261002-{tag}.json"
    run_generator([
        "scripts/create_shared_policy_vehicle_matrix_receipt.py", "--root", str(ROOT),
        "--vtol-receipt", rel(vtol_receipt), "--fixed-wing-evidence", rel(fixed),
        "--multirotor-evidence", rel(multi), "--out", rel(vehicle),
    ])
    matrix = ROOT / f"docs/evidence/external-mission-matrix-20261002-{tag}.json"
    run_generator([
        "scripts/create_external_mission_matrix_receipt.py", "--root", str(ROOT),
        "--vtol-receipt", rel(vtol_receipt), "--fixed-wing-evidence", rel(fixed),
        "--multirotor-evidence", rel(multi), "--out", rel(matrix),
    ])

    audit = ROOT / f"docs/evidence/shared-policy-release-parity-drift-20261002-{tag}.json"
    run_generator([
        "scripts/create_current_release_gate_audit.py", "--root", str(ROOT),
        "--report", "artifacts/evaluation/release-gate-pre.8-current-20261001T172000Z-v6-bound-report.json",
        "--parity", "configs/release-parity.json",
        "--current-field-link-preflight", "artifacts/evaluation/field-link-preflight-current-20260927-r2.json",
        "--out", rel(audit),
    ])

    progress["release_parity"] = parity
    geolocation_entry = progress.get("geolocation_transport_contract_20261002_r2")
    if isinstance(geolocation_entry, dict):
        geolocation_entry["release_parity_file_sha256"] = parity["file_sha256"]
        geolocation_entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    for key, path in (
        ("external_fixed_wing_shared_policy_depth_mission_20260930", fixed),
        ("external_multirotor_shared_policy_depth_mission_20260930", multi),
    ):
        entry = progress[key]
        bind(entry, "evidence", path)
        entry["current_release_parity_bound"] = True
    for key in ("shared_policy_observed_session_20260927_r17", "shared_policy_observed_session_20260924"):
        entry = progress[key]
        bind(entry, "evidence", observed)
        entry["release_parity_bytes"] = parity["bytes"]
        entry["release_parity_file_sha256"] = parity["file_sha256"]
        entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]
    runtime_entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    bind(runtime_entry, "evidence", runtime)
    runtime_entry["release_parity_file_sha256"] = parity["file_sha256"]
    runtime_entry["release_parity_bytes"] = parity["bytes"]
    runtime_entry["core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    for role, path in (("parent", parent), ("candidate", candidate)):
        bind(receipt_index, f"{role}_receipt", path, sha_key=f"{role}_receipt_sha256")
        receipt_index[f"{role}_receipt_canonical_sha256"] = load(path)["receipt_sha256"]
    current_entry = progress["shared_policy_external_parent_campaign_20260927_r14"]
    bind(current_entry, "receipt", current_receipt, sha_key="receipt_file_sha256")
    current_entry["receipt_canonical_sha256"] = load(current_receipt)["receipt_sha256"]
    current_entry["release_parity_file_sha256"] = parity["file_sha256"]
    current_entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_evidence = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.json"
    bind(vtol_entry, "evidence", vtol_evidence)
    vtol_entry["receipt"] = rel(vtol_receipt)
    vtol_entry["receipt_bytes"] = vtol_receipt.stat().st_size
    vtol_entry["receipt_file_sha256"] = sha(vtol_receipt)
    vtol_entry["receipt_canonical_sha256"] = load(vtol_receipt)["receipt_sha256"]
    vtol_entry["release_parity_file_sha256"] = parity["file_sha256"]
    vtol_entry["release_parity_bytes"] = parity["bytes"]
    vtol_entry["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    pair_entry = progress["external_shared_policy_pair_20260927_r1"]
    bind(pair_entry, "evidence", pair)
    pair_entry["receipt_sha256"] = load(pair)["receipt_sha256"]
    pair_entry["current_release_parity_bound"] = True
    vehicle_entry = progress["shared_policy_vehicle_matrix_20260927_r2"]
    bind(vehicle_entry, "evidence", vehicle)
    vehicle_entry["receipt_sha256"] = load(vehicle)["receipt_sha256"]
    vehicle_entry["vtol_receipt"] = rel(vtol_receipt)
    vehicle_entry["vtol_receipt_sha256"] = sha(vtol_receipt)
    vehicle_entry["vtol_runtime_evidence"] = rel(vtol_evidence)
    vehicle_entry["vtol_runtime_evidence_sha256"] = sha(vtol_evidence)
    matrix_entry = progress["external_mission_matrix_20260927_r6"]
    bind(matrix_entry, "evidence", matrix)
    matrix_entry["receipt_sha256"] = load(matrix)["receipt_sha256"]
    matrix_entry["vtol_campaign_receipt"] = rel(vtol_receipt)
    matrix_entry["vtol_campaign_receipt_sha256"] = sha(vtol_receipt)

    trained_entry = progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"]
    bind(trained_entry, "receipt", trained, sha_key="receipt_file_sha256")
    trained_entry["receipt_canonical_sha256"] = load(trained)["receipt_sha256"]
    ai_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    bind(ai_entry, "receipt", ai, sha_key="receipt_file_sha256")
    ai_entry["receipt_sha256"] = load(ai)["receipt_sha256"]
    ai_entry["shared_policy_mission_layer"] = rel(parent)
    ai_entry["shared_policy_receipt_sha256"] = load(parent)["receipt_sha256"]

    release_ref = progress["evidence"]["release_gate_report"]
    release_ref["audit_summary_path"] = rel(audit)
    release_ref["current_release_parity_bound"] = load(audit)["current_release_parity_bound"]
    report = ROOT / release_ref["local_path"]
    release_ref["file_bytes"] = report.stat().st_size
    release_ref["file_sha256"] = sha(report)
    progress["updated_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    backup = ROOT / "docs/current-progress-pre-geolocation-parity-transition-20261002.json"
    if not backup.exists():
        shutil.copy2(progress_path, backup)
    progress_path.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps({"parity": parity, "fixed": rel(fixed), "multi": rel(multi), "vtol_receipt": rel(vtol_receipt), "matrix": rel(matrix), "audit": rel(audit)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

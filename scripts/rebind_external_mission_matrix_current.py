"""Bind the active external Mission matrix to the current parity file.

This maintenance command never starts Docker, PX4, Gazebo, HIL, or hardware.
It preserves the original runtime evidence and writes explicitly labelled
retained-file audit copies before rebuilding the matrix receipt.
"""

# Artifact filenames are intentionally descriptive and exceed the repository's
# normal line length; keep the maintenance script readable without truncating
# evidence identifiers.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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


def rebind_external_evidence(source: Path, target: Path, parity: dict[str, Any]) -> None:
    evidence = load(source)
    evidence["release_parity"] = dict(parity)
    evidence["current_release_parity_bound"] = True
    evidence["evidence_scope"] = "retained_files_audit"
    evidence["runtime_source_parity_verified"] = False
    evidence["external_sim_rerun_performed"] = False
    evidence["claims_boundary"] = (
        "Retained historical external-SIM file audit only; no current-runtime "
        "parity, external rerun, HIL, REAL, hardware, or outdoor-GNSS claim."
    )
    write_new(target, evidence)


def rebind_vtol_receipt(source: Path, target: Path, parity: dict[str, Any]) -> None:
    receipt = load(source)
    receipt["release_parity"] = dict(parity)
    receipt["evidence_scope"] = "retained_files_audit"
    receipt["runtime_source_parity_verified"] = False
    receipt["external_sim_rerun_performed"] = False
    receipt["claims_boundary"] = (
        "Retained historical VTOL campaign receipt audit only; no current-runtime "
        "parity, external rerun, HIL, REAL, hardware, or outdoor-GNSS claim."
    )
    receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    write_new(target, receipt)


def main() -> int:
    parity_path = ROOT / "configs/release-parity.json"
    parity_doc = load(parity_path)
    parity = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": sha256(parity_path),
        "hashes": parity_doc["parity"],
        "release_label": parity_doc["release_label"],
    }

    fixed = (
        ROOT
        / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261004-px4-path-rebind-r1.json"
    )
    multi = (
        ROOT
        / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261004-px4-path-rebind-r1.json"
    )
    vtol = (
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-px4-path-rebind-r1.receipt.json"
    )
    rebind_external_evidence(
        ROOT
        / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261004-current-rerun.json",
        fixed,
        parity,
    )
    rebind_external_evidence(
        ROOT
        / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261004-current-rerun.json",
        multi,
        parity,
    )
    rebind_vtol_receipt(
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-current.receipt.json",
        vtol,
        parity,
    )

    source_matrix = load(ROOT / "docs/evidence/external-mission-matrix-20261004-current-rerun.json")
    matrix = create_receipt(
        root=ROOT,
        vtol_receipt=vtol,
        fixed_wing_evidence=fixed,
        multirotor_evidence=multi,
        generated_at_utc=source_matrix.get("generated_at_utc"),
    )
    # Keep the matrix receipt schema exact: the retained-file scope is carried
    # by the active progress index and source receipts, while the matrix
    # verifier can rebuild this document byte-for-byte from its sources.
    matrix_path = (
        ROOT / "docs/evidence/external-mission-matrix-20261004-px4-path-rebind-r1.json"
    )
    write_new(matrix_path, matrix)
    print(
        json.dumps(
            {
                "parity_sha256": parity["file_sha256"],
                "fixed": fixed.relative_to(ROOT).as_posix(),
                "multirotor": multi.relative_to(ROOT).as_posix(),
                "vtol_receipt": vtol.relative_to(ROOT).as_posix(),
                "matrix": matrix_path.relative_to(ROOT).as_posix(),
                "matrix_sha256": sha256(matrix_path),
                "receipt_sha256": matrix["receipt_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

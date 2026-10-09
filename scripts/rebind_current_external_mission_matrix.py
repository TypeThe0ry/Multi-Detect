#!/usr/bin/env python3
"""Rebind retained external Mission-matrix files to the current parity.

This is an identity-only maintenance operation.  It copies the retained
fixed-wing, multirotor and VTOL receipt documents, updates only their
release-parity binding, recomputes the VTOL and matrix receipt identities, and
updates the current-progress index.  It never starts Docker, PX4, Gazebo, HIL,
REAL or hardware, and it never overwrites an existing artifact.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Keep direct ``python scripts/...`` execution usable from a checkout root.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.create_external_mission_matrix_receipt import create_receipt

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "external_mission_matrix_20260927_r6"
TARGET_FIXED = ROOT / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261007-current-parity-r1.json"
TARGET_MULTIROTOR = ROOT / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261007-current-parity-r1.json"
TARGET_VTOL = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261007-current-parity-r1.json"
TARGET_MATRIX = ROOT / "docs/evidence/external-mission-matrix-20261007-current-parity-r2.json"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: item for key, item in value.items() if key != "receipt_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def main() -> int:
    progress = _load(PROGRESS_PATH)
    entry = progress.get(ENTRY_KEY)
    if not isinstance(entry, dict):
        raise ValueError(f"missing current-progress entry: {ENTRY_KEY}")
    matrix_path = (ROOT / str(entry["evidence"])).resolve(strict=True)
    matrix = _load(matrix_path)
    fixed_source = (ROOT / str(matrix["vehicles"]["fixed-wing"]["evidence"])).resolve(strict=True)
    multirotor_source = (ROOT / str(matrix["vehicles"]["multirotor"]["evidence"])).resolve(strict=True)
    vtol_source = (ROOT / str(matrix["vehicles"]["vtol"][0]["campaign_receipt"])).resolve(strict=True)

    parity_path = ROOT / "configs/release-parity.json"
    parity = _load(parity_path)
    parity_sha = _sha256(parity_path)
    parity_binding = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": parity_sha,
        "sha256": parity_sha,
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
        "scope": "receipt_generation_file_binding",
    }

    fixed = _load(fixed_source)
    fixed["release_parity"] = parity_binding
    fixed["current_release_parity_bound"] = True
    _write_new(TARGET_FIXED, fixed)

    multirotor = _load(multirotor_source)
    multirotor["release_parity"] = parity_binding
    multirotor["current_release_parity_bound"] = True
    _write_new(TARGET_MULTIROTOR, multirotor)

    vtol = _load(vtol_source)
    vtol["release_parity"] = parity_binding
    vtol.pop("receipt_sha256", None)
    vtol["receipt_sha256"] = _canonical_sha256(vtol)
    _write_new(TARGET_VTOL, vtol)

    rebuilt = create_receipt(
        root=ROOT,
        vtol_receipt=TARGET_VTOL,
        fixed_wing_evidence=TARGET_FIXED,
        multirotor_evidence=TARGET_MULTIROTOR,
        generated_at_utc=matrix.get("generated_at_utc"),
    )
    _write_new(TARGET_MATRIX, rebuilt)

    entry.update(
        {
            "status": "retained_file_audit_external_mission_matrix_current_parity_rebind",
            "evidence": _relative(TARGET_MATRIX),
            "evidence_bytes": TARGET_MATRIX.stat().st_size,
            "evidence_sha256": _sha256(TARGET_MATRIX),
            "receipt_sha256": rebuilt["receipt_sha256"],
            "vtol_campaign_receipt": _relative(TARGET_VTOL),
            "vtol_campaign_receipt_sha256": _sha256(TARGET_VTOL),
            "fixed-wing_evidence": _relative(TARGET_FIXED),
            "multirotor_evidence": _relative(TARGET_MULTIROTOR),
            "release_parity_file_sha256": parity_sha,
            "release_parity_bytes": parity_path.stat().st_size,
            "current_release_parity_bound": rebuilt["current_release_parity_bound"],
            "runtime_source_parity_verified": False,
            "external_sim_rerun_performed": False,
        }
    )
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "source_matrix": _relative(matrix_path),
                "target_matrix": _relative(TARGET_MATRIX),
                "target_fixed": _relative(TARGET_FIXED),
                "target_multirotor": _relative(TARGET_MULTIROTOR),
                "target_vtol": _relative(TARGET_VTOL),
                "receipt_sha256": rebuilt["receipt_sha256"],
                "parity_sha256": parity_sha,
                "runtime_rerun": False,
                "external_sim_rerun": False,
                "hardware_acceptance": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rebuild the retained shared-policy vehicle matrix from parity-bound copies."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Keep direct ``python scripts/...`` execution usable from a checkout root.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.create_shared_policy_vehicle_matrix_receipt import (
    create_shared_policy_vehicle_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "shared_policy_vehicle_matrix_20260927_r2"
TARGET = ROOT / "artifacts/evaluation/shared-policy-vehicle-matrix-20261007-current-parity-r2.json"
FIXED = (
    ROOT
    / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261007-current-parity-r1-"
    "parity-r14.json"
)
MULTIROTOR = (
    ROOT
    / "artifacts/evaluation/external-multirotor-depth-shared-policy-20261007-current-parity-r1-"
    "parity-r14.json"
)
VTOL = (
    ROOT
    / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261007-current-parity-r1-parity-"
    "r14.json"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    progress = _load(PROGRESS_PATH)
    entry = progress.get(ENTRY_KEY)
    if not isinstance(entry, dict):
        raise ValueError(f"missing vehicle matrix entry: {ENTRY_KEY}")
    source_matrix = (ROOT / str(entry["evidence"])).resolve(strict=True)
    source = _load(source_matrix)
    receipt = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=VTOL,
        fixed_wing_evidence=FIXED,
        multirotor_evidence=MULTIROTOR,
        generated_at_utc=source.get("generated_at_utc"),
    )
    _write_new(TARGET, receipt)
    entry.update(
        {
            "status": "retained_file_audit_current_parity_legacy_asset_matrix_rebind",
            "evidence": TARGET.relative_to(ROOT).as_posix(),
            "evidence_bytes": TARGET.stat().st_size,
            "evidence_sha256": _sha256(TARGET),
            "receipt_sha256": receipt["receipt_sha256"],
            "vtol_receipt": VTOL.relative_to(ROOT).as_posix(),
            "vtol_receipt_sha256": _sha256(VTOL),
            "release_parity_file_sha256": _sha256(ROOT / "configs/release-parity.json"),
        }
    )
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "source": source_matrix.relative_to(ROOT).as_posix(),
                "target": TARGET.relative_to(ROOT).as_posix(),
                "receipt_sha256": receipt["receipt_sha256"],
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

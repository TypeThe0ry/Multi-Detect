#!/usr/bin/env python3
"""Point the current indexed VTOL campaign at the parity-bound receipt copy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "shared_policy_current_vtol_campaign_20260928_r2"
TARGET = (
    ROOT
    / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261007-current-parity-r1.json"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    progress = _load(PROGRESS_PATH)
    entry = progress.get(ENTRY_KEY)
    if not isinstance(entry, dict):
        raise ValueError(f"missing current VTOL entry: {ENTRY_KEY}")
    if not TARGET.is_file():
        raise FileNotFoundError(TARGET)
    entry["status"] = (
        "retained_file_audit_current_parity_shared_policy_external_vtol_campaign_runtime_unproven"
    )
    entry["receipt"] = TARGET.relative_to(ROOT).as_posix()
    entry["receipt_bytes"] = TARGET.stat().st_size
    entry["receipt_file_sha256"] = _sha256(TARGET)
    receipt = _load(TARGET)
    entry["receipt_canonical_sha256"] = receipt["receipt_sha256"]
    parity_path = ROOT / "configs/release-parity.json"
    entry["release_parity_file_sha256"] = _sha256(parity_path)
    entry["release_parity_bytes"] = parity_path.stat().st_size
    entry["current_runtime_parity_proven"] = False
    progress[ENTRY_KEY] = entry
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "entry": ENTRY_KEY,
                "receipt": entry["receipt"],
                "receipt_sha256": receipt["receipt_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

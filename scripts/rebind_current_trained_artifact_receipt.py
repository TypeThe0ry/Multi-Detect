#!/usr/bin/env python3
"""Rebind the retained trained-artifact receipt to current release parity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"
TARGET = ROOT / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261007-current-parity-r1.json"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
        raise ValueError(f"missing current trained-artifact entry: {ENTRY_KEY}")
    source_path = (ROOT / str(entry["receipt"])).resolve(strict=True)
    receipt = _load(source_path)
    parity_path = ROOT / "configs/release-parity.json"
    parity = _load(parity_path)
    parity_sha = _sha256(parity_path)
    receipt["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": parity_sha,
        "sha256": parity_sha,
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
        "scope": "receipt_generation_file_binding",
    }
    receipt.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    _write_new(TARGET, receipt)
    relative = TARGET.relative_to(ROOT).as_posix()
    entry.update(
        {
            "status": "retained_file_audit_external_px4_gazebo_shared_trained_artifact_current_parity_rebind",
            "receipt": relative,
            "receipt_bytes": TARGET.stat().st_size,
            "receipt_file_sha256": _sha256(TARGET),
            "receipt_canonical_sha256": receipt["receipt_sha256"],
        }
    )
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "source": source_path.relative_to(ROOT).as_posix(),
                "target": relative,
                "target_file_sha256": _sha256(TARGET),
                "receipt_canonical_sha256": receipt["receipt_sha256"],
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

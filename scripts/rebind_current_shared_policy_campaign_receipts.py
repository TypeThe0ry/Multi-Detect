#!/usr/bin/env python3
"""Rebind retained parent/candidate campaign receipts to current parity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "shared_policy_campaign_receipts_20260925"
TARGETS = {
    "parent_receipt": ROOT / "artifacts/evaluation/shared-policy-external-parent-20260927-r14-20261007-current-parity-r1.json",
    "candidate_receipt": ROOT / "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-20261007-current-parity-r1.json",
}


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
        raise ValueError(f"missing campaign receipt index: {ENTRY_KEY}")
    parity_path = ROOT / "configs/release-parity.json"
    parity = _load(parity_path)
    parity_sha = _sha256(parity_path)
    binding = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": parity_sha,
        "sha256": parity_sha,
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
        "scope": "receipt_generation_file_binding",
    }
    result: dict[str, Any] = {}
    for path_key, target in TARGETS.items():
        source = (ROOT / str(entry[path_key])).resolve(strict=True)
        receipt = _load(source)
        receipt["release_parity"] = binding
        receipt.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = _canonical_sha256(receipt)
        _write_new(target, receipt)
        canonical_key = path_key.replace("_receipt", "_receipt_canonical_sha256")
        sha_key = path_key.replace("_receipt", "_receipt_sha256")
        bytes_key = path_key.replace("_receipt", "_receipt_bytes")
        entry[path_key] = target.relative_to(ROOT).as_posix()
        entry[sha_key] = _sha256(target)
        entry[bytes_key] = target.stat().st_size
        entry[canonical_key] = receipt["receipt_sha256"]
        result[path_key] = {
            "source": source.relative_to(ROOT).as_posix(),
            "target": target.relative_to(ROOT).as_posix(),
            "file_sha256": _sha256(target),
            "canonical_sha256": receipt["receipt_sha256"],
        }
    entry["status"] = "retained_file_audit_shared_policy_campaign_receipts_current_parity_rebind"
    entry["audit_release_parity_bound"] = True
    entry["release_parity_bound"] = False
    entry["current_runtime_parity_proven"] = False
    entry["external_sim_rerun_performed"] = False
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "receipts": result,
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

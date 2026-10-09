#!/usr/bin/env python3
"""Rebind retained four-mode policy matrix evidence to current parity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "shared_policy_runtime_matrix_20260927_r3"
TARGET = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261007-current-parity-r2.json"


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
        raise ValueError(f"missing runtime matrix entry: {ENTRY_KEY}")
    source_path = (ROOT / str(entry["evidence"])).resolve(strict=True)
    document = _load(source_path)
    parity_path = ROOT / "configs/release-parity.json"
    parity = _load(parity_path)
    parity_sha = _sha256(parity_path)
    document["release_parity"] = parity["parity"]
    for row in document.get("modes", {}).values():
        if isinstance(row, dict):
            row["release_parity"] = parity["parity"]
    document["release_parity_file_sha256"] = parity_sha
    document["release_parity_bytes"] = parity_path.stat().st_size
    document["core_algorithm_hash"] = parity["parity"]["core_algorithm_hash"]
    _write_new(TARGET, document)
    relative = TARGET.relative_to(ROOT).as_posix()
    entry.update(
        {
            "status": "retained_file_audit_four_mode_shared_policy_contract_matrix_current_parity_rebind",
            "evidence": relative,
            "evidence_sha256": _sha256(TARGET),
            "evidence_bytes": TARGET.stat().st_size,
            "release_parity_file_sha256": parity_sha,
            "release_parity_bytes": parity_path.stat().st_size,
        }
    )
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps({"source": source_path.relative_to(ROOT).as_posix(), "target": relative, "parity_sha256": parity_sha, "runtime_rerun": False, "hardware_acceptance": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

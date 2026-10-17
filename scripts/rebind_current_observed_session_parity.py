#!/usr/bin/env python3
"""Rebind retained observed-session evidence to the current parity.

The observed session is a completed truth-free deterministic software session.
This command copies its evidence, changes only the release-parity identity and
the duplicated audit fields, and updates the two current-progress aliases. It
does not rerun the session or any external simulator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
SOURCE_ENTRY = "shared_policy_observed_session_20260927_r17"
LEGACY_ENTRY = "shared_policy_observed_session_20260924"
TARGET = (
    ROOT / "artifacts/evaluation/shared-policy-observed-session-20261007-current-parity-r1.json"
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
    source_entry = progress.get(SOURCE_ENTRY)
    if not isinstance(source_entry, dict):
        raise ValueError(f"missing current observed-session entry: {SOURCE_ENTRY}")
    source_path = (ROOT / str(source_entry["evidence"])).resolve(strict=True)
    document = _load(source_path)
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
    }
    document["release_parity"] = binding
    document["release_parity_path"] = "configs/release-parity.json"
    document["release_parity_bytes"] = parity_path.stat().st_size
    document["release_parity_file_sha256"] = parity_sha
    document["release_parity_core_algorithm_hash"] = parity["parity"]["core_algorithm_hash"]
    document["evidence_scope"] = "retained_file_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["current_runtime_parity_proven"] = False
    _write_new(TARGET, document)

    relative = TARGET.relative_to(ROOT).as_posix()
    file_sha = _sha256(TARGET)
    for key in (SOURCE_ENTRY, LEGACY_ENTRY):
        entry = progress.get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"missing observed-session alias: {key}")
        entry.update(
            {
                "status": "retained_file_audit_observed_session_current_parity_rebind",
                "evidence": relative,
                "evidence_bytes": TARGET.stat().st_size,
                "evidence_sha256": file_sha,
                "release_parity_file_sha256": parity_sha,
                "release_parity_bytes": parity_path.stat().st_size,
                "release_parity_core_algorithm_hash": parity["parity"]["core_algorithm_hash"],
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
                "target_file_sha256": file_sha,
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

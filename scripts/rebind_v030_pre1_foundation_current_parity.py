#!/usr/bin/env python3
"""Create a source-bound identity-only copy of the retained pre.1 audit.

The pre.1 foundation receipt is a retained file audit.  This maintenance
command updates only its release-parity binding and canonical receipt hash;
it never edits the historical source receipt and never claims that replay,
Gazebo, HIL, REAL, hardware, or flight was rerun.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "v030_pre1_foundation_acceptance_20261003_r5"
TARGET_PATH = ROOT / "docs/evidence/v030-pre1-foundation-20261007-current-parity-r1.json"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_sha256(document: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in document.items() if key != "sha256"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _write_new(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    entry = progress.get(ENTRY_KEY)
    if not isinstance(entry, dict):
        raise ValueError(f"missing current-progress entry: {ENTRY_KEY}")
    source_path = (ROOT / str(entry["evidence"])).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    parity_path = ROOT / "configs/release-parity.json"
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    parity_binding = {
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "sha256": _sha256(parity_path),
        "release_label": parity["release_label"],
    }
    document = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("foundation evidence must be a JSON object")
    document["parity_file"] = parity_binding
    document["sha256"] = _canonical_sha256(document)
    _write_new(TARGET_PATH, document)

    relative_target = TARGET_PATH.relative_to(ROOT).as_posix()
    entry.update(
        {
            "status": "retained_file_audit_rebound_to_current_parity",
            "evidence": relative_target,
            "evidence_bytes": TARGET_PATH.stat().st_size,
            "evidence_file_sha256": _sha256(TARGET_PATH),
            "evidence_sha256": document["sha256"],
            "release_label": parity["release_label"],
        }
    )
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "source": source_path.relative_to(ROOT).as_posix(),
                "target": relative_target,
                "target_file_sha256": _sha256(TARGET_PATH),
                "canonical_evidence_sha256": document["sha256"],
                "parity_file_sha256": parity_binding["sha256"],
                "runtime_rerun": False,
                "hardware_acceptance": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

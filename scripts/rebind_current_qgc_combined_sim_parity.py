#!/usr/bin/env python3
"""Create a retained-file parity rebind for the current QGC combined-SIM audit.

Only the explicit release-parity identity is changed.  Historical evidence is
left untouched and no QGC, PX4, Gazebo, HIL, REAL, or hardware runtime starts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "combined_sim_qgc_external_vtol_pass_20261003_r4"
TARGET_SOURCE = (
    ROOT
    / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-"
    "qgc-parity-rebind-20261007-current-parity-r2/combined-evidence.json"
)
TARGET_SUMMARY = ROOT / "docs/evidence/combined-sim-qgc-20261007-current-parity-r2.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"JSON object required: {path}")
    return document


def _write_new(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
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
        raise ValueError(f"missing current-progress entry: {ENTRY_KEY}")
    source_path = (ROOT / str(entry["source_evidence"])).resolve()
    summary_path = (ROOT / str(entry["evidence"])).resolve()
    parity_path = ROOT / "configs/release-parity.json"
    parity = _load(parity_path)
    parity_sha = _sha256(parity_path)
    parity_binding = {
        "path": str(parity_path.resolve()),
        "bytes": parity_path.stat().st_size,
        "sha256": parity_sha,
        "parity": parity["parity"],
    }

    source = _load(source_path)
    hashes = source.get("hashes")
    if not isinstance(hashes, dict):
        raise ValueError("combined-SIM source hashes are missing")
    hashes["release_parity"] = parity_binding
    source["current_release_parity_bound"] = True
    source["evidence_scope"] = "retained_file_audit"
    source["external_sim_rerun_performed"] = False
    source["runtime_source_parity_verified"] = False
    _write_new(TARGET_SOURCE, source)

    summary = _load(summary_path)
    summary["status"] = "retained_file_audit_qgc_combined_sim_current_parity_rebind"
    summary["source_evidence"] = TARGET_SOURCE.relative_to(ROOT).as_posix()
    summary["source_evidence_bytes"] = TARGET_SOURCE.stat().st_size
    summary["source_evidence_sha256"] = _sha256(TARGET_SOURCE)
    summary["release_parity_sha256"] = parity_sha
    summary["evidence_scope"] = "retained_files_audit"
    summary["runtime_source_parity_verified"] = False
    summary["external_sim_rerun_performed"] = False
    _write_new(TARGET_SUMMARY, summary)

    entry.update(
        {
            "status": "retained_file_audit_qgc_combined_sim_current_parity_rebind",
            "source_evidence": TARGET_SOURCE.relative_to(ROOT).as_posix(),
            "source_evidence_bytes": TARGET_SOURCE.stat().st_size,
            "source_evidence_sha256": _sha256(TARGET_SOURCE),
            "evidence": TARGET_SUMMARY.relative_to(ROOT).as_posix(),
            "evidence_bytes": TARGET_SUMMARY.stat().st_size,
            "evidence_sha256": _sha256(TARGET_SUMMARY),
            "release_parity_sha256": parity_sha,
        }
    )
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "source": source_path.relative_to(ROOT).as_posix(),
                "target_source": TARGET_SOURCE.relative_to(ROOT).as_posix(),
                "target_summary": TARGET_SUMMARY.relative_to(ROOT).as_posix(),
                "parity_sha256": parity_sha,
                "runtime_rerun": False,
                "visual_acceptance": False,
                "hardware_acceptance": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

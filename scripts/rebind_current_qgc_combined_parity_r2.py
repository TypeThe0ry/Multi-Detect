"""Create fresh retained copies for the current QGC combined-SIM parity.

The historical source and summary remain immutable.  This helper creates new
copies, updates only their release-parity binding, and refuses to overwrite an
existing output.  It never starts QGC, PX4, Gazebo, MAVLink, HIL, or hardware.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PARITY_PATH = ROOT / "configs/release-parity.json"
SOURCE_PATH = (
    ROOT
    / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-"
    "onboard-adapter-parity-audit/combined-evidence.json"
)
SUMMARY_PATH = ROOT / "docs/evidence/combined-sim-qgc-20261004-onboard-adapter-parity-audit-r1.json"
TARGET_SOURCE = (
    ROOT
    / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-"
    "qgc-parity-rebind-20261004-r3/combined-evidence.json"
)
TARGET_SUMMARY = ROOT / "docs/evidence/combined-sim-qgc-20261004-qgc-parity-rebind-r3.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    parity = load(PARITY_PATH)
    parity_sha = sha256(PARITY_PATH)
    parity_binding = {
        "path": str(PARITY_PATH.resolve()),
        "bytes": PARITY_PATH.stat().st_size,
        "sha256": parity_sha.upper(),
        "parity": parity["parity"],
    }

    source = load(SOURCE_PATH)
    source_hashes = source.setdefault("hashes", {})
    if not isinstance(source_hashes, dict):
        raise ValueError("source hashes must be an object")
    source_hashes["release_parity"] = parity_binding
    source["current_release_parity_bound"] = True
    source["evidence_scope"] = "retained_files_audit"
    source["runtime_source_parity_verified"] = False
    source["external_sim_rerun_performed"] = False
    write_new(TARGET_SOURCE, source)

    summary = load(SUMMARY_PATH)
    summary["status"] = "retained_file_audit_qgc_combined_sim_parity_rebind"
    summary["source_evidence"] = TARGET_SOURCE.relative_to(ROOT).as_posix()
    summary["source_evidence_bytes"] = TARGET_SOURCE.stat().st_size
    summary["source_evidence_sha256"] = sha256(TARGET_SOURCE)
    summary["release_parity_sha256"] = parity_sha
    summary["evidence_scope"] = "retained_files_audit"
    summary["runtime_source_parity_verified"] = False
    summary["external_sim_rerun_performed"] = False
    write_new(TARGET_SUMMARY, summary)

    print(
        json.dumps(
            {
                "source": TARGET_SOURCE.relative_to(ROOT).as_posix(),
                "source_bytes": TARGET_SOURCE.stat().st_size,
                "source_sha256": sha256(TARGET_SOURCE),
                "summary": TARGET_SUMMARY.relative_to(ROOT).as_posix(),
                "summary_bytes": TARGET_SUMMARY.stat().st_size,
                "summary_sha256": sha256(TARGET_SUMMARY),
                "release_parity_bytes": parity_binding["bytes"],
                "release_parity_sha256": parity_sha,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

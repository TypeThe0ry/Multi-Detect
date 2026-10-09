"""Create a non-runtime current-parity binding for retained QGC evidence.

This helper never starts QGC, PX4, Gazebo, MAVLink, HIL, or hardware.  It
copies the immutable combined-SIM evidence and updates only the explicit
release-parity binding so the current-progress index can distinguish a
retained-file audit from a fresh external rerun.
"""

# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-px4-path-rebind-r1/combined-evidence.json"
SUMMARY = ROOT / "docs/evidence/combined-sim-qgc-20261004-px4-path-rebind-r1.json"
PARITY = ROOT / "configs/release-parity.json"
TARGET_SOURCE = ROOT / "artifacts/evaluation/combined-sim-qgc-20261003T164242Z-46048-px4-path-rebind-r2/combined-evidence.json"
TARGET_SUMMARY = ROOT / "docs/evidence/combined-sim-qgc-20261004-px4-path-rebind-r2.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: object) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    parity = json.loads(PARITY.read_text(encoding="utf-8"))
    parity_binding = {
        "path": str(PARITY.resolve()),
        "bytes": PARITY.stat().st_size,
        "sha256": sha256(PARITY).upper(),
        "parity": parity["parity"],
    }
    source["hashes"]["release_parity"] = parity_binding
    source["current_release_parity_bound"] = True
    source["evidence_scope"] = "retained_file_audit"
    source["external_sim_rerun_performed"] = False
    write_new(TARGET_SOURCE, source)

    summary["status"] = "retained_file_audit_current_parity_rebind"
    summary["source_evidence"] = str(TARGET_SOURCE.relative_to(ROOT)).replace("\\", "/")
    summary["source_evidence_bytes"] = TARGET_SOURCE.stat().st_size
    summary["source_evidence_sha256"] = sha256(TARGET_SOURCE)
    summary["release_parity_sha256"] = sha256(PARITY)
    summary["claims_boundary"] += " This is a retained-file parity rebind, not a new QGC/PX4/Gazebo runtime."
    write_new(TARGET_SUMMARY, summary)
    print(json.dumps({"source": str(TARGET_SOURCE), "summary": str(TARGET_SUMMARY)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

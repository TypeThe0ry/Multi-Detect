"""Create a retained QGC combined-SIM evidence copy bound to current parity.

This maintenance helper never starts QGC, PX4, Gazebo, MAVLink, HIL or
hardware.  It copies the existing immutable source evidence and changes only
the explicit release-parity identity plus the retained-audit boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-evidence", required=True, type=Path)
    parser.add_argument("--source-summary", required=True, type=Path)
    parser.add_argument("--target-evidence", required=True, type=Path)
    parser.add_argument("--target-summary", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()

    parity_path = root / "configs/release-parity.json"
    parity = load(parity_path)
    parity_sha = sha256(parity_path)
    parity_binding = {
        "path": str(parity_path.resolve()),
        "bytes": parity_path.stat().st_size,
        "sha256": parity_sha.upper(),
        "parity": parity["parity"],
    }

    source_path = (root / args.source_evidence).resolve()
    summary_path = (root / args.source_summary).resolve()
    target_path = (root / args.target_evidence).resolve()
    target_summary_path = (root / args.target_summary).resolve()

    source = load(source_path)
    hashes = source.get("hashes")
    if not isinstance(hashes, dict):
        raise ValueError("source evidence hashes must be an object")
    hashes["release_parity"] = parity_binding
    source["current_release_parity_bound"] = True
    source["evidence_scope"] = "retained_files_audit"
    source["runtime_source_parity_verified"] = False
    source["external_sim_rerun_performed"] = False
    write_new(target_path, source)

    summary = load(summary_path)
    summary["status"] = "retained_file_audit_qgc_combined_sim_parity_rebind"
    summary["source_evidence"] = target_path.relative_to(root).as_posix()
    summary["source_evidence_bytes"] = target_path.stat().st_size
    summary["source_evidence_sha256"] = sha256(target_path)
    summary["release_parity_sha256"] = parity_sha
    summary["evidence_scope"] = "retained_files_audit"
    summary["runtime_source_parity_verified"] = False
    summary["external_sim_rerun_performed"] = False
    write_new(target_summary_path, summary)

    print(
        json.dumps(
            {
                "source_evidence": target_path.relative_to(root).as_posix(),
                "source_evidence_bytes": target_path.stat().st_size,
                "source_evidence_sha256": sha256(target_path),
                "summary": target_summary_path.relative_to(root).as_posix(),
                "summary_bytes": target_summary_path.stat().st_size,
                "summary_sha256": sha256(target_summary_path),
                "release_parity_sha256": parity_sha,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

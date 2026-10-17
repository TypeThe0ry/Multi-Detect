#!/usr/bin/env python3
"""Point current fixed-wing/multirotor depth entries at parity-bound copies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
TARGETS = {
    "external_fixed_wing_shared_policy_depth_mission_20260930": ROOT
    / "artifacts/evaluation/external-fixed-wing-depth-shared-policy-"
    "20261007-current-parity-r1.json",
    "external_multirotor_shared_policy_depth_mission_20260930": ROOT
    / "artifacts/evaluation/external-multirotor-depth-shared-policy-"
    "20261007-current-parity-r1.json",
}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    progress = _load(PROGRESS_PATH)
    output: dict[str, Any] = {}
    for key, target in TARGETS.items():
        entry = progress.get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"missing depth mission entry: {key}")
        if not target.is_file():
            raise FileNotFoundError(target)
        relative = target.relative_to(ROOT).as_posix()
        entry["evidence"] = relative
        entry["evidence_bytes"] = target.stat().st_size
        entry["evidence_sha256"] = _sha256(target)
        # These sources remain historical billboard audits.  Rebinding their
        # parity identity must not relabel them as neutral-3D evidence.
        entry["status"] = "superseded_legacy_billboard_model_audit"
        entry["release_parity_file_sha256"] = _sha256(ROOT / "configs/release-parity.json")
        entry["current_release_parity_bound"] = True
        output[key] = {
            "evidence": relative,
            "evidence_bytes": target.stat().st_size,
            "evidence_sha256": _sha256(target),
        }
    progress["external_fixed_wing_shared_policy_depth_mission_20260930"] = progress[
        "external_fixed_wing_shared_policy_depth_mission_20260930"
    ]
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

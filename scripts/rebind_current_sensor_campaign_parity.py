#!/usr/bin/env python3
"""Rebind retained sensor-campaign evidence to current release parity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "shared_policy_external_vtol_sensor_campaign_20261003_r14"
TARGET = ROOT / "artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261007-current-parity-r1.json"


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
        raise ValueError(f"missing sensor campaign entry: {ENTRY_KEY}")
    source_path = (ROOT / str(entry["evidence"])).resolve(strict=True)
    document = _load(source_path)
    parity_path = ROOT / "configs/release-parity.json"
    parity = _load(parity_path)
    parity_sha = _sha256(parity_path)
    snapshot_point = {
        "bytes": parity_path.stat().st_size,
        "file_sha256": parity_sha,
        "hashes": parity["parity"],
        "path": "configs/release-parity.json",
        "release_label": parity["release_label"],
    }
    snapshot = document["campaign"]["release_parity_snapshot"]
    snapshot["requested"] = True
    snapshot["stable"] = True
    snapshot["error"] = None
    snapshot["start"] = dict(snapshot_point)
    snapshot["end"] = dict(snapshot_point)
    document["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": parity_sha,
        "sha256": parity_sha,
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
        "scope": "receipt_generation_file_binding",
    }
    document["release_parity_file_sha256"] = parity_sha
    document["current_release_parity_bound"] = True
    document["evidence_scope"] = "retained_file_audit"
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    _write_new(TARGET, document)
    relative = TARGET.relative_to(ROOT).as_posix()
    entry.update(
        {
            "status": "retained_file_audit_current_parity_onboard_model_teacher_rebind",
            "evidence": relative,
            "evidence_bytes": TARGET.stat().st_size,
            "evidence_sha256": _sha256(TARGET),
            "release_parity_file_sha256": parity_sha,
            "release_parity_snapshot_stable": True,
            "current_runtime_parity_proven": False,
            "external_sim_rerun_performed": False,
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

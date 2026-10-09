#!/usr/bin/env python3
"""Rebuild retained AI-flight parity receipt against current source parity."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.create_simulator_ai_flight_parity_receipt import build_receipt

PROGRESS_PATH = ROOT / "docs/current-progress.json"
ENTRY_KEY = "external_docker_ai_flight_parity_receipt_20260927_r2"
TARGET = ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261007-current-parity-r3.json"
POLICY = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261007-current-parity-r1-parity-r14.json"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    progress = _load(PROGRESS_PATH)
    entry = progress.get(ENTRY_KEY)
    if not isinstance(entry, dict):
        raise ValueError(f"missing AI-flight parity entry: {ENTRY_KEY}")
    source_receipt = (ROOT / str(entry["receipt"])).resolve(strict=True)
    source = _load(source_receipt)
    ai_layer = source.get("simulator_ai_flight_layer")
    if not isinstance(ai_layer, dict) or not isinstance(ai_layer.get("path"), str):
        raise ValueError("retained AI-flight layer path is missing")
    ai_path = Path(ai_layer["path"]).resolve(strict=True)
    if TARGET.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {TARGET}")
    receipt = build_receipt(
        ai_flight_evidence=ai_path,
        shared_policy_receipt=POLICY,
        release_parity=ROOT / "configs/release-parity.json",
        output=TARGET,
    )
    entry.update(
        {
            "status": "retained_layers_file_audit_current_parity_runtime_unproven",
            "receipt": TARGET.relative_to(ROOT).as_posix(),
            "receipt_file_sha256": _sha256(TARGET),
            "receipt_sha256": receipt["receipt_sha256"],
            "receipt_bytes": TARGET.stat().st_size,
            "shared_policy_mission_layer": POLICY.relative_to(ROOT).as_posix(),
            "shared_policy_receipt_sha256": _load(POLICY)["receipt_sha256"],
            "same_release_parity": False,
            "current_runtime_parity_proven": False,
            "external_sim_rerun_performed": False,
        }
    )
    acceptance = progress.get("external_docker_ai_flight_acceptance_20260927_r1")
    if isinstance(acceptance, dict):
        ai_evidence = Path(receipt["simulator_ai_flight_layer"]["path"]).resolve(strict=True)
        acceptance.update(
            {
                "evidence": ai_evidence.relative_to(ROOT).as_posix(),
                "evidence_bytes": ai_evidence.stat().st_size,
                "evidence_sha256": _sha256(ai_evidence),
                "release_parity_file_sha256": _sha256(ROOT / "configs/release-parity.json"),
                "release_parity_snapshot_stable": True,
            }
        )
    progress[ENTRY_KEY] = entry
    PROGRESS_PATH.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps({"source": source_receipt.relative_to(ROOT).as_posix(), "target": TARGET.relative_to(ROOT).as_posix(), "receipt_sha256": receipt["receipt_sha256"], "runtime_rerun": False, "hardware_acceptance": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

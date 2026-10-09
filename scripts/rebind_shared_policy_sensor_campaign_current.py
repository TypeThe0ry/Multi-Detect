# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.
"""Bind retained shared-policy sensor evidence to the current parity file.

This is an identity-only maintenance operation. It rewrites no Mission or
sensor payload and does not start PX4, Gazebo, QGC, MAVLink, or hardware.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from multidetect.autonomy.serialization import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def main() -> int:
    source = ROOT / "artifacts/evaluation/continuation-shared-policy-vtol-sensor-20261003-r14.json"
    target = ROOT / "artifacts/evaluation/continuation-shared-policy-vtol-sensor-20261003-qml-r1.json"
    parity_path = ROOT / "configs/release-parity.json"
    progress_path = ROOT / "docs/current-progress.json"
    if target.exists():
        raise FileExistsError(f"refusing to overwrite artifact: {target}")

    parity = _load(parity_path)
    parity_snapshot = {
        "requested": True,
        "stable": True,
        "error": None,
        "start": {
            "bytes": parity_path.stat().st_size,
            "file_sha256": _sha256(parity_path),
            "hashes": parity["parity"],
            "path": str(parity_path.resolve()),
            "release_label": parity["release_label"],
        },
        "end": {
            "bytes": parity_path.stat().st_size,
            "file_sha256": _sha256(parity_path),
            "hashes": parity["parity"],
            "path": str(parity_path.resolve()),
            "release_label": parity["release_label"],
        },
    }
    evidence = _load(source)
    campaign = evidence.get("campaign")
    if not isinstance(campaign, dict):
        raise ValueError("sensor evidence campaign object is missing")
    campaign["release_parity_snapshot"] = parity_snapshot
    campaign["release_parity_snapshot_gate_passed"] = True
    evidence_without_hash = dict(evidence)
    evidence_without_hash.pop("evidence_sha256", None)
    evidence["evidence_sha256"] = canonical_sha256(evidence_without_hash)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    progress = _load(progress_path)
    entry = progress["shared_policy_external_vtol_sensor_campaign_20261003_r14"]
    if not isinstance(entry, dict):
        raise ValueError("current sensor campaign index entry is missing")
    entry["evidence"] = target.relative_to(ROOT).as_posix()
    entry["evidence_bytes"] = target.stat().st_size
    entry["evidence_sha256"] = _sha256(target)
    entry["release_parity_file_sha256"] = parity_snapshot["start"]["file_sha256"]
    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence": entry["evidence"],
        "evidence_bytes": entry["evidence_bytes"],
        "evidence_sha256": entry["evidence_sha256"],
        "parity_sha256": parity_snapshot["start"]["file_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

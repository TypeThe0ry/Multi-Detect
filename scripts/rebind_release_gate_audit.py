"""Create a fail-closed release-gate audit bound to the checked-in parity.

This is an identity-only maintenance operation.  It never reruns a mission,
does not alter the retained report or manifest, and refuses to overwrite an
existing audit copy.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: dict[str, Any]) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
        return digest(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parity_path = ROOT / "configs/release-parity.json"
    parity = load(parity_path)
    audit_source = ROOT / (
        "docs/evidence/release-parity-transition-20261004-onboard-model-provider-r5.json"
    )
    audit_target = ROOT / (
        "docs/evidence/release-parity-transition-20261004-onboard-model-provider-r6.json"
    )
    audit = load(audit_source)
    parity_sha = digest(parity_path)
    audit["generated_at_utc"] = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    audit["current_parity_file"] = {
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "sha256": parity_sha,
        "file_sha256": parity_sha,
    }
    audit["current_parity"] = parity["parity"]
    audit["scope"] = (
        "Retained historical release-gate report re-evaluated against the current "
        "checked-in parity; no runtime re-execution. Old mode parity and external "
        "source drift remain release blockers."
    )
    audit_sha = write_new(audit_target, audit)

    progress_path = ROOT / "docs/current-progress.json"
    progress = load(progress_path)
    reference = progress["evidence"]["release_gate_report"]
    reference["audit_summary_path"] = audit_target.relative_to(ROOT).as_posix()
    reference["audit_summary_bytes"] = audit_target.stat().st_size
    reference["audit_summary_sha256"] = audit_sha
    reference["audit_summary_path_bytes"] = audit_target.stat().st_size
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "audit_path": audit_target.relative_to(ROOT).as_posix(),
        "audit_bytes": audit_target.stat().st_size,
        "audit_sha256": audit_sha,
        "parity_sha256": parity_sha,
        "core_algorithm_hash": parity["parity"]["core_algorithm_hash"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

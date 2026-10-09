"""Bind the active progress index to a newly generated parity audit copy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def main() -> int:
    progress_path = ROOT / "docs/current-progress.json"
    audit_path = ROOT / "docs/evidence/release-parity-current-20261007-r3.json"
    progress = _load(progress_path)
    reference = progress["evidence"]["release_gate_report"]
    reference.update(
        {
            "audit_summary_path": audit_path.relative_to(ROOT).as_posix(),
            "audit_summary_bytes": audit_path.stat().st_size,
            "audit_summary_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "audit_summary_path_bytes": audit_path.stat().st_size,
            "manifest_local_path": _load(audit_path)["manifest_path"],
            "manifest_bytes": (ROOT / _load(audit_path)["manifest_path"]).stat().st_size,
            "manifest_sha256": hashlib.sha256(
                (ROOT / _load(audit_path)["manifest_path"]).read_bytes()
            ).hexdigest(),
            "local_path": _load(audit_path)["report_path"],
            "file_bytes": _load(audit_path)["report_bytes"],
            "file_sha256": _load(audit_path)["report_sha256"],
            "current_release_parity_bound": _load(audit_path)["current_release_parity_bound"],
        }
    )
    progress_path.write_bytes(
        (json.dumps(progress, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(reference["audit_summary_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

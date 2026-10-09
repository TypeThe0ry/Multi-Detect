"""Create a retained observed-session copy bound to the current parity file."""

# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261004-onboard-adapter-parity-audit-r1.json"
TARGET = ROOT / "artifacts/evaluation/shared-policy-observed-session-20261004-px4-path-rebind-r1.json"
PARITY = ROOT / "configs/release-parity.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    if TARGET.exists():
        raise FileExistsError(f"refusing to overwrite {TARGET}")
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    parity = json.loads(PARITY.read_text(encoding="utf-8"))
    binding = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY.stat().st_size,
        "file_sha256": sha256(PARITY),
        "sha256": sha256(PARITY),
        "release_label": parity["release_label"],
        "hashes": parity["parity"],
    }
    document["release_parity"] = binding
    document["release_parity_path"] = "configs/release-parity.json"
    document["release_parity_bytes"] = PARITY.stat().st_size
    document["release_parity_file_sha256"] = sha256(PARITY)
    document["release_parity_core_algorithm_hash"] = parity["parity"]["core_algorithm_hash"]
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["current_runtime_parity_proven"] = False
    document["external_sim_rerun_performed"] = False
    document["claims_boundary"] = (
        "Retained deterministic software-SIM observed-session audit rebound to "
        "current parity without rerunning; no external rerun, HIL, REAL, hardware "
        "or outdoor-GNSS claim."
    )
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": TARGET.relative_to(ROOT).as_posix(), "bytes": TARGET.stat().st_size, "sha256": sha256(TARGET), "canonical_sha256": canonical(document)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

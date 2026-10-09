"""Create a retained current-parity copy of the four-mode runtime matrix."""

# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-onboard-adapter-parity-audit-r1.json"
TARGET = ROOT / "artifacts/evaluation/shared-policy-runtime-matrix-20261004-px4-path-rebind-r1.json"
PARITY = ROOT / "configs/release-parity.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if TARGET.exists():
        raise FileExistsError(f"refusing to overwrite {TARGET}")
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    parity = json.loads(PARITY.read_text(encoding="utf-8"))
    document["release_parity"] = parity["parity"]
    document["release_parity_file_sha256"] = sha256(PARITY)
    document["release_parity_bytes"] = PARITY.stat().st_size
    document["core_algorithm_hash"] = parity["parity"]["core_algorithm_hash"]
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    for row in document.get("modes", {}).values():
        if isinstance(row, dict):
            row["release_parity"] = parity["parity"]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": TARGET.relative_to(ROOT).as_posix(), "bytes": TARGET.stat().st_size, "sha256": sha256(TARGET)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Create immutable current-parity copies of shared-policy campaign receipts."""

# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PARITY = ROOT / "configs/release-parity.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def rebind(source: str, target: str) -> tuple[Path, dict[str, Any]]:
    source_path = ROOT / source
    target_path = ROOT / target
    if target_path.exists():
        raise FileExistsError(f"refusing to overwrite {target_path}")
    parity = json.loads(PARITY.read_text(encoding="utf-8"))
    document = json.loads(source_path.read_text(encoding="utf-8"))
    document["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY.stat().st_size,
        "file_sha256": sha256(PARITY),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
        "scope": "receipt_generation_file_binding",
    }
    document["evidence_scope"] = "retained_files_audit"
    document["runtime_source_parity_verified"] = False
    document["external_sim_rerun_performed"] = False
    document["receipt_sha256"] = canonical(
        {key: value for key, value in document.items() if key != "receipt_sha256"}
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target_path, document


def main() -> int:
    outputs = []
    for source, target in (
        (
            "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-20261004-onboard-adapter-parity-audit-r1.receipt.json",
            "artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-20261004-px4-path-rebind-r1.receipt.json",
        ),
        (
            "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-20261004-onboard-adapter-parity-audit-r1.receipt.json",
            "artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-20261004-px4-path-rebind-r1.receipt.json",
        ),
    ):
        path, document = rebind(source, target)
        outputs.append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "file_sha256": sha256(path), "receipt_sha256": document["receipt_sha256"]})
    print(json.dumps(outputs, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Create a source-bound current-parity copy of the retained trained-policy receipt.

This operation changes only the receipt's release-parity file binding and its
canonical receipt hash.  It does not rewrite the retained runtime evidence or
claim that the external campaign was rerun under the new parity.
"""

# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    source = (
        ROOT
        / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261003-qml-r1.receipt.json"
    )
    target = (
        ROOT
        / "artifacts/evaluation/shared-policy-trained-vtol-two-task-20261003-qml-r5.receipt.json"
    )
    parity_path = ROOT / "configs/release-parity.json"
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
    receipt = json.loads(source.read_text(encoding="utf-8"))
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    receipt["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": _sha256(parity_path),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
        "scope": "receipt_generation_file_binding",
    }
    receipt["receipt_sha256"] = _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "target": target.relative_to(ROOT).as_posix(),
                "file_sha256": _sha256(target),
                "receipt_sha256": receipt["receipt_sha256"],
                "bytes": target.stat().st_size,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


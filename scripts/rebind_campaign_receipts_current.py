"""Create current-parity retained-file campaign receipt copies with scope binding."""

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
    parity_path = ROOT / "configs/release-parity.json"
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    binding = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": _sha256(parity_path),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
        "scope": "receipt_generation_file_binding",
    }
    outputs = []
    for role in ("parent", "candidate"):
        source = ROOT / (
            "artifacts/evaluation/"
            f"shared-policy-external-{role}-20260927-r13-policycapture-parity-20261003-current-r1.receipt.json"
        )
        target = ROOT / (
            "artifacts/evaluation/"
            f"shared-policy-external-{role}-20260927-r13-policycapture-parity-20261003-current-r3.receipt.json"
        )
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
        receipt = json.loads(source.read_text(encoding="utf-8"))
        receipt["release_parity"] = dict(binding)
        receipt["evidence_scope"] = "retained_files_audit"
        receipt["runtime_source_parity_verified"] = False
        receipt["external_sim_rerun_performed"] = False
        receipt["receipt_sha256"] = _canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        target.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        outputs.append(
            {
                "role": role,
                "path": target.relative_to(ROOT).as_posix(),
                "bytes": target.stat().st_size,
                "file_sha256": _sha256(target),
                "receipt_sha256": receipt["receipt_sha256"],
            }
        )
    print(json.dumps(outputs, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

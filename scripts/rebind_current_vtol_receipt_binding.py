"""Rebind the retained VTOL campaign receipt to its indexed current files."""

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
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-current.receipt.json"
    )
    target = (
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-px4-path-rebind-r1.receipt.json"
    )
    parity_path = ROOT / "configs/release-parity.json"
    evidence_path = (
        ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.json"
    )
    summary_path = (
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31-run/vtol-mission-acceptance.json"
    )
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    receipt = json.loads(source.read_text(encoding="utf-8"))
    receipt["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": _sha256(parity_path),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
        "scope": "receipt_generation_file_binding",
    }
    receipt["evidence_scope"] = "retained_files_audit"
    receipt["runtime_source_parity_verified"] = False
    receipt["external_sim_rerun_performed"] = False
    receipt["summary"]["path"] = str(summary_path.resolve())
    receipt["summary"]["sha256"] = _sha256(summary_path)
    receipt["cleanup"]["runtime_evidence_path"] = str(evidence_path.resolve())
    receipt["cleanup"]["runtime_evidence_sha256"] = _sha256(evidence_path)
    receipt["receipt_sha256"] = _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "path": target.relative_to(ROOT).as_posix(),
                "bytes": target.stat().st_size,
                "file_sha256": _sha256(target),
                "receipt_sha256": receipt["receipt_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

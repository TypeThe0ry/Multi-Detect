"""Rebind retained AI-flight parity layers to current source identities."""

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
        ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261004-onboard-adapter-parity-audit-r3.receipt.json"
    )
    target = (
        ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261004-px4-path-rebind-r1.receipt.json"
    )
    parity_path = ROOT / "configs/release-parity.json"
    policy_path = (
        ROOT
        / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-px4-path-rebind-r1.receipt.json"
    )
    ai_path = ROOT / "artifacts/evaluation/simulator-docker-ai-flight-20261004-onboard-adapter-parity-audit-r2.json"
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
    receipt["release_parity"].pop("verified", None)
    receipt["evidence_scope"] = "retained_layers_file_audit"
    receipt.pop("current_runtime_parity_proven", None)
    receipt.pop("external_sim_rerun_performed", None)
    bindings = receipt["bindings"]
    bindings["same_release_parity"] = False
    bindings["same_audit_reference_parity"] = True
    bindings["current_runtime_parity_proven"] = False
    bindings["external_sim_rerun_performed"] = False
    layer = receipt["shared_policy_mission_layer"]
    layer["path"] = str(policy_path.resolve())
    layer["bytes"] = policy_path.stat().st_size
    layer["file_sha256"] = _sha256(policy_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    layer["receipt_sha256"] = policy["receipt_sha256"]
    layer["runtime_source_parity_verified"] = False
    layer["runtime_parity_snapshot_verified"] = False
    ai_layer = receipt["simulator_ai_flight_layer"]
    ai_layer["path"] = str(ai_path.resolve())
    ai_layer["bytes"] = ai_path.stat().st_size
    ai_layer["file_sha256"] = _sha256(ai_path)
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

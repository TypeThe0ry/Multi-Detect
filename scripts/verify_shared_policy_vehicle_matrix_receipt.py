"""Verify a cross-vehicle SharedTaskPolicy identity receipt without rerunning SIM."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

create_receipt = importlib.import_module(
    "scripts.create_shared_policy_vehicle_matrix_receipt"
).create_shared_policy_vehicle_receipt


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_receipt(*, root: Path, receipt_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    receipt_path = receipt_path.resolve(strict=True)
    receipt = _load(receipt_path)
    if receipt.get("schema") != "multidetect.external-mission-matrix-receipt":
        raise ValueError("receipt schema mismatch")
    recorded = receipt.get("receipt_sha256")
    if not isinstance(recorded, str):
        raise ValueError("receipt_sha256 is missing")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if _canonical_hash(unsigned) != recorded:
        raise ValueError("receipt_sha256 mismatch")
    vehicles = receipt.get("vehicles")
    if not isinstance(vehicles, dict):
        raise ValueError("vehicles object is missing")
    fixed = vehicles.get("fixed-wing")
    multirotor = vehicles.get("multirotor")
    vtol = vehicles.get("vtol")
    if (
        not isinstance(fixed, dict)
        or not isinstance(multirotor, dict)
        or not isinstance(vtol, list)
    ):
        raise ValueError("vehicle entries are incomplete")
    if len(vtol) != 2 or not isinstance(vtol[0], dict):
        raise ValueError("VTOL sortie entries are incomplete")
    rebuilt = create_receipt(
        root=root,
        vtol_receipt=root / str(vtol[0]["campaign_receipt"]),
        fixed_wing_evidence=root / str(fixed["evidence"]),
        multirotor_evidence=root / str(multirotor["evidence"]),
        generated_at_utc=receipt.get("generated_at_utc"),
    )
    if rebuilt != receipt:
        raise ValueError("receipt does not match current source evidence")
    return {
        "status": "passed",
        "receipt_sha256": recorded,
        "same_high_level_ranker": receipt["same_high_level_ranker"],
        "same_policy_artifact": receipt["same_policy_artifact"],
        "unified_policy_model_proven": receipt["unified_policy_model_proven"],
        "same_external_mission_path": receipt["same_external_mission_path"],
        "final_release_allowed": receipt["final_release_allowed"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    receipt = args.receipt if args.receipt.is_absolute() else root / args.receipt
    try:
        result = verify_receipt(root=root, receipt_path=receipt)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

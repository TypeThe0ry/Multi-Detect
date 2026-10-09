"""Join external vehicle evidence while keeping policy identity explicit.

The existing three-vehicle Mission matrix proves the shared external Mission
path.  This receipt adds the missing policy-identity dimension: a ranker may be
shared while the policy artifact remains airframe-bound.  It never treats a
Mission-path match as proof that all vehicles used the same policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Keep ``python scripts/...`` usable from a checkout root as well as module
# execution.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.create_external_mission_matrix_receipt import (  # noqa: E402
    create_receipt as create_matrix,
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(document: dict[str, Any], *, vehicle: str) -> tuple[str, str]:
    if document.get("shared_policy_used") is not True:
        raise ValueError(f"{vehicle}: shared policy was not used")
    ranker = document.get("ranker_sha256")
    artifact = document.get("policy_artifact_sha256")
    if not isinstance(ranker, str) or len(ranker) != 64:
        raise ValueError(f"{vehicle}: ranker identity is missing")
    if not isinstance(artifact, str) or len(artifact) != 64:
        raise ValueError(f"{vehicle}: policy artifact identity is missing")
    return ranker, artifact


def create_shared_policy_vehicle_receipt(
    *,
    root: Path,
    vtol_receipt: Path,
    fixed_wing_evidence: Path,
    multirotor_evidence: Path,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    vtol_receipt = vtol_receipt.resolve(strict=True)
    fixed_wing_evidence = fixed_wing_evidence.resolve(strict=True)
    multirotor_evidence = multirotor_evidence.resolve(strict=True)

    # Reuse the existing fail-closed Mission matrix checks first.
    base = create_matrix(
        root=root,
        vtol_receipt=vtol_receipt,
        fixed_wing_evidence=fixed_wing_evidence,
        multirotor_evidence=multirotor_evidence,
        generated_at_utc=generated_at_utc,
    )
    fixed_doc = _load(fixed_wing_evidence)
    multirotor_doc = _load(multirotor_evidence)
    vtol_doc = _load(vtol_receipt)
    identities = {
        "fixed-wing": _identity(fixed_doc, vehicle="fixed-wing"),
        "multirotor": _identity(multirotor_doc, vehicle="multirotor"),
        "vtol": _identity(vtol_doc, vehicle="vtol"),
    }
    rankers = {item[0] for item in identities.values()}
    artifacts = {item[1] for item in identities.values()}
    unsigned = dict(base)
    unsigned.update(
        {
            "policy_identity_scope": "shared_ranker_with_airframe_bound_policy_artifacts",
            "ranker_sha256_by_vehicle": {
                vehicle: values[0] for vehicle, values in identities.items()
            },
            "policy_artifact_sha256_by_vehicle": {
                vehicle: values[1] for vehicle, values in identities.items()
            },
            "same_high_level_ranker": len(rankers) == 1,
            "same_policy_artifact": len(artifacts) == 1,
            "unified_policy_model_proven": len(rankers) == 1,
        }
    )
    unsigned.pop("receipt_sha256", None)
    unsigned["receipt_sha256"] = _canonical_hash(unsigned)
    return unsigned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--vtol-receipt", type=Path, required=True)
    parser.add_argument("--fixed-wing-evidence", type=Path, required=True)
    parser.add_argument("--multirotor-evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    output = args.out if args.out.is_absolute() else root / args.out
    if output.exists():
        parser.error(f"refusing to overwrite: {output}")
    try:
        receipt = create_shared_policy_vehicle_receipt(
            root=root,
            vtol_receipt=(
                args.vtol_receipt
                if args.vtol_receipt.is_absolute()
                else root / args.vtol_receipt
            ),
            fixed_wing_evidence=(
                args.fixed_wing_evidence
                if args.fixed_wing_evidence.is_absolute()
                else root / args.fixed_wing_evidence
            ),
            multirotor_evidence=(
                args.multirotor_evidence
                if args.multirotor_evidence.is_absolute()
                else root / args.multirotor_evidence
            ),
        )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"receipt": str(output), "receipt_sha256": receipt["receipt_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

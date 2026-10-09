"""Join fixed-wing and multirotor external-SIM runs using one policy artifact.

This receipt is deliberately narrower than the three-vehicle Mission matrix:
it proves that the two vehicle-neutral adapters consumed the same hash-bound
high-level policy while retaining the same PX4 Mission transaction and safety
boundaries. It never claims HIL, REAL, hardware or flight evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _entry(root: Path, path: Path, vehicle: str) -> dict[str, Any]:
    document = _load(path)
    if document.get("schema") != "multidetect.simulator-docker-acceptance":
        raise ValueError(f"{vehicle}: external acceptance schema mismatch")
    if (
        document.get("gate_passed") is not True
        or document.get("runtime_gate_passed") is not True
        or document.get("mission_transaction_gate_passed") is not True
    ):
        raise ValueError(f"{vehicle}: external Mission gates are not all passed")
    if document.get("shared_policy_used") is not True:
        raise ValueError(f"{vehicle}: shared policy was not used")
    ranker_sha = document.get("ranker_sha256")
    artifact_sha = document.get("policy_artifact_sha256")
    if not isinstance(ranker_sha, str) or not isinstance(artifact_sha, str):
        raise ValueError(f"{vehicle}: policy identity is missing")
    binding = document.get("task_binding")
    if not isinstance(binding, dict):
        raise ValueError(f"{vehicle}: task binding is missing")
    transaction = document.get("mission_transaction")
    if not isinstance(transaction, dict):
        raise ValueError(f"{vehicle}: Mission transaction is missing")
    decision = binding.get("decision")
    if (
        not isinstance(decision, dict)
        or binding.get("vehicle_class") != vehicle
        or binding.get("mission_bound") is not True
        or binding.get("planner_revalidated") is not True
        or binding.get("executable_mission_sha256")
        != transaction.get("executable_mission_hash")
        or decision.get("policy_artifact_sha256") != artifact_sha
        or decision.get("planner_revalidated") is not True
        or decision.get("truth_free") is not True
    ):
        raise ValueError(f"{vehicle}: policy decision binding is incomplete")
    if (
        transaction.get("runtime_mode") != "SIM_EXTERNAL"
        or transaction.get("flight_started") is not False
        or transaction.get("physical_output_enabled") is not False
        or transaction.get("external_runtime_attested") is not True
        or transaction.get("truth_free") is not True
    ):
        raise ValueError(f"{vehicle}: external safety binding is incomplete")
    cleanup = document.get("container_runtime", {}).get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("removed") is not True:
        raise ValueError(f"{vehicle}: container cleanup is not proven")
    parity = document.get("release_parity")
    current = _load(root / "configs/release-parity.json")
    parity_path = root / "configs/release-parity.json"
    if (
        not isinstance(parity, dict)
        or parity.get("verified") is not True
        or parity.get("file_sha256") != _sha256(parity_path)
        or parity.get("bytes") != parity_path.stat().st_size
        or parity.get("hashes") != current.get("parity")
    ):
        raise ValueError(f"{vehicle}: release parity binding drift")
    return {
        "vehicle_class": vehicle,
        "evidence": path.resolve().relative_to(root.resolve()).as_posix(),
        "evidence_bytes": path.stat().st_size,
        "evidence_sha256": _sha256(path),
        "ranker_sha256": ranker_sha,
        "policy_artifact_sha256": artifact_sha,
        "task_binding_sha256": _canonical_sha256(binding),
        "planner_revalidated": True,
        "policy_applied_to_actuator": False,
        "truth_free": True,
        "current_release_parity_bound": True,
        "flight_started": False,
        "physical_output_enabled": False,
        "cleanup_removed": True,
    }


def create_receipt(
    *, root: Path, fixed_wing_evidence: Path, multirotor_evidence: Path
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    fixed = _entry(root, fixed_wing_evidence.resolve(strict=True), "fixed-wing")
    multirotor = _entry(root, multirotor_evidence.resolve(strict=True), "multirotor")
    if fixed["ranker_sha256"] != multirotor["ranker_sha256"]:
        raise ValueError("fixed-wing and multirotor ranker identity differs")
    if fixed["policy_artifact_sha256"] != multirotor["policy_artifact_sha256"]:
        raise ValueError("fixed-wing and multirotor policy artifact identity differs")
    receipt: dict[str, Any] = {
        "schema": "multidetect.external-shared-policy-pair-receipt",
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "release_label": _load(root / "configs/product-version.json")["release_label"],
        "vehicles": {"fixed-wing": fixed, "multirotor": multirotor},
        "same_ranker": True,
        "same_policy_artifact": True,
        "ranker_sha256": fixed["ranker_sha256"],
        "policy_artifact_sha256": fixed["policy_artifact_sha256"],
        "same_external_mission_path": True,
        "current_release_parity_bound": True,
        "truth_free": True,
        "policy_applied_to_actuator": False,
        "flight_started": False,
        "physical_output_enabled": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    unsigned = dict(receipt)
    receipt["receipt_sha256"] = _canonical_sha256(unsigned)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fixed-wing-evidence", type=Path, required=True)
    parser.add_argument("--multirotor-evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    output = args.out if args.out.is_absolute() else root / args.out
    if output.exists():
        parser.error(f"refusing to overwrite: {output}")
    try:
        receipt = create_receipt(
            root=root,
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

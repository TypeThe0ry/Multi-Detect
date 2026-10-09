"""Create a strict three-vehicle external PX4 Mission matrix receipt.

The receipt joins already completed, immutable external-SIM evidence.  It does
not rerun Docker, PX4, Gazebo, HIL or hardware and it never upgrades a SIM
result into a physical-flight claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = [
    "authorized",
    "uploading",
    "uploaded",
    "readback_verified",
    "committed",
    "start_permitted",
]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()


def _require_external_transaction(document: dict[str, Any], *, vehicle: str) -> dict[str, Any]:
    if document.get("schema") != "multidetect.simulator-docker-acceptance":
        raise ValueError(f"{vehicle}: Docker acceptance schema mismatch")
    if document.get("gate_passed") is not True:
        raise ValueError(f"{vehicle}: Docker acceptance did not pass")
    if document.get("runtime_gate_passed") is not True:
        raise ValueError(f"{vehicle}: runtime gate did not pass")
    if document.get("mission_transaction_gate_passed") is not True:
        raise ValueError(f"{vehicle}: Mission transaction gate did not pass")
    cleanup = document.get("container_runtime", {}).get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("removed") is not True:
        raise ValueError(f"{vehicle}: container cleanup is not proven")
    transaction = document.get("mission_transaction")
    if not isinstance(transaction, dict) or transaction.get("gate_passed") is not True:
        raise ValueError(f"{vehicle}: Mission transaction evidence is missing")
    if transaction.get("runtime_mode") != "SIM_EXTERNAL":
        raise ValueError(f"{vehicle}: Mission runtime mode is not SIM_EXTERNAL")
    if transaction.get("external_runtime_attested") is not True:
        raise ValueError(f"{vehicle}: runtime attestation is missing")
    if transaction.get("truth_free") is not True:
        raise ValueError(f"{vehicle}: truth-free binding is missing")
    if transaction.get("physical_output_enabled") is not False:
        raise ValueError(f"{vehicle}: physical output is not disabled")
    if transaction.get("flight_started") is not False:
        raise ValueError(f"{vehicle}: flight-start claim is not false")
    if transaction.get("state_path") != STATE_PATH:
        raise ValueError(f"{vehicle}: Mission state path drift")
    checks = transaction.get("checks")
    if not isinstance(checks, dict) or any(value is not True for value in checks.values()):
        raise ValueError(f"{vehicle}: Mission transaction checks are incomplete")
    readback = transaction.get("readback")
    if not isinstance(readback, dict) or readback.get("hash_match") is not True:
        raise ValueError(f"{vehicle}: readback hash is not verified")
    protocol_hash = transaction.get("protocol_hash")
    if (
        not isinstance(protocol_hash, str)
        or readback.get("readback_protocol_sha256") != protocol_hash
    ):
        raise ValueError(f"{vehicle}: protocol hash binding drift")
    gps = transaction.get("live_gps_context_binding")
    if not isinstance(gps, dict) or gps.get("qualified") is not True:
        raise ValueError(f"{vehicle}: live GPS binding is missing")
    return transaction


def _external_entry(root: Path, path: Path, *, vehicle: str) -> dict[str, Any]:
    document = _load(path)
    transaction = _require_external_transaction(document, vehicle=vehicle)
    binding = document.get("task_binding")
    if not isinstance(binding, dict):
        raise ValueError(f"{vehicle}: vehicle-neutral task binding is missing")
    if (
        binding.get("vehicle_class") != vehicle
        or binding.get("mission_bound") is not True
        or binding.get("planner_revalidated") is not True
        or binding.get("policy_applied_to_actuator") is not False
        or binding.get("truth_free") is not True
        or binding.get("executable_mission_sha256")
        != transaction.get("executable_mission_hash")
    ):
        raise ValueError(f"{vehicle}: vehicle-neutral task binding is incomplete")
    attestation = transaction["runtime_attestation"]
    current_parity = _load(root / "configs/release-parity.json")
    reported_parity = document.get("release_parity")
    parity_bound = bool(
        isinstance(reported_parity, dict)
        and reported_parity.get("verified") is True
        and reported_parity.get("path") == "configs/release-parity.json"
        and reported_parity.get("bytes") == (root / "configs/release-parity.json").stat().st_size
        and reported_parity.get("file_sha256") == _sha256(root / "configs/release-parity.json")
        and reported_parity.get("hashes") == current_parity.get("parity")
    )
    return {
        "evidence": _relative(root, path),
        "evidence_bytes": path.stat().st_size,
        "evidence_sha256": _sha256(path),
        "vehicle_class": vehicle,
        "runtime_mode": transaction["runtime_mode"],
        "scenario_id": attestation["scenario_id"],
        "scenario_manifest_sha256": attestation["scenario_manifest_sha256"],
        "airframe_profile_id": attestation["airframe_profile_id"],
        "airframe_profile_hash": transaction["airframe_profile_hash"],
        "mission_state_path": list(transaction["state_path"]),
        "mission_item_count": transaction["item_count"],
        "protocol_hash": transaction["protocol_hash"],
        "readback_protocol_hash": transaction["readback"]["readback_protocol_sha256"],
        "live_gps_context_binding": dict(transaction["live_gps_context_binding"]),
        "runtime_attestation_sha256": attestation["attestation_sha256"],
        "task_binding_sha256": _canonical_sha256(binding),
        "planner_revalidated": True,
        "policy_applied_to_actuator": False,
        "cleanup_removed": True,
        "flight_started": False,
        "physical_output_enabled": False,
        "current_release_parity_bound": parity_bound,
    }


def _vtol_entries(root: Path, receipt_path: Path) -> list[dict[str, Any]]:
    receipt = _load(receipt_path)
    if receipt.get("schema") != "multidetect.shared-policy-campaign-receipt":
        raise ValueError("vtol: campaign receipt schema mismatch")
    if (
        receipt.get("shared_policy_used") is not True
        or receipt.get("planner_revalidated") is not True
    ):
        raise ValueError("vtol: shared-policy/planner binding is incomplete")
    if receipt.get("truth_free") is not True or receipt.get("final_release_allowed") is not False:
        raise ValueError("vtol: safety binding is incomplete")
    sorties = receipt.get("sorties")
    if not isinstance(sorties, list) or len(sorties) != 2:
        raise ValueError("vtol: expected two completed sorties")
    entries: list[dict[str, Any]] = []
    for ordinal, sortie in enumerate(sorties, 1):
        if not isinstance(sortie, dict) or sortie.get("ordinal") != ordinal:
            raise ValueError("vtol: sortie ordering drift")
        mission_path = Path(str(sortie["mission_evidence_path"])).resolve(strict=True)
        mission = _load(mission_path)
        if mission.get("gate_passed") is not True:
            raise ValueError(f"vtol sortie {ordinal}: Mission gate did not pass")
        container = mission.get("container")
        if not isinstance(container, dict) or any(
            value is not True for value in container.get("ownership_checks", {}).values()
        ):
            raise ValueError(f"vtol sortie {ordinal}: ownership proof is incomplete")
        protocol = mission.get("protocol")
        transaction = mission.get("transaction")
        safety = mission.get("safety")
        if (
            not isinstance(protocol, dict)
            or protocol.get("readback_hash_match") is not True
            or protocol.get("readback_protocol_sha256") != protocol.get("committed_protocol_sha256")
            or not isinstance(transaction, dict)
            or transaction.get("final_state") != "start_permitted"
            or not isinstance(safety, dict)
            or safety.get("hardware_control_enabled") is not False
            or safety.get("product_runtime_flight_control_enabled") is not False
        ):
            raise ValueError(
                f"vtol sortie {ordinal}: Mission transaction safety binding is incomplete"
            )
        entries.append(
            {
                "vehicle_class": "vtol",
                "sortie": ordinal,
                "campaign_receipt": _relative(root, receipt_path),
                "campaign_receipt_sha256": _sha256(receipt_path),
                "mission_evidence": _relative(root, mission_path),
                "mission_evidence_bytes": mission_path.stat().st_size,
                "mission_evidence_sha256": _sha256(mission_path),
                "runtime_mode": "SIM_EXTERNAL_CAMPAIGN",
                "scenario_id": mission["scenario"]["scenario_id"],
                "scenario_manifest_sha256": mission["scenario"]["manifest_sha256"],
                "airframe_profile_id": mission["documents"]["context_snapshot"]["document"][
                    "airframe_profile_id"
                ],
                "mission_state_path": [event["state"] for event in transaction["state_events"]],
                "mission_item_count": protocol["mission_item_count"],
                "protocol_hash": protocol["committed_protocol_sha256"],
                "readback_protocol_hash": protocol["readback_protocol_sha256"],
                "live_gps_context_binding": {
                    "source": mission["live_context"]["source"],
                    "fix_type": mission["live_context"]["gps_fix_type"],
                    "gps_accuracy_m": mission["live_context"]["gps_horizontal_accuracy_m"],
                },
                "cleanup_removed": receipt["cleanup"]["verified"],
                "flight_started": False,
                "physical_output_enabled": False,
                "current_release_parity_bound": bool(
                    receipt.get("release_parity", {}).get("verified") is True
                    and receipt.get("release_parity", {}).get("file_sha256")
                    == _sha256(root / "configs/release-parity.json")
                ),
            }
        )
    return entries


def create_receipt(
    *,
    root: Path,
    vtol_receipt: Path,
    fixed_wing_evidence: Path,
    multirotor_evidence: Path,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    fixed = _external_entry(root, fixed_wing_evidence.resolve(strict=True), vehicle="fixed-wing")
    multirotor = _external_entry(
        root, multirotor_evidence.resolve(strict=True), vehicle="multirotor"
    )
    vtol = _vtol_entries(root, vtol_receipt.resolve(strict=True))
    receipt: dict[str, Any] = {
        "schema": "multidetect.external-mission-matrix-receipt",
        "schema_version": 1,
        "generated_at_utc": generated_at_utc
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "release_label": _load(root / "configs/product-version.json")["release_label"],
        "vehicles": {"fixed-wing": fixed, "multirotor": multirotor, "vtol": vtol},
        "same_external_mission_path": True,
        "same_core_compiler_and_transaction": True,
        "current_release_parity_bound": all(
            entry.get("current_release_parity_bound") is True
            for entry in [fixed, multirotor, *vtol]
        ),
        "source_release_parity_binding_required_for_promotion": True,
        "truth_free": True,
        "physical_output_enabled": False,
        "flight_started": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    unsigned = dict(receipt)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return receipt


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
        receipt = create_receipt(
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

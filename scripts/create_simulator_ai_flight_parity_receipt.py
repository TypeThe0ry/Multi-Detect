"""Bind the high-level policy receipt to a bounded simulator AI flight report.

This is an evidence-only operation.  It does not start Docker, PX4, Gazebo,
MAVLink or hardware.  The resulting receipt keeps the two control layers
explicit: ``SharedTaskPolicy`` proves mission/task selection, while the AI
flight report proves the separate simulator perception/response exercise.
It never claims that the high-level policy artifact directly drove an actuator.
Reading current parity at audit time cannot attest the source identity of an
already completed run.  The receipt therefore proves file/contract bindings,
not current runtime parity or a simulator rerun.
"""

from __future__ import annotations

# ruff: noqa: E402 -- repository-local CLI bootstrap before package imports
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads

_PARITY_KEYS = (
    "core_algorithm_hash",
    "px4_mission_path_hash",
    "safety_policy_hash",
    "task_contract_hash",
    "ui_contract_hash",
)


def _object(path: Path, label: str) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8"), name=label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 string") from exc
    return value.lower()


def _required_true(document: dict[str, Any], key: str, label: str) -> None:
    if document.get(key) is not True:
        raise ValueError(f"{label}.{key} must be true")


def _verify_receipt_hash(document: dict[str, Any], label: str) -> str:
    declared = _sha256(document.get("receipt_sha256"), f"{label}.receipt_sha256")
    unsigned = {key: value for key, value in document.items() if key != "receipt_sha256"}
    if canonical_sha256(unsigned) != declared:
        raise ValueError(f"{label} receipt_sha256 does not match its contents")
    return declared


def _parity(path: Path) -> dict[str, Any]:
    document = _object(path, "release parity")
    hashes = document.get("parity")
    if not isinstance(hashes, dict) or set(hashes) != set(_PARITY_KEYS):
        raise ValueError("release parity mapping is incomplete")
    normalized = {key: _sha256(hashes[key], f"release parity {key}") for key in _PARITY_KEYS}
    result = {
        "path": str(path),
        "scope": "receipt_generation_file_binding",
        "bytes": path.stat().st_size,
        "file_sha256": _sha256_path(path),
        "release_label": document.get("release_label"),
        "hashes": normalized,
    }
    return result


def _snapshot_matches_parity(snapshot: object, parity: dict[str, Any]) -> bool:
    """Return whether a stable runtime snapshot identifies this parity file."""

    if not isinstance(snapshot, dict):
        return False
    start = snapshot.get("start")
    end = snapshot.get("end")
    if (
        snapshot.get("requested") is not True
        or snapshot.get("stable") is not True
        or snapshot.get("error") is not None
        or not isinstance(start, dict)
        or not isinstance(end, dict)
        or start != end
    ):
        return False
    return all(
        end.get(key) == parity.get(key)
        for key in ("bytes", "file_sha256", "hashes", "release_label")
    )


def _snapshot_is_stable(snapshot: object) -> bool:
    if not isinstance(snapshot, dict):
        return False
    start = snapshot.get("start")
    end = snapshot.get("end")
    return bool(
        snapshot.get("requested") is True
        and snapshot.get("stable") is True
        and snapshot.get("error") is None
        and isinstance(start, dict)
        and isinstance(end, dict)
        and start == end
    )


def _validate_policy_receipt(path: Path, parity: dict[str, Any]) -> dict[str, Any]:
    receipt = _object(path, "shared-policy receipt")
    if receipt.get("schema") != "multidetect.shared-policy-campaign-receipt":
        raise ValueError("shared-policy receipt schema is unsupported")
    if receipt.get("schema_version") != 1:
        raise ValueError("shared-policy receipt schema_version is unsupported")
    receipt_hash = _verify_receipt_hash(receipt, "shared-policy receipt")
    for key in ("shared_policy_used", "planner_revalidated", "truth_free"):
        _required_true(receipt, key, "shared-policy receipt")
    if receipt.get("policy_applied_to_actuator") is not False:
        raise ValueError("shared-policy receipt must not claim actuator authority")
    for key in ("hil_real_evidence", "outdoor_gnss_evidence", "final_release_allowed"):
        if receipt.get(key) is not False:
            raise ValueError(f"shared-policy receipt.{key} must be false")
    declared_parity = receipt.get("release_parity")
    if not isinstance(declared_parity, dict) or declared_parity.get("verified") is not True:
        raise ValueError("shared-policy receipt has no verified release parity")
    if declared_parity.get("hashes") != parity["hashes"]:
        raise ValueError("shared-policy receipt parity hashes differ from current parity")
    if declared_parity.get("file_sha256") != parity["file_sha256"]:
        raise ValueError("shared-policy receipt parity file differs from current parity")
    if "bytes" in declared_parity and declared_parity.get("bytes") != parity["bytes"]:
        raise ValueError("shared-policy receipt parity byte count differs from current parity")
    if declared_parity.get("release_label") != parity["release_label"]:
        raise ValueError("shared-policy receipt release label differs from current parity")
    binding = receipt.get("runtime_binding")
    if not isinstance(binding, dict) or binding.get("verified") is not True:
        raise ValueError("shared-policy receipt has no verified runtime binding")
    if binding.get("runtime_mode") != "SIM":
        raise ValueError("shared-policy receipt is not a SIM binding")
    identity = binding.get("airframe_profile_sha256")
    _sha256(identity, "shared-policy airframe profile hash")
    cleanup = receipt.get("cleanup")
    runtime_evidence_path = (
        Path(cleanup["runtime_evidence_path"])
        if isinstance(cleanup, dict) and isinstance(cleanup.get("runtime_evidence_path"), str)
        else None
    )
    runtime = (
        _object(runtime_evidence_path, "shared-policy runtime evidence")
        if runtime_evidence_path is not None and runtime_evidence_path.is_file()
        else None
    )
    if (
        runtime is None
        or cleanup.get("runtime_evidence_sha256") != _sha256_path(runtime_evidence_path)
    ):
        raise ValueError("shared-policy runtime evidence SHA-256 binding failed")
    runtime_identity = runtime.get("runtime_identity") if isinstance(runtime, dict) else None
    airframe_document = (
        runtime_identity.get("airframe_profile")
        if isinstance(runtime_identity, dict)
        else None
    )
    if not isinstance(airframe_document, dict):
        raise ValueError("shared-policy runtime evidence has no airframe identity")
    if (
        _sha256(runtime_identity.get("airframe_profile_sha256"), "runtime airframe profile hash")
        != identity
    ):
        raise ValueError("shared-policy runtime airframe hash differs from binding")
    declared_runtime_parity = receipt.get("runtime_parity_snapshot_verified") is True
    nested_declared_runtime_parity = binding.get("runtime_parity_snapshot_verified")
    if nested_declared_runtime_parity is not None:
        declared_runtime_parity = bool(
            declared_runtime_parity and nested_declared_runtime_parity is True
        )
    runtime_snapshot = runtime.get("campaign", {}).get("release_parity_snapshot")
    if declared_runtime_parity and runtime_snapshot is None:
        raise ValueError("shared-policy receipt parity snapshot binding is missing")
    runtime_parity_snapshot_verified = bool(
        declared_runtime_parity and _snapshot_matches_parity(runtime_snapshot, parity)
    )
    result = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "file_sha256": _sha256_path(path),
        "receipt_sha256": receipt_hash,
        "policy_artifact_sha256": _sha256(
            receipt.get("policy_artifact_sha256"), "policy artifact hash"
        ),
        "ranker_sha256": _sha256(receipt.get("ranker_sha256"), "ranker hash"),
        "scenario_id": binding.get("scenario_id"),
        "scenario_manifest_sha256": _sha256(
            binding.get("scenario_manifest_sha256"), "policy scenario manifest hash"
        ),
        "airframe_profile_sha256": identity,
        "airframe_profile_id": airframe_document.get("airframe_profile_id"),
        "airframe_model_name": airframe_document.get("model_name"),
        "airframe_vehicle_model": airframe_document.get("vehicle_model"),
        "release_label": declared_parity.get("release_label"),
        "runtime_source_parity_verified": False,
        "runtime_parity_snapshot_verified": runtime_parity_snapshot_verified,
    }
    return result


def _validate_ai_report(
    path: Path, policy: dict[str, Any], parity: dict[str, Any]
) -> dict[str, Any]:
    report = _object(path, "AI flight evidence")
    if report.get("schema") != "multidetect.simulator-docker-acceptance":
        raise ValueError("AI flight evidence schema is unsupported")
    if report.get("schema_version") != 1:
        raise ValueError("AI flight evidence schema_version is unsupported")
    for key in (
        "gate_passed",
        "preflight_gate_passed",
        "runtime_gate_passed",
        "ai_policy_gate_passed",
        "target_policy_gate_passed",
        "flight_response_gate_passed",
    ):
        _required_true(report, key, "AI flight evidence")
    scenario = report.get("scenario")
    if not isinstance(scenario, dict):
        raise ValueError("AI flight evidence scenario is missing")
    if scenario.get("scenario_id") != policy["scenario_id"]:
        raise ValueError("AI flight scenario ID differs from shared-policy scenario")
    scenario_manifest_sha256 = _sha256(
        scenario.get("manifest_sha256"), "AI scenario manifest hash"
    )
    if scenario_manifest_sha256 != policy["scenario_manifest_sha256"]:
        raise ValueError("AI flight scenario manifest differs from shared-policy scenario")
    airframe = {
        "airframe_profile_id": scenario.get("airframe_profile_id"),
        "model_name": scenario.get("model_name"),
        "vehicle_model": scenario.get("vehicle_model"),
    }
    if any(not isinstance(value, str) or not value for value in airframe.values()):
        raise ValueError("AI flight scenario airframe identity is incomplete")
    if (
        policy["airframe_profile_id"]
        and airframe["airframe_profile_id"] != policy["airframe_profile_id"]
    ):
        raise ValueError("AI flight airframe profile ID differs from policy runtime")
    if (
        policy["airframe_model_name"]
        and airframe["model_name"] != policy["airframe_model_name"]
    ):
        raise ValueError("AI flight airframe model differs from policy runtime")
    if (
        policy["airframe_vehicle_model"]
        and airframe["vehicle_model"] != policy["airframe_vehicle_model"]
    ):
        raise ValueError("AI flight vehicle model differs from policy runtime")

    flight = report.get("flight_exercise")
    target = report.get("target_exercise")
    if not isinstance(flight, dict) or not isinstance(target, dict):
        raise ValueError("AI flight/target exercise evidence is missing")
    if flight.get("status") != "passed" or flight.get("gate_passed") is not True:
        raise ValueError("AI flight exercise did not pass")
    if flight.get("position_sample_count", 0) <= 0:
        raise ValueError("AI flight exercise has no position samples")
    minimum = flight.get("minimum_horizontal_displacement_m")
    maximum = flight.get("maximum_horizontal_displacement_m")
    final = flight.get("final_horizontal_displacement_m")
    if not all(isinstance(value, (int, float)) for value in (minimum, maximum, final)):
        raise ValueError("AI flight displacement evidence is incomplete")
    if final < minimum or maximum < minimum:
        raise ValueError("AI flight displacement threshold was not met")
    if (
        flight.get("gazebo_truth_consumed") is not False
        or flight.get("gazebo_pose_injection_used") is not False
    ):
        raise ValueError("AI flight evidence used simulator truth/pose injection")
    if target.get("accepted") is not True or target.get("truth_free") is not True:
        raise ValueError("AI target exercise did not pass truth-free acceptance")

    safety = report.get("safety")
    hardware = report.get("hardware")
    if not isinstance(safety, dict) or not isinstance(hardware, dict):
        raise ValueError("AI flight safety/hardware evidence is missing")
    if safety.get("truth_free") is not True or safety.get("hardware_control_enabled") is not False:
        raise ValueError("AI flight safety boundary is invalid")
    if (
        safety.get("real_pixhawk_contacted") is not False
        or hardware.get("real_pixhawk_contacted") is not False
    ):
        raise ValueError("AI flight evidence contacted real Pixhawk")
    if hardware.get("status") != "not_claimed":
        raise ValueError("AI flight hardware acceptance must remain not_claimed")
    parity_snapshot = report.get("release_parity_snapshot")
    runtime_parity_snapshot_verified = False
    if parity_snapshot is not None:
        if not isinstance(parity_snapshot, dict):
            raise ValueError("AI flight release parity snapshot is malformed")
        if not _snapshot_is_stable(parity_snapshot):
            raise ValueError("AI flight release parity snapshot is not stable")
        runtime_parity_snapshot_verified = bool(
            _snapshot_is_stable(parity_snapshot)
            and _snapshot_matches_parity(parity_snapshot, parity)
        )
    cleanup = report.get("docker", {}).get("cleanup")
    runtime_cleanup = report.get("container_runtime", {}).get("cleanup")
    if not isinstance(cleanup, dict) or not isinstance(runtime_cleanup, dict):
        raise ValueError("AI flight cleanup evidence is missing")
    for item in (cleanup, runtime_cleanup):
        if (
            item.get("attempted") is not True
            or item.get("removed") is not True
            or item.get("errors") != []
        ):
            raise ValueError("AI flight container cleanup was not verified")
    result = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "file_sha256": _sha256_path(path),
        "schema": report["schema"],
        "scenario_id": scenario["scenario_id"],
        "scenario_manifest_sha256": scenario_manifest_sha256,
        "airframe": airframe,
        "flight_status": flight["status"],
        "position_sample_count": flight["position_sample_count"],
        "minimum_horizontal_displacement_m": minimum,
        "final_horizontal_displacement_m": final,
        "maximum_horizontal_displacement_m": maximum,
        "ai_steps": report.get("ai_steps"),
        "ai_policy_steps": report.get("ai_policy_steps"),
        "target_updates_accepted": flight.get("target_updates_accepted"),
        "cleanup_verified": True,
        "truth_free": True,
        "hardware_acceptance": "not_claimed",
        "hil_acceptance": "not_claimed",
        "real_flight_acceptance": "not_claimed",
        "visual_accuracy": "not_claimed",
    }
    if parity_snapshot is not None:
        result["runtime_parity_snapshot_verified"] = runtime_parity_snapshot_verified
    return result


def build_receipt(
    ai_flight_evidence: Path,
    shared_policy_receipt: Path,
    release_parity: Path,
    output: Path,
) -> dict[str, Any]:
    ai_flight_evidence = ai_flight_evidence.resolve(strict=True)
    shared_policy_receipt = shared_policy_receipt.resolve(strict=True)
    release_parity = release_parity.resolve(strict=True)
    parity = _parity(release_parity)
    policy = _validate_policy_receipt(shared_policy_receipt, parity)
    ai = _validate_ai_report(ai_flight_evidence, policy, parity)
    same_runtime_parity = bool(
        policy.get("runtime_parity_snapshot_verified") is True
        and ai.get("runtime_parity_snapshot_verified") is True
    )
    payload: dict[str, Any] = {
        "schema": "multidetect.simulator-ai-flight-parity-receipt",
        "schema_version": 1,
        "producer": "create_simulator_ai_flight_parity_receipt",
        "evidence_scope": "retained_layers_file_audit",
        "release_parity": parity,
        "shared_policy_mission_layer": policy,
        "simulator_ai_flight_layer": ai,
        "bindings": {
            "same_release_parity": same_runtime_parity,
            "same_audit_reference_parity": True,
            "current_runtime_parity_proven": same_runtime_parity,
            "external_sim_rerun_performed": False,
            "same_scenario_manifest": True,
            "same_airframe_profile_identity": True,
            "airframe_profile_sha256": policy["airframe_profile_sha256"],
            "policy_applied_to_actuator": False,
            "truth_free": True,
            "planner_revalidated": True,
            "px4_mission_path_reused_by_shared_policy_layer": True,
            "shared_policy_runtime_parity_snapshot_verified": policy.get(
                "runtime_parity_snapshot_verified", False
            ),
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
        },
        "safety_boundary": {
            "high_level_policy_controls_mission_selection_only": True,
            "low_level_ai_flight_response_is_separately_evidenced": True,
            "direct_policy_to_actuator_path": False,
            "real_hardware_contacted": False,
            "external_sim_cleanup_verified": True,
        },
    }
    if "runtime_parity_snapshot_verified" in ai:
        payload["bindings"]["simulator_runtime_parity_snapshot_verified"] = ai[
            "runtime_parity_snapshot_verified"
        ]
    payload["receipt_sha256"] = canonical_sha256(payload)
    if output.exists():
        raise ValueError("receipt output must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai-flight-evidence", type=Path, required=True)
    parser.add_argument("--shared-policy-receipt", type=Path, required=True)
    parser.add_argument("--release-parity", type=Path, default=Path("configs/release-parity.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = build_receipt(
        args.ai_flight_evidence,
        args.shared_policy_receipt,
        args.release_parity,
        args.output,
    )
    print(json.dumps({"status": "passed", "receipt_sha256": receipt["receipt_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify a shared-policy SIM -> HIL/REAL handoff envelope.

The verifier joins already-produced, immutable sidecars.  It never starts a
backend, opens MAVLink, uploads a Mission, or grants actuator authority.  A
passing result means that the target runtime binding uses the same policy,
airframe and release-parity identities as the source SIM receipt.  It is not a
HIL, REAL, flight, outdoor-GNSS or release acceptance claim.
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
for _path in (_ROOT, _ROOT / "src", _ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads

SCHEMA = "multidetect.shared-policy-runtime-handoff"
SCHEMA_VERSION = 1
_PARITY_KEYS = (
    "core_algorithm_hash",
    "px4_mission_path_hash",
    "safety_policy_hash",
    "task_contract_hash",
    "ui_contract_hash",
)


class SharedPolicyRuntimeHandoffError(ValueError):
    """Raised when the handoff envelope is malformed or identity-inconsistent."""


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise SharedPolicyRuntimeHandoffError(f"{label} must be a SHA-256 string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise SharedPolicyRuntimeHandoffError(f"{label} must be a SHA-256 string") from exc
    return value.lower()


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"), name=label)
    except (OSError, TypeError, ValueError) as exc:
        raise SharedPolicyRuntimeHandoffError(f"{label} cannot be read as strict JSON") from exc
    if not isinstance(raw, dict):
        raise SharedPolicyRuntimeHandoffError(f"{label} must be a JSON object")
    return raw


def _parity(path: Path) -> dict[str, Any]:
    document = _object(path, "release parity")
    values = document.get("parity")
    if not isinstance(values, dict) or set(values) != set(_PARITY_KEYS):
        raise SharedPolicyRuntimeHandoffError("release parity mapping is incomplete")
    return {
        "release_label": document.get("release_label"),
        "hashes": {
            key: _sha256_text(values[key], f"release parity {key}")
            for key in _PARITY_KEYS
        },
    }


def _verify_receipt(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = _object(path, "shared-policy campaign receipt")
    if receipt.get("schema") != "multidetect.shared-policy-campaign-receipt":
        raise SharedPolicyRuntimeHandoffError(
            "source receipt schema is not shared-policy campaign receipt"
        )
    if receipt.get("schema_version") != 1:
        raise SharedPolicyRuntimeHandoffError("unsupported source receipt schema version")
    stored = receipt.get("receipt_sha256")
    if not isinstance(stored, str):
        raise SharedPolicyRuntimeHandoffError("source receipt_sha256 is missing")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if stored.lower() != canonical_sha256(unsigned):
        raise SharedPolicyRuntimeHandoffError("source receipt_sha256 mismatch")
    if (
        receipt.get("shared_policy_used") is not True
        or receipt.get("truth_free") is not True
        or receipt.get("planner_revalidated") is not True
        or receipt.get("policy_applied_to_actuator") is not False
        or receipt.get("final_release_allowed") is not False
    ):
        raise SharedPolicyRuntimeHandoffError(
            "source receipt is not a fail-closed shared-policy receipt"
        )
    runtime = receipt.get("runtime_binding")
    parity = receipt.get("release_parity")
    if not isinstance(runtime, dict) or runtime.get("verified") is not True:
        raise SharedPolicyRuntimeHandoffError("source runtime binding is not verified")
    if runtime.get("runtime_mode") != "SIM":
        raise SharedPolicyRuntimeHandoffError("source receipt must originate from SIM")
    if not isinstance(parity, dict) or parity.get("verified") is not True:
        raise SharedPolicyRuntimeHandoffError("source release parity is not verified")
    policy_hash = _sha256_text(receipt.get("policy_artifact_sha256"), "source policy artifact")
    ranker_hash = _sha256_text(receipt.get("ranker_sha256"), "source ranker")
    parity_hashes = parity.get("hashes")
    if not isinstance(parity_hashes, dict) or set(parity_hashes) != set(_PARITY_KEYS):
        raise SharedPolicyRuntimeHandoffError("source receipt parity identities are incomplete")
    identity = {
        "policy_artifact_sha256": policy_hash,
        "ranker_sha256": ranker_hash,
        "airframe_profile_sha256": _sha256_text(
            runtime.get("airframe_profile_sha256"), "source airframe profile"
        ),
        "release_parity": {
            "release_label": parity.get("release_label"),
            "hashes": {
                key: _sha256_text(parity_hashes[key], f"source parity {key}")
                for key in _PARITY_KEYS
            },
        },
    }
    return receipt, identity


def _verify_live_binding(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = _object(path, "live runtime binding")
    if binding.get("schema") != "multidetect.live-runtime-observation-mission-binding":
        raise SharedPolicyRuntimeHandoffError("target binding schema is not live runtime binding")
    if binding.get("schema_version") != 1 or binding.get("gate_passed") is not True:
        raise SharedPolicyRuntimeHandoffError("target live runtime binding gate is not passed")
    stored = binding.get("evidence_sha256")
    unsigned = {key: value for key, value in binding.items() if key != "evidence_sha256"}
    if not isinstance(stored, str) or stored.lower() != canonical_sha256(unsigned):
        raise SharedPolicyRuntimeHandoffError("target live runtime binding evidence hash mismatch")
    mode = binding.get("runtime_mode")
    if mode not in {"hil", "real"}:
        raise SharedPolicyRuntimeHandoffError("target runtime mode must be hil or real")
    claims = binding.get("claims")
    checks = binding.get("checks")
    boundary = binding.get("claim_boundary")
    if (
        not isinstance(claims, dict)
        or claims.get("release_eligible") is not False
        or claims.get("hardware_acceptance") is not False
        or claims.get("hil_acceptance") is not False
        or claims.get("real_acceptance") is not False
        or claims.get("flight_acceptance") is not False
        or claims.get("operator_visual_acceptance") is not False
        or not isinstance(boundary, dict)
        or boundary.get("release_eligible") is not False
        or not isinstance(checks, dict)
        or not checks
        or not all(value is True for value in checks.values())
    ):
        raise SharedPolicyRuntimeHandoffError(
            "target binding claim boundary is unsafe or incomplete"
        )
    mission = binding.get("mission")
    live = binding.get("live_observation")
    if not isinstance(mission, dict) or not isinstance(live, dict):
        raise SharedPolicyRuntimeHandoffError("target binding lacks Mission/live identity")
    return binding, {
        "runtime_mode": mode,
        "mission_hash": _sha256_text(mission.get("mission_hash"), "target mission"),
        "airframe_profile_sha256": _sha256_text(
            mission.get("airframe_profile_hash"), "target airframe profile"
        ),
        "runtime_descriptor_sha256": _sha256_text(
            live.get("runtime_descriptor_hash"), "target runtime descriptor"
        ),
        "runtime_input_sha256": _sha256_text(
            live.get("runtime_input_hash"), "target runtime input"
        ),
    }


def build_handoff(
    receipt_path: Path,
    live_binding_path: Path,
    policy_artifact_path: Path,
    release_parity_path: Path,
    *,
    policy_artifact_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic SIM -> HIL/REAL handoff audit document."""

    receipt_path = receipt_path.resolve(strict=True)
    live_binding_path = live_binding_path.resolve(strict=True)
    policy_artifact_path = policy_artifact_path.resolve(strict=True)
    release_parity_path = release_parity_path.resolve(strict=True)
    receipt, source = _verify_receipt(receipt_path)
    binding, target = _verify_live_binding(live_binding_path)
    policy_file_hash = _sha256_path(policy_artifact_path)
    if policy_artifact_file_sha256 is not None and policy_file_hash != _sha256_text(
        policy_artifact_file_sha256, "policy artifact file"
    ):
        raise SharedPolicyRuntimeHandoffError("policy artifact file SHA-256 mismatch")
    try:
        artifact = PolicyArtifact.from_mapping(_object(policy_artifact_path, "policy artifact"))
    except (TypeError, ValueError, KeyError) as exc:
        raise SharedPolicyRuntimeHandoffError("policy artifact is invalid") from exc
    parity = _parity(release_parity_path)
    checks = {
        "source_receipt_document_valid": True,
        "source_receipt_hash_valid": True,
        "source_shared_policy_fail_closed": True,
        "source_runtime_binding_verified": True,
        "source_release_parity_verified": True,
        "target_binding_document_valid": True,
        "target_binding_hash_valid": True,
        "target_binding_gate_passed": True,
        "target_claim_boundary_safe": True,
        "target_mode_allowed": target["runtime_mode"] in {"hil", "real"},
        "policy_artifact_file_valid": True,
        "policy_artifact_identity_matches": (
            artifact.canonical_hash == source["policy_artifact_sha256"]
            and artifact.airframe_profile_hash == source["airframe_profile_sha256"]
            and artifact.ranker_sha256 == source["ranker_sha256"]
            and artifact.airframe_profile_hash == target["airframe_profile_sha256"]
        ),
        "release_parity_file_valid": True,
        "release_parity_identity_matches": parity == source["release_parity"],
        "airframe_identity_matches": (
            source["airframe_profile_sha256"] == target["airframe_profile_sha256"]
        ),
        "outputs_locked": all(
            value is False
            for value in (
                receipt.get("policy_applied_to_actuator"),
                binding.get("claims", {}).get("flight_acceptance"),
                binding.get("claims", {}).get("hardware_acceptance"),
            )
        ),
        "mode_transition_valid": target["runtime_mode"] in {"hil", "real"},
    }
    checks["no_errors"] = all(checks.values())
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": (
            "shared_policy_runtime_handoff_verified"
            if checks["no_errors"]
            else "shared_policy_runtime_handoff_error"
        ),
        "source": {
            "path": str(receipt_path),
            "file_sha256": _sha256_path(receipt_path),
            "receipt_sha256": receipt["receipt_sha256"],
            "runtime_mode": "SIM",
            **source,
        },
        "target": {
            "path": str(live_binding_path),
            "file_sha256": _sha256_path(live_binding_path),
            "evidence_sha256": binding["evidence_sha256"],
            **target,
        },
        "policy_artifact": {
            "path": str(policy_artifact_path),
            "file_sha256": policy_file_hash,
            "canonical_sha256": artifact.canonical_hash,
        },
        "release_parity": {
            "path": str(release_parity_path),
            "file_sha256": _sha256_path(release_parity_path),
            **parity,
        },
        "checks": checks,
        "gate_passed": checks["no_errors"],
        "claims": {
            "identity_handoff_only": True,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
        },
        "reasons": (
            []
            if checks["no_errors"]
            else [key for key, value in checks.items() if value is not True]
        ),
    }
    document["handoff_sha256"] = canonical_sha256(document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--live-binding", type=Path, required=True)
    parser.add_argument("--policy-artifact", type=Path, required=True)
    parser.add_argument("--policy-artifact-file-sha256")
    parser.add_argument("--release-parity", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    document = build_handoff(
        args.receipt,
        args.live_binding,
        args.policy_artifact,
        args.release_parity,
        policy_artifact_file_sha256=args.policy_artifact_file_sha256,
    )
    if args.out.exists():
        raise SystemExit("handoff output must be new")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "passed" if document["gate_passed"] else "failed",
                "handoff_sha256": document["handoff_sha256"],
            }
        )
    )
    return 0 if document["gate_passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

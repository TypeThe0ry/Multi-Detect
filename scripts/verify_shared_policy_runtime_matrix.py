"""Verify one shared task-policy artifact against every RuntimeMode profile.

This is a software contract check.  It constructs only immutable runtime
descriptors and never starts a backend, sends MAVLink, or grants actuator
authority.
"""

from __future__ import annotations

# ruff: noqa: E402 -- repository-local CLI bootstrap before package imports
import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Keep the repository CLI self-contained when invoked as
# ``python scripts/verify_shared_policy_runtime_matrix.py``.  The test suite
# imports the module with the repository root already on ``sys.path``; direct
# operator use must not depend on an editable install or caller-managed
# ``PYTHONPATH``.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.runtime import (
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeMode,
    RuntimeProfile,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_PARITY_KEYS = (
    "core_algorithm_hash",
    "px4_mission_path_hash",
    "safety_policy_hash",
    "task_contract_hash",
    "ui_contract_hash",
)


def _sha256_text(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 string") from exc
    return value.lower()


def _normalize_release_parity(
    value: Mapping[str, object] | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    if set(value) != set(_PARITY_KEYS):
        raise ValueError("release parity must contain exactly the five contract hashes")
    return {
        key: _sha256_text(value[key], f"release parity {key}")
        for key in _PARITY_KEYS
    }


def build_runtime_matrix(
    *,
    artifact: PolicyArtifact,
    airframe_profile_hash: str,
    release_parity: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Build and verify the four-mode policy/profile contract matrix."""

    hashes = {
        "airframe": airframe_profile_hash,
        "vehicle": "1" * 64,
        "firmware": "2" * 64,
        "endpoint": "3" * 64,
        "recording": "4" * 64,
        "scenario": "5" * 64,
    }
    profiles = {
        RuntimeMode.REPLAY: RuntimeProfile.replay(),
        RuntimeMode.SIM: RuntimeProfile.sim(),
        RuntimeMode.HIL: RuntimeProfile.hil(
            vehicle_identity_hash=hashes["vehicle"],
            firmware_identity_hash=hashes["firmware"],
            airframe_profile_hash=hashes["airframe"],
            endpoint_binding_hash=hashes["endpoint"],
        ),
        # Policy selection is observation-only in REAL.  The flight profile
        # remains a separate, authorization-controlled boundary.
        RuntimeMode.REAL: RuntimeProfile.observer(),
    }
    descriptors = {
        RuntimeMode.REPLAY: RuntimeDescriptor(
            mode=RuntimeMode.REPLAY,
            profile=profiles[RuntimeMode.REPLAY],
            backend_id="policy-matrix-replay",
            airframe_profile_hash=hashes["airframe"],
            recording_hash=hashes["recording"],
            deterministic=True,
            fault_injection_enabled=False,
        ),
        RuntimeMode.SIM: RuntimeDescriptor(
            mode=RuntimeMode.SIM,
            profile=profiles[RuntimeMode.SIM],
            backend_id="policy-matrix-sim",
            airframe_profile_hash=hashes["airframe"],
            scenario_id="policy-matrix",
            scenario_manifest_hash=hashes["scenario"],
            scenario_seed=7,
            deterministic=True,
            fault_injection_enabled=False,
        ),
        RuntimeMode.HIL: RuntimeDescriptor(
            mode=RuntimeMode.HIL,
            profile=profiles[RuntimeMode.HIL],
            backend_id="policy-matrix-hil",
            airframe_profile_hash=hashes["airframe"],
            scenario_id="policy-matrix",
            scenario_manifest_hash=hashes["scenario"],
            scenario_seed=7,
            deterministic=False,
            fault_injection_enabled=False,
        ),
        RuntimeMode.REAL: RuntimeDescriptor(
            mode=RuntimeMode.REAL,
            profile=profiles[RuntimeMode.REAL],
            backend_id="policy-matrix-real-observer",
            airframe_profile_hash=hashes["airframe"],
            deterministic=False,
            fault_injection_enabled=False,
        ),
    }
    normalized_parity = _normalize_release_parity(release_parity)
    rows: dict[str, Any] = {}
    for mode in RuntimeMode:
        descriptor = descriptors[mode]
        if artifact.airframe_profile_hash != descriptor.airframe_profile_hash:
            raise ValueError(f"policy artifact airframe mismatch in {mode.value}")
        if RuntimeCapability.FLIGHT_CONTROL_OUTPUT in descriptor.profile.capabilities:
            raise ValueError(f"policy profile has actuator capability in {mode.value}")
        rows[mode.value] = {
            "profile_id": descriptor.profile.profile_id.value,
            "profile_hash": descriptor.profile.canonical_hash,
            "descriptor_hash": descriptor.canonical_hash,
            "capabilities": [item.value for item in descriptor.profile.capabilities],
            "endpoint_class": descriptor.profile.endpoint_class.value,
            "policy_artifact_sha256": artifact.canonical_hash,
            "airframe_profile_hash": descriptor.airframe_profile_hash,
            "release_parity": normalized_parity,
            "flight_control_output": False,
        }
    return {
        "schema_version": 1,
        "status": "passed_shared_policy_runtime_matrix",
        "contract": "multidetect.shared-policy-runtime-mode-matrix",
        "policy_artifact_sha256": artifact.canonical_hash,
        "airframe_profile_hash": airframe_profile_hash,
        "release_parity": normalized_parity,
        "modes": rows,
        "same_policy_artifact": len({row["policy_artifact_sha256"] for row in rows.values()}) == 1,
        "same_airframe_profile": len({row["airframe_profile_hash"] for row in rows.values()}) == 1,
        "same_release_parity": normalized_parity is not None
        and len(
            {
                tuple(sorted(row["release_parity"].items()))
                for row in rows.values()
            }
        )
        == 1,
        "same_ui_contract": normalized_parity is not None,
        "ui_visual_acceptance": "not_claimed",
        "actuator_capability_granted": False,
        "backend_started": False,
        "mavlink_sent": False,
        "hardware_acceptance": "not_claimed",
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-file-sha256", required=True)
    parser.add_argument("--airframe-profile-sha256", required=True)
    parser.add_argument(
        "--release-parity",
        type=Path,
        default=_ROOT / "configs" / "release-parity.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    artifact_path = args.artifact.resolve()
    if _sha256(artifact_path) != args.artifact_file_sha256:
        raise SystemExit("policy artifact file SHA-256 mismatch")
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact = PolicyArtifact.from_mapping(document)
    if artifact.airframe_profile_hash != args.airframe_profile_sha256:
        raise SystemExit("policy artifact airframe_profile_hash mismatch")
    parity_document = json.loads(args.release_parity.resolve().read_text(encoding="utf-8"))
    if not isinstance(parity_document, dict) or not isinstance(
        parity_document.get("parity"), dict
    ):
        raise SystemExit("release parity document is missing parity mapping")
    result = build_runtime_matrix(
        artifact=artifact,
        airframe_profile_hash=args.airframe_profile_sha256,
        release_parity=parity_document["parity"],
    )
    output = args.out.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

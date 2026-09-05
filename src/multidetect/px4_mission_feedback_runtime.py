"""Bind PX4 Mission feedback to one signed HIL/REAL runtime identity.

The Mission feedback observer is deliberately receive-only, but a pair of
``MISSION_CURRENT``/``MISSION_ITEM_REACHED`` messages is not enough to prove
which vehicle and endpoint produced the evidence.  This module adds the
missing binding without granting any output authority:

* the signed RuntimeProfile document is hash-bound;
* the observed heartbeat and sanitized AUTOPILOT_VERSION are reduced to the
  same stable vehicle/firmware identities used by the live runtime launcher;
* the resolved endpoint and baud rate are independently re-hashed;
* the Mission airframe profile is compared with the signed profile; and
* flight-control output must remain locked with zero opt-in.

HIL observations require an exact ``hil`` profile.  REAL observations require
an exact ``flight`` profile, but load it with output disabled.  An ``observer``
profile is useful for diagnostics, yet intentionally lacks the vehicle,
airframe, endpoint and signing bindings needed for release evidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .autonomy.contracts import ExecutableMission
from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_bool,
    require_int,
    require_schema_version,
    require_sha256,
    require_string_array,
    require_text,
    strict_json_loads,
    strict_object,
)
from .pixhawk import PixhawkHeartbeatIdentity, PixhawkReadOnlyConfig
from .px4_mission_feedback import Px4MissionFeedbackSnapshot
from .runtime import RuntimeMode, RuntimeProfile, RuntimeProfileId
from .runtime_launcher import (
    build_pixhawk_runtime_identity,
    derive_endpoint_binding_hash,
    derive_vehicle_identity_hash,
)
from .runtime_profiles import (
    RUNTIME_PROFILE_DOCUMENT_SCHEMA_VERSION,
    RuntimeIdentity,
    load_runtime_profile_document,
)

PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION = 1
PX4_MISSION_FEEDBACK_RUNTIME_MODES = (RuntimeMode.HIL, RuntimeMode.REAL)


class Px4MissionFeedbackRuntimeBindingError(AutonomyContractError):
    """Raised when a feedback/runtime identity binding is malformed."""


def _runtime_mode(value: RuntimeMode | str) -> RuntimeMode:
    if isinstance(value, RuntimeMode):
        mode = value
    else:
        try:
            mode = RuntimeMode(require_text(value, "runtime_mode"))
        except ValueError as exc:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_mode must be 'hil' or 'real'"
            ) from exc
    if mode not in PX4_MISSION_FEEDBACK_RUNTIME_MODES:
        raise Px4MissionFeedbackRuntimeBindingError(
            "runtime_mode must be 'hil' or 'real'"
        )
    return mode


def _optional_uint8(value: object, name: str) -> int | None:
    if value is None:
        return None
    return require_int(value, name, minimum=0, maximum=255)


def _heartbeat_from_mapping(raw: object) -> PixhawkHeartbeatIdentity:
    value = strict_object(
        raw,
        name="runtime_binding.heartbeat_identity",
        required=frozenset(
            {
                "source_system_id",
                "source_component_id",
                "autopilot_id",
                "vehicle_type_id",
                "mavlink_version",
            }
        ),
    )
    source_system_id = _optional_uint8(
        value["source_system_id"],
        "runtime_binding.heartbeat_identity.source_system_id",
    )
    if source_system_id == 0:
        raise Px4MissionFeedbackRuntimeBindingError(
            "runtime_binding heartbeat source_system_id must be in [1, 255]"
        )
    source_component_id = _optional_uint8(
        value["source_component_id"],
        "runtime_binding.heartbeat_identity.source_component_id",
    )
    if source_component_id == 0:
        raise Px4MissionFeedbackRuntimeBindingError(
            "runtime_binding heartbeat source_component_id must be in [1, 255]"
        )
    return PixhawkHeartbeatIdentity(
        source_system_id=source_system_id,
        source_component_id=source_component_id,
        autopilot_id=_optional_uint8(
            value["autopilot_id"],
            "runtime_binding.heartbeat_identity.autopilot_id",
        ),
        vehicle_type_id=_optional_uint8(
            value["vehicle_type_id"],
            "runtime_binding.heartbeat_identity.vehicle_type_id",
        ),
        mavlink_version=_optional_uint8(
            value["mavlink_version"],
            "runtime_binding.heartbeat_identity.mavlink_version",
        ),
    )


def _heartbeat_mapping(identity: PixhawkHeartbeatIdentity) -> dict[str, int | None]:
    if not isinstance(identity, PixhawkHeartbeatIdentity):
        raise Px4MissionFeedbackRuntimeBindingError(
            "provider.heartbeat_identity must be a PixhawkHeartbeatIdentity"
        )
    return {
        "source_system_id": identity.source_system_id,
        "source_component_id": identity.source_component_id,
        "autopilot_id": identity.autopilot_id,
        "vehicle_type_id": identity.vehicle_type_id,
        "mavlink_version": identity.mavlink_version,
    }


def _heartbeat_complete(identity: PixhawkHeartbeatIdentity) -> bool:
    return all(
        value is not None
        for value in (
            identity.source_system_id,
            identity.source_component_id,
            identity.autopilot_id,
            identity.vehicle_type_id,
            identity.mavlink_version,
        )
    )


def _profile_document(
    path: str | Path,
) -> tuple[Path, bytes, Mapping[str, Any], RuntimeProfile, str]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise Px4MissionFeedbackRuntimeBindingError(
            f"cannot read runtime profile: {resolved}"
        ) from exc
    raw = strict_json_loads(payload, name="runtime profile document")
    document = strict_object(
        raw,
        name="RuntimeProfileDocument",
        required=frozenset(
            {
                "schema_version",
                "profile",
                "key_id",
                "signature_hmac_sha256",
            }
        ),
    )
    require_schema_version(
        document["schema_version"],
        expected=RUNTIME_PROFILE_DOCUMENT_SCHEMA_VERSION,
    )
    profile = RuntimeProfile.from_mapping(document["profile"])
    key_id = require_text(document["key_id"], "runtime profile key_id")
    require_sha256(
        document["signature_hmac_sha256"],
        "runtime profile signature_hmac_sha256",
    )
    return resolved, payload, document, profile, key_id


def _profile_identity_matches(
    profile: RuntimeProfile,
    identity: RuntimeIdentity,
) -> bool:
    required = (
        (profile.vehicle_identity_hash, identity.vehicle_identity_hash),
        (profile.airframe_profile_hash, identity.airframe_profile_hash),
        (profile.endpoint_binding_hash, identity.endpoint_binding_hash),
    )
    if profile.firmware_identity_hash is not None:
        required = (
            *required,
            (profile.firmware_identity_hash, identity.firmware_identity_hash),
        )
    if profile.profile_id is RuntimeProfileId.FLIGHT:
        required = (
            *required,
            (profile.signing_identity_hash, identity.signing_identity_hash),
        )
    return all(expected is not None and expected == observed for expected, observed in required)


def _expected_profile_id(mode: RuntimeMode) -> RuntimeProfileId:
    return RuntimeProfileId.HIL if mode is RuntimeMode.HIL else RuntimeProfileId.FLIGHT


def _expected_loader_reason(
    *,
    mode: RuntimeMode,
    profile_valid: bool,
) -> str:
    if mode is RuntimeMode.HIL:
        return "HIL profile is advisory-only; flight-control output remains locked"
    if not profile_valid:
        return "REAL profile signature or device binding is invalid"
    return "REAL profile is valid but output opt-in is not enabled"


def _binding_reasons(
    *,
    mode: RuntimeMode,
    mission_airframe_profile_hash: str,
    feedback: Px4MissionFeedbackSnapshot,
    profile: RuntimeProfile,
    identity: RuntimeIdentity,
    heartbeat: PixhawkHeartbeatIdentity,
    signature_valid: bool,
    identity_valid: bool,
    profile_valid: bool,
    flight_control_enabled: bool,
    output_locked: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if profile.profile_id is not _expected_profile_id(mode):
        reasons.append(
            f"{mode.value}_runtime_profile_must_be_{_expected_profile_id(mode).value}"
        )
    if mode is RuntimeMode.HIL and identity.signing_identity_hash is not None:
        reasons.append("hil_runtime_must_not_bind_signing_identity")
    if not _heartbeat_complete(heartbeat):
        reasons.append("heartbeat_identity_incomplete")
    if heartbeat.autopilot_id != 12:
        reasons.append("heartbeat_autopilot_is_not_px4")
    if (
        heartbeat.source_system_id != feedback.source_system_id
        or heartbeat.source_component_id != feedback.source_component_id
    ):
        reasons.append("heartbeat_source_does_not_match_mission_feedback")
    if identity.airframe_profile_hash != mission_airframe_profile_hash:
        reasons.append("runtime_airframe_profile_does_not_match_mission")
    if profile.firmware_identity_hash is None:
        reasons.append("runtime_profile_firmware_identity_missing")
    if identity.firmware_identity_hash is None:
        reasons.append("observed_firmware_identity_missing")
    elif (
        profile.firmware_identity_hash is not None
        and profile.firmware_identity_hash != identity.firmware_identity_hash
    ):
        reasons.append("runtime_profile_firmware_identity_mismatch")
    if not signature_valid:
        reasons.append("runtime_profile_signature_invalid")
    if not identity_valid:
        reasons.append("runtime_profile_identity_invalid")
    if not profile_valid:
        reasons.append("runtime_profile_invalid")
    if flight_control_enabled:
        reasons.append("flight_control_output_must_remain_disabled")
    if not output_locked:
        reasons.append("flight_control_output_must_remain_locked")
    return tuple(reasons)


def build_px4_mission_feedback_runtime_binding(
    mission: ExecutableMission,
    feedback: Px4MissionFeedbackSnapshot,
    provider: object,
    *,
    runtime_mode: RuntimeMode | str,
    runtime_profile_path: str | Path,
    hmac_key: bytes,
    expected_key_id: str,
    signing_identity_hash: str | None = None,
) -> dict[str, object]:
    """Build a signed, receive-only runtime binding for one observation."""

    if not isinstance(mission, ExecutableMission):
        raise Px4MissionFeedbackRuntimeBindingError(
            "mission must be an ExecutableMission"
        )
    if not isinstance(feedback, Px4MissionFeedbackSnapshot):
        raise Px4MissionFeedbackRuntimeBindingError(
            "feedback must be a Px4MissionFeedbackSnapshot"
        )
    mode = _runtime_mode(runtime_mode)
    profile_path, payload, _, source_profile, key_id = _profile_document(
        runtime_profile_path
    )
    clean_expected_key_id = require_text(expected_key_id, "expected_key_id")
    if key_id != clean_expected_key_id:
        raise Px4MissionFeedbackRuntimeBindingError(
            "runtime profile key_id does not match the expected key"
        )
    try:
        heartbeat = provider.heartbeat_identity
    except AttributeError as exc:
        raise Px4MissionFeedbackRuntimeBindingError(
            "provider must expose heartbeat_identity"
        ) from exc
    identity = build_pixhawk_runtime_identity(
        provider,
        airframe_profile_hash=mission.airframe_profile_hash,
        signing_identity_hash=signing_identity_hash,
    )
    result = load_runtime_profile_document(
        profile_path,
        expected_mode=mode,
        observed_identity=identity,
        hmac_key=hmac_key,
        expected_key_id=clean_expected_key_id,
        require_signature=True,
        allow_flight_control_output=False,
    )
    if source_profile != result.profile:
        raise Px4MissionFeedbackRuntimeBindingError(
            "runtime profile parser results are inconsistent"
        )
    expected_profile_id = _expected_profile_id(mode)
    if result.profile.profile_id is not expected_profile_id:
        raise Px4MissionFeedbackRuntimeBindingError(
            f"{mode.value.upper()} Mission feedback release evidence requires "
            f"an exact {expected_profile_id.value} runtime profile"
        )
    reasons = _binding_reasons(
        mode=mode,
        mission_airframe_profile_hash=mission.airframe_profile_hash,
        feedback=feedback,
        profile=result.profile,
        identity=identity,
        heartbeat=heartbeat,
        signature_valid=result.signature_valid,
        identity_valid=result.identity_valid,
        profile_valid=result.profile_valid,
        flight_control_enabled=result.flight_control_enabled,
        output_locked=result.output_locked,
    )
    binding: dict[str, object] = {
        "schema_version": PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION,
        "runtime_mode": mode.value,
        "profile_document_sha256": hashlib.sha256(payload).hexdigest(),
        "profile": result.profile.to_mapping(),
        "profile_sha256": result.profile.canonical_hash,
        "key_id": key_id,
        "heartbeat_identity": _heartbeat_mapping(heartbeat),
        "observed_identity": identity.to_mapping(),
        "signature_valid": result.signature_valid,
        "identity_valid": result.identity_valid,
        "profile_valid": result.profile_valid,
        "flight_control_enabled": result.flight_control_enabled,
        "output_locked": result.output_locked,
        "reason": result.reason,
        "reasons": reasons,
        "gate_passed": not reasons,
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    return verify_px4_mission_feedback_runtime_binding(
        binding,
        mission_airframe_profile_hash=mission.airframe_profile_hash,
        feedback=feedback,
        transport={
            "configured_endpoint": provider.config.endpoint,
            "resolved_endpoint": provider.resolved_endpoint,
            "baud": provider.config.baud,
        },
        runtime_mode=mode,
        runtime_profile_path=profile_path,
    )


def verify_px4_mission_feedback_runtime_binding(
    raw: object,
    *,
    mission_airframe_profile_hash: str,
    feedback: Px4MissionFeedbackSnapshot,
    transport: Mapping[str, object],
    runtime_mode: RuntimeMode | str,
    runtime_profile_path: str | Path | None = None,
) -> dict[str, object]:
    """Recompute one runtime binding, optionally reopening its signed profile."""

    try:
        airframe_profile_hash = require_sha256(
            mission_airframe_profile_hash,
            "mission_airframe_profile_hash",
        )
        if not isinstance(feedback, Px4MissionFeedbackSnapshot):
            raise Px4MissionFeedbackRuntimeBindingError(
                "feedback must be a Px4MissionFeedbackSnapshot"
            )
        mode = _runtime_mode(runtime_mode)
        value = strict_object(
            raw,
            name="Px4MissionFeedbackRuntimeBinding",
            required=frozenset(
                {
                    "schema_version",
                    "runtime_mode",
                    "profile_document_sha256",
                    "profile",
                    "profile_sha256",
                    "key_id",
                    "heartbeat_identity",
                    "observed_identity",
                    "signature_valid",
                    "identity_valid",
                    "profile_valid",
                    "flight_control_enabled",
                    "output_locked",
                    "reason",
                    "reasons",
                    "gate_passed",
                    "binding_sha256",
                }
            ),
        )
        require_schema_version(
            value["schema_version"],
            expected=PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION,
        )
        if _runtime_mode(value["runtime_mode"]) is not mode:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding runtime_mode does not match the observation"
            )
        document_sha256 = require_sha256(
            value["profile_document_sha256"],
            "runtime_binding.profile_document_sha256",
        )
        profile = RuntimeProfile.from_mapping(value["profile"])
        if require_sha256(
            value["profile_sha256"],
            "runtime_binding.profile_sha256",
        ) != profile.canonical_hash:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding profile_sha256 does not match profile"
            )
        key_id = require_text(value["key_id"], "runtime_binding.key_id")
        heartbeat = _heartbeat_from_mapping(value["heartbeat_identity"])
        identity = RuntimeIdentity.from_mapping(value["observed_identity"])

        configured_endpoint = require_text(
            transport.get("configured_endpoint"),
            "transport.configured_endpoint",
        )
        resolved_endpoint_value = transport.get("resolved_endpoint")
        resolved_endpoint = (
            None
            if resolved_endpoint_value is None
            else require_text(resolved_endpoint_value, "transport.resolved_endpoint")
        )
        baud = require_int(transport.get("baud"), "transport.baud", minimum=1)
        endpoint_hash = derive_endpoint_binding_hash(
            PixhawkReadOnlyConfig(endpoint=configured_endpoint, baud=baud),
            resolved_endpoint=resolved_endpoint,
        )
        if identity.endpoint_binding_hash != endpoint_hash:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding endpoint identity does not match transport"
            )
        if identity.airframe_profile_hash != airframe_profile_hash:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding airframe identity does not match Mission"
            )
        derived_vehicle_identity = (
            derive_vehicle_identity_hash(heartbeat)
            if _heartbeat_complete(heartbeat)
            else None
        )
        if identity.vehicle_identity_hash != derived_vehicle_identity:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding vehicle identity does not match heartbeat"
            )

        signature_valid = require_bool(
            value["signature_valid"],
            "runtime_binding.signature_valid",
        )
        identity_valid = require_bool(
            value["identity_valid"],
            "runtime_binding.identity_valid",
        )
        independently_valid_identity = _profile_identity_matches(profile, identity)
        if identity_valid is not independently_valid_identity:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding identity_valid does not match the profile binding"
            )
        profile_valid = require_bool(
            value["profile_valid"],
            "runtime_binding.profile_valid",
        )
        if profile_valid is not (signature_valid and identity_valid):
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding profile_valid does not match signature and identity"
            )
        flight_control_enabled = require_bool(
            value["flight_control_enabled"],
            "runtime_binding.flight_control_enabled",
        )
        output_locked = require_bool(
            value["output_locked"],
            "runtime_binding.output_locked",
        )
        if output_locked is flight_control_enabled:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding output lock contradicts flight-control state"
            )
        reason = require_text(value["reason"], "runtime_binding.reason")
        expected_reason = _expected_loader_reason(
            mode=mode,
            profile_valid=profile_valid,
        )
        if reason != expected_reason:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding reason does not match the locked profile decision"
            )

        reasons = _binding_reasons(
            mode=mode,
            mission_airframe_profile_hash=airframe_profile_hash,
            feedback=feedback,
            profile=profile,
            identity=identity,
            heartbeat=heartbeat,
            signature_valid=signature_valid,
            identity_valid=identity_valid,
            profile_valid=profile_valid,
            flight_control_enabled=flight_control_enabled,
            output_locked=output_locked,
        )
        if require_string_array(value["reasons"], "runtime_binding.reasons") != reasons:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding reasons do not match the recomputed gate"
            )
        gate_passed = require_bool(value["gate_passed"], "runtime_binding.gate_passed")
        if gate_passed is not (not reasons):
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding gate_passed does not match the recomputed gate"
            )

        if runtime_profile_path is not None:
            _, payload, document, source_profile, source_key_id = _profile_document(
                runtime_profile_path
            )
            if hashlib.sha256(payload).hexdigest() != document_sha256:
                raise Px4MissionFeedbackRuntimeBindingError(
                    "runtime profile file SHA-256 does not match the observation"
                )
            if source_profile != profile:
                raise Px4MissionFeedbackRuntimeBindingError(
                    "runtime profile file does not match the observation profile"
                )
            if source_key_id != key_id:
                raise Px4MissionFeedbackRuntimeBindingError(
                    "runtime profile file key_id does not match the observation"
                )
            require_sha256(
                document["signature_hmac_sha256"],
                "runtime profile signature_hmac_sha256",
            )

        claimed_hash = require_sha256(
            value["binding_sha256"],
            "runtime_binding.binding_sha256",
        )
        without_hash = dict(value)
        del without_hash["binding_sha256"]
        if canonical_sha256(without_hash) != claimed_hash:
            raise Px4MissionFeedbackRuntimeBindingError(
                "runtime_binding binding_sha256 does not match the binding"
            )
    except Px4MissionFeedbackRuntimeBindingError:
        raise
    except (AutonomyContractError, OSError, TypeError, ValueError) as exc:
        raise Px4MissionFeedbackRuntimeBindingError(str(exc)) from exc
    return dict(value)


__all__ = [
    "PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION",
    "PX4_MISSION_FEEDBACK_RUNTIME_MODES",
    "Px4MissionFeedbackRuntimeBindingError",
    "build_px4_mission_feedback_runtime_binding",
    "verify_px4_mission_feedback_runtime_binding",
]

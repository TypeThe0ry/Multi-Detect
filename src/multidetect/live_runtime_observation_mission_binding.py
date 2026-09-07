"""Bind a live RuntimeObservation result to PX4 Mission feedback evidence.

The live camera producer and the PX4 Mission feedback observer deliberately
write separate sidecars.  Keeping those producers independent is useful for
recovery, but a release reviewer still needs one deterministic join point:

* the bounded receive-only ``RuntimeObservationResult`` must be reopened;
* the executable Mission and v2 PX4 feedback observation must agree;
* the signed HIL/REAL RuntimeProfile must be verified with its HMAC key;
* an explicit endpoint/vehicle identity sidecar must match the heartbeat,
  resolved endpoint, baud, and the profile-bound identities; and
* all four evidence producers must remain output locked.

This module implements that join without starting a camera, opening a MAVLink
link, or enabling flight-control output.  It is a software provenance gate,
not a HIL, REAL, flight, or operator-visual acceptance claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    require_text,
    strict_json_loads,
    strict_object,
)
from .pixhawk import PixhawkHeartbeatIdentity, PixhawkReadOnlyConfig
from .px4_mission_feedback_cli import load_px4_mission_feedback_observation
from .runtime import RuntimeMode, RuntimeProfile, RuntimeProfileId
from .runtime_launcher import derive_endpoint_binding_hash, derive_vehicle_identity_hash
from .runtime_observation import load_runtime_observation_result
from .runtime_profiles import RuntimeIdentity, load_runtime_profile_document

SCHEMA = "multidetect.live-runtime-observation-mission-binding"
SCHEMA_VERSION = 1
EVENT = "live_runtime_observation_mission_binding_verified"
ERROR_EVENT = "live_runtime_observation_mission_binding_error"
IDENTITY_SCHEMA = "multidetect.runtime-endpoint-vehicle-identity"
IDENTITY_SCHEMA_VERSION = 1
IDENTITY_EVENT = "runtime_endpoint_vehicle_identity_observed"
_SUPPORTED_MODES = (RuntimeMode.HIL, RuntimeMode.REAL)
_MAX_OBSERVATION_BYTES = 256 * 1024 * 1024
_MAX_FEEDBACK_BYTES = 16 * 1024 * 1024
_MAX_PROFILE_BYTES = 2 * 1024 * 1024
_MAX_IDENTITY_BYTES = 2 * 1024 * 1024
_MAX_MISSION_BYTES = 16 * 1024 * 1024

_CHECK_NAMES = (
    "sources_distinct",
    "live_result_document_valid",
    "live_result_gate_passed",
    "live_result_terminated",
    "mission_document_valid",
    "mission_identity_matches_feedback",
    "mission_feedback_document_valid",
    "mission_feedback_gate_passed",
    "runtime_profile_document_valid",
    "runtime_profile_signature_valid",
    "runtime_profile_identity_valid",
    "runtime_profile_kind_matches",
    "runtime_binding_document_valid",
    "runtime_binding_gate_passed",
    "endpoint_identity_document_valid",
    "endpoint_identity_hashes_valid",
    "mode_matches",
    "profile_document_hash_matches",
    "runtime_descriptor_hash_matches",
    "runtime_input_hash_matches",
    "heartbeat_identity_matches",
    "endpoint_transport_matches",
    "airframe_identity_matches",
    "all_outputs_locked",
    "no_errors",
)

_CLAIM_BOUNDARY = {
    "software_sidecar_join": True,
    "release_eligible": False,
    "hardware_acceptance": "not_claimed",
    "hil_acceptance": "not_claimed",
    "real_acceptance": "not_claimed",
    "flight_acceptance": "not_claimed",
    "operator_visual_acceptance": "not_claimed",
}


class LiveRuntimeObservationMissionBindingError(ValueError):
    """Raised when one of the join inputs is malformed or inconsistent."""


def _mode(value: object, *, label: str = "runtime_mode") -> RuntimeMode:
    try:
        mode = value if isinstance(value, RuntimeMode) else RuntimeMode(require_text(value, label))
    except (AutonomyContractError, ValueError) as exc:
        raise LiveRuntimeObservationMissionBindingError(f"{label} must be 'hil' or 'real'") from exc
    if mode not in _SUPPORTED_MODES:
        raise LiveRuntimeObservationMissionBindingError(f"{label} must be 'hil' or 'real'")
    return mode


def _timestamp(value: object, *, label: str) -> str:
    text = require_text(value, label)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LiveRuntimeObservationMissionBindingError(
            f"{label} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveRuntimeObservationMissionBindingError(f"{label} must include a timezone")
    return text


def _resolve_source(
    value: str | os.PathLike[str],
    *,
    label: str,
    base_directory: Path,
    maximum_bytes: int,
) -> tuple[Path, str, bytes]:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise LiveRuntimeObservationMissionBindingError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    if candidate.is_symlink():
        raise LiveRuntimeObservationMissionBindingError(f"{label} must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base_directory)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveRuntimeObservationMissionBindingError(
            f"{label} must resolve inside base directory {base_directory}"
        ) from exc
    if not resolved.is_file():
        raise LiveRuntimeObservationMissionBindingError(f"{label} must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise LiveRuntimeObservationMissionBindingError(f"{label} cannot be read") from exc
    if not payload:
        raise LiveRuntimeObservationMissionBindingError(f"{label} is empty")
    if len(payload) > maximum_bytes:
        raise LiveRuntimeObservationMissionBindingError(f"{label} exceeds {maximum_bytes} bytes")
    return resolved, resolved.relative_to(base_directory).as_posix(), payload


def _source_info(path: Path, relative: str, payload: bytes) -> dict[str, object]:
    return {
        "path": relative,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _json_source(
    value: str | os.PathLike[str],
    *,
    label: str,
    base_directory: Path,
    maximum_bytes: int,
) -> tuple[Path, dict[str, object], Mapping[str, Any]]:
    path, relative, payload = _resolve_source(
        value,
        label=label,
        base_directory=base_directory,
        maximum_bytes=maximum_bytes,
    )
    try:
        raw = strict_json_loads(payload, name=label)
        if not isinstance(raw, Mapping):
            raise LiveRuntimeObservationMissionBindingError(f"{label} must be a JSON object")
        document = dict(raw)
    except (AutonomyContractError, TypeError) as exc:
        raise LiveRuntimeObservationMissionBindingError(str(exc)) from exc
    return path, _source_info(path, relative, payload), document


def _heartbeat_from_mapping(raw: object) -> PixhawkHeartbeatIdentity:
    value = strict_object(
        raw,
        name="heartbeat_identity",
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
    source_system_id = require_int(
        value["source_system_id"],
        "heartbeat source_system_id",
        minimum=1,
        maximum=255,
    )
    source_component_id = require_int(
        value["source_component_id"], "heartbeat source_component_id", minimum=1, maximum=255
    )
    return PixhawkHeartbeatIdentity(
        source_system_id=source_system_id,
        source_component_id=source_component_id,
        autopilot_id=require_int(
            value["autopilot_id"],
            "heartbeat autopilot_id",
            minimum=0,
            maximum=255,
        ),
        vehicle_type_id=require_int(
            value["vehicle_type_id"], "heartbeat vehicle_type_id", minimum=0, maximum=255
        ),
        mavlink_version=require_int(
            value["mavlink_version"], "heartbeat mavlink_version", minimum=0, maximum=255
        ),
    )


def _heartbeat_mapping(identity: PixhawkHeartbeatIdentity) -> dict[str, int]:
    return {
        "source_system_id": identity.source_system_id,
        "source_component_id": identity.source_component_id,
        "autopilot_id": identity.autopilot_id,
        "vehicle_type_id": identity.vehicle_type_id,
        "mavlink_version": identity.mavlink_version,
    }


def _endpoint_mapping(raw: object) -> tuple[dict[str, object], PixhawkReadOnlyConfig]:
    value = strict_object(
        raw,
        name="endpoint",
        required=frozenset({"configured_endpoint", "resolved_endpoint", "baud"}),
    )
    configured = require_text(value["configured_endpoint"], "endpoint.configured_endpoint")
    resolved_value = value["resolved_endpoint"]
    resolved = (
        None
        if resolved_value is None
        else require_text(resolved_value, "endpoint.resolved_endpoint")
    )
    baud = require_int(value["baud"], "endpoint.baud", minimum=1)
    return (
        {
            "configured_endpoint": configured,
            "resolved_endpoint": resolved,
            "baud": baud,
        },
        PixhawkReadOnlyConfig(endpoint=configured, baud=baud),
    )


def _identity_without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("identity_sha256", None)
    return result


def verify_runtime_endpoint_vehicle_identity_sidecar(raw: object) -> dict[str, object]:
    """Strictly verify one endpoint/vehicle identity sidecar."""

    try:
        value = strict_object(
            raw,
            name="RuntimeEndpointVehicleIdentity",
            required=frozenset(
                {
                    "schema",
                    "schema_version",
                    "event",
                    "runtime_mode",
                    "observed_at_utc",
                    "runtime_descriptor_hash",
                    "runtime_input_hash",
                    "runtime_profile_document_sha256",
                    "endpoint",
                    "heartbeat_identity",
                    "observed_identity",
                    "source_kind",
                    "read_only",
                    "receive_only",
                    "flight_control_enabled",
                    "output_locked",
                    "identity_sha256",
                }
            ),
        )
        if require_text(value["schema"], "schema") != IDENTITY_SCHEMA:
            raise LiveRuntimeObservationMissionBindingError("identity sidecar schema is invalid")
        require_schema_version(value["schema_version"], expected=IDENTITY_SCHEMA_VERSION)
        if require_text(value["event"], "event") != IDENTITY_EVENT:
            raise LiveRuntimeObservationMissionBindingError("identity sidecar event is invalid")
        mode = _mode(value["runtime_mode"])
        _timestamp(value["observed_at_utc"], label="observed_at_utc")
        require_sha256(value["runtime_descriptor_hash"], "runtime_descriptor_hash")
        require_sha256(value["runtime_input_hash"], "runtime_input_hash")
        require_sha256(
            value["runtime_profile_document_sha256"],
            "runtime_profile_document_sha256",
        )
        endpoint, config = _endpoint_mapping(value["endpoint"])
        heartbeat = _heartbeat_from_mapping(value["heartbeat_identity"])
        identity = RuntimeIdentity.from_mapping(value["observed_identity"])
        if mode is RuntimeMode.HIL and identity.signing_identity_hash is not None:
            raise LiveRuntimeObservationMissionBindingError(
                "HIL identity sidecar must not carry a REAL signing identity"
            )
        if mode is RuntimeMode.REAL and identity.signing_identity_hash is None:
            raise LiveRuntimeObservationMissionBindingError(
                "REAL identity sidecar requires a signing identity"
            )
        if identity.endpoint_binding_hash != derive_endpoint_binding_hash(
            config,
            resolved_endpoint=endpoint["resolved_endpoint"],
        ):
            raise LiveRuntimeObservationMissionBindingError(
                "identity sidecar endpoint_binding_hash does not match endpoint"
            )
        if identity.vehicle_identity_hash != derive_vehicle_identity_hash(heartbeat):
            raise LiveRuntimeObservationMissionBindingError(
                "identity sidecar vehicle_identity_hash does not match heartbeat"
            )
        if require_text(value["source_kind"], "source_kind") != "live_mavlink_receive_only":
            raise LiveRuntimeObservationMissionBindingError(
                "identity sidecar source_kind is invalid"
            )
        if require_bool(value["read_only"], "read_only") is not True:
            raise LiveRuntimeObservationMissionBindingError("identity sidecar must be read-only")
        if require_bool(value["receive_only"], "receive_only") is not True:
            raise LiveRuntimeObservationMissionBindingError("identity sidecar must be receive-only")
        if require_bool(value["flight_control_enabled"], "flight_control_enabled") is not False:
            raise LiveRuntimeObservationMissionBindingError(
                "identity sidecar flight-control output must remain disabled"
            )
        if require_bool(value["output_locked"], "output_locked") is not True:
            raise LiveRuntimeObservationMissionBindingError(
                "identity sidecar output must remain locked"
            )
        claimed_hash = require_sha256(value["identity_sha256"], "identity_sha256")
        if canonical_sha256(_identity_without_hash(value)) != claimed_hash:
            raise LiveRuntimeObservationMissionBindingError(
                "identity sidecar identity_sha256 does not match canonical data"
            )
    except LiveRuntimeObservationMissionBindingError:
        raise
    except (AutonomyContractError, TypeError, ValueError) as exc:
        raise LiveRuntimeObservationMissionBindingError(str(exc)) from exc
    return dict(value)


def build_runtime_endpoint_vehicle_identity_sidecar(
    *,
    runtime_mode: RuntimeMode | str,
    runtime_descriptor_hash: str,
    runtime_input_hash: str,
    runtime_profile_document_sha256: str,
    endpoint: Mapping[str, object],
    heartbeat_identity: PixhawkHeartbeatIdentity | Mapping[str, object],
    observed_identity: RuntimeIdentity | Mapping[str, object],
    observed_at_utc: str,
) -> dict[str, object]:
    """Build a canonical receive-only endpoint/vehicle identity sidecar."""

    mode = _mode(runtime_mode)
    if isinstance(heartbeat_identity, PixhawkHeartbeatIdentity):
        heartbeat = heartbeat_identity
    else:
        heartbeat = _heartbeat_from_mapping(heartbeat_identity)
    if isinstance(observed_identity, RuntimeIdentity):
        identity = observed_identity
    else:
        identity = RuntimeIdentity.from_mapping(observed_identity)
    endpoint_value, _ = _endpoint_mapping(endpoint)
    result: dict[str, object] = {
        "schema": IDENTITY_SCHEMA,
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "event": IDENTITY_EVENT,
        "runtime_mode": mode.value,
        "observed_at_utc": _timestamp(observed_at_utc, label="observed_at_utc"),
        "runtime_descriptor_hash": require_sha256(
            runtime_descriptor_hash,
            "runtime_descriptor_hash",
        ),
        "runtime_input_hash": require_sha256(runtime_input_hash, "runtime_input_hash"),
        "runtime_profile_document_sha256": require_sha256(
            runtime_profile_document_sha256,
            "runtime_profile_document_sha256",
        ),
        "endpoint": endpoint_value,
        "heartbeat_identity": _heartbeat_mapping(heartbeat),
        "observed_identity": identity.to_mapping(),
        "source_kind": "live_mavlink_receive_only",
        "read_only": True,
        "receive_only": True,
        "flight_control_enabled": False,
        "output_locked": True,
    }
    result["identity_sha256"] = canonical_sha256(result)
    return verify_runtime_endpoint_vehicle_identity_sidecar(result)


def _empty_live_summary(source: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source": source,
        "result_sha256": None,
        "runtime_mode": None,
        "runtime_descriptor_hash": None,
        "runtime_input_hash": None,
        "processed_frame_count": None,
        "stream_exhausted": None,
        "termination_reason": None,
        "gate_passed": False,
    }


def _empty_mission_summary(source: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source": source,
        "mission_id": None,
        "mission_hash": None,
        "mission_item_count": None,
        "airframe_profile_hash": None,
    }


def _empty_feedback_summary(source: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source": source,
        "schema_version": None,
        "evidence_sha256": None,
        "feedback_sha256": None,
        "runtime_binding_sha256": None,
        "mission": None,
        "transport": None,
        "feedback_gate_passed": False,
        "runtime_binding_gate_passed": False,
        "gate_passed": False,
    }


def _empty_profile_summary(source: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source": source,
        "document_sha256": None,
        "profile_sha256": None,
        "profile_id": None,
        "key_id": None,
        "signature_valid": False,
        "identity_valid": False,
        "profile_valid": False,
        "output_locked": False,
        "gate_passed": False,
    }


def _empty_identity_summary(source: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source": source,
        "runtime_mode": None,
        "runtime_descriptor_hash": None,
        "runtime_input_hash": None,
        "runtime_profile_document_sha256": None,
        "endpoint": None,
        "heartbeat_identity": None,
        "observed_identity": None,
        "identity_sha256": None,
        "read_only": False,
        "receive_only": False,
        "flight_control_enabled": None,
        "output_locked": False,
        "gate_passed": False,
    }


def _empty_document(
    *,
    reasons: Sequence[str],
    sources: Mapping[str, dict[str, object] | None],
    error: Mapping[str, str] | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": ERROR_EVENT,
        "runtime_mode": None,
        "live_observation": _empty_live_summary(sources.get("live_observation")),
        "mission": _empty_mission_summary(sources.get("mission")),
        "mission_feedback": _empty_feedback_summary(sources.get("mission_feedback")),
        "runtime_profile": _empty_profile_summary(sources.get("runtime_profile")),
        "endpoint_vehicle_identity": _empty_identity_summary(
            sources.get("endpoint_vehicle_identity")
        ),
        "checks": {name: False for name in _CHECK_NAMES},
        "gate_passed": False,
        "reasons": list(reasons),
        "claims": {
            "source_kind": "software_sidecar_join",
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "flight_acceptance": False,
            "operator_visual_acceptance": False,
        },
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }
    if error is not None:
        document["error"] = dict(error)
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def _write_json(path: str | Path, document: Mapping[str, object]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        finally:
            raise
    return target


def _profile_summary(
    source: dict[str, object],
    *,
    profile_document_sha256: str,
    profile_sha256: str,
    profile_id: str,
    key_id: str,
    signature_valid: bool,
    identity_valid: bool,
    profile_valid: bool,
    output_locked: bool,
) -> dict[str, object]:
    return {
        "source": source,
        "document_sha256": profile_document_sha256,
        "profile_sha256": profile_sha256,
        "profile_id": profile_id,
        "key_id": key_id,
        "signature_valid": signature_valid,
        "identity_valid": identity_valid,
        "profile_valid": profile_valid,
        "output_locked": output_locked,
        "gate_passed": signature_valid and identity_valid and profile_valid and output_locked,
    }


def _runtime_binding_sha256(binding: Mapping[str, Any]) -> str | None:
    value = binding.get("binding_sha256")
    if not isinstance(value, str):
        return None
    try:
        return require_sha256(value, "runtime_binding.binding_sha256")
    except AutonomyContractError:
        return None


def _feedback_summary(
    source: dict[str, object],
    feedback: Mapping[str, Any],
) -> dict[str, object]:
    binding = feedback.get("runtime_binding")
    binding_map = binding if isinstance(binding, Mapping) else {}
    return {
        "source": source,
        "schema_version": feedback.get("schema_version"),
        "evidence_sha256": feedback.get("evidence_sha256"),
        "feedback_sha256": feedback.get("feedback_sha256"),
        "runtime_binding_sha256": _runtime_binding_sha256(binding_map),
        "mission": feedback.get("mission"),
        "transport": feedback.get("transport"),
        "feedback_gate_passed": feedback.get("feedback_gate_passed") is True,
        "runtime_binding_gate_passed": binding_map.get("gate_passed") is True,
        "gate_passed": feedback.get("feedback_gate_passed") is True,
    }


def verify_live_runtime_observation_mission_binding(
    runtime_observation_result: str | Path,
    mission_feedback_observation: str | Path,
    runtime_profile: str | Path,
    endpoint_vehicle_identity: str | Path,
    *,
    mission: str | Path,
    base_directory: str | Path = ".",
    hmac_key: bytes | None,
    expected_key_id: str | None = None,
    expected_release_label: str | None = None,
) -> dict[str, object]:
    """Reopen, verify, and join all v190 live/PX4 identity sidecars.

    ``mission`` is required so the feedback's embedded Mission identity is
    tied back to the executable Mission contract rather than accepted as a
    free-standing string/hash pair.
    """

    root = Path(base_directory).expanduser().resolve()
    source_documents: dict[str, tuple[Path, dict[str, object], Mapping[str, Any]]] = {}
    sources: dict[str, dict[str, object] | None] = {
        "live_observation": None,
        "mission": None,
        "mission_feedback": None,
        "runtime_profile": None,
        "endpoint_vehicle_identity": None,
    }
    source_specs = (
        (
            "live_observation",
            runtime_observation_result,
            "live observation",
            _MAX_OBSERVATION_BYTES,
        ),
        ("mission", mission, "executable Mission", _MAX_MISSION_BYTES),
        (
            "mission_feedback",
            mission_feedback_observation,
            "Mission feedback observation",
            _MAX_FEEDBACK_BYTES,
        ),
        ("runtime_profile", runtime_profile, "runtime profile", _MAX_PROFILE_BYTES),
        (
            "endpoint_vehicle_identity",
            endpoint_vehicle_identity,
            "endpoint/vehicle identity sidecar",
            _MAX_IDENTITY_BYTES,
        ),
    )
    reasons: list[str] = []
    for key, source, label, maximum in source_specs:
        try:
            path, info, document = _json_source(
                source,
                label=label,
                base_directory=root,
                maximum_bytes=maximum,
            )
            source_documents[key] = (path, info, document)
            sources[key] = info
        except (LiveRuntimeObservationMissionBindingError, OSError, TypeError, ValueError) as exc:
            reasons.append(f"{label}: {exc}")

    resolved_paths = [entry[0] for entry in source_documents.values()]
    sources_distinct = len(resolved_paths) == len(set(resolved_paths)) and len(resolved_paths) == 5
    if not sources_distinct:
        reasons.append("all live/PX4/Mission/profile/identity source paths must be distinct")

    if reasons:
        return _empty_document(
            reasons=reasons,
            sources=sources,
            error={"type": "LiveRuntimeObservationMissionBindingError", "message": reasons[0]},
        )

    live_path, live_source, _ = source_documents["live_observation"]
    _, mission_source, mission_raw = source_documents["mission"]
    feedback_path, feedback_source, _ = source_documents["mission_feedback"]
    profile_path, profile_source, profile_raw = source_documents["runtime_profile"]
    _, identity_source, identity_raw = source_documents["endpoint_vehicle_identity"]

    checks = {name: False for name in _CHECK_NAMES}
    checks["sources_distinct"] = sources_distinct
    parsed_reasons: list[str] = []
    live_result = None
    mission_document = None
    feedback_document = None
    identity_document = None
    profile_result = None
    mode: RuntimeMode | None = None

    try:
        live_result = load_runtime_observation_result(
            live_path,
        )
        checks["live_result_document_valid"] = True
        checks["live_result_gate_passed"] = live_result.gate_passed
        report = live_result.runtime_run_report
        checks["live_result_terminated"] = (
            report.termination_reason in {"stream_exhausted", "frame_limit_reached"}
            and report.finished_at_s >= report.started_at_s
        )
        if not checks["live_result_gate_passed"]:
            parsed_reasons.append("live RuntimeObservation gate is not qualified")
        if not checks["live_result_terminated"]:
            parsed_reasons.append("live RuntimeObservation run is not terminated")
        mode = live_result.mode
    except (AutonomyContractError, TypeError, ValueError, OSError) as exc:
        parsed_reasons.append(f"live RuntimeObservation: {exc}")

    try:
        mission_document = ExecutableMission.from_mapping(mission_raw)
        checks["mission_document_valid"] = True
    except (AutonomyContractError, TypeError, ValueError) as exc:
        parsed_reasons.append(f"executable Mission: {exc}")

    if mission_document is not None:
        try:
            feedback_document = load_px4_mission_feedback_observation(
                feedback_path,
                expected_mission=mission_document,
                runtime_profile_path=profile_path,
            )
            checks["mission_feedback_document_valid"] = True
            checks["mission_feedback_gate_passed"] = (
                feedback_document.get("feedback_gate_passed") is True
            )
            binding = feedback_document.get("runtime_binding")
            checks["runtime_binding_document_valid"] = isinstance(binding, Mapping)
            checks["runtime_binding_gate_passed"] = (
                isinstance(binding, Mapping) and binding.get("gate_passed") is True
            )
            if not checks["mission_feedback_gate_passed"]:
                parsed_reasons.append("PX4 Mission feedback gate is not qualified")
            if not checks["runtime_binding_document_valid"]:
                parsed_reasons.append("PX4 Mission feedback has no v2 runtime binding")
            if not checks["runtime_binding_gate_passed"]:
                parsed_reasons.append("PX4 Mission feedback runtime binding gate is not qualified")
        except (AutonomyContractError, TypeError, ValueError, OSError) as exc:
            parsed_reasons.append(f"PX4 Mission feedback observation: {exc}")

    try:
        identity_document = verify_runtime_endpoint_vehicle_identity_sidecar(identity_raw)
        checks["endpoint_identity_document_valid"] = True
        checks["endpoint_identity_hashes_valid"] = True
    except (AutonomyContractError, TypeError, ValueError) as exc:
        parsed_reasons.append(f"endpoint/vehicle identity sidecar: {exc}")

    feedback_binding = (
        feedback_document.get("runtime_binding") if isinstance(feedback_document, Mapping) else None
    )
    feedback_mode = (
        feedback_document.get("runtime_mode") if isinstance(feedback_document, Mapping) else None
    )
    identity_mode = identity_document.get("runtime_mode") if identity_document else None
    if mode is not None:
        checks["mode_matches"] = feedback_mode == mode.value and identity_mode == mode.value
        if not checks["mode_matches"]:
            parsed_reasons.append("live, PX4 feedback, and identity sidecar runtime modes differ")

    expected_profile_id = (
        RuntimeProfileId.HIL.value
        if mode is RuntimeMode.HIL
        else RuntimeProfileId.FLIGHT.value
        if mode is RuntimeMode.REAL
        else None
    )
    try:
        RuntimeProfile.from_mapping(profile_raw.get("profile"))
        require_text(profile_raw.get("key_id"), "runtime profile key_id")
        if hmac_key is None:
            raise LiveRuntimeObservationMissionBindingError(
                "runtime profile HMAC key is required for v190 join verification"
            )
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise LiveRuntimeObservationMissionBindingError(
                "runtime profile HMAC key must contain at least 32 bytes"
            )
        profile_result = load_runtime_profile_document(
            profile_path,
            expected_mode=mode,
            observed_identity=(
                RuntimeIdentity.from_mapping(identity_document["observed_identity"])
                if identity_document is not None
                else None
            ),
            hmac_key=hmac_key,
            expected_key_id=expected_key_id
            or (feedback_binding.get("key_id") if isinstance(feedback_binding, Mapping) else None),
            require_signature=True,
            allow_flight_control_output=False,
            require_firmware_identity_binding=True,
        )
        checks["runtime_profile_document_valid"] = True
        checks["runtime_profile_signature_valid"] = profile_result.signature_valid
        checks["runtime_profile_identity_valid"] = profile_result.identity_valid
        checks["runtime_profile_kind_matches"] = (
            expected_profile_id is not None
            and profile_result.profile.profile_id.value == expected_profile_id
        )
        if not checks["runtime_profile_signature_valid"]:
            parsed_reasons.append("runtime profile signature is not valid")
        if not checks["runtime_profile_identity_valid"]:
            parsed_reasons.append("runtime profile identity is not valid")
        if not checks["runtime_profile_kind_matches"]:
            parsed_reasons.append("runtime profile kind does not match HIL/REAL mode")
    except (
        AutonomyContractError,
        LiveRuntimeObservationMissionBindingError,
        TypeError,
        ValueError,
        OSError,
    ) as exc:
        parsed_reasons.append(f"runtime profile: {exc}")

    if live_result is not None and identity_document is not None:
        checks["runtime_descriptor_hash_matches"] = (
            live_result.runtime_run_report.descriptor_hash
            == identity_document.get("runtime_descriptor_hash")
        )
        checks["runtime_input_hash_matches"] = (
            live_result.runtime_run_report.input_hash == identity_document.get("runtime_input_hash")
        )
        if not checks["runtime_descriptor_hash_matches"]:
            parsed_reasons.append("runtime descriptor hash does not match identity sidecar")
        if not checks["runtime_input_hash_matches"]:
            parsed_reasons.append("runtime input hash does not match identity sidecar")

    if (
        isinstance(feedback_document, Mapping)
        and isinstance(identity_document, Mapping)
        and isinstance(feedback_binding, Mapping)
    ):
        observed_identity = feedback_binding.get("observed_identity")
        identity_observed = identity_document.get("observed_identity")
        checks["heartbeat_identity_matches"] = feedback_binding.get(
            "heartbeat_identity"
        ) == identity_document.get("heartbeat_identity")
        feedback_transport = feedback_document.get("transport")
        identity_endpoint = identity_document.get("endpoint")
        checks["endpoint_transport_matches"] = (
            isinstance(feedback_transport, Mapping)
            and isinstance(identity_endpoint, Mapping)
            and feedback_transport.get("configured_endpoint")
            == identity_endpoint.get("configured_endpoint")
            and feedback_transport.get("resolved_endpoint")
            == identity_endpoint.get("resolved_endpoint")
            and feedback_transport.get("baud") == identity_endpoint.get("baud")
        )
        feedback_mission = feedback_document.get("mission")
        checks["airframe_identity_matches"] = (
            isinstance(observed_identity, Mapping)
            and isinstance(identity_observed, Mapping)
            and isinstance(feedback_mission, Mapping)
            and observed_identity == identity_observed
            and observed_identity.get("airframe_profile_hash")
            == feedback_mission.get("airframe_profile_hash")
        )
        if not checks["heartbeat_identity_matches"]:
            parsed_reasons.append(
                "identity sidecar heartbeat differs from feedback runtime binding"
            )
        if not checks["endpoint_transport_matches"]:
            parsed_reasons.append("identity sidecar endpoint differs from feedback transport")
        if not checks["airframe_identity_matches"]:
            parsed_reasons.append(
                "identity sidecar airframe identity differs from Mission feedback"
            )

        profile_document_hashes = {
            profile_source["sha256"],
            identity_document.get("runtime_profile_document_sha256"),
            feedback_binding.get("profile_document_sha256"),
        }
        checks["profile_document_hash_matches"] = len(profile_document_hashes) == 1
        if not checks["profile_document_hash_matches"]:
            parsed_reasons.append("runtime profile document hash differs across sidecars")

    if (
        mission_document is not None
        and isinstance(feedback_document, Mapping)
        and isinstance(feedback_document.get("mission"), Mapping)
    ):
        mission_value = feedback_document["mission"]
        checks["mission_identity_matches_feedback"] = (
            mission_value.get("mission_id") == mission_document.mission_id
            and mission_value.get("mission_hash") == mission_document.canonical_hash
            and mission_value.get("mission_item_count") == len(mission_document.items)
            and mission_value.get("airframe_profile_hash") == mission_document.airframe_profile_hash
        )
        if not checks["mission_identity_matches_feedback"]:
            parsed_reasons.append("Mission source does not match feedback Mission identity")

    checks["all_outputs_locked"] = (
        live_result is not None
        and live_result.to_mapping().get("output_locked") is True
        and isinstance(feedback_document, Mapping)
        and isinstance(feedback_document.get("transport"), Mapping)
        and feedback_document["transport"].get("read_only") is True
        and feedback_document["transport"].get("messages_transmitted") == 0
        and isinstance(feedback_binding, Mapping)
        and feedback_binding.get("flight_control_enabled") is False
        and feedback_binding.get("output_locked") is True
        and identity_document is not None
        and identity_document.get("flight_control_enabled") is False
        and identity_document.get("output_locked") is True
        and profile_result is not None
        and profile_result.flight_control_enabled is False
        and profile_result.output_locked is True
    )
    if not checks["all_outputs_locked"]:
        parsed_reasons.append("one or more joined sources are not output locked")

    if profile_result is not None and isinstance(feedback_binding, Mapping):
        checks["runtime_profile_identity_valid"] = checks["runtime_profile_identity_valid"] and (
            feedback_binding.get("profile_sha256") == profile_result.profile.canonical_hash
        )
        if feedback_binding.get("profile_sha256") != profile_result.profile.canonical_hash:
            parsed_reasons.append(
                "feedback runtime binding profile hash differs from signed profile"
            )

    checks["no_errors"] = not parsed_reasons
    document: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT if not parsed_reasons else ERROR_EVENT,
        "runtime_mode": mode.value if mode is not None else None,
        "live_observation": (
            {
                "source": live_source,
                "result_sha256": live_result.canonical_hash,
                "runtime_mode": live_result.mode.value,
                "runtime_descriptor_hash": live_result.runtime_run_report.descriptor_hash,
                "runtime_input_hash": live_result.runtime_run_report.input_hash,
                "processed_frame_count": live_result.runtime_run_report.processed_frame_count,
                "stream_exhausted": live_result.runtime_run_report.stream_exhausted,
                "termination_reason": live_result.runtime_run_report.termination_reason,
                "gate_passed": live_result.gate_passed,
            }
            if live_result is not None
            else _empty_live_summary(live_source)
        ),
        "mission": (
            {
                "source": mission_source,
                "mission_id": mission_document.mission_id,
                "mission_hash": mission_document.canonical_hash,
                "mission_item_count": len(mission_document.items),
                "airframe_profile_hash": mission_document.airframe_profile_hash,
            }
            if mission_document is not None
            else _empty_mission_summary(mission_source)
        ),
        "mission_feedback": (
            _feedback_summary(feedback_source, feedback_document)
            if feedback_document is not None
            else _empty_feedback_summary(feedback_source)
        ),
        "runtime_profile": (
            _profile_summary(
                profile_source,
                profile_document_sha256=profile_result.document_sha256,
                profile_sha256=profile_result.profile.canonical_hash,
                profile_id=profile_result.profile.profile_id.value,
                key_id=require_text(profile_raw.get("key_id"), "runtime profile key_id"),
                signature_valid=profile_result.signature_valid,
                identity_valid=profile_result.identity_valid,
                profile_valid=profile_result.profile_valid,
                output_locked=profile_result.output_locked,
            )
            if profile_result is not None
            else _empty_profile_summary(profile_source)
        ),
        "endpoint_vehicle_identity": (
            {
                "source": identity_source,
                "runtime_mode": identity_document.get("runtime_mode"),
                "runtime_descriptor_hash": identity_document.get("runtime_descriptor_hash"),
                "runtime_input_hash": identity_document.get("runtime_input_hash"),
                "runtime_profile_document_sha256": identity_document.get(
                    "runtime_profile_document_sha256"
                ),
                "endpoint": identity_document.get("endpoint"),
                "heartbeat_identity": identity_document.get("heartbeat_identity"),
                "observed_identity": identity_document.get("observed_identity"),
                "identity_sha256": identity_document.get("identity_sha256"),
                "read_only": identity_document.get("read_only"),
                "receive_only": identity_document.get("receive_only"),
                "flight_control_enabled": identity_document.get("flight_control_enabled"),
                "output_locked": identity_document.get("output_locked"),
                "gate_passed": checks["endpoint_identity_document_valid"]
                and checks["endpoint_identity_hashes_valid"],
            }
            if identity_document is not None
            else _empty_identity_summary(identity_source)
        ),
        "checks": checks,
        "gate_passed": all(checks.values()),
        "reasons": parsed_reasons
        if parsed_reasons
        else [name for name in _CHECK_NAMES if not checks[name]],
        "claims": {
            "source_kind": "software_sidecar_join",
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "flight_acceptance": False,
            "operator_visual_acceptance": False,
        },
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }
    if document["gate_passed"] is not True:
        document["error"] = {
            "type": "LiveRuntimeObservationMissionBindingError",
            "message": str(parsed_reasons[0] if parsed_reasons else "join checks failed"),
        }
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def build_live_runtime_observation_mission_binding(
    runtime_observation_result: str | Path,
    mission_feedback_observation: str | Path,
    runtime_profile: str | Path,
    endpoint_vehicle_identity: str | Path,
    **kwargs: Any,
) -> dict[str, object]:
    """Build alias retained for evidence producers that use ``build`` naming."""

    return verify_live_runtime_observation_mission_binding(
        runtime_observation_result,
        mission_feedback_observation,
        runtime_profile,
        endpoint_vehicle_identity,
        **kwargs,
    )


def _key_from_environment(name: str | None) -> bytes:
    if name is None or not name.strip():
        raise LiveRuntimeObservationMissionBindingError(
            "--runtime-profile-hmac-key-env is required"
        )
    value = os.environ.get(name)
    if value is None or not value:
        raise LiveRuntimeObservationMissionBindingError(
            f"runtime profile HMAC key environment variable is missing: {name}"
        )
    return value.encode("utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-observation", type=Path, required=True)
    parser.add_argument("--mission-feedback", type=Path, required=True)
    parser.add_argument("--runtime-profile", type=Path, required=True)
    parser.add_argument("--endpoint-vehicle-identity", type=Path, required=True)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument(
        "--base-directory",
        type=Path,
        default=Path.cwd(),
        help="root under which all five input files must resolve",
    )
    parser.add_argument("--runtime-profile-hmac-key-env", required=True)
    parser.add_argument("--runtime-profile-key-id")
    parser.add_argument("--expected-release-label")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        key = _key_from_environment(args.runtime_profile_hmac_key_env)
        document = verify_live_runtime_observation_mission_binding(
            args.runtime_observation,
            args.mission_feedback,
            args.runtime_profile,
            args.endpoint_vehicle_identity,
            mission=args.mission,
            base_directory=args.base_directory,
            hmac_key=key,
            expected_key_id=args.runtime_profile_key_id,
            expected_release_label=args.expected_release_label,
        )
    except (LiveRuntimeObservationMissionBindingError, OSError, TypeError, ValueError) as exc:
        document = _empty_document(
            reasons=[str(exc) or type(exc).__name__],
            sources={
                "live_observation": None,
                "mission": None,
                "mission_feedback": None,
                "runtime_profile": None,
                "endpoint_vehicle_identity": None,
            },
            error={"type": type(exc).__name__, "message": str(exc) or type(exc).__name__},
        )
    _write_json(args.out, document)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if document["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ERROR_EVENT",
    "EVENT",
    "IDENTITY_EVENT",
    "IDENTITY_SCHEMA",
    "IDENTITY_SCHEMA_VERSION",
    "LiveRuntimeObservationMissionBindingError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_live_runtime_observation_mission_binding",
    "build_parser",
    "build_runtime_endpoint_vehicle_identity_sidecar",
    "main",
    "verify_live_runtime_observation_mission_binding",
    "verify_runtime_endpoint_vehicle_identity_sidecar",
]

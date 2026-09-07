"""Capture and verify receive-only PX4 Mission execution feedback.

The observer is the field-facing companion to :mod:`px4_mission_feedback`.
It opens one MAVLink endpoint through the read-only Pixhawk provider, binds
``MISSION_CURRENT``/``MISSION_ITEM_REACHED`` to an exact executable Mission,
and writes a deterministic report.  A successful feedback sub-gate is still
not a HIL, REAL, flight, or operator-visual acceptance claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .autonomy.contracts import ExecutableMission
from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    optional_text,
    require_array,
    require_bool,
    require_int,
    require_real,
    require_sha256,
    require_string_array,
    require_text,
    strict_json_loads,
    strict_object,
)
from .compat import UTC
from .mission_planning import load_executable_mission_document
from .pixhawk import (
    PIXHAWK_AUTOPILOT_IDS,
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)
from .px4_mission import (
    PX4_FIXED_WING_ADAPTER_HASH,
    PX4_FIXED_WING_ADAPTER_ID,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
)
from .px4_mission_feedback import (
    PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S,
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackSnapshot,
    Px4MissionFeedbackTracker,
)
from .px4_mission_feedback_runtime import (
    build_px4_mission_feedback_runtime_binding,
    verify_px4_mission_feedback_runtime_binding,
)
from .runtime import RuntimeMode, RuntimeProfile
from .runtime_launcher import (
    build_pixhawk_runtime_identity,
    signing_identity_hash_from_hex,
)
from .runtime_profiles import (
    RuntimeIdentity,
    load_runtime_profile_document,
    sign_runtime_profile_document,
)

PX4_MISSION_FEEDBACK_OBSERVATION_LEGACY_SCHEMA_VERSION = 1
PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION = 2
PX4_MISSION_FEEDBACK_OBSERVATION_EVENT = "px4_mission_feedback_observed"
PX4_MISSION_FEEDBACK_OBSERVATION_MAX_DURATION_S = 3_600.0
PX4_MISSION_FEEDBACK_OBSERVATION_MAX_ERROR_SAMPLES = 8
PX4_MISSION_FEEDBACK_PROFILE_PROVISION_EVENT = "px4_runtime_profile_provisioned"
PX4_MISSION_FEEDBACK_PROFILE_MAX_PROBE_DURATION_S = 300.0
_RUNTIME_MODES = frozenset({"sim", "hil", "real"})
_PROFILE_RUNTIME_MODES = frozenset({"hil", "real"})
_OPERATIONAL_SYSTEM_STATUS_IDS = frozenset({3, 4})
_MISSION_ADAPTERS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}
# MAV_TYPE values are kept local so profile provisioning remains usable when
# the optional pymavlink dependency is not imported by tests or offline tools.
_VEHICLE_TYPE_IDS = {
    "fixed-wing": frozenset({1}),
    "multirotor": frozenset({2, 3, 4, 13, 14, 15, 29, 35}),
    "vtol": frozenset({19, 20, 21, 22, 23, 24, 25}),
}

_TRANSMIT_COUNTER_FIELDS = (
    "telemetry_request_messages_transmitted",
    "application_messages_transmitted",
    "flight_control_messages_transmitted",
)
_MISSING_COUNTER = object()


class Px4MissionFeedbackObservationError(AutonomyContractError):
    """Raised when an observation report is malformed or self-inconsistent."""


@dataclass(frozen=True, slots=True)
class _TransmitCounterBreakdown:
    """Normalized transport output counters used by the feedback gate.

    ``messages_transmitted`` remains the historical total.  New providers
    expose the three explicit buckets so receive-only telemetry requests are
    distinguishable from application or flight-control output.  A legacy
    provider/report without those fields is conservatively treated as
    application output, preserving the old fail-closed behaviour.
    """

    total: int
    telemetry_requests: int
    application: int
    flight_control: int
    explicit: bool


@dataclass(frozen=True, slots=True)
class Px4RuntimeProfileProvision:
    """One profile derived from an observed receive-only PX4 identity."""

    runtime_mode: RuntimeMode
    vehicle_class: str
    mission_id: str
    mission_hash: str
    airframe_profile_hash: str
    configured_endpoint: str
    resolved_endpoint: str
    baud: int
    heartbeat_identity: PixhawkHeartbeatIdentity
    autopilot_version_identity: PixhawkAutopilotVersionIdentity
    runtime_identity: RuntimeIdentity
    profile: RuntimeProfile
    profile_document: dict[str, Any]
    key_id: str
    probe_started_at_s: float
    probe_completed_at_s: float
    sample_count: int
    messages_received: int
    messages_transmitted: int
    telemetry_request_messages_transmitted: int
    application_messages_transmitted: int
    flight_control_messages_transmitted: int

    def summary(self, *, path: Path, document_sha256: str) -> dict[str, object]:
        """Return a secret-free operator summary for the generated file."""

        return {
            "event": PX4_MISSION_FEEDBACK_PROFILE_PROVISION_EVENT,
            "path": str(path),
            "document_sha256": require_sha256(document_sha256, "document_sha256"),
            "runtime_mode": self.runtime_mode.value,
            "vehicle_class": self.vehicle_class,
            "mission": {
                "mission_id": self.mission_id,
                "mission_hash": self.mission_hash,
                "airframe_profile_hash": self.airframe_profile_hash,
            },
            "transport": {
                "configured_endpoint": self.configured_endpoint,
                "resolved_endpoint": self.resolved_endpoint,
                "baud": self.baud,
                "read_only": True,
                "messages_received": self.messages_received,
                "messages_transmitted": self.messages_transmitted,
                "telemetry_request_messages_transmitted": (
                    self.telemetry_request_messages_transmitted
                ),
                "application_messages_transmitted": self.application_messages_transmitted,
                "flight_control_messages_transmitted": self.flight_control_messages_transmitted,
            },
            "heartbeat_identity": self.heartbeat_identity.to_document(),
            "autopilot_version_identity": self.autopilot_version_identity.to_document(),
            "runtime_identity": self.runtime_identity.to_mapping(),
            "profile_id": self.profile.profile_id.value,
            "profile_sha256": self.profile.canonical_hash,
            "key_id": self.key_id,
            "probe": {
                "started_at_s": self.probe_started_at_s,
                "completed_at_s": self.probe_completed_at_s,
                "sample_count": self.sample_count,
            },
            "output_locked": True,
            "claims": {
                "release_eligible": False,
                "hardware_acceptance": False,
                "hil_acceptance": False,
                "real_acceptance": False,
                "operator_visual_acceptance": False,
            },
        }


def provision_px4_mission_feedback_runtime_profile(
    mission: ExecutableMission,
    provider: PixhawkReadOnlyTelemetryProvider,
    *,
    runtime_mode: str,
    hmac_key: bytes,
    key_id: str,
    signing_identity_hash: str | None = None,
    expected_system_id: int = 1,
    expected_component_id: int = 1,
    duration_s: float = 10.0,
    poll_interval_s: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Px4RuntimeProfileProvision:
    """Probe one endpoint read-only and sign its exact HIL/REAL profile.

    The returned document is not an acceptance report.  It is the capability
    input that a later feedback observation must reopen and match against a
    fresh heartbeat, endpoint and Mission airframe identity.
    """

    if not isinstance(mission, ExecutableMission):
        raise Px4MissionFeedbackObservationError("mission must be an ExecutableMission")
    if not isinstance(provider, PixhawkReadOnlyTelemetryProvider) and not all(
        hasattr(provider, name)
        for name in (
            "config",
            "snapshot",
            "transport_link_healthy",
            "heartbeat_identity",
            "autopilot_version_identity",
            "autopilot_version_observed_at_s",
            "resolved_endpoint",
            "is_read_only",
            "messages_received",
            "messages_transmitted",
        )
    ):
        raise Px4MissionFeedbackObservationError(
            "provider must expose the read-only Pixhawk identity interface"
        )
    mode_text = _runtime_mode(runtime_mode)
    if mode_text not in _PROFILE_RUNTIME_MODES:
        raise Px4MissionFeedbackObservationError(
            "runtime profile provisioning supports only hil or real"
        )
    mode = RuntimeMode(mode_text)
    vehicle_class = _mission_vehicle_class(mission)
    expected_system = require_int(
        expected_system_id,
        "expected_system_id",
        minimum=1,
        maximum=255,
    )
    expected_component = require_int(
        expected_component_id,
        "expected_component_id",
        minimum=1,
        maximum=255,
    )
    signing_hash = (
        None
        if signing_identity_hash is None
        else require_sha256(signing_identity_hash, "signing_identity_hash")
    )
    if mode is RuntimeMode.HIL and signing_hash is not None:
        raise Px4MissionFeedbackObservationError(
            "HIL runtime profile cannot carry a REAL signing identity"
        )
    if mode is RuntimeMode.REAL and signing_hash is None:
        raise Px4MissionFeedbackObservationError(
            "REAL runtime profile requires a signing identity hash"
        )
    if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
        raise Px4MissionFeedbackObservationError("hmac_key must contain at least 32 bytes")
    clean_key_id = require_text(key_id, "key_id")
    duration = _bounded_real(
        duration_s,
        "duration_s",
        minimum=0.0,
        maximum=PX4_MISSION_FEEDBACK_PROFILE_MAX_PROBE_DURATION_S,
    )
    poll_interval = _bounded_real(
        poll_interval_s,
        "poll_interval_s",
        minimum=0.001,
        maximum=5.0,
    )
    if not callable(clock) or not callable(sleeper):
        raise Px4MissionFeedbackObservationError("clock and sleeper must be callable")

    started_at_s = _clock_value(clock(), "profile probe start")
    deadline_s = started_at_s + duration
    previous_clock_s = started_at_s
    receive_error_samples: list[str] = []
    sample_count = 0
    reasons: tuple[str, ...] = ("heartbeat_not_observed",)
    while True:
        now_s = _clock_value(clock(), "profile probe clock")
        if now_s < previous_clock_s:
            raise Px4MissionFeedbackObservationError("profile probe clock moved backwards")
        previous_clock_s = now_s
        try:
            provider.snapshot(now_s=now_s)
        except Exception as exc:
            if len(receive_error_samples) < PX4_MISSION_FEEDBACK_OBSERVATION_MAX_ERROR_SAMPLES:
                message = str(exc).strip() or type(exc).__name__
                receive_error_samples.append(f"{type(exc).__name__}: {message[:512]}")
        sample_count += 1
        reasons = _runtime_profile_probe_reasons(
            provider,
            now_s=now_s,
            vehicle_class=vehicle_class,
            expected_system_id=expected_system,
            expected_component_id=expected_component,
            probe_started_at_s=started_at_s,
        )
        if not reasons and not receive_error_samples:
            break

        after_sample_s = _clock_value(clock(), "profile probe post-sample clock")
        if after_sample_s < previous_clock_s:
            raise Px4MissionFeedbackObservationError("profile probe clock moved backwards")
        previous_clock_s = after_sample_s
        if after_sample_s >= deadline_s:
            break
        sleeper(min(poll_interval, deadline_s - after_sample_s))

    completed_at_s = _clock_value(clock(), "profile probe completion clock")
    if completed_at_s < previous_clock_s:
        raise Px4MissionFeedbackObservationError("profile probe clock moved backwards")
    if receive_error_samples:
        reasons = (*reasons, "mavlink_receive_errors")
    if reasons:
        detail = ", ".join(dict.fromkeys((*reasons, *receive_error_samples)))
        raise Px4MissionFeedbackObservationError(f"runtime profile probe failed: {detail}")

    heartbeat = provider.heartbeat_identity
    if not isinstance(heartbeat, PixhawkHeartbeatIdentity):
        raise Px4MissionFeedbackObservationError(
            "provider heartbeat_identity must be a PixhawkHeartbeatIdentity"
        )
    autopilot_version = provider.autopilot_version_identity
    if not isinstance(autopilot_version, PixhawkAutopilotVersionIdentity):
        raise Px4MissionFeedbackObservationError(
            "complete PX4 AUTOPILOT_VERSION identity is required"
        )
    runtime_identity = build_pixhawk_runtime_identity(
        provider,
        airframe_profile_hash=mission.airframe_profile_hash,
        signing_identity_hash=signing_hash,
    )
    if runtime_identity.vehicle_identity_hash is None:
        raise Px4MissionFeedbackObservationError("complete PX4 heartbeat identity is required")
    if runtime_identity.firmware_identity_hash is None:
        raise Px4MissionFeedbackObservationError(
            "complete PX4 AUTOPILOT_VERSION identity is required"
        )
    transmit_counters = _provider_transmit_counters(provider)
    if transmit_counters is None:
        raise Px4MissionFeedbackObservationError("provider transmit counters are invalid")
    if mode is RuntimeMode.HIL:
        profile = RuntimeProfile.hil(
            vehicle_identity_hash=runtime_identity.vehicle_identity_hash,
            firmware_identity_hash=runtime_identity.firmware_identity_hash,
            airframe_profile_hash=runtime_identity.airframe_profile_hash,
            endpoint_binding_hash=runtime_identity.endpoint_binding_hash,
        )
    else:
        if signing_hash is None:  # Defensive guard for optimized Python.
            raise Px4MissionFeedbackObservationError(
                "REAL runtime profile requires a signing identity hash"
            )
        profile = RuntimeProfile.flight(
            vehicle_identity_hash=runtime_identity.vehicle_identity_hash,
            firmware_identity_hash=runtime_identity.firmware_identity_hash,
            airframe_profile_hash=runtime_identity.airframe_profile_hash,
            signing_identity_hash=signing_hash,
            endpoint_binding_hash=runtime_identity.endpoint_binding_hash,
        )
    document = sign_runtime_profile_document(
        profile,
        hmac_key=hmac_key,
        key_id=clean_key_id,
    )
    provider_config = provider.config
    configured_endpoint = require_text(
        getattr(provider_config, "endpoint", None),
        "provider.config.endpoint",
    )
    resolved_endpoint = require_text(
        getattr(provider, "resolved_endpoint", None),
        "provider.resolved_endpoint",
    )
    baud = require_int(
        getattr(provider_config, "baud", None),
        "provider.config.baud",
        minimum=1,
    )
    return Px4RuntimeProfileProvision(
        runtime_mode=mode,
        vehicle_class=vehicle_class,
        mission_id=mission.mission_id,
        mission_hash=mission.canonical_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        configured_endpoint=configured_endpoint,
        resolved_endpoint=resolved_endpoint,
        baud=baud,
        heartbeat_identity=heartbeat,
        autopilot_version_identity=autopilot_version,
        runtime_identity=runtime_identity,
        profile=profile,
        profile_document=document,
        key_id=clean_key_id,
        probe_started_at_s=started_at_s,
        probe_completed_at_s=completed_at_s,
        sample_count=sample_count,
        messages_received=require_int(
            provider.messages_received,
            "provider.messages_received",
            minimum=1,
        ),
        messages_transmitted=transmit_counters.total,
        telemetry_request_messages_transmitted=transmit_counters.telemetry_requests,
        application_messages_transmitted=transmit_counters.application,
        flight_control_messages_transmitted=transmit_counters.flight_control,
    )


def write_px4_mission_feedback_runtime_profile(
    destination: str | Path,
    provision: Px4RuntimeProfileProvision,
    *,
    hmac_key: bytes,
    overwrite: bool = False,
) -> Path:
    """Verify and atomically persist a provisioned signed profile."""

    if not isinstance(provision, Px4RuntimeProfileProvision):
        raise TypeError("provision must be a Px4RuntimeProfileProvision")
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be boolean")
    output = Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise Px4MissionFeedbackObservationError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            provision.profile_document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        load_result = load_runtime_profile_document(
            temporary_path,
            expected_mode=provision.runtime_mode,
            observed_identity=provision.runtime_identity,
            hmac_key=hmac_key,
            expected_key_id=provision.key_id,
            require_signature=True,
            allow_flight_control_output=False,
        )
        if (
            load_result.profile != provision.profile
            or not load_result.profile_valid
            or not load_result.output_locked
            or load_result.flight_control_enabled
        ):
            raise Px4MissionFeedbackObservationError(
                "provisioned runtime profile failed locked-output verification"
            )
        os.replace(temporary_path, output)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output


def _mission_vehicle_class(mission: ExecutableMission) -> str:
    for vehicle_class, (adapter_id, adapter_hash) in _MISSION_ADAPTERS.items():
        if (
            mission.airframe_adapter_id == adapter_id
            and mission.airframe_adapter_hash == adapter_hash
        ):
            return vehicle_class
    raise Px4MissionFeedbackObservationError(
        "Mission adapter is not the current fixed-wing, multirotor or VTOL adapter"
    )


def _runtime_profile_probe_reasons(
    provider: PixhawkReadOnlyTelemetryProvider,
    *,
    now_s: float,
    vehicle_class: str,
    expected_system_id: int,
    expected_component_id: int,
    probe_started_at_s: float,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if getattr(provider, "is_read_only", None) is not True:
        reasons.append("provider_not_read_only")
    counters = _provider_transmit_counters(provider)
    if counters is None:
        reasons.append("invalid_transmit_counter")
    elif counters.application or counters.flight_control:
        if counters.explicit:
            if counters.application:
                reasons.append("application_messages_transmitted")
            if counters.flight_control:
                reasons.append("flight_control_messages_transmitted")
        else:
            reasons.append("mavlink_messages_transmitted")
    messages_received = getattr(provider, "messages_received", None)
    if (
        isinstance(messages_received, bool)
        or not isinstance(messages_received, int)
        or messages_received < 1
    ):
        reasons.append("heartbeat_not_received")

    resolved_endpoint = getattr(provider, "resolved_endpoint", None)
    if (
        not isinstance(resolved_endpoint, str)
        or not resolved_endpoint.strip()
        or resolved_endpoint.strip().lower() == "auto"
    ):
        reasons.append("endpoint_not_resolved")
    link_health = provider.transport_link_healthy(now_s=now_s)
    if link_health is not True:
        reasons.append("heartbeat_not_fresh")

    heartbeat = getattr(provider, "heartbeat_identity", None)
    if not isinstance(heartbeat, PixhawkHeartbeatIdentity):
        return tuple((*reasons, "heartbeat_identity_invalid"))
    required = (
        heartbeat.source_system_id,
        heartbeat.source_component_id,
        heartbeat.autopilot_id,
        heartbeat.vehicle_type_id,
        heartbeat.system_status_id,
        heartbeat.mavlink_version,
    )
    if any(value is None for value in required):
        reasons.append("heartbeat_identity_incomplete")
        return tuple(reasons)
    if heartbeat.source_system_id != expected_system_id:
        reasons.append("heartbeat_system_id_mismatch")
    if heartbeat.source_component_id != expected_component_id:
        reasons.append("heartbeat_component_id_mismatch")
    if heartbeat.autopilot_id != PIXHAWK_AUTOPILOT_IDS["px4"]:
        reasons.append("heartbeat_autopilot_is_not_px4")
    if heartbeat.vehicle_type_id not in _VEHICLE_TYPE_IDS[vehicle_class]:
        reasons.append("heartbeat_vehicle_type_does_not_match_mission")
    if heartbeat.system_status_id not in _OPERATIONAL_SYSTEM_STATUS_IDS:
        reasons.append("heartbeat_system_status_not_operational")
    if heartbeat.mavlink_version is None or heartbeat.mavlink_version <= 0:
        reasons.append("heartbeat_mavlink_version_invalid")
    autopilot_version = getattr(provider, "autopilot_version_identity", None)
    if not isinstance(autopilot_version, PixhawkAutopilotVersionIdentity):
        reasons.append("autopilot_version_identity_missing")
    else:
        if (
            autopilot_version.source_system_id != heartbeat.source_system_id
            or autopilot_version.source_component_id != heartbeat.source_component_id
        ):
            reasons.append("autopilot_version_source_does_not_match_heartbeat")
        observed_at_s = getattr(provider, "autopilot_version_observed_at_s", None)
        if isinstance(observed_at_s, bool):
            reasons.append("autopilot_version_not_observed_during_probe")
        else:
            try:
                observed_at = float(observed_at_s)
            except (TypeError, ValueError, OverflowError):
                reasons.append("autopilot_version_not_observed_during_probe")
            else:
                if (
                    not math.isfinite(observed_at)
                    or observed_at < probe_started_at_s
                    or observed_at > now_s
                ):
                    reasons.append("autopilot_version_not_observed_during_probe")
    return tuple(reasons)


def _provider_transmit_counters(
    provider: object,
) -> _TransmitCounterBreakdown | None:
    """Normalize provider output counters without hiding malformed values.

    Older adapters only expose ``messages_transmitted``.  Their non-zero total
    is conservatively classified as application output.  New Pixhawk adapters
    expose explicit telemetry/application/flight-control buckets; telemetry
    requests are accepted by the receive-only gate when the two output buckets
    remain zero.
    """

    try:
        total_value = provider.messages_transmitted  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if isinstance(total_value, bool) or not isinstance(total_value, int) or total_value < 0:
        return None
    values: dict[str, object] = {}
    for field in _TRANSMIT_COUNTER_FIELDS:
        try:
            value = getattr(provider, field)
        except AttributeError:
            continue
        values[field] = value
    return _normalise_transmit_counters(
        total_value,
        values,
        context="provider",
    )


def _normalise_transmit_counters(
    total_value: object,
    values: Mapping[str, object],
    *,
    context: str,
) -> _TransmitCounterBreakdown:
    """Validate the total and derive missing counter buckets.

    The optional fields were added after the v1/v2 report contracts shipped,
    so an all-absent report is interpreted using the historical conservative
    rule (all non-zero output is application output).  Partially populated
    reports are accepted when the omitted buckets can be derived exactly.
    """

    total = require_int(total_value, f"{context}.messages_transmitted", minimum=0)
    explicit = any(field in values for field in _TRANSMIT_COUNTER_FIELDS)
    if not explicit:
        return _TransmitCounterBreakdown(total, 0, total, 0, False)

    parsed: dict[str, int] = {}
    for field in _TRANSMIT_COUNTER_FIELDS:
        if field in values:
            parsed[field] = require_int(values[field], f"{context}.{field}", minimum=0)
    telemetry = parsed.get("telemetry_request_messages_transmitted", 0)
    flight_control = parsed.get("flight_control_messages_transmitted", 0)
    if "application_messages_transmitted" in parsed:
        application = parsed["application_messages_transmitted"]
    else:
        application = total - telemetry - flight_control
        if application < 0:
            raise Px4MissionFeedbackObservationError(
                f"{context} transmit counters exceed messages_transmitted"
            )
    if total != telemetry + application + flight_control:
        raise Px4MissionFeedbackObservationError(
            f"{context} transmit counters do not sum to messages_transmitted"
        )
    return _TransmitCounterBreakdown(total, telemetry, application, flight_control, True)


def observe_px4_mission_feedback(
    mission: ExecutableMission,
    provider: PixhawkReadOnlyTelemetryProvider,
    *,
    runtime_mode: str,
    duration_s: float = 30.0,
    poll_interval_s: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
    runtime_profile_path: str | Path | None = None,
    runtime_profile_hmac_key: bytes | None = None,
    runtime_profile_key_id: str | None = None,
    runtime_signing_identity_hash: str | None = None,
) -> dict[str, object]:
    """Observe one bounded Mission feedback window and return strict evidence."""

    if not isinstance(mission, ExecutableMission):
        raise Px4MissionFeedbackObservationError("mission must be an ExecutableMission")
    if not isinstance(provider, PixhawkReadOnlyTelemetryProvider) and not all(
        hasattr(provider, name)
        for name in (
            "config",
            "snapshot",
            "mission_feedback_snapshot",
            "is_read_only",
            "messages_received",
            "messages_transmitted",
            "message_type_counts",
        )
    ):
        raise Px4MissionFeedbackObservationError(
            "provider must expose the read-only Pixhawk feedback interface"
        )
    mode = _runtime_mode(runtime_mode)
    duration = _bounded_real(
        duration_s,
        "duration_s",
        minimum=0.0,
        maximum=PX4_MISSION_FEEDBACK_OBSERVATION_MAX_DURATION_S,
    )
    poll_interval = _bounded_real(
        poll_interval_s,
        "poll_interval_s",
        minimum=0.001,
        maximum=5.0,
    )
    if not callable(clock) or not callable(sleeper) or not callable(observed_at):
        raise Px4MissionFeedbackObservationError("clock, sleeper and observed_at must be callable")

    started_at_s = _clock_value(clock(), "clock start")
    deadline_s = started_at_s + duration
    sample_count = 0
    receive_error_count = 0
    receive_error_samples: list[dict[str, str]] = []
    previous_clock_s = started_at_s

    while True:
        now_s = _clock_value(clock(), "sample clock")
        if now_s < previous_clock_s:
            raise Px4MissionFeedbackObservationError("observation clock moved backwards")
        previous_clock_s = now_s
        try:
            provider.snapshot(now_s=now_s)
        except Exception as exc:  # The report must survive a missing/unreachable field link.
            receive_error_count += 1
            if len(receive_error_samples) < PX4_MISSION_FEEDBACK_OBSERVATION_MAX_ERROR_SAMPLES:
                receive_error_samples.append(_error_sample(exc))
        sample_count += 1

        after_sample_s = _clock_value(clock(), "post-sample clock")
        if after_sample_s < previous_clock_s:
            raise Px4MissionFeedbackObservationError("observation clock moved backwards")
        previous_clock_s = after_sample_s
        if after_sample_s >= deadline_s:
            break
        sleeper(min(poll_interval, deadline_s - after_sample_s))

    completed_at_s = _clock_value(clock(), "completion clock")
    if completed_at_s < previous_clock_s:
        raise Px4MissionFeedbackObservationError("observation clock moved backwards")
    feedback = provider.mission_feedback_snapshot(now_s=completed_at_s)
    if not isinstance(feedback, Px4MissionFeedbackSnapshot):
        raise Px4MissionFeedbackObservationError(
            "provider has no bound Px4MissionFeedbackTracker snapshot"
        )
    if (
        feedback.mission_id != mission.mission_id
        or feedback.mission_hash != mission.canonical_hash
        or feedback.mission_item_count != len(mission.items)
    ):
        raise Px4MissionFeedbackObservationError(
            "provider feedback does not match the requested executable Mission"
        )

    read_only = require_bool(provider.is_read_only, "provider.is_read_only")
    messages_received = require_int(
        provider.messages_received,
        "provider.messages_received",
        minimum=0,
    )
    transmit_counters = _provider_transmit_counters(provider)
    if transmit_counters is None:
        raise Px4MissionFeedbackObservationError("provider transmit counters are invalid")
    message_type_counts = _message_type_counts(provider.message_type_counts)
    relevant_count = sum(
        message_type_counts.get(name, 0) for name in ("MISSION_CURRENT", "MISSION_ITEM_REACHED")
    )
    if relevant_count < feedback.accepted_event_count + feedback.rejected_event_count:
        raise Px4MissionFeedbackObservationError(
            "provider message counters under-report Mission feedback events"
        )

    provider_config = provider.config
    configured_endpoint = require_text(
        getattr(provider_config, "endpoint", None),
        "provider.config.endpoint",
    )
    baud = require_int(getattr(provider_config, "baud", None), "provider.config.baud", minimum=1)
    resolved_endpoint = optional_text(
        getattr(provider, "resolved_endpoint", None),
        "provider.resolved_endpoint",
    )
    feedback_reasons = _gate_reasons(
        feedback=feedback,
        read_only=read_only,
        counters=transmit_counters,
        receive_error_count=receive_error_count,
    )
    runtime_options = (
        runtime_profile_path,
        runtime_profile_hmac_key,
        runtime_profile_key_id,
        runtime_signing_identity_hash,
    )
    runtime_binding: dict[str, object] | None = None
    if any(value is not None for value in runtime_options):
        if mode not in {"hil", "real"}:
            raise Px4MissionFeedbackObservationError(
                "runtime profile binding is only supported for HIL/REAL observations"
            )
        missing = [
            name
            for name, value in (
                ("runtime_profile_path", runtime_profile_path),
                ("runtime_profile_hmac_key", runtime_profile_hmac_key),
                ("runtime_profile_key_id", runtime_profile_key_id),
            )
            if value is None
        ]
        if missing:
            raise Px4MissionFeedbackObservationError(
                "runtime profile binding is incomplete: " + ", ".join(missing)
            )
        runtime_binding = build_px4_mission_feedback_runtime_binding(
            mission,
            feedback,
            provider,
            runtime_mode=mode,
            runtime_profile_path=runtime_profile_path,
            hmac_key=runtime_profile_hmac_key,
            expected_key_id=runtime_profile_key_id,
            signing_identity_hash=runtime_signing_identity_hash,
        )
    runtime_reasons = (
        ()
        if runtime_binding is None
        else tuple(f"runtime_binding:{reason}" for reason in runtime_binding["reasons"])
    )
    reasons = (*feedback_reasons, *runtime_reasons)
    timestamp = observed_at()
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise Px4MissionFeedbackObservationError(
            "observed_at must return a timezone-aware datetime"
        )

    transport_document: dict[str, object] = {
        "configured_endpoint": configured_endpoint,
        "resolved_endpoint": resolved_endpoint,
        "baud": baud,
        "expected_system_id": feedback.expected_system_id,
        "expected_component_id": feedback.expected_component_id,
        "read_only": read_only,
        "messages_received": messages_received,
        "messages_transmitted": transmit_counters.total,
        "message_type_counts": message_type_counts,
    }
    if transmit_counters.explicit:
        transport_document.update(
            {
                "telemetry_request_messages_transmitted": transmit_counters.telemetry_requests,
                "application_messages_transmitted": transmit_counters.application,
                "flight_control_messages_transmitted": transmit_counters.flight_control,
            }
        )
    result: dict[str, object] = {
        "schema_version": (
            PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION
            if runtime_binding is not None
            else PX4_MISSION_FEEDBACK_OBSERVATION_LEGACY_SCHEMA_VERSION
        ),
        "event": PX4_MISSION_FEEDBACK_OBSERVATION_EVENT,
        "runtime_mode": mode,
        "observed_at_utc": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "mission": {
            "mission_id": mission.mission_id,
            "mission_hash": mission.canonical_hash,
            "mission_item_count": len(mission.items),
            **(
                {"airframe_profile_hash": mission.airframe_profile_hash}
                if runtime_binding is not None
                else {}
            ),
        },
        "sampling": {
            "duration_s": duration,
            "poll_interval_s": poll_interval,
            "started_at_s": started_at_s,
            "completed_at_s": completed_at_s,
            "sample_count": sample_count,
            "receive_error_count": receive_error_count,
            "receive_error_samples": receive_error_samples,
        },
        "transport": transport_document,
        "feedback": feedback.to_mapping(),
        "feedback_sha256": feedback.canonical_hash,
        "feedback_gate_passed": not reasons,
        "reasons": reasons,
        "claims": {
            "source_kind": "live_mavlink_receive_only",
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "operator_visual_acceptance": False,
        },
    }
    if runtime_binding is not None:
        result["runtime_binding"] = runtime_binding
    result["evidence_sha256"] = canonical_sha256(result)
    return verify_px4_mission_feedback_observation(result, expected_mission=mission)


def verify_px4_mission_feedback_observation(
    raw: object,
    *,
    expected_mission: ExecutableMission | None = None,
    runtime_profile_path: str | Path | None = None,
) -> dict[str, object]:
    """Recompute all derived gates and hashes in one observation report."""

    try:
        if not isinstance(raw, Mapping):
            raise Px4MissionFeedbackObservationError(
                "Px4MissionFeedbackObservation must be an object"
            )
        schema_version = require_int(raw.get("schema_version"), "schema_version", minimum=1)
        if schema_version not in {
            PX4_MISSION_FEEDBACK_OBSERVATION_LEGACY_SCHEMA_VERSION,
            PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION,
        }:
            raise Px4MissionFeedbackObservationError(
                f"unsupported schema_version {schema_version}; expected 1 or 2"
            )
        required_fields = {
            "schema_version",
            "event",
            "runtime_mode",
            "observed_at_utc",
            "mission",
            "sampling",
            "transport",
            "feedback",
            "feedback_sha256",
            "feedback_gate_passed",
            "reasons",
            "claims",
            "evidence_sha256",
        }
        if schema_version == PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION:
            required_fields.add("runtime_binding")
        value = strict_object(
            raw,
            name="Px4MissionFeedbackObservation",
            required=frozenset(required_fields),
        )
        if require_text(value["event"], "event") != PX4_MISSION_FEEDBACK_OBSERVATION_EVENT:
            raise Px4MissionFeedbackObservationError("unsupported observation event")
        mode = _runtime_mode(value["runtime_mode"])
        _utc_timestamp(value["observed_at_utc"], "observed_at_utc")

        mission_required = {"mission_id", "mission_hash", "mission_item_count"}
        if schema_version == PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION:
            mission_required.add("airframe_profile_hash")
        mission_value = strict_object(
            value["mission"],
            name="mission",
            required=frozenset(mission_required),
        )
        mission_id = require_text(mission_value["mission_id"], "mission.mission_id")
        mission_hash = require_sha256(mission_value["mission_hash"], "mission.mission_hash")
        mission_item_count = require_int(
            mission_value["mission_item_count"],
            "mission.mission_item_count",
            minimum=1,
        )
        mission_airframe_profile_hash = (
            None
            if schema_version == PX4_MISSION_FEEDBACK_OBSERVATION_LEGACY_SCHEMA_VERSION
            else require_sha256(
                mission_value["airframe_profile_hash"],
                "mission.airframe_profile_hash",
            )
        )
        feedback = Px4MissionFeedbackSnapshot.from_mapping(value["feedback"])
        if (
            feedback.mission_id != mission_id
            or feedback.mission_hash != mission_hash
            or feedback.mission_item_count != mission_item_count
        ):
            raise Px4MissionFeedbackObservationError(
                "feedback identity does not match the report Mission"
            )
        if require_sha256(value["feedback_sha256"], "feedback_sha256") != feedback.canonical_hash:
            raise Px4MissionFeedbackObservationError("feedback_sha256 does not match feedback")
        if expected_mission is not None:
            if not isinstance(expected_mission, ExecutableMission):
                raise Px4MissionFeedbackObservationError(
                    "expected_mission must be an ExecutableMission"
                )
            if (
                mission_id != expected_mission.mission_id
                or mission_hash != expected_mission.canonical_hash
                or mission_item_count != len(expected_mission.items)
            ):
                raise Px4MissionFeedbackObservationError(
                    "report does not match the expected executable Mission"
                )
            if (
                mission_airframe_profile_hash is not None
                and mission_airframe_profile_hash != expected_mission.airframe_profile_hash
            ):
                raise Px4MissionFeedbackObservationError(
                    "report airframe profile does not match the expected Mission"
                )

        sampling = strict_object(
            value["sampling"],
            name="sampling",
            required=frozenset(
                {
                    "duration_s",
                    "poll_interval_s",
                    "started_at_s",
                    "completed_at_s",
                    "sample_count",
                    "receive_error_count",
                    "receive_error_samples",
                }
            ),
        )
        _bounded_real(
            sampling["duration_s"],
            "sampling.duration_s",
            minimum=0.0,
            maximum=PX4_MISSION_FEEDBACK_OBSERVATION_MAX_DURATION_S,
        )
        _bounded_real(
            sampling["poll_interval_s"],
            "sampling.poll_interval_s",
            minimum=0.001,
            maximum=5.0,
        )
        started = require_real(sampling["started_at_s"], "sampling.started_at_s", minimum=0.0)
        require_real(
            sampling["completed_at_s"],
            "sampling.completed_at_s",
            minimum=started,
        )
        require_int(sampling["sample_count"], "sampling.sample_count", minimum=1)
        receive_error_count = require_int(
            sampling["receive_error_count"],
            "sampling.receive_error_count",
            minimum=0,
        )
        error_samples = require_array(
            sampling["receive_error_samples"],
            "sampling.receive_error_samples",
        )
        if len(error_samples) > min(
            receive_error_count,
            PX4_MISSION_FEEDBACK_OBSERVATION_MAX_ERROR_SAMPLES,
        ):
            raise Px4MissionFeedbackObservationError(
                "receive_error_samples exceeds its bounded error count"
            )
        if receive_error_count > 0 and not error_samples:
            raise Px4MissionFeedbackObservationError(
                "receive errors require at least one bounded error sample"
            )
        if receive_error_count == 0 and error_samples:
            raise Px4MissionFeedbackObservationError(
                "receive_error_samples requires a receive error"
            )
        for index, sample in enumerate(error_samples):
            sample_value = strict_object(
                sample,
                name=f"sampling.receive_error_samples[{index}]",
                required=frozenset({"error_type", "message"}),
            )
            require_text(sample_value["error_type"], f"error sample {index} type")
            require_text(sample_value["message"], f"error sample {index} message")

        transport = strict_object(
            value["transport"],
            name="transport",
            required=frozenset(
                {
                    "configured_endpoint",
                    "resolved_endpoint",
                    "baud",
                    "expected_system_id",
                    "expected_component_id",
                    "read_only",
                    "messages_received",
                    "messages_transmitted",
                    "message_type_counts",
                }
            ),
            optional=frozenset(_TRANSMIT_COUNTER_FIELDS),
        )
        require_text(transport["configured_endpoint"], "transport.configured_endpoint")
        optional_text(transport["resolved_endpoint"], "transport.resolved_endpoint")
        require_int(transport["baud"], "transport.baud", minimum=1)
        for name in ("expected_system_id", "expected_component_id"):
            expected = transport[name]
            if expected is not None:
                require_int(expected, f"transport.{name}", minimum=1, maximum=255)
            if expected != getattr(feedback, name):
                raise Px4MissionFeedbackObservationError(
                    f"transport.{name} does not match feedback"
                )
        read_only = require_bool(transport["read_only"], "transport.read_only")
        messages_received = require_int(
            transport["messages_received"],
            "transport.messages_received",
            minimum=0,
        )
        transmit_counters = _normalise_transmit_counters(
            transport["messages_transmitted"],
            {field: transport[field] for field in _TRANSMIT_COUNTER_FIELDS if field in transport},
            context="transport",
        )
        counts = _message_type_counts(transport["message_type_counts"])
        if sum(counts.values()) != messages_received:
            raise Px4MissionFeedbackObservationError(
                "message_type_counts does not match messages_received"
            )
        relevant_count = sum(
            counts.get(name, 0) for name in ("MISSION_CURRENT", "MISSION_ITEM_REACHED")
        )
        if relevant_count < feedback.accepted_event_count + feedback.rejected_event_count:
            raise Px4MissionFeedbackObservationError(
                "message_type_counts under-report Mission feedback"
            )

        reasons = require_string_array(value["reasons"], "reasons")
        expected_reasons: tuple[str, ...] = _gate_reasons(
            feedback=feedback,
            read_only=read_only,
            counters=transmit_counters,
            receive_error_count=receive_error_count,
        )
        if schema_version == PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION:
            if mission_airframe_profile_hash is None:  # Defensive under python -O.
                raise Px4MissionFeedbackObservationError(
                    "v2 observation requires mission airframe identity"
                )
            runtime_binding = verify_px4_mission_feedback_runtime_binding(
                value["runtime_binding"],
                mission_airframe_profile_hash=mission_airframe_profile_hash,
                feedback=feedback,
                transport=transport,
                runtime_mode=mode,
                runtime_profile_path=runtime_profile_path,
            )
            expected_reasons = (
                *expected_reasons,
                *(f"runtime_binding:{reason}" for reason in runtime_binding["reasons"]),
            )
        elif runtime_profile_path is not None:
            raise Px4MissionFeedbackObservationError(
                "legacy v1 observation cannot bind a runtime profile file"
            )
        if reasons != expected_reasons:
            raise Px4MissionFeedbackObservationError(
                "reasons do not match the recomputed feedback gate"
            )
        gate_passed = require_bool(value["feedback_gate_passed"], "feedback_gate_passed")
        if gate_passed is not (not expected_reasons):
            raise Px4MissionFeedbackObservationError(
                "feedback_gate_passed does not match the recomputed gate"
            )

        claims = strict_object(
            value["claims"],
            name="claims",
            required=frozenset(
                {
                    "source_kind",
                    "release_eligible",
                    "hardware_acceptance",
                    "hil_acceptance",
                    "real_acceptance",
                    "operator_visual_acceptance",
                }
            ),
        )
        if require_text(claims["source_kind"], "claims.source_kind") != (
            "live_mavlink_receive_only"
        ):
            raise Px4MissionFeedbackObservationError("claims.source_kind is invalid")
        for name in (
            "release_eligible",
            "hardware_acceptance",
            "hil_acceptance",
            "real_acceptance",
            "operator_visual_acceptance",
        ):
            if require_bool(claims[name], f"claims.{name}") is not False:
                raise Px4MissionFeedbackObservationError(
                    f"claims.{name} must remain false for this sub-gate"
                )

        claimed_hash = require_sha256(value["evidence_sha256"], "evidence_sha256")
        without_hash = dict(value)
        del without_hash["evidence_sha256"]
        if canonical_sha256(without_hash) != claimed_hash:
            raise Px4MissionFeedbackObservationError(
                "evidence_sha256 does not match the observation report"
            )
    except Px4MissionFeedbackObservationError:
        raise
    except (AutonomyContractError, TypeError, ValueError) as exc:
        raise Px4MissionFeedbackObservationError(str(exc)) from exc
    return dict(value)


def write_px4_mission_feedback_observation(
    destination: str | Path,
    document: Mapping[str, object],
    *,
    overwrite: bool = True,
) -> Path:
    """Verify and atomically write stable UTF-8 observation JSON."""

    verified = verify_px4_mission_feedback_observation(document)
    output = Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise Px4MissionFeedbackObservationError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(verified, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output)
    except BaseException:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        finally:
            raise
    return output


def load_px4_mission_feedback_observation(
    path: str | Path,
    *,
    expected_mission: ExecutableMission | None = None,
    runtime_profile_path: str | Path | None = None,
) -> dict[str, object]:
    """Read and strictly verify one saved observation report."""

    source = Path(path).expanduser().resolve(strict=True)
    raw = strict_json_loads(source.read_bytes(), name="PX4 Mission feedback observation")
    return verify_px4_mission_feedback_observation(
        raw,
        expected_mission=expected_mission,
        runtime_profile_path=runtime_profile_path,
    )


def _gate_reasons(
    *,
    feedback: Px4MissionFeedbackSnapshot,
    read_only: bool,
    counters: _TransmitCounterBreakdown,
    receive_error_count: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if receive_error_count:
        reasons.append("mavlink_receive_errors")
    if not feedback.qualified:
        reasons.append(f"feedback:{feedback.reason}")
    if not read_only:
        reasons.append("provider_not_read_only")
    if counters.application or counters.flight_control:
        if counters.explicit:
            if counters.application:
                reasons.append("application_messages_transmitted")
            if counters.flight_control:
                reasons.append("flight_control_messages_transmitted")
        else:
            reasons.append("mavlink_messages_transmitted")
    return tuple(reasons)


def _error_sample(exc: Exception) -> dict[str, str]:
    message = str(exc).strip() or type(exc).__name__
    return {
        "error_type": type(exc).__name__,
        "message": message[:512],
    }


def _message_type_counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise Px4MissionFeedbackObservationError("message_type_counts must be an object")
    result: dict[str, int] = {}
    for key, value in raw.items():
        name = require_text(key, "message_type_counts key").upper()
        if name in result:
            raise Px4MissionFeedbackObservationError(
                "message_type_counts contains duplicate normalized keys"
            )
        result[name] = require_int(value, f"message_type_counts.{name}", minimum=0)
    return dict(sorted(result.items()))


def _runtime_mode(value: object) -> str:
    mode = require_text(value, "runtime_mode").lower()
    if mode not in _RUNTIME_MODES:
        raise Px4MissionFeedbackObservationError("runtime_mode must be one of: sim, hil, real")
    return mode


def _clock_value(value: object, name: str) -> float:
    return _bounded_real(value, name, minimum=0.0)


def _bounded_real(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        return require_real(value, name, minimum=minimum, maximum=maximum)
    except AutonomyContractError as exc:
        raise Px4MissionFeedbackObservationError(str(exc)) from exc


def _utc_timestamp(value: object, name: str) -> datetime:
    text = require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Px4MissionFeedbackObservationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Px4MissionFeedbackObservationError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _runtime_binding_arguments(args: argparse.Namespace) -> dict[str, object]:
    profile_path = args.runtime_profile
    hmac_key_env = args.runtime_profile_hmac_key_env
    key_id = args.runtime_profile_key_id
    signing_hash = args.runtime_signing_identity_hash
    signing_key_env = args.runtime_signing_key_hex_env
    supplied = (
        profile_path is not None
        or hmac_key_env is not None
        or key_id is not None
        or signing_hash is not None
        or signing_key_env is not None
    )
    if args.runtime_mode == "sim":
        if supplied:
            raise Px4MissionFeedbackObservationError(
                "runtime profile evidence is reserved for HIL/REAL observations"
            )
        return {}
    missing = [
        option
        for option, value in (
            ("--runtime-profile", profile_path),
            ("--runtime-profile-hmac-key-env", hmac_key_env),
            ("--runtime-profile-key-id", key_id),
        )
        if value in (None, "")
    ]
    if missing:
        raise Px4MissionFeedbackObservationError(
            f"{args.runtime_mode.upper()} observation requires " + ", ".join(missing)
        )
    variable_name = require_text(
        hmac_key_env,
        "runtime profile HMAC key environment variable",
    )
    raw_hmac_key = os.environ.get(variable_name)
    if raw_hmac_key is None or not raw_hmac_key:
        raise Px4MissionFeedbackObservationError(
            f"runtime profile HMAC key environment variable is missing: {variable_name}"
        )
    if signing_hash is not None and signing_key_env is not None:
        raise Px4MissionFeedbackObservationError(
            "--runtime-signing-identity-hash and --runtime-signing-key-hex-env "
            "are mutually exclusive"
        )
    if signing_key_env is not None:
        signing_variable = require_text(
            signing_key_env,
            "runtime signing key environment variable",
        )
        raw_signing_key = os.environ.get(signing_variable)
        if raw_signing_key is None or not raw_signing_key.strip():
            raise Px4MissionFeedbackObservationError(
                f"runtime signing key environment variable is missing: {signing_variable}"
            )
        signing_hash = signing_identity_hash_from_hex(raw_signing_key)
    if args.runtime_mode == "real" and signing_hash is None:
        raise Px4MissionFeedbackObservationError(
            "REAL observation requires --runtime-signing-identity-hash or "
            "--runtime-signing-key-hex-env"
        )
    if args.runtime_mode == "hil" and signing_hash is not None:
        raise Px4MissionFeedbackObservationError(
            "HIL observation cannot carry a REAL signing identity"
        )
    return {
        "runtime_profile_path": profile_path,
        "runtime_profile_hmac_key": raw_hmac_key.encode("utf-8"),
        "runtime_profile_key_id": require_text(key_id, "runtime profile key_id"),
        "runtime_signing_identity_hash": signing_hash,
    }


def _observer_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="capture hash-bound receive-only PX4 Mission progress feedback"
    )
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--baud", type=int, default=57_600)
    parser.add_argument("--runtime-mode", choices=sorted(_RUNTIME_MODES), required=True)
    parser.add_argument(
        "--runtime-profile",
        type=Path,
        help="signed runtime-profile-document bound to this HIL/REAL observation",
    )
    parser.add_argument(
        "--runtime-profile-hmac-key-env",
        help="environment variable containing the runtime profile HMAC key",
    )
    parser.add_argument(
        "--runtime-profile-key-id",
        help="expected key_id in the signed runtime profile document",
    )
    parser.add_argument(
        "--runtime-signing-identity-hash",
        help="SHA-256 identity of the REAL MAVLink signing key",
    )
    parser.add_argument(
        "--runtime-signing-key-hex-env",
        help="environment variable containing the REAL MAVLink signing key in hex",
    )
    parser.add_argument("--expected-system-id", type=int, default=1)
    parser.add_argument("--expected-component-id", type=int, default=1)
    parser.add_argument(
        "--request-autopilot-version",
        action="store_true",
        help="request a bounded AUTOPILOT_VERSION telemetry response for identity binding",
    )
    parser.add_argument(
        "--maximum-feedback-age-seconds",
        type=float,
        default=PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S,
    )
    parser.add_argument("--provider-stale-after-seconds", type=float, default=1.5)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.05)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="write an unqualified diagnostic report with exit code 0",
    )
    return parser


def _verifier_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="verify a PX4 Mission feedback observation report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--mission", type=Path)
    parser.add_argument(
        "--runtime-profile",
        type=Path,
        help="reopen and hash-check the signed profile bound by a v2 report",
    )
    parser.add_argument(
        "--allow-unqualified",
        action="store_true",
        help="return success after structural verification when the feedback gate is closed",
    )
    return parser


def _provision_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "derive and sign one HIL/REAL runtime profile from a fresh, receive-only PX4 heartbeat"
        )
    )
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--baud", type=int, default=57_600)
    parser.add_argument("--runtime-mode", choices=sorted(_PROFILE_RUNTIME_MODES), required=True)
    parser.add_argument("--runtime-profile-hmac-key-env", required=True)
    parser.add_argument("--runtime-profile-key-id", required=True)
    parser.add_argument("--runtime-signing-identity-hash")
    parser.add_argument(
        "--runtime-signing-key-hex-env",
        help="environment variable containing the REAL MAVLink signing key in hex",
    )
    parser.add_argument("--expected-system-id", type=int, default=1)
    parser.add_argument("--expected-component-id", type=int, default=1)
    parser.add_argument(
        "--request-autopilot-version",
        action="store_true",
        help="request a bounded AUTOPILOT_VERSION telemetry response during provisioning",
    )
    parser.add_argument("--provider-stale-after-seconds", type=float, default=1.5)
    parser.add_argument("--probe-duration-seconds", type=float, default=10.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.05)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing profile only after the new file verifies",
    )
    return parser


def provision_main(argv: list[str] | None = None) -> int:
    args = _provision_parser().parse_args(argv)
    provider: PixhawkReadOnlyTelemetryProvider | None = None
    try:
        hmac_variable = require_text(
            args.runtime_profile_hmac_key_env,
            "runtime profile HMAC key environment variable",
        )
        hmac_value = os.environ.get(hmac_variable)
        if hmac_value is None or not hmac_value:
            raise Px4MissionFeedbackObservationError(
                f"runtime profile HMAC key environment variable is missing: {hmac_variable}"
            )
        hmac_key = hmac_value.encode("utf-8")

        signing_hash = args.runtime_signing_identity_hash
        signing_key_environment = args.runtime_signing_key_hex_env
        if signing_hash is not None and signing_key_environment is not None:
            raise Px4MissionFeedbackObservationError(
                "--runtime-signing-identity-hash and --runtime-signing-key-hex-env "
                "are mutually exclusive"
            )
        if signing_key_environment is not None:
            variable = require_text(
                signing_key_environment,
                "runtime signing key environment variable",
            )
            signing_key = os.environ.get(variable)
            if signing_key is None or not signing_key.strip():
                raise Px4MissionFeedbackObservationError(
                    f"runtime signing key environment variable is missing: {variable}"
                )
            signing_hash = signing_identity_hash_from_hex(signing_key)

        mission = load_executable_mission_document(args.mission)
        provider = PixhawkReadOnlyTelemetryProvider(
            PixhawkReadOnlyConfig(
                endpoint=args.endpoint,
                baud=args.baud,
                stale_after_seconds=args.provider_stale_after_seconds,
                expected_system_id=args.expected_system_id,
                expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS["px4"],
                require_operational_state=True,
                request_ranging_streams=False,
                request_autopilot_version=args.request_autopilot_version,
            )
        )
        provision = provision_px4_mission_feedback_runtime_profile(
            mission,
            provider,
            runtime_mode=args.runtime_mode,
            hmac_key=hmac_key,
            key_id=args.runtime_profile_key_id,
            signing_identity_hash=signing_hash,
            expected_system_id=args.expected_system_id,
            expected_component_id=args.expected_component_id,
            duration_s=args.probe_duration_seconds,
            poll_interval_s=args.poll_interval_seconds,
        )
        output = write_px4_mission_feedback_runtime_profile(
            args.out,
            provision,
            hmac_key=hmac_key,
            overwrite=args.overwrite,
        )
        document_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
        print(
            json.dumps(
                provision.summary(path=output, document_sha256=document_sha256),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError, AutonomyContractError) as exc:
        print(
            json.dumps(
                {
                    "event": "px4_runtime_profile_provision_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if provider is not None:
            provider.close()


def main(argv: list[str] | None = None) -> int:
    resolved_argv = list(sys.argv[1:] if argv is None else argv)
    if resolved_argv[:1] == ["verify"]:
        return verify_main(resolved_argv[1:])
    if resolved_argv[:1] == ["provision"]:
        return provision_main(resolved_argv[1:])
    args = _observer_parser().parse_args(resolved_argv)
    provider: PixhawkReadOnlyTelemetryProvider | None = None
    try:
        mission = load_executable_mission_document(args.mission)
        runtime_binding_arguments = _runtime_binding_arguments(args)
        tracker = Px4MissionFeedbackTracker(
            mission,
            config=Px4MissionFeedbackConfig(
                maximum_age_s=args.maximum_feedback_age_seconds,
                expected_system_id=args.expected_system_id,
                expected_component_id=args.expected_component_id,
            ),
        )
        provider = PixhawkReadOnlyTelemetryProvider(
            PixhawkReadOnlyConfig(
                endpoint=args.endpoint,
                baud=args.baud,
                stale_after_seconds=args.provider_stale_after_seconds,
                expected_system_id=args.expected_system_id,
                expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS["px4"],
                request_ranging_streams=False,
                request_autopilot_version=args.request_autopilot_version,
            ),
            mission_feedback_tracker=tracker,
        )
        document = observe_px4_mission_feedback(
            mission,
            provider,
            runtime_mode=args.runtime_mode,
            duration_s=args.duration_seconds,
            poll_interval_s=args.poll_interval_seconds,
            **runtime_binding_arguments,
        )
        output = write_px4_mission_feedback_observation(args.out, document)
        print(
            json.dumps(
                {
                    "event": "px4_mission_feedback_observation_written",
                    "path": str(output),
                    "feedback_gate_passed": document["feedback_gate_passed"],
                    "evidence_sha256": document["evidence_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if document["feedback_gate_passed"] or args.report_only else 1
    except (OSError, ValueError, AutonomyContractError) as exc:
        print(
            json.dumps(
                {
                    "event": "px4_mission_feedback_observation_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if provider is not None:
            provider.close()


def verify_main(argv: list[str] | None = None) -> int:
    args = _verifier_parser().parse_args(argv)
    try:
        mission = None if args.mission is None else load_executable_mission_document(args.mission)
        document = load_px4_mission_feedback_observation(
            args.report,
            expected_mission=mission,
            runtime_profile_path=args.runtime_profile,
        )
        print(
            json.dumps(
                {
                    "event": "px4_mission_feedback_observation_verified",
                    "feedback_gate_passed": document["feedback_gate_passed"],
                    "evidence_sha256": document["evidence_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if document["feedback_gate_passed"] or args.allow_unqualified else 1
    except (OSError, ValueError, AutonomyContractError) as exc:
        print(
            json.dumps(
                {
                    "event": "px4_mission_feedback_verification_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


__all__ = [
    "PX4_MISSION_FEEDBACK_OBSERVATION_EVENT",
    "PX4_MISSION_FEEDBACK_OBSERVATION_LEGACY_SCHEMA_VERSION",
    "PX4_MISSION_FEEDBACK_OBSERVATION_MAX_DURATION_S",
    "PX4_MISSION_FEEDBACK_OBSERVATION_SCHEMA_VERSION",
    "PX4_MISSION_FEEDBACK_PROFILE_MAX_PROBE_DURATION_S",
    "PX4_MISSION_FEEDBACK_PROFILE_PROVISION_EVENT",
    "Px4MissionFeedbackObservationError",
    "Px4RuntimeProfileProvision",
    "load_px4_mission_feedback_observation",
    "main",
    "observe_px4_mission_feedback",
    "provision_main",
    "provision_px4_mission_feedback_runtime_profile",
    "verify_main",
    "verify_px4_mission_feedback_observation",
    "write_px4_mission_feedback_observation",
    "write_px4_mission_feedback_runtime_profile",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

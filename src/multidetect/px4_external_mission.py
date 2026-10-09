"""External PX4 Mission transport for an already-owned SITL connection.

This module is deliberately below policy selection and above the MAVLink
socket.  The caller must provide a compiled mission and a connection that was
already inspected as an owned, disposable PX4/Gazebo runtime.  It performs
only the hash-bound Mission transaction (upload, INT readback, commit and
start permit); it does not create containers, arm vehicles, select modes or
emit actuator commands.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from inspect import getattr_static
from pathlib import Path
from typing import Any

from .autonomy import ContextSnapshot, PlatformNeutralPlan
from .mission_journal import (
    DurableMissionJournal,
    MissionJournalError,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from .px4_mission import (
    MAV_MISSION_TYPE_MISSION,
    PX4_AUTOPILOT_COMPONENT_ID,
    PX4_SYSTEM_ID,
    CompiledPx4Mission,
    MissionUploadPlan,
    Px4FixedWingSafetyPolicy,
    Px4MissionError,
    Px4MissionItemInt,
    Px4MissionSafetyPolicy,
    Px4MissionTransaction,
    Px4MultirotorSafetyPolicy,
    normalize_px4_vehicle_class,
    px4_wire_hash,
)
from .px4_mission_acceptance import _build_transaction_documents

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_PROFILE_BY_VEHICLE = {
    "vtol": "px4_vtol_gazebo_v1",
    "fixed-wing": "px4_fixed_wing_gazebo_v1",
    "multirotor": "px4_multirotor_gazebo_v1",
}


@dataclass(frozen=True, slots=True)
class ExternalRuntimeAttestation:
    """Proof that the injected connection belongs to an owned SIM runtime."""

    container_id: str
    image: str
    vehicle_class: str
    airframe_profile_id: str
    scenario_id: str
    scenario_manifest_sha256: str
    ownership_checks: tuple[tuple[str, bool], ...]
    telemetry_qualified: bool
    truth_free: bool
    hardware_control_enabled: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ExternalRuntimeAttestation:
        if not isinstance(value, Mapping):
            raise ExternalPx4MissionError("runtime attestation must be an object")
        required = {
            "container_id",
            "image",
            "vehicle_class",
            "airframe_profile_id",
            "scenario_id",
            "scenario_manifest_sha256",
            "ownership_checks",
            "telemetry_qualified",
            "truth_free",
            "hardware_control_enabled",
        }
        if set(value) != required:
            raise ExternalPx4MissionError("runtime attestation fields are not exact")
        vehicle = normalize_px4_vehicle_class(str(value["vehicle_class"]))
        container_id = str(value["container_id"]).lower()
        image = str(value["image"])
        profile = str(value["airframe_profile_id"])
        scenario_id = str(value["scenario_id"])
        manifest_hash = str(value["scenario_manifest_sha256"]).lower()
        if _CONTAINER_ID_RE.fullmatch(container_id) is None:
            raise ExternalPx4MissionError("runtime attestation container_id must be 64 hex")
        if _IMAGE_DIGEST_RE.fullmatch(image) is None:
            raise ExternalPx4MissionError("runtime attestation image must be digest-pinned")
        if profile != _PROFILE_BY_VEHICLE[vehicle]:
            raise ExternalPx4MissionError("runtime attestation profile does not match vehicle")
        if not scenario_id.strip() or _SHA256_RE.fullmatch(manifest_hash) is None:
            raise ExternalPx4MissionError("runtime attestation scenario identity is invalid")
        checks = value["ownership_checks"]
        if (
            not isinstance(checks, Mapping)
            or not checks
            or any(type(flag) is not bool or flag is not True for flag in checks.values())
        ):
            raise ExternalPx4MissionError("runtime ownership checks must all be true")
        if value["telemetry_qualified"] is not True or value["truth_free"] is not True:
            raise ExternalPx4MissionError("runtime telemetry/truth-free attestation failed")
        if value["hardware_control_enabled"] is not False:
            raise ExternalPx4MissionError("runtime hardware control must be disabled")
        return cls(
            container_id=container_id,
            image=image,
            vehicle_class=vehicle,
            airframe_profile_id=profile,
            scenario_id=scenario_id,
            scenario_manifest_sha256=manifest_hash,
            ownership_checks=tuple(sorted((str(k), True) for k in checks)),
            telemetry_qualified=True,
            truth_free=True,
            hardware_control_enabled=False,
        )

    @property
    def canonical_hash(self) -> str:
        payload = {
            "container_id": self.container_id,
            "image": self.image,
            "vehicle_class": self.vehicle_class,
            "airframe_profile_id": self.airframe_profile_id,
            "scenario_id": self.scenario_id,
            "scenario_manifest_sha256": self.scenario_manifest_sha256,
            "ownership_checks": dict(self.ownership_checks),
            "telemetry_qualified": self.telemetry_qualified,
            "truth_free": self.truth_free,
            "hardware_control_enabled": self.hardware_control_enabled,
        }
        from .autonomy.serialization import canonical_sha256

        return canonical_sha256(payload)

    def to_mapping(self) -> dict[str, object]:
        return {
            "container_id": self.container_id,
            "image": self.image,
            "vehicle_class": self.vehicle_class,
            "airframe_profile_id": self.airframe_profile_id,
            "scenario_id": self.scenario_id,
            "scenario_manifest_sha256": self.scenario_manifest_sha256,
            "ownership_checks": dict(self.ownership_checks),
            "telemetry_qualified": self.telemetry_qualified,
            "truth_free": self.truth_free,
            "hardware_control_enabled": self.hardware_control_enabled,
            "attestation_sha256": self.canonical_hash,
        }


def attest_owned_px4_container(
    record: Mapping[str, object],
    *,
    expected_container_id: str,
    expected_image: str,
    vehicle_class: str,
    scenario_id: str,
    scenario_manifest_sha256: str,
    expected_model: str,
    expected_purpose_label: str,
    control_port: int,
    telemetry_qualified: bool,
    expected_readonly_mounts: Mapping[str, str] | None = None,
) -> ExternalRuntimeAttestation:
    """Convert one Docker inspect record into a strict runtime attestation.

    This function is read-only.  It neither starts nor stops a container and
    it does not open a MAVLink connection.  A caller must perform telemetry
    qualification separately and pass its result explicitly.
    """

    if not isinstance(record, Mapping):
        raise ExternalPx4MissionError("Docker inspect record must be an object")
    config = record.get("Config")
    host_config = record.get("HostConfig")
    state = record.get("State")
    if (
        not isinstance(config, Mapping)
        or not isinstance(host_config, Mapping)
        or not isinstance(state, Mapping)
    ):
        raise ExternalPx4MissionError("Docker inspect record lacks runtime sections")
    labels = config.get("Labels")
    environment = config.get("Env")
    if not isinstance(labels, Mapping) or not isinstance(environment, list):
        raise ExternalPx4MissionError("Docker inspect labels/environment are malformed")
    bindings = host_config.get("PortBindings") or {}
    expected_binding = {
        f"{control_port}/udp": [{"HostIp": "127.0.0.1", "HostPort": str(control_port)}]
    }
    checks = {
        "container_identity": record.get("Id") == expected_container_id,
        "running": state.get("Running") is True,
        "pinned_image": config.get("Image") == expected_image,
        "purpose_label": labels.get("multidetect.purpose") == expected_purpose_label,
        "expected_model": f"PX4_SIM_MODEL={expected_model}" in environment,
        "bridge_network": host_config.get("NetworkMode") == "bridge",
        "not_privileged": host_config.get("Privileged") is False,
        "no_device_mapping": not host_config.get("Devices"),
        "exact_loopback_control_port": bindings == expected_binding,
    }
    if expected_readonly_mounts is None:
        # The strict legacy form is still useful for a connection that is not
        # launched by the repository Docker planner: it must have no mounts.
        checks["no_host_mounts"] = not record.get("Mounts")
    else:
        # The repository-owned Docker runtime intentionally bind-mounts exactly
        # three read-only assets.  Accept only that exact set; arbitrary host
        # mounts remain rejected rather than weakening the ownership proof.
        observed_mounts = record.get("Mounts")
        observed_entries = observed_mounts if isinstance(observed_mounts, list) else []
        observed_by_target = {
            item.get("Destination"): item
            for item in observed_entries
            if isinstance(item, Mapping) and isinstance(item.get("Destination"), str)
        }

        def _normalized_path(value: object) -> str:
            if not isinstance(value, str) or not value:
                return ""
            return os.path.normcase(os.path.abspath(os.path.normpath(value)))

        expected = {
            str(target): _normalized_path(source)
            for target, source in expected_readonly_mounts.items()
        }
        checks["expected_readonly_mounts"] = (
            isinstance(observed_mounts, list)
            and len(observed_mounts) == len(expected)
            and set(observed_by_target) == set(expected)
            and all(
                _normalized_path(observed_by_target[target].get("Source")) == source
                and observed_by_target[target].get("RW") is False
                for target, source in expected.items()
            )
        )
    if not all(checks.values()):
        failed = ", ".join(name for name, passed in checks.items() if not passed)
        raise ExternalPx4MissionError(f"owned PX4 container attestation failed: {failed}")
    return ExternalRuntimeAttestation.from_mapping(
        {
            "container_id": expected_container_id,
            "image": expected_image,
            "vehicle_class": vehicle_class,
            "airframe_profile_id": _PROFILE_BY_VEHICLE[normalize_px4_vehicle_class(vehicle_class)],
            "scenario_id": scenario_id,
            "scenario_manifest_sha256": scenario_manifest_sha256,
            "ownership_checks": checks,
            "telemetry_qualified": telemetry_qualified,
            "truth_free": True,
            "hardware_control_enabled": False,
        }
    )


class ExternalPx4MissionError(RuntimeError):
    """Raised when an external Mission protocol invariant is not proven."""


def _now(clock: Callable[[], float] | None) -> float:
    return float(clock()) if clock is not None else time.monotonic()


def _receive(
    connection: Any,
    message_types: Sequence[str],
    timeout_s: float,
    *,
    receive: Callable[[float], Any | None] | None,
    clock: Callable[[], float] | None,
) -> Any | None:
    if receive is None:
        return connection.recv_match(
            type=list(message_types), blocking=True, timeout=float(timeout_s)
        )
    deadline = _now(clock) + float(timeout_s)
    while (remaining := deadline - _now(clock)) > 0:
        message = receive(remaining)
        if message is None:
            return None
        if str(message.get_type()) in message_types:
            return message
    return None


def _source(message: Any, message_type: str) -> tuple[int, int]:
    try:
        identity = int(message.get_srcSystem()), int(message.get_srcComponent())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExternalPx4MissionError(f"{message_type} has no PX4 source identity") from exc
    expected = (PX4_SYSTEM_ID, PX4_AUTOPILOT_COMPONENT_ID)
    if identity != expected:
        raise ExternalPx4MissionError(
            f"{message_type} source identity mismatch: observed {identity[0]}/{identity[1]}"
        )
    return identity


def _validated_gcs_identity(connection: Any, gcs_identity: tuple[int, int]) -> tuple[int, int]:
    """Bind declared source metadata; only metadata-free legacy fixtures bypass it."""

    if (
        type(gcs_identity) is not tuple
        or len(gcs_identity) != 2
        or any(type(value) is not int or not 1 <= value <= 255 for value in gcs_identity)
    ):
        raise ExternalPx4MissionError("gcs_identity must be a pair of nonzero byte integers")
    missing = object()

    def source_value(owner: Any, name: str) -> Any:
        # Dynamic send-method recorders must not manufacture identity metadata.
        if getattr_static(owner, name, missing) is missing:
            return missing
        try:
            return getattr(owner, name)
        except AttributeError as exc:
            raise ExternalPx4MissionError("declared GCS identity metadata is unavailable") from exc

    mav = getattr(connection, "mav", missing)
    actual = (source_value(mav, "srcSystem"), source_value(mav, "srcComponent"))
    has_mav_source = any(value is not missing for value in actual)
    if not has_mav_source and gcs_identity != (250, 191):
        raise ExternalPx4MissionError("explicit GCS identity requires actual MAVLink source")
    if has_mav_source and (
        any(type(value) is not int for value in actual) or actual != gcs_identity
    ):
        raise ExternalPx4MissionError("GCS identity differs from MAVLink source")
    source = (
        source_value(connection, "source_system"),
        source_value(connection, "source_component"),
    )
    if any(value is not missing for value in source) and (
        any(type(value) is not int for value in source) or source != gcs_identity
    ):
        raise ExternalPx4MissionError("GCS identity differs from connection source")
    # The default alone supports legacy fixtures with no exposed source metadata.
    return gcs_identity


def _target_is_gcs(
    message: Any,
    message_type: str,
    gcs_identity: tuple[int, int] = (250, 191),
) -> bool:
    try:
        target = message.target_system, message.target_component
    except AttributeError as exc:
        raise ExternalPx4MissionError(f"{message_type} has no target identity") from exc
    if any(type(value) is not int or not 0 <= value <= 255 for value in target):
        raise ExternalPx4MissionError(f"{message_type} has an invalid target identity")
    # Broadcasts and other GCS components must not advance this transaction.
    return target == gcs_identity


def _mission_type(message: Any) -> int:
    return int(getattr(message, "mission_type", MAV_MISSION_TYPE_MISSION))


def upload_mission_int(
    connection: Any,
    transaction: Px4MissionTransaction,
    upload_plan: MissionUploadPlan,
    *,
    message_timeout_s: float = 1.5,
    maximum_duration_s: float = 30.0,
    maximum_idle_retries: int = 6,
    receive: Callable[[float], Any | None] | None = None,
    clock: Callable[[], float] | None = None,
    now_utc: datetime | None = None,
    gcs_identity: tuple[int, int] = (250, 191),
) -> dict[str, Any]:
    """Upload one canonical mission using only ``MISSION_REQUEST_INT``."""

    gcs_identity = _validated_gcs_identity(connection, gcs_identity)
    connection.mav.mission_count_send(*upload_plan.count_send_args())
    requested_sequences: list[int] = []
    request_types: list[str] = []
    retries = 0
    premature_ack_count = 0
    deadline = _now(clock) + maximum_duration_s
    while _now(clock) < deadline:
        message = _receive(
            connection,
            ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"),
            min(message_timeout_s, max(0.0, deadline - _now(clock))),
            receive=receive,
            clock=clock,
        )
        if message is None:
            retries += 1
            if retries > maximum_idle_retries:
                raise ExternalPx4MissionError("MISSION upload timed out after bounded retries")
            connection.mav.mission_count_send(*upload_plan.count_send_args())
            continue
        message_type = str(message.get_type())
        _source(message, message_type)
        if not _target_is_gcs(message, message_type, gcs_identity):
            continue
        if _mission_type(message) != MAV_MISSION_TYPE_MISSION:
            raise ExternalPx4MissionError("MISSION packet has a non-primary mission_type")
        if message_type in {"MISSION_REQUEST", "MISSION_REQUEST_INT"}:
            if message_type != "MISSION_REQUEST_INT":
                raise ExternalPx4MissionError("legacy MISSION_REQUEST is forbidden")
            sequence = int(message.seq)
            item = transaction.mission_item_for_request(
                sequence, request_message_type=message_type, now_utc=now_utc
            )
            connection.mav.mission_item_int_send(*item.to_send_args())
            requested_sequences.append(sequence)
            request_types.append(message_type)
            continue
        if len(set(requested_sequences)) < upload_plan.item_count:
            # A back-to-back Mission replacement can leave the previous PX4
            # MISSION_ACK in the MAVLink receive queue.  It is not an ACK for
            # this upload because PX4 has not requested every item yet.  Keep
            # waiting for the bounded request sequence; a persistent stream of
            # premature ACKs still fails closed below.
            premature_ack_count += 1
            if premature_ack_count > maximum_idle_retries:
                raise ExternalPx4MissionError(
                    "MISSION upload received too many premature ACKs before "
                    "all mission items were requested"
                )
            continue
        acknowledgement = int(message.type)
        transaction.accept_upload_ack(
            ack_type=acknowledgement,
            mission_type=_mission_type(message),
            source_system=PX4_SYSTEM_ID,
            source_component=PX4_AUTOPILOT_COMPONENT_ID,
            now_utc=now_utc,
        )
        return {
            "acknowledged": True,
            "acknowledgement_code": acknowledgement,
            "request_sequences": requested_sequences,
            "request_message_types": request_types,
            "idle_retry_count": retries,
            "premature_ack_count": premature_ack_count,
            "int_only": True,
        }
    raise ExternalPx4MissionError("MISSION upload exceeded its maximum duration")


def _item_from_message(
    message: Any, gcs_identity: tuple[int, int] = (250, 191)
) -> Px4MissionItemInt:
    message_type = str(message.get_type())
    if message_type != "MISSION_ITEM_INT":
        raise ExternalPx4MissionError("legacy MISSION_ITEM readback is forbidden")
    _source(message, message_type)
    if not _target_is_gcs(message, message_type, gcs_identity):
        raise ExternalPx4MissionError("MISSION_ITEM_INT target identity mismatch")
    raw_current = message.current
    if (
        isinstance(raw_current, bool)
        or not isinstance(raw_current, int)
        or raw_current not in (0, 1)
    ):
        raise ExternalPx4MissionError("MISSION_ITEM_INT current must be an integer 0 or 1")
    sequence = int(message.seq)
    # PX4 readback describes the live execution cursor, not the initial cursor
    # used in our canonical upload.  Compare immutable route content with the
    # upload's canonical marker; actual progress still comes from MISSION_CURRENT.
    return Px4MissionItemInt(
        target_system=PX4_SYSTEM_ID,
        target_component=PX4_AUTOPILOT_COMPONENT_ID,
        sequence=sequence,
        frame=int(message.frame),
        command=int(message.command),
        current=1 if sequence == 0 else 0,
        autocontinue=int(message.autocontinue),
        param1=float(message.param1),
        param2=float(message.param2),
        param3=float(message.param3),
        param4=float(message.param4),
        x=int(message.x),
        y=int(message.y),
        z=float(message.z),
        mission_type=_mission_type(message),
    )


def readback_mission_int(
    connection: Any,
    transaction: Px4MissionTransaction,
    upload_plan: MissionUploadPlan,
    *,
    message_timeout_s: float = 1.5,
    maximum_retries_per_item: int = 5,
    receive: Callable[[float], Any | None] | None = None,
    clock: Callable[[], float] | None = None,
    now_utc: datetime | None = None,
    gcs_identity: tuple[int, int] = (250, 191),
) -> dict[str, Any]:
    """Read the stored mission back with ``MISSION_REQUEST_INT`` only."""

    gcs_identity = _validated_gcs_identity(connection, gcs_identity)
    connection.mav.mission_request_list_send(
        PX4_SYSTEM_ID, PX4_AUTOPILOT_COMPONENT_ID, MAV_MISSION_TYPE_MISSION
    )
    deadline = _now(clock) + max(3.0, message_timeout_s * 4.0)
    count_message = None
    while _now(clock) < deadline:
        candidate = _receive(
            connection,
            ("MISSION_COUNT",),
            min(message_timeout_s, max(0.0, deadline - _now(clock))),
            receive=receive,
            clock=clock,
        )
        if candidate is None:
            continue
        _source(candidate, "MISSION_COUNT")
        if not _target_is_gcs(candidate, "MISSION_COUNT", gcs_identity):
            continue
        count_message = candidate
        break
    if count_message is None:
        raise ExternalPx4MissionError("MISSION readback did not return MISSION_COUNT")
    if _mission_type(count_message) != MAV_MISSION_TYPE_MISSION:
        raise ExternalPx4MissionError("MISSION_COUNT has a non-primary mission_type")
    count = int(count_message.count)
    if count != upload_plan.item_count:
        raise ExternalPx4MissionError(
            f"MISSION_COUNT mismatch: expected {upload_plan.item_count}, got {count}"
        )

    items: list[Px4MissionItemInt] = []
    raw_execution_current_flags: list[int] = []
    retries = 0
    for expected_sequence in range(count):
        item = None
        for attempt in range(maximum_retries_per_item + 1):
            connection.mav.mission_request_int_send(
                PX4_SYSTEM_ID,
                PX4_AUTOPILOT_COMPONENT_ID,
                expected_sequence,
                MAV_MISSION_TYPE_MISSION,
            )
            message = _receive(
                connection,
                ("MISSION_ITEM_INT", "MISSION_ITEM", "MISSION_ACK"),
                message_timeout_s,
                receive=receive,
                clock=clock,
            )
            if message is None:
                retries += 1
                continue
            message_type = str(message.get_type())
            _source(message, message_type)
            if not _target_is_gcs(message, message_type, gcs_identity):
                retries += 1
                continue
            if message_type == "MISSION_ITEM":
                raise ExternalPx4MissionError("legacy MISSION_ITEM readback is forbidden")
            if message_type == "MISSION_ACK":
                raise ExternalPx4MissionError("PX4 aborted MISSION readback with ACK")
            candidate = _item_from_message(message, gcs_identity)
            if candidate.sequence != expected_sequence:
                retries += 1
                if attempt >= maximum_retries_per_item:
                    raise ExternalPx4MissionError("MISSION_ITEM_INT sequence mismatch")
                continue
            item = candidate
            break
        if item is None:
            raise ExternalPx4MissionError(
                f"MISSION_ITEM_INT readback timed out for sequence {expected_sequence}"
            )
        items.append(item)
        raw_execution_current_flags.append(message.current)

    readback_hash = px4_wire_hash(tuple(items))
    try:
        verified_hash = transaction.verify_readback(tuple(items), now_utc=now_utc)
    except Px4MissionError as exc:
        mismatch_sequences = [
            item.sequence
            for item, expected in zip(items, upload_plan.items, strict=True)
            if item != expected
        ]
        raise ExternalPx4MissionError(
            "MISSION readback rejected by transaction: "
            f"{exc}; observed={readback_hash} expected={upload_plan.protocol_hash} "
            f"mismatch_sequences={mismatch_sequences} "
            f"observed_items={[item.to_mapping() for item in items]}"
        ) from exc
    if readback_hash != upload_plan.protocol_hash or verified_hash != upload_plan.protocol_hash:
        mismatch_sequences = [
            item.sequence
            for item, expected in zip(items, upload_plan.items, strict=True)
            if item != expected
        ]
        raise ExternalPx4MissionError(
            "MISSION readback protocol hash mismatch: "
            f"expected={upload_plan.protocol_hash} "
            f"observed={readback_hash} verified={verified_hash} "
            f"mismatch_sequences={mismatch_sequences}"
        )
    return {
        "mission_count": count,
        "received_sequences": [item.sequence for item in items],
        "received_message_types": ["MISSION_ITEM_INT"] * len(items),
        "raw_execution_current_flags": raw_execution_current_flags,
        "execution_current_normalized": True,
        "mission_progress_source": "MISSION_CURRENT",
        "retry_count": retries,
        "uploaded_protocol_sha256": upload_plan.protocol_hash,
        "readback_protocol_sha256": readback_hash,
        "hash_match": True,
        "items": [item.to_mapping() for item in items],
    }


def run_external_px4_mission_transaction(
    *,
    connection: Any,
    vehicle_class: str,
    compiled: CompiledPx4Mission,
    context_snapshot: ContextSnapshot,
    platform_plan: PlatformNeutralPlan,
    policy: Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
    journal_path: str | Path,
    now_utc: datetime,
    transaction_id: str,
    runtime_attestation: ExternalRuntimeAttestation | None = None,
    receive: Callable[[float], Any | None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run external upload/readback/commit/permit without starting flight."""

    normalized = normalize_px4_vehicle_class(vehicle_class)
    if not isinstance(compiled, CompiledPx4Mission):
        raise ExternalPx4MissionError("compiled has the wrong type")
    if runtime_attestation is not None:
        if not isinstance(runtime_attestation, ExternalRuntimeAttestation):
            raise ExternalPx4MissionError("runtime_attestation has the wrong type")
        if runtime_attestation.vehicle_class != normalized:
            raise ExternalPx4MissionError("runtime attestation vehicle does not match")
        if runtime_attestation.airframe_profile_id != context_snapshot.airframe_profile_id:
            raise ExternalPx4MissionError("runtime attestation profile does not match context")
    path = Path(journal_path).expanduser().resolve()
    if path.exists():
        raise ExternalPx4MissionError(f"refusing to overwrite existing journal: {path}")
    report, bindings, grant = _build_transaction_documents(
        vehicle_class=normalized,
        compiled=compiled,
        context_snapshot=context_snapshot,
        platform_plan=platform_plan,
        policy=policy,
        transaction_id=transaction_id,
        now_utc=now_utc,
        runtime_mode="SIM",
        transport="pymavlink_mission_item_int",
        runtime_provenance_source="injected-unattested-mavlink-connection",
        physical_output_enabled=False,
    )
    transaction = Px4MissionTransaction(
        transaction_id=transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now_utc,
    )
    journal = DurableMissionJournal.create(path, identity=transaction, now_utc=now_utc)
    persistent = PersistentPx4MissionTransaction(transaction, journal)
    states: list[str] = []
    try:
        states.append(persistent.state.value)
        upload_plan = persistent.begin_upload(now_utc=now_utc)
        states.append(persistent.state.value)
        upload = upload_mission_int(
            connection,
            persistent,
            upload_plan,
            receive=receive,
            clock=clock,
            now_utc=now_utc,
        )
        states.append(persistent.state.value)
        readback = readback_mission_int(
            connection,
            persistent,
            upload_plan,
            receive=receive,
            clock=clock,
            now_utc=now_utc,
        )
        states.append(persistent.state.value)
        committed = persistent.commit(now_utc=now_utc)
        states.append(persistent.state.value)
        permit = persistent.permit_start(now_utc=now_utc)
        states.append(persistent.state.value)
    finally:
        persistent.close()

    journal_summary: dict[str, Any] | None = None
    reopened = DurableMissionJournal.open(path)
    try:
        recovery = reopened.recover()
        journal_summary = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "state": recovery.state.value,
            "recovery_required": recovery.recovery_required,
            "record_count": len(recovery.records),
            "identity_hash": recovery.identity.canonical_hash,
            "transaction_id": recovery.identity.transaction_id,
            "executable_mission_hash": recovery.identity.executable_mission_hash,
            "protocol_hash": recovery.identity.protocol_hash,
            "mission_item_count": recovery.identity.mission_item_count,
        }
    except (MissionJournalError, OSError, ValueError) as exc:
        raise ExternalPx4MissionError("external mission journal recovery failed") from exc
    finally:
        reopened.close()
    checks = {
        "state_path_exact": tuple(states)
        == tuple(
            state.value
            for state in (
                MissionTransactionState.AUTHORIZED,
                MissionTransactionState.UPLOADING,
                MissionTransactionState.UPLOADED,
                MissionTransactionState.READBACK_VERIFIED,
                MissionTransactionState.COMMITTED,
                MissionTransactionState.START_PERMITTED,
            )
        ),
        "readback_hash_match": readback["readback_protocol_sha256"] == compiled.protocol_hash,
        "commit_hash_match": committed == compiled.protocol_hash,
        "start_permit_bound": permit.protocol_hash == compiled.protocol_hash,
        "journal_recovery_clear": not journal_summary["recovery_required"],
        "journal_state_start_permitted": journal_summary["state"]
        == MissionTransactionState.START_PERMITTED.value,
    }
    return {
        "schema": "multidetect.px4-external-mission-transaction.v1",
        "schema_version": 1,
        "gate_passed": all(checks.values()),
        "vehicle_class": normalized,
        "runtime_mode": "SIM_EXTERNAL" if runtime_attestation is not None else "SIM",
        "truth_free": True,
        "physical_output_enabled": False,
        "execution_adapter": "external_px4_mission_item_int",
        "external_runtime": runtime_attestation is not None,
        "external_runtime_attested": runtime_attestation is not None,
        "transport_exercised": True,
        "runtime_attestation": (
            runtime_attestation.to_mapping() if runtime_attestation is not None else None
        ),
        "actuator_output_enabled": False,
        "transport": "pymavlink_mission_item_int",
        "transaction_id": transaction_id,
        "state_path": states,
        "executable_mission_hash": compiled.executable_mission.canonical_hash,
        "protocol_hash": compiled.protocol_hash,
        "item_count": upload_plan.item_count,
        "airframe_adapter_id": compiled.executable_mission.airframe_adapter_id,
        "airframe_profile_hash": compiled.executable_mission.airframe_profile_hash,
        "upload": upload,
        "readback": readback,
        "committed_hash": committed,
        "permit": {
            "transaction_id": permit.transaction_id,
            "executable_mission_hash": permit.executable_mission_hash,
            "protocol_hash": permit.protocol_hash,
        },
        "mission_journal": journal_summary,
        "checks": checks,
        "flight_started": False,
    }


__all__ = [
    "attest_owned_px4_container",
    "ExternalPx4MissionError",
    "ExternalRuntimeAttestation",
    "readback_mission_int",
    "run_external_px4_mission_transaction",
    "upload_mission_int",
]

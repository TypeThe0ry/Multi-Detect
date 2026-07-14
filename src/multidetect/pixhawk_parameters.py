from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .pixhawk import PixhawkDependencyError, resolve_pixhawk_endpoint

_BYTEWISE_PARAMETER_FORMATS = {
    1: "<B",  # MAV_PARAM_TYPE_UINT8
    2: "<b",  # MAV_PARAM_TYPE_INT8
    3: "<H",  # MAV_PARAM_TYPE_UINT16
    4: "<h",  # MAV_PARAM_TYPE_INT16
    5: "<I",  # MAV_PARAM_TYPE_UINT32
    6: "<i",  # MAV_PARAM_TYPE_INT32
    9: "<f",  # MAV_PARAM_TYPE_REAL32
}
_INTEGER_PARAMETER_TYPES = frozenset(range(1, 9))
_NUMERIC_PARAMETER_TYPES = frozenset(range(1, 11))
_PX4_PARAMETER_HASH_INDICES = frozenset({32_767, 65_535})
_MAVLINK_V1_STX = 0xFE
_MAVLINK_V2_STX = 0xFD
_MAVLINK_MSG_ID_PARAM_VALUE = 22


@dataclass(frozen=True, slots=True)
class PixhawkParameterBackupConfig:
    """Configuration for one explicitly authorized MAVLink parameter-list read."""

    endpoint: str
    parameter_encoding: Literal["bytewise", "c_cast"]
    active_read_request_acknowledged: bool
    baud: int = 57_600
    target_system_id: int = 1
    target_component_id: int = 1
    local_system_id: int = 245
    local_component_id: int = 191
    timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 2.0
    minimum_parameters: int = 1
    maximum_parameters: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("Pixhawk parameter endpoint cannot be empty")
        if self.parameter_encoding not in {"bytewise", "c_cast"}:
            raise ValueError("Pixhawk parameter encoding must be bytewise or c_cast")
        if self.active_read_request_acknowledged is not True:
            raise ValueError(
                "Pixhawk parameter active read request must be explicitly acknowledged"
            )
        if isinstance(self.baud, bool) or not isinstance(self.baud, int) or self.baud <= 0:
            raise ValueError("Pixhawk parameter baud must be a positive integer")
        for label, value in (
            ("target system ID", self.target_system_id),
            ("target component ID", self.target_component_id),
            ("local system ID", self.local_system_id),
            ("local component ID", self.local_component_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"Pixhawk parameter {label} must be in [1, 255]")
        for label, value in (
            ("timeout", self.timeout_seconds),
            ("idle timeout", self.idle_timeout_seconds),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Pixhawk parameter {label} must be finite and positive")
        if self.idle_timeout_seconds > self.timeout_seconds:
            raise ValueError("Pixhawk parameter idle timeout cannot exceed total timeout")
        for label, value in (
            ("minimum parameter count", self.minimum_parameters),
            ("maximum parameter count", self.maximum_parameters),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Pixhawk parameter {label} must be a positive integer")
        if self.minimum_parameters > self.maximum_parameters:
            raise ValueError(
                "Pixhawk parameter minimum count cannot exceed maximum parameter count"
            )


@dataclass(frozen=True, slots=True)
class PixhawkParameterRecord:
    name: str
    value: int | float
    raw_value_hex: str
    parameter_type: int
    index: int

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "raw_value_hex": self.raw_value_hex,
            "parameter_type": self.parameter_type,
            "index": self.index,
        }


@dataclass(frozen=True, slots=True)
class PixhawkParameterSnapshot:
    captured_at_utc: str
    configured_endpoint: str
    resolved_endpoint: str | None
    parameter_encoding: Literal["bytewise", "c_cast"]
    target_system_id: int
    target_component_id: int
    duration_seconds: float
    expected_parameter_count: int | None
    received_parameter_count: int
    rejected_source_message_count: int
    invalid_parameter_message_count: int
    active_read_requests_transmitted: int
    px4_parameter_hash_raw_hex: str | None
    parameters: tuple[PixhawkParameterRecord, ...]
    complete: bool
    passed: bool
    failure_reasons: tuple[str, ...]

    @property
    def parameter_list_sha256(self) -> str:
        canonical = json.dumps(
            [parameter.to_document() for parameter in self.parameters],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event": "pixhawk_parameter_backup_completed",
            "captured_at_utc": self.captured_at_utc,
            "configured_endpoint": self.configured_endpoint,
            "resolved_endpoint": self.resolved_endpoint,
            "parameter_encoding": self.parameter_encoding,
            "target_system_id": self.target_system_id,
            "target_component_id": self.target_component_id,
            "duration_seconds": self.duration_seconds,
            "expected_parameter_count": self.expected_parameter_count,
            "received_parameter_count": self.received_parameter_count,
            "rejected_source_message_count": self.rejected_source_message_count,
            "invalid_parameter_message_count": self.invalid_parameter_message_count,
            "active_read_request": True,
            "request_message_type": "PARAM_REQUEST_LIST",
            "active_read_requests_transmitted": self.active_read_requests_transmitted,
            "px4_parameter_hash_raw_hex": self.px4_parameter_hash_raw_hex,
            "messages_transmitted": self.active_read_requests_transmitted,
            "parameter_write_messages_transmitted": 0,
            "flight_command_messages_transmitted": 0,
            "mission_messages_transmitted": 0,
            "actuator_messages_transmitted": 0,
            "hardware_control_enabled": False,
            "complete": self.complete,
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons),
            "parameter_list_sha256": self.parameter_list_sha256,
            "parameters": [parameter.to_document() for parameter in self.parameters],
        }


_SNAPSHOT_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "captured_at_utc",
        "configured_endpoint",
        "resolved_endpoint",
        "parameter_encoding",
        "target_system_id",
        "target_component_id",
        "duration_seconds",
        "expected_parameter_count",
        "received_parameter_count",
        "rejected_source_message_count",
        "invalid_parameter_message_count",
        "active_read_request",
        "request_message_type",
        "active_read_requests_transmitted",
        "px4_parameter_hash_raw_hex",
        "messages_transmitted",
        "parameter_write_messages_transmitted",
        "flight_command_messages_transmitted",
        "mission_messages_transmitted",
        "actuator_messages_transmitted",
        "hardware_control_enabled",
        "complete",
        "passed",
        "failure_reasons",
        "parameter_list_sha256",
        "parameters",
    }
)


class PixhawkParameterBackupClient:
    """Sends exactly one list-read request and has no parameter-write or command path."""

    def __init__(
        self,
        config: PixhawkParameterBackupConfig,
        *,
        connection: Any | None = None,
    ) -> None:
        self.config = config
        self._connection = connection
        self._owns_connection = connection is None
        self._resolved_endpoint: str | None = None
        self._active_read_requests_transmitted = 0

    @property
    def active_read_requests_transmitted(self) -> int:
        return self._active_read_requests_transmitted

    @property
    def parameter_write_messages_transmitted(self) -> int:
        return 0

    @property
    def hardware_control_enabled(self) -> bool:
        return False

    def connect(self) -> None:
        if self._connection is not None:
            return
        try:
            from pymavlink import mavutil
        except ImportError as exc:  # pragma: no cover - optional dependency boundary.
            raise PixhawkDependencyError(
                "Install the optional Pixhawk dependency: pip install -e '.[pixhawk]'"
            ) from exc
        endpoint = resolve_pixhawk_endpoint(self.config.endpoint)
        self._connection = mavutil.mavlink_connection(
            endpoint,
            baud=self.config.baud,
            source_system=self.config.local_system_id,
            source_component=self.config.local_component_id,
            autoreconnect=True,
        )
        self._resolved_endpoint = endpoint

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if self._owns_connection and connection is not None:
            connection.close()

    def capture(self) -> PixhawkParameterSnapshot:
        if self._active_read_requests_transmitted:
            raise RuntimeError("Pixhawk parameter backup client is single-use")
        self.connect()
        connection = self._connection
        if connection is None:
            raise RuntimeError("Pixhawk parameter connection failed to initialize")

        started_s = time.monotonic()
        connection.mav.param_request_list_send(
            self.config.target_system_id,
            self.config.target_component_id,
        )
        self._active_read_requests_transmitted += 1

        records_by_index: dict[int, PixhawkParameterRecord] = {}
        index_by_name: dict[str, int] = {}
        expected_count: int | None = None
        rejected_source_messages = 0
        invalid_parameter_messages = 0
        protocol_failures: list[str] = []
        last_parameter_s: float | None = None
        px4_parameter_hash_raw_hex: str | None = None

        while True:
            now_s = time.monotonic()
            elapsed_s = now_s - started_s
            if elapsed_s >= self.config.timeout_seconds:
                break
            if (
                last_parameter_s is not None
                and now_s - last_parameter_s >= self.config.idle_timeout_seconds
            ):
                break
            remaining_s = self.config.timeout_seconds - elapsed_s
            wait_s = min(0.2, remaining_s)
            message = connection.recv_match(
                type="PARAM_VALUE",
                blocking=True,
                timeout=wait_s,
            )
            if message is None:
                continue
            source_system_id = _message_source_id(message, "get_srcSystem", "srcSystem")
            source_component_id = _message_source_id(
                message,
                "get_srcComponent",
                "srcComponent",
            )
            if source_system_id != self.config.target_system_id or source_component_id != (
                self.config.target_component_id
            ):
                rejected_source_messages += 1
                continue
            try:
                if int(message.param_index) in _PX4_PARAMETER_HASH_INDICES:
                    received_hash = _raw_param_value_bytes(message).hex()
                    if (
                        px4_parameter_hash_raw_hex is not None
                        and px4_parameter_hash_raw_hex != received_hash
                    ):
                        _append_unique(
                            protocol_failures,
                            "PX4 parameter hash changed during backup",
                        )
                    else:
                        px4_parameter_hash_raw_hex = received_hash
                    last_parameter_s = time.monotonic()
                    continue
                record, reported_count = _parameter_record_from_message(
                    message,
                    maximum_parameters=self.config.maximum_parameters,
                    parameter_encoding=self.config.parameter_encoding,
                )
            except (TypeError, ValueError) as exc:
                invalid_parameter_messages += 1
                _append_unique(protocol_failures, str(exc))
                continue
            if expected_count is None:
                expected_count = reported_count
            elif reported_count != expected_count:
                _append_unique(
                    protocol_failures,
                    "inconsistent PARAM_VALUE parameter counts were received",
                )
                continue
            existing_at_index = records_by_index.get(record.index)
            existing_name_index = index_by_name.get(record.name)
            if existing_at_index is not None and existing_at_index != record:
                _append_unique(
                    protocol_failures,
                    f"parameter index {record.index} changed during backup",
                )
                continue
            if existing_name_index is not None and existing_name_index != record.index:
                _append_unique(
                    protocol_failures,
                    f"parameter name {record.name!r} appeared at multiple indices",
                )
                continue
            records_by_index[record.index] = record
            index_by_name[record.name] = record.index
            last_parameter_s = time.monotonic()
            if expected_count is not None and len(records_by_index) == expected_count:
                break

        finished_s = time.monotonic()
        parameters = tuple(records_by_index[index] for index in sorted(records_by_index))
        complete = (
            expected_count is not None
            and len(parameters) == expected_count
            and not protocol_failures
        )
        failure_reasons = list(protocol_failures)
        if expected_count is None:
            _append_unique(failure_reasons, "no PARAM_VALUE response was received")
        elif len(parameters) != expected_count:
            _append_unique(
                failure_reasons,
                "parameter list is incomplete: "
                f"received={len(parameters)}, expected={expected_count}",
            )
        if len(parameters) < self.config.minimum_parameters:
            _append_unique(
                failure_reasons,
                "received parameter count is below the configured minimum: "
                f"received={len(parameters)}, minimum={self.config.minimum_parameters}",
            )
        passed = complete and len(parameters) >= self.config.minimum_parameters
        return PixhawkParameterSnapshot(
            captured_at_utc=datetime.now(timezone.utc).isoformat(),
            configured_endpoint=self.config.endpoint,
            resolved_endpoint=self._resolved_endpoint or self.config.endpoint,
            parameter_encoding=self.config.parameter_encoding,
            target_system_id=self.config.target_system_id,
            target_component_id=self.config.target_component_id,
            duration_seconds=max(0.0, finished_s - started_s),
            expected_parameter_count=expected_count,
            received_parameter_count=len(parameters),
            rejected_source_message_count=rejected_source_messages,
            invalid_parameter_message_count=invalid_parameter_messages,
            active_read_requests_transmitted=self._active_read_requests_transmitted,
            px4_parameter_hash_raw_hex=px4_parameter_hash_raw_hex,
            parameters=parameters,
            complete=complete,
            passed=passed,
            failure_reasons=tuple(failure_reasons),
        )


def write_pixhawk_parameter_snapshot(
    path: Path,
    snapshot: PixhawkParameterSnapshot,
    *,
    force: bool = False,
) -> None:
    """Atomically persist a snapshot; reject accidental overwrite before hardware access."""

    _write_json_atomic(
        path,
        snapshot.to_document(),
        force=force,
        artifact_label="Pixhawk parameter backup",
    )


def load_verified_pixhawk_parameter_snapshot(path: Path) -> PixhawkParameterSnapshot:
    """Strictly verify a complete backup and its self-consistency hash."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Pixhawk parameter backup must contain one JSON object")
    document_keys = frozenset(document)
    if document_keys != _SNAPSHOT_DOCUMENT_KEYS:
        missing = sorted(_SNAPSHOT_DOCUMENT_KEYS - document_keys)
        unexpected = sorted(document_keys - _SNAPSHOT_DOCUMENT_KEYS)
        raise ValueError(
            f"Pixhawk parameter backup schema mismatch: missing={missing}, unexpected={unexpected}"
        )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported Pixhawk parameter backup schema version")
    if document["event"] != "pixhawk_parameter_backup_completed":
        raise ValueError("Pixhawk parameter backup event type is invalid")
    for key in ("configured_endpoint", "resolved_endpoint"):
        if not isinstance(document[key], str) or not document[key].strip():
            raise ValueError(f"Pixhawk parameter backup {key} is invalid")
    encoding = document["parameter_encoding"]
    if not isinstance(encoding, str) or encoding not in {"bytewise", "c_cast"}:
        raise ValueError("Pixhawk parameter backup encoding is invalid")
    target_system_id = _bounded_document_int(
        document,
        "target_system_id",
        minimum=1,
        maximum=255,
    )
    target_component_id = _bounded_document_int(
        document,
        "target_component_id",
        minimum=1,
        maximum=255,
    )
    duration_seconds = _finite_document_number(document, "duration_seconds", minimum=0.0)
    expected_count = _bounded_document_int(
        document,
        "expected_parameter_count",
        minimum=1,
        maximum=65_535,
    )
    received_count = _bounded_document_int(
        document,
        "received_parameter_count",
        minimum=1,
        maximum=65_535,
    )
    rejected_source_count = _bounded_document_int(
        document,
        "rejected_source_message_count",
        minimum=0,
        maximum=2**31 - 1,
    )
    invalid_message_count = _bounded_document_int(
        document,
        "invalid_parameter_message_count",
        minimum=0,
        maximum=2**31 - 1,
    )
    required_values = {
        "active_read_request": True,
        "request_message_type": "PARAM_REQUEST_LIST",
        "active_read_requests_transmitted": 1,
        "messages_transmitted": 1,
        "parameter_write_messages_transmitted": 0,
        "flight_command_messages_transmitted": 0,
        "mission_messages_transmitted": 0,
        "actuator_messages_transmitted": 0,
        "hardware_control_enabled": False,
        "complete": True,
        "passed": True,
        "failure_reasons": [],
        "invalid_parameter_message_count": 0,
    }
    for key, expected_value in required_values.items():
        actual_value = document[key]
        if isinstance(expected_value, bool):
            matches = actual_value is expected_value
        elif isinstance(expected_value, int):
            matches = type(actual_value) is int and actual_value == expected_value
        else:
            matches = type(actual_value) is type(expected_value) and actual_value == expected_value
        if not matches:
            raise ValueError(f"Pixhawk parameter backup invariant failed: {key}={document[key]!r}")
    captured_at_utc = document["captured_at_utc"]
    if not isinstance(captured_at_utc, str):
        raise ValueError("Pixhawk parameter backup UTC timestamp is invalid")
    try:
        captured_at = datetime.fromisoformat(captured_at_utc)
    except ValueError as exc:
        raise ValueError("Pixhawk parameter backup UTC timestamp is invalid") from exc
    if captured_at.utcoffset() is None or captured_at.utcoffset().total_seconds() != 0:
        raise ValueError("Pixhawk parameter backup UTC timestamp must use a zero UTC offset")
    hash_raw_hex = document["px4_parameter_hash_raw_hex"]
    if hash_raw_hex is not None:
        _decode_hex_bytes(hash_raw_hex, key="px4_parameter_hash_raw_hex", byte_count=4)
    parameter_documents = document["parameters"]
    if not isinstance(parameter_documents, list):
        raise ValueError("Pixhawk parameter backup parameters must be a list")
    if expected_count != received_count or received_count != len(parameter_documents):
        raise ValueError("Pixhawk parameter backup parameter counts are inconsistent")
    parameters: list[PixhawkParameterRecord] = []
    names: set[str] = set()
    for expected_index, parameter_document in enumerate(parameter_documents):
        if not isinstance(parameter_document, dict) or frozenset(parameter_document) != {
            "name",
            "value",
            "raw_value_hex",
            "parameter_type",
            "index",
        }:
            raise ValueError("Pixhawk parameter backup contains an invalid parameter record")
        name = _decode_parameter_name(parameter_document["name"])
        if name in names:
            raise ValueError(f"Pixhawk parameter backup repeats parameter name {name!r}")
        names.add(name)
        index = parameter_document["index"]
        if isinstance(index, bool) or not isinstance(index, int) or index != expected_index:
            raise ValueError("Pixhawk parameter backup indices are not contiguous and ordered")
        parameter_type = parameter_document["parameter_type"]
        if (
            isinstance(parameter_type, bool)
            or not isinstance(parameter_type, int)
            or parameter_type not in _NUMERIC_PARAMETER_TYPES
        ):
            raise ValueError("Pixhawk parameter backup contains an invalid parameter type")
        raw_value = _decode_hex_bytes(
            parameter_document["raw_value_hex"],
            key=f"parameters[{expected_index}].raw_value_hex",
            byte_count=4,
        )
        decoded_value = _decode_parameter_value_from_raw(
            raw_value=raw_value,
            parameter_type=parameter_type,
            parameter_encoding=encoding,
        )
        stored_value = parameter_document["value"]
        if (
            isinstance(stored_value, bool)
            or not isinstance(stored_value, (int, float))
            or (isinstance(stored_value, float) and not math.isfinite(stored_value))
            or stored_value != decoded_value
        ):
            raise ValueError(f"Pixhawk parameter backup value mismatch for {name!r}")
        parameters.append(
            PixhawkParameterRecord(
                name=name,
                value=decoded_value,
                raw_value_hex=raw_value.hex(),
                parameter_type=parameter_type,
                index=index,
            )
        )
    snapshot = PixhawkParameterSnapshot(
        captured_at_utc=captured_at_utc,
        configured_endpoint=document["configured_endpoint"],
        resolved_endpoint=document["resolved_endpoint"],
        parameter_encoding=encoding,
        target_system_id=target_system_id,
        target_component_id=target_component_id,
        duration_seconds=duration_seconds,
        expected_parameter_count=expected_count,
        received_parameter_count=received_count,
        rejected_source_message_count=rejected_source_count,
        invalid_parameter_message_count=invalid_message_count,
        active_read_requests_transmitted=1,
        px4_parameter_hash_raw_hex=hash_raw_hex,
        parameters=tuple(parameters),
        complete=True,
        passed=True,
        failure_reasons=(),
    )
    expected_hash = document["parameter_list_sha256"]
    _decode_hex_bytes(expected_hash, key="parameter_list_sha256", byte_count=32)
    if snapshot.parameter_list_sha256 != expected_hash.lower():
        raise ValueError("Pixhawk parameter backup parameter-list SHA-256 mismatch")
    return snapshot


def compare_pixhawk_parameter_snapshots(
    before: PixhawkParameterSnapshot,
    after: PixhawkParameterSnapshot,
    *,
    allowed_changes: frozenset[str] = frozenset(),
    required_changes: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Compare two verified snapshots and fail closed on any unlisted difference."""

    for name in allowed_changes | required_changes:
        _decode_parameter_name(name)
    before_by_name = {parameter.name: parameter for parameter in before.parameters}
    after_by_name = {parameter.name: parameter for parameter in after.parameters}
    before_names = frozenset(before_by_name)
    after_names = frozenset(after_by_name)
    added_names = sorted(after_names - before_names)
    removed_names = sorted(before_names - after_names)
    changed_names: list[str] = []
    moved_names: list[str] = []
    for name in sorted(before_names & after_names):
        before_parameter = before_by_name[name]
        after_parameter = after_by_name[name]
        if (
            before_parameter.value,
            before_parameter.raw_value_hex,
            before_parameter.parameter_type,
        ) != (
            after_parameter.value,
            after_parameter.raw_value_hex,
            after_parameter.parameter_type,
        ):
            changed_names.append(name)
        if before_parameter.index != after_parameter.index:
            moved_names.append(name)
    observed_changes = frozenset((*added_names, *removed_names, *changed_names, *moved_names))
    identity_failures: list[str] = []
    if before.parameter_encoding != after.parameter_encoding:
        identity_failures.append("parameter encoding changed between snapshots")
    if before.target_system_id != after.target_system_id:
        identity_failures.append("target system ID changed between snapshots")
    if before.target_component_id != after.target_component_id:
        identity_failures.append("target component ID changed between snapshots")
    unexpected_changes = sorted(observed_changes - allowed_changes)
    missing_required_changes = sorted(required_changes - observed_changes)
    gate_failures = [
        *identity_failures,
        *(
            ["unexpected parameter changes: " + ", ".join(unexpected_changes)]
            if unexpected_changes
            else []
        ),
        *(
            ["required parameter changes were not observed: " + ", ".join(missing_required_changes)]
            if missing_required_changes
            else []
        ),
    ]
    return {
        "schema_version": 1,
        "event": "pixhawk_parameter_diff_completed",
        "before_parameter_list_sha256": before.parameter_list_sha256,
        "after_parameter_list_sha256": after.parameter_list_sha256,
        "target_system_id": after.target_system_id,
        "target_component_id": after.target_component_id,
        "parameter_encoding": after.parameter_encoding,
        "before_parameter_count": len(before.parameters),
        "after_parameter_count": len(after.parameters),
        "added": [after_by_name[name].to_document() for name in added_names],
        "removed": [before_by_name[name].to_document() for name in removed_names],
        "changed": [
            {
                "name": name,
                "before": before_by_name[name].to_document(),
                "after": after_by_name[name].to_document(),
            }
            for name in changed_names
        ],
        "index_moved": [
            {
                "name": name,
                "before_index": before_by_name[name].index,
                "after_index": after_by_name[name].index,
            }
            for name in moved_names
        ],
        "observed_change_names": sorted(observed_changes),
        "allowed_change_names": sorted(allowed_changes),
        "required_change_names": sorted(required_changes),
        "unexpected_change_names": unexpected_changes,
        "missing_required_change_names": missing_required_changes,
        "gate_passed": not gate_failures,
        "gate_failures": gate_failures,
        "messages_transmitted": 0,
        "hardware_control_enabled": False,
    }


def write_pixhawk_parameter_diff(
    path: Path,
    document: dict[str, object],
    *,
    force: bool = False,
) -> None:
    _write_json_atomic(
        path,
        document,
        force=force,
        artifact_label="Pixhawk parameter diff",
    )


def write_pixhawk_parameter_report(
    path: Path,
    document: dict[str, object],
    *,
    force: bool = False,
) -> None:
    _write_json_atomic(
        path,
        document,
        force=force,
        artifact_label="Pixhawk parameter verification report",
    )


def _parameter_record_from_message(
    message: Any,
    *,
    maximum_parameters: int,
    parameter_encoding: Literal["bytewise", "c_cast"],
) -> tuple[PixhawkParameterRecord, int]:
    name = _decode_parameter_name(getattr(message, "param_id", None))
    parameter_type = int(message.param_type)
    reported_count = int(message.param_count)
    index = int(message.param_index)
    if parameter_type not in _NUMERIC_PARAMETER_TYPES:
        raise ValueError("PARAM_VALUE contained an invalid parameter type")
    if not 1 <= reported_count <= maximum_parameters:
        raise ValueError(
            "PARAM_VALUE reported an invalid parameter count: "
            f"count={reported_count}, maximum={maximum_parameters}"
        )
    if not 0 <= index < reported_count:
        raise ValueError(
            "PARAM_VALUE contained an invalid parameter index: "
            f"index={index}, count={reported_count}"
        )
    raw_value = _raw_param_value_bytes(message)
    value = _decode_parameter_value_from_raw(
        raw_value=raw_value,
        parameter_type=parameter_type,
        parameter_encoding=parameter_encoding,
    )
    return (
        PixhawkParameterRecord(
            name=name,
            value=value,
            raw_value_hex=raw_value.hex(),
            parameter_type=parameter_type,
            index=index,
        ),
        reported_count,
    )


def _decode_parameter_value_from_raw(
    *,
    raw_value: bytes,
    parameter_type: int,
    parameter_encoding: Literal["bytewise", "c_cast"],
) -> int | float:
    if parameter_encoding == "bytewise":
        value_format = _BYTEWISE_PARAMETER_FORMATS.get(parameter_type)
        if value_format is None:
            raise ValueError(
                "classic MAVLink bytewise backup does not support 64-bit parameter types"
            )
        value = struct.unpack(value_format, raw_value[: struct.calcsize(value_format)])[0]
    else:
        value = struct.unpack("<f", raw_value)[0]
        if parameter_type in _INTEGER_PARAMETER_TYPES:
            value = int(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("PARAM_VALUE contained a non-finite decoded value")
    return value


def _raw_param_value_bytes(message: Any) -> bytes:
    # pymavlink's ``get_payload`` is not stable across MAVLink 1 and 2
    # decoders. In particular, MAVLink 2 messages received from PX4 may
    # prefix it with the source system and three-byte message id. Parse the
    # wire frame instead, where the PARAM_VALUE float is always the first
    # four payload bytes.
    get_msgbuf = getattr(message, "get_msgbuf", None)
    if callable(get_msgbuf):
        frame = get_msgbuf()
        if frame is not None:
            frame_bytes = bytes(frame)
            raw_value = _raw_param_value_bytes_from_frame(frame_bytes)
            if raw_value is not None:
                return raw_value
    return struct.pack("<f", float(message.param_value))


def _raw_param_value_bytes_from_frame(frame: bytes) -> bytes | None:
    if len(frame) < 2:
        return None
    payload_length = frame[1]
    if frame[0] == _MAVLINK_V2_STX:
        payload_offset = 10
        if len(frame) < payload_offset + payload_length + 2 or payload_length < 4:
            return None
        message_id = int.from_bytes(frame[7:10], byteorder="little")
    elif frame[0] == _MAVLINK_V1_STX:
        payload_offset = 6
        if len(frame) < payload_offset + payload_length + 2 or payload_length < 4:
            return None
        message_id = frame[5]
    else:
        return None
    if message_id != _MAVLINK_MSG_ID_PARAM_VALUE:
        return None
    return frame[payload_offset : payload_offset + 4]


def _decode_parameter_name(raw_name: object) -> str:
    if isinstance(raw_name, bytes):
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="strict")
    elif isinstance(raw_name, str):
        name = raw_name.split("\x00", 1)[0]
    else:
        raise TypeError("PARAM_VALUE parameter ID is not bytes or text")
    if not name or len(name.encode("ascii", errors="strict")) > 16:
        raise ValueError("PARAM_VALUE contained an invalid parameter ID")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in name):
        raise ValueError("PARAM_VALUE parameter ID contains non-printable ASCII")
    return name


def _message_source_id(message: Any, method_name: str, attribute_name: str) -> int | None:
    method = getattr(message, method_name, None)
    value = method() if callable(method) else getattr(message, attribute_name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _bounded_document_int(
    document: dict[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Pixhawk parameter backup {key} is invalid")
    return value


def _finite_document_number(
    document: dict[str, object],
    key: str,
    *,
    minimum: float,
) -> float:
    value = document[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ValueError(f"Pixhawk parameter backup {key} is invalid")
    return float(value)


def _decode_hex_bytes(value: object, *, key: str, byte_count: int) -> bytes:
    if not isinstance(value, str) or len(value) != byte_count * 2:
        raise ValueError(f"Pixhawk parameter backup {key} is invalid")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"Pixhawk parameter backup {key} is invalid") from exc
    if len(decoded) != byte_count:
        raise ValueError(f"Pixhawk parameter backup {key} is invalid")
    return decoded


def _write_json_atomic(
    path: Path,
    document: dict[str, object],
    *,
    force: bool,
    artifact_label: str,
) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{artifact_label} already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


__all__ = [
    "PixhawkParameterBackupClient",
    "PixhawkParameterBackupConfig",
    "PixhawkParameterRecord",
    "PixhawkParameterSnapshot",
    "compare_pixhawk_parameter_snapshots",
    "load_verified_pixhawk_parameter_snapshot",
    "write_pixhawk_parameter_diff",
    "write_pixhawk_parameter_report",
    "write_pixhawk_parameter_snapshot",
]

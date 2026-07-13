from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PAYLOAD_BENCH_PROTOCOL_VERSION = 1


def sign_payload_bench_message(document: Mapping[str, Any], *, hmac_key: bytes) -> dict[str, Any]:
    _require_key(hmac_key)
    unsigned = dict(document)
    unsigned.pop("signature_hmac_sha256", None)
    signature = hmac.new(hmac_key, _canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    return {**unsigned, "signature_hmac_sha256": signature}


def check_inert_payload_hardware_bench(
    *,
    controller_log: str | Path,
    sensor_log: str | Path,
    controller_hmac_key: bytes,
    sensor_hmac_key: bytes,
    bench_id: str,
    controller_id: str,
    sensor_id: str,
    controller_key_id: str,
    sensor_key_id: str,
    inert_load_only: bool,
    people_excluded_from_test_area: bool,
    minimum_confirmed_cycles: int = 20,
    maximum_age_hours: float = 168.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_key(controller_hmac_key)
    _require_key(sensor_hmac_key)
    if hmac.compare_digest(controller_hmac_key, sensor_hmac_key):
        raise ValueError("payload bench controller and sensor HMAC keys must differ")
    identifiers = (bench_id, controller_id, sensor_id, controller_key_id, sensor_key_id)
    if any(not isinstance(value, str) or not value.strip() for value in identifiers):
        raise ValueError("payload bench identifiers and key IDs must be non-empty")
    if controller_id == sensor_id:
        raise ValueError("payload bench controller and sensor IDs must differ")
    if controller_key_id == sensor_key_id:
        raise ValueError("payload bench controller and sensor key IDs must differ")
    if minimum_confirmed_cycles <= 0:
        raise ValueError("payload bench minimum confirmed cycles must be positive")
    if not math.isfinite(maximum_age_hours) or maximum_age_hours <= 0:
        raise ValueError("payload bench maximum age must be finite and positive")
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    controller_records = _load_authenticated_log(
        controller_log,
        hmac_key=controller_hmac_key,
        expected_message_type="controller_cycle",
        expected_bench_id=bench_id,
        expected_source_id=controller_id,
        expected_key_id=controller_key_id,
        checked_at=checked_at,
        maximum_age_hours=maximum_age_hours,
    )
    sensor_records = _load_authenticated_log(
        sensor_log,
        hmac_key=sensor_hmac_key,
        expected_message_type="sensor_confirmation",
        expected_bench_id=bench_id,
        expected_source_id=sensor_id,
        expected_key_id=sensor_key_id,
        checked_at=checked_at,
        maximum_age_hours=maximum_age_hours,
    )

    reasons: list[str] = []
    if inert_load_only is not True:
        reasons.append("bench operator did not declare inert-load-only operation")
    if people_excluded_from_test_area is not True:
        reasons.append("bench operator did not confirm test-area people exclusion")
    executed: dict[str, dict[str, Any]] = {}
    uncertain_cycles: set[str] = set()
    firmware_versions: set[str] = set()
    for record in controller_records:
        cycle_id = str(record["cycle_id"])
        firmware = record.get("firmware_version")
        if not isinstance(firmware, str) or not firmware.strip():
            reasons.append("controller record is missing firmware_version")
        else:
            firmware_versions.add(firmware.strip())
        status = record.get("status")
        retry_count = record.get("automatic_retry_count")
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            reasons.append("controller automatic_retry_count is invalid")
            continue
        if status == "executed":
            if (
                record.get("controller_healthy") is not True
                or record.get("interlock_healthy") is not True
                or retry_count != 0
                or record.get("inert_load") is not True
            ):
                reasons.append("executed controller cycle is unhealthy, retried, or non-inert")
                continue
            executed[cycle_id] = record
        elif status == "uncertain":
            if record.get("inert_load") is not True:
                reasons.append("uncertain controller cycle is not declared inert")
            elif retry_count != 0:
                reasons.append("uncertain controller cycle was automatically retried")
            else:
                uncertain_cycles.add(cycle_id)
        else:
            reasons.append("controller record status must be executed or uncertain")

    confirmations: dict[str, dict[str, Any]] = {}
    for record in sensor_records:
        cycle_id = str(record["cycle_id"])
        if (
            record.get("payload_absent") is not True
            or record.get("sensor_healthy") is not True
            or record.get("inert_load") is not True
        ):
            reasons.append("sensor confirmation is unhealthy, present, or non-inert")
            continue
        confirmations[cycle_id] = record
    confirmed_ids = sorted(set(executed) & set(confirmations))
    orphan_confirmations = sorted(set(confirmations) - set(executed))
    if orphan_confirmations:
        reasons.append("sensor log contains confirmations without executed controller cycles")
    if len(confirmed_ids) < minimum_confirmed_cycles:
        reasons.append("confirmed inert payload cycles are below the required minimum")
    uncertain_result_no_retry_verified = bool(uncertain_cycles)
    if not uncertain_result_no_retry_verified:
        reasons.append(
            "no uncertain-result fault injection with zero automatic retries was recorded"
        )
    if len(firmware_versions) != 1:
        reasons.append("controller firmware version changed or is missing across the bench")
    passed = not reasons
    return {
        "event": f"inert_payload_hardware_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": checked_at.isoformat(),
        "hardware_observed": bool(controller_records and sensor_records),
        "simulation_only": False,
        "passed": passed,
        "reasons": reasons,
        "bench_id": bench_id,
        "controller_id": controller_id,
        "confirmation_sensor_id": sensor_id,
        "controller_and_sensor_id_separated": True,
        "controller_and_sensor_key_separated": True,
        "controller_firmware_version": next(iter(firmware_versions), None),
        "authenticated_controller_records": len(controller_records),
        "authenticated_sensor_records": len(sensor_records),
        "executed_cycles": len(executed),
        "confirmed_cycles": len(confirmed_ids),
        "uncertain_fault_injection_cycles": len(uncertain_cycles),
        "independent_confirmation_verified": len(confirmed_ids) >= minimum_confirmed_cycles,
        "uncertain_result_no_retry_verified": uncertain_result_no_retry_verified,
        "inert_load_only": inert_load_only,
        "people_excluded_from_test_area": people_excluded_from_test_area,
        "command_channel_present": False,
        "flight_control_enabled": False,
        "physical_release_approved": False,
        "production_approved": False,
    }


def _load_authenticated_log(
    path: str | Path,
    *,
    hmac_key: bytes,
    expected_message_type: str,
    expected_bench_id: str,
    expected_source_id: str,
    expected_key_id: str,
    checked_at: datetime,
    maximum_age_hours: float,
) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("payload bench log cannot be read") from exc
    records: list[dict[str, Any]] = []
    last_sequence = -1
    cycle_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"payload bench log line {line_number} is invalid JSON") from exc
        if not isinstance(record, dict):
            raise ValueError(f"payload bench log line {line_number} must be an object")
        _verify_record_signature(record, hmac_key=hmac_key, line_number=line_number)
        if record.get("protocol_version") != PAYLOAD_BENCH_PROTOCOL_VERSION:
            raise ValueError("payload bench protocol version does not match")
        if record.get("message_type") != expected_message_type:
            raise ValueError("payload bench message type does not match the log")
        if record.get("bench_id") != expected_bench_id:
            raise ValueError("payload bench ID does not match")
        if record.get("source_id") != expected_source_id:
            raise ValueError("payload bench source ID does not match")
        if record.get("key_id") != expected_key_id:
            raise ValueError("payload bench key ID does not match")
        if (
            record.get("hardware_observed") is not True
            or record.get("simulation_only") is not False
        ):
            raise ValueError("payload bench record is not hardware evidence")
        sequence = record.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= last_sequence:
            raise ValueError("payload bench record sequence is not strictly increasing")
        last_sequence = sequence
        cycle_id = record.get("cycle_id")
        if not isinstance(cycle_id, str) or not cycle_id.strip() or cycle_id in cycle_ids:
            raise ValueError("payload bench cycle ID is missing or duplicated")
        cycle_ids.add(cycle_id)
        _verify_record_time(
            record,
            checked_at=checked_at,
            maximum_age_hours=maximum_age_hours,
        )
        records.append(record)
    if not records:
        raise ValueError("payload bench log contains no authenticated records")
    return records


def _verify_record_signature(
    record: Mapping[str, Any], *, hmac_key: bytes, line_number: int
) -> None:
    signature = record.get("signature_hmac_sha256")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        raise ValueError(f"payload bench log line {line_number} signature is invalid")
    unsigned = dict(record)
    unsigned.pop("signature_hmac_sha256", None)
    expected = hmac.new(hmac_key, _canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise ValueError(f"payload bench log line {line_number} authentication failed")


def _verify_record_time(
    record: Mapping[str, Any], *, checked_at: datetime, maximum_age_hours: float
) -> None:
    raw = record.get("observed_at_utc")
    if not isinstance(raw, str):
        raise ValueError("payload bench record time is missing")
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("payload bench record time is invalid") from exc
    if observed.tzinfo is None:
        raise ValueError("payload bench record time requires a timezone")
    age_hours = (checked_at - observed.astimezone(UTC)).total_seconds() / 3600.0
    if age_hours < 0 or age_hours > maximum_age_hours:
        raise ValueError("payload bench record time is stale or in the future")


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload bench message is not canonical JSON") from exc


def _require_key(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("payload bench HMAC key must contain at least 32 bytes")


__all__ = [
    "PAYLOAD_BENCH_PROTOCOL_VERSION",
    "check_inert_payload_hardware_bench",
    "sign_payload_bench_message",
]

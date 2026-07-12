from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import MissionConfig
from .domain import PayloadState

PAYLOAD_INVENTORY_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class ObservedPayloadSlot:
    slot_id: str
    payload_type: str
    state: PayloadState
    present: bool = True
    presence_sensor_healthy: bool | None = None


@dataclass(frozen=True, slots=True)
class PayloadInventorySnapshot:
    observed_at_s: float
    source_id: str
    controller_healthy: bool | None
    installed_slots: tuple[ObservedPayloadSlot, ...] | None
    simulation_only: bool
    protocol_version: int | None = None
    module_id: str | None = None
    interlock_healthy: bool | None = None
    sequence: int | None = None
    key_id: str | None = None
    authenticated: bool | None = None


@dataclass(frozen=True, slots=True)
class PayloadInventoryVerification:
    allowed: bool
    source_id: str
    reasons: tuple[str, ...]
    simulation_only: bool


class PayloadInventoryProvider(Protocol):
    def snapshot(self, *, now_s: float) -> PayloadInventorySnapshot: ...


class ConfiguredSimulationPayloadInventoryProvider:
    """Reports the configured slots for replay only; it is not hardware evidence."""

    def __init__(self, config: MissionConfig) -> None:
        self._config = config

    def snapshot(self, *, now_s: float) -> PayloadInventorySnapshot:
        return PayloadInventorySnapshot(
            observed_at_s=now_s,
            source_id="configured-simulation",
            controller_healthy=True,
            installed_slots=tuple(
                ObservedPayloadSlot(
                    slot_id=payload.slot_id,
                    payload_type=payload.payload_type,
                    state=PayloadState.LOCKED,
                    presence_sensor_healthy=True,
                )
                for payload in self._config.payloads
            ),
            simulation_only=True,
            protocol_version=PAYLOAD_INVENTORY_PROTOCOL_VERSION,
            module_id="configured-simulation-module",
            interlock_healthy=True,
            sequence=0,
            key_id="configured-simulation",
            authenticated=True,
        )


class FailClosedPayloadInventoryProvider:
    """Live default until an independently reviewed payload controller is connected."""

    def snapshot(self, *, now_s: float) -> PayloadInventorySnapshot:
        return PayloadInventorySnapshot(
            observed_at_s=now_s,
            source_id="payload-controller-unavailable",
            controller_healthy=None,
            installed_slots=None,
            simulation_only=False,
            protocol_version=None,
            module_id=None,
            interlock_healthy=None,
            sequence=None,
            key_id=None,
            authenticated=None,
        )


class FilePayloadInventoryProvider:
    """Reads an authenticated controller status file without writing to the controller."""

    def __init__(
        self,
        path: str | Path,
        *,
        hmac_key: bytes | None = None,
        expected_key_id: str | None = None,
    ) -> None:
        if hmac_key is not None and not hmac_key:
            raise ValueError("payload inventory HMAC key cannot be empty")
        if expected_key_id is not None and hmac_key is None:
            raise ValueError("expected_key_id requires an HMAC key")
        self.path = Path(path)
        self.hmac_key = hmac_key
        self.expected_key_id = expected_key_id
        self._highest_sequence: int | None = None
        self._highest_sequence_digest: str | None = None

    def snapshot(self, *, now_s: float) -> PayloadInventorySnapshot:
        del now_s
        snapshot = load_payload_inventory_snapshot(
            self.path,
            hmac_key=self.hmac_key,
            expected_key_id=self.expected_key_id,
        )
        if snapshot.sequence is None:
            raise ValueError("payload inventory sequence is missing")
        document_digest = _payload_inventory_document_digest(self.path)
        if self._highest_sequence is not None and snapshot.sequence < self._highest_sequence:
            raise ValueError("payload inventory sequence moved backwards")
        if (
            self._highest_sequence is not None
            and snapshot.sequence == self._highest_sequence
            and document_digest != self._highest_sequence_digest
        ):
            raise ValueError("payload inventory content changed without a new sequence")
        if self._highest_sequence is None or snapshot.sequence > self._highest_sequence:
            self._highest_sequence = snapshot.sequence
            self._highest_sequence_digest = document_digest
        return snapshot


def verify_payload_inventory(
    config: MissionConfig,
    snapshot: PayloadInventorySnapshot,
    *,
    now_s: float,
) -> PayloadInventoryVerification:
    if not math.isfinite(now_s) or now_s < 0:
        raise ValueError("payload inventory now_s must be a finite non-negative number")
    reasons: list[str] = []
    if not isinstance(snapshot.source_id, str) or not snapshot.source_id.strip():
        reasons.append("payload inventory source identity is not confirmed")
    if snapshot.protocol_version != PAYLOAD_INVENTORY_PROTOCOL_VERSION:
        reasons.append("payload inventory protocol version is not supported")
    if snapshot.sequence is None or snapshot.sequence < 0:
        reasons.append("payload inventory sequence is invalid")
    if not isinstance(snapshot.module_id, str) or not snapshot.module_id.strip():
        reasons.append("payload module identity is not confirmed")
    if snapshot.interlock_healthy is not True:
        reasons.append("payload module interlock health is not confirmed")
    if not snapshot.simulation_only and snapshot.authenticated is not True:
        reasons.append("payload inventory report is not authenticated")
    if not math.isfinite(snapshot.observed_at_s) or snapshot.observed_at_s < 0:
        reasons.append("payload inventory timestamp is invalid")
    else:
        age_s = now_s - snapshot.observed_at_s
        if age_s < 0 or age_s > config.safety.sensor_data_max_age_seconds:
            reasons.append("payload inventory is stale")
    if snapshot.controller_healthy is not True:
        reasons.append("payload controller health is not confirmed")
    observed = snapshot.installed_slots
    if observed is None:
        reasons.append("installed payload inventory is unknown")
    else:
        observed_by_id = {slot.slot_id: slot for slot in observed}
        if len(observed_by_id) != len(observed):
            reasons.append("observed payload slot IDs are not unique")
        expected_by_id = {payload.slot_id: payload for payload in config.payloads}
        if set(observed_by_id) != set(expected_by_id):
            reasons.append("observed payload slots do not match the mission configuration")
        for slot_id, expected in expected_by_id.items():
            slot = observed_by_id.get(slot_id)
            if slot is None:
                continue
            if not slot.present:
                reasons.append(f"payload slot {slot_id} is not present")
            if slot.presence_sensor_healthy is not True:
                reasons.append(f"payload slot {slot_id} presence sensor is not healthy")
            if slot.payload_type != expected.payload_type:
                reasons.append(f"payload slot {slot_id} type does not match")
            if slot.state is not PayloadState.LOCKED:
                reasons.append(f"payload slot {slot_id} is not locked")
    return PayloadInventoryVerification(
        allowed=not reasons,
        source_id=snapshot.source_id,
        reasons=tuple(reasons),
        simulation_only=snapshot.simulation_only,
    )


def load_payload_inventory_snapshot(
    path: str | Path,
    *,
    hmac_key: bytes | None = None,
    expected_key_id: str | None = None,
) -> PayloadInventorySnapshot:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("payload inventory report must be a JSON object")
    authenticated: bool | None = None
    if hmac_key is not None:
        _verify_report_signature(raw, hmac_key=hmac_key, expected_key_id=expected_key_id)
        authenticated = True
    slots_raw = raw.get("installed_slots")
    if not isinstance(slots_raw, list):
        raise ValueError("payload inventory installed_slots must be an array")
    try:
        slots = tuple(
            ObservedPayloadSlot(
                slot_id=str(item["slot_id"]),
                payload_type=str(item["payload_type"]),
                state=PayloadState(item["state"]),
                present=_strict_bool(item["present"]),
                presence_sensor_healthy=_strict_bool(item["presence_sensor_healthy"]),
            )
            for item in slots_raw
        )
        return PayloadInventorySnapshot(
            observed_at_s=float(raw["observed_at_s"]),
            source_id=str(raw["source_id"]),
            controller_healthy=_strict_bool(raw["controller_healthy"]),
            installed_slots=slots,
            simulation_only=_strict_bool(raw["simulation_only"]),
            protocol_version=int(raw["protocol_version"]),
            module_id=str(raw["module_id"]),
            interlock_healthy=_strict_bool(raw["interlock_healthy"]),
            sequence=int(raw["sequence"]),
            key_id=(str(raw["key_id"]) if raw.get("key_id") is not None else None),
            authenticated=authenticated,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid payload inventory report: {exc}") from exc


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("payload inventory boolean fields must be true or false")
    return value


def sign_payload_inventory_document(document: dict[str, object], *, hmac_key: bytes) -> str:
    if not hmac_key:
        raise ValueError("payload inventory HMAC key cannot be empty")
    return hmac.new(hmac_key, _canonical_report_bytes(document), hashlib.sha256).hexdigest()


def _payload_inventory_document_digest(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("payload inventory report must be a JSON object")
    return hashlib.sha256(_canonical_report_bytes(document)).hexdigest()


def _verify_report_signature(
    document: dict[str, object],
    *,
    hmac_key: bytes,
    expected_key_id: str | None,
) -> None:
    signature = document.get("signature_hmac_sha256")
    key_id = document.get("key_id")
    if not isinstance(signature, str) or len(signature) != 64:
        raise ValueError("payload inventory signature is missing or invalid")
    if expected_key_id is not None and key_id != expected_key_id:
        raise ValueError("payload inventory key ID does not match")
    expected = sign_payload_inventory_document(document, hmac_key=hmac_key)
    if not hmac.compare_digest(signature.lower(), expected):
        raise ValueError("payload inventory HMAC verification failed")


def _canonical_report_bytes(document: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature_hmac_sha256"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def unavailable_inventory_verification(
    *,
    source_id: str,
    reason: str,
) -> PayloadInventoryVerification:
    return PayloadInventoryVerification(
        allowed=False,
        source_id=source_id,
        reasons=(reason,),
        simulation_only=False,
    )


__all__ = [
    "ConfiguredSimulationPayloadInventoryProvider",
    "FailClosedPayloadInventoryProvider",
    "FilePayloadInventoryProvider",
    "ObservedPayloadSlot",
    "PAYLOAD_INVENTORY_PROTOCOL_VERSION",
    "PayloadInventoryProvider",
    "PayloadInventorySnapshot",
    "PayloadInventoryVerification",
    "load_payload_inventory_snapshot",
    "sign_payload_inventory_document",
    "unavailable_inventory_verification",
    "verify_payload_inventory",
]

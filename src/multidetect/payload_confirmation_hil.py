from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from .mission import MissionController

PAYLOAD_CONFIRMATION_HIL_PROTOCOL_VERSION = 1
PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES = 2048


class PayloadConfirmationHilError(ValueError):
    """Independent payload confirmation evidence violated its HIL contract."""


@dataclass(frozen=True, slots=True)
class PayloadConfirmationHilMessage:
    mission_id: str
    sensor_id: str
    release_id: str
    payload_slot_id: str
    payload_absent: bool
    sensor_healthy: bool
    observed_at_s: float
    sequence: int
    key_id: str
    protocol_version: int = PAYLOAD_CONFIRMATION_HIL_PROTOCOL_VERSION
    message_type: str = "independent_payload_confirmation"
    simulation_only: bool = True
    inert_load_required: bool = True
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        for name in ("mission_id", "sensor_id", "release_id", "payload_slot_id", "key_id"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.payload_absent, bool):
            raise PayloadConfirmationHilError("payload_absent must be boolean")
        if not isinstance(self.sensor_healthy, bool):
            raise PayloadConfirmationHilError("sensor_healthy must be boolean")
        _require_timestamp(self.observed_at_s, "observed_at_s")
        _require_nonnegative_int(self.sequence, "sequence")
        if self.protocol_version != PAYLOAD_CONFIRMATION_HIL_PROTOCOL_VERSION:
            raise PayloadConfirmationHilError("confirmation HIL protocol version is unsupported")
        if self.message_type != "independent_payload_confirmation":
            raise PayloadConfirmationHilError("confirmation HIL message type is invalid")
        if self.simulation_only is not True:
            raise PayloadConfirmationHilError("confirmation HIL must be simulation-only")
        if self.inert_load_required is not True:
            raise PayloadConfirmationHilError("confirmation HIL requires an inert load")
        if self.physical_release_enabled is not False:
            raise PayloadConfirmationHilError("confirmation HIL cannot enable physical release")


@dataclass(frozen=True, slots=True)
class PayloadConfirmationHilVerification:
    valid: bool
    reasons: tuple[str, ...]
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class PayloadConfirmationHilReceipt:
    message: PayloadConfirmationHilMessage
    verification: PayloadConfirmationHilVerification
    mission_advanced: bool
    simulation_only: bool = True
    physical_release_enabled: bool = False


class PayloadConfirmationHilCodec:
    """Canonical JSON and a dedicated HMAC key for independent sensor evidence."""

    def __init__(self, *, hmac_key: bytes, expected_key_id: str) -> None:
        if len(hmac_key) < 32:
            raise ValueError("confirmation HIL HMAC key must contain at least 32 bytes")
        self.expected_key_id = _require_text(expected_key_id, "expected_key_id")
        self.hmac_key = hmac_key

    def encode(self, message: PayloadConfirmationHilMessage) -> bytes:
        document = asdict(message)
        if message.key_id != self.expected_key_id:
            raise PayloadConfirmationHilError("confirmation HIL key ID does not match codec")
        signature = hmac.new(
            self.hmac_key,
            _canonical_bytes(document),
            hashlib.sha256,
        ).hexdigest()
        encoded = json.dumps(
            {**document, "signature_hmac_sha256": signature},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES:
            raise PayloadConfirmationHilError("confirmation HIL message exceeds size limit")
        return encoded

    def decode(self, encoded: bytes) -> PayloadConfirmationHilMessage:
        if not encoded or len(encoded) > PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES:
            raise PayloadConfirmationHilError("confirmation HIL message size is invalid")
        try:
            document = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayloadConfirmationHilError(
                "confirmation HIL message is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(document, dict):
            raise PayloadConfirmationHilError("confirmation HIL message must be an object")
        if document.get("key_id") != self.expected_key_id:
            raise PayloadConfirmationHilError("confirmation HIL key ID does not match")
        signature = document.get("signature_hmac_sha256")
        if not isinstance(signature, str) or len(signature) != 64:
            raise PayloadConfirmationHilError("confirmation HIL signature is missing or invalid")
        expected = hmac.new(
            self.hmac_key,
            _canonical_bytes(document),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected):
            raise PayloadConfirmationHilError("confirmation HIL HMAC verification failed")
        unsigned = {key: value for key, value in document.items() if key != "signature_hmac_sha256"}
        try:
            return PayloadConfirmationHilMessage(**unsigned)
        except (TypeError, ValueError) as exc:
            raise PayloadConfirmationHilError(f"invalid confirmation HIL message: {exc}") from exc


class PayloadConfirmationHilGuard:
    """Bind independent sensor evidence to one release and reject replay mutation."""

    def __init__(
        self,
        *,
        mission_id: str,
        release_id: str,
        payload_slot_id: str,
        release_requested_at_s: float,
        controller_module_id: str,
        allowed_sensor_ids: frozenset[str],
        maximum_age_s: float,
    ) -> None:
        self.mission_id = _require_text(mission_id, "mission_id")
        self.release_id = _require_text(release_id, "release_id")
        self.payload_slot_id = _require_text(payload_slot_id, "payload_slot_id")
        self.controller_module_id = _require_text(controller_module_id, "controller_module_id")
        _require_timestamp(release_requested_at_s, "release_requested_at_s")
        self.release_requested_at_s = release_requested_at_s
        if not allowed_sensor_ids:
            raise ValueError("confirmation HIL requires at least one allowed sensor")
        normalized = frozenset(_require_text(item, "sensor_id") for item in allowed_sensor_ids)
        if self.controller_module_id in normalized:
            raise ValueError("confirmation sensor identity must differ from controller module")
        if not math.isfinite(maximum_age_s) or maximum_age_s <= 0:
            raise ValueError("confirmation HIL maximum age must be finite and positive")
        self.allowed_sensor_ids = normalized
        self.maximum_age_s = maximum_age_s
        self._highest_sequence: int | None = None
        self._digests_by_sequence: dict[int, bytes] = {}

    def verify(
        self,
        message: PayloadConfirmationHilMessage,
        *,
        now_s: float,
    ) -> PayloadConfirmationHilVerification:
        _require_timestamp(now_s, "now_s")
        reasons: list[str] = []
        if message.mission_id != self.mission_id:
            reasons.append("confirmation HIL mission ID does not match")
        if message.release_id != self.release_id:
            reasons.append("confirmation HIL release ID does not match")
        if message.payload_slot_id != self.payload_slot_id:
            reasons.append("confirmation HIL payload slot does not match")
        if message.sensor_id == self.controller_module_id:
            reasons.append("confirmation HIL source is not independent from the controller")
        if message.sensor_id not in self.allowed_sensor_ids:
            reasons.append("confirmation HIL sensor is not allowed")
        if not message.sensor_healthy:
            reasons.append("confirmation HIL sensor health is not confirmed")
        if not message.payload_absent:
            reasons.append("confirmation HIL does not report payload departure")
        age_s = now_s - message.observed_at_s
        if age_s < 0 or age_s > self.maximum_age_s:
            reasons.append("confirmation HIL evidence is stale")
        if message.observed_at_s < self.release_requested_at_s:
            reasons.append("confirmation HIL evidence predates the release request")
        digest = _canonical_bytes(asdict(message))
        existing = self._digests_by_sequence.get(message.sequence)
        if existing is not None:
            if existing != digest:
                reasons.append("confirmation HIL content changed without a new sequence")
                return PayloadConfirmationHilVerification(False, tuple(reasons))
            return PayloadConfirmationHilVerification(
                not reasons,
                tuple(reasons),
                idempotent_replay=True,
            )
        if self._highest_sequence is not None and message.sequence <= self._highest_sequence:
            reasons.append("confirmation HIL sequence did not increase")
        if reasons:
            return PayloadConfirmationHilVerification(False, tuple(reasons))
        self._highest_sequence = message.sequence
        self._digests_by_sequence[message.sequence] = digest
        return PayloadConfirmationHilVerification(True, ())


class MissionPayloadConfirmationHilAdapter:
    """Apply authenticated independent departure evidence to an active mission release."""

    def __init__(
        self,
        *,
        mission: MissionController,
        release_id: str,
        controller_module_id: str,
        allowed_sensor_ids: frozenset[str],
        codec: PayloadConfirmationHilCodec,
        maximum_age_s: float = 1.0,
    ) -> None:
        binding = mission.payload_release_binding(release_id=release_id)
        self.mission = mission
        self.release_id = release_id
        self.codec = codec
        self.guard = PayloadConfirmationHilGuard(
            mission_id=binding.mission_id,
            release_id=binding.release_id,
            payload_slot_id=binding.payload_slot_id,
            release_requested_at_s=binding.requested_at_s,
            controller_module_id=controller_module_id,
            allowed_sensor_ids=allowed_sensor_ids,
            maximum_age_s=maximum_age_s,
        )

    def accept(self, encoded: bytes, *, now_s: float) -> PayloadConfirmationHilReceipt:
        try:
            message = self.codec.decode(encoded)
        except PayloadConfirmationHilError as exc:
            self.mission.audit.append(
                "payload.independent_hil_decode_rejected",
                now_s,
                {"release_id": self.release_id, "error_type": type(exc).__name__},
            )
            raise
        verification = self.guard.verify(message, now_s=now_s)
        if not verification.valid:
            self.mission.audit.append(
                "payload.independent_hil_evidence_rejected",
                now_s,
                {
                    "release_id": self.release_id,
                    "sensor_id": message.sensor_id,
                    "reasons": verification.reasons,
                },
            )
            raise PayloadConfirmationHilError(
                "independent confirmation rejected: " + "; ".join(verification.reasons)
            )
        if verification.idempotent_replay:
            return PayloadConfirmationHilReceipt(message, verification, mission_advanced=False)
        self.mission.report_independent_confirmation(
            release_id=self.release_id,
            source_id=f"independent-hil:{message.sensor_id}",
            now_s=now_s,
        )
        self.mission.audit.append(
            "payload.independent_hil_confirmation_accepted",
            now_s,
            {
                "release_id": self.release_id,
                "sensor_id": message.sensor_id,
                "sequence": message.sequence,
                "simulation_only": True,
                "physical_release_enabled": False,
            },
        )
        return PayloadConfirmationHilReceipt(message, verification, mission_advanced=True)


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature_hmac_sha256"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadConfirmationHilError(f"{name} must be a non-empty string")
    return value.strip()


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayloadConfirmationHilError(f"{name} must be a non-negative integer")
    return value


def _require_timestamp(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadConfirmationHilError(f"{name} must be finite and non-negative")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise PayloadConfirmationHilError(f"{name} must be finite and non-negative")
    return converted


__all__ = [
    "PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES",
    "PAYLOAD_CONFIRMATION_HIL_PROTOCOL_VERSION",
    "MissionPayloadConfirmationHilAdapter",
    "PayloadConfirmationHilCodec",
    "PayloadConfirmationHilError",
    "PayloadConfirmationHilGuard",
    "PayloadConfirmationHilMessage",
    "PayloadConfirmationHilReceipt",
    "PayloadConfirmationHilVerification",
]

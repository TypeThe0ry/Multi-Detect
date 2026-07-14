from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from .compat import StrEnum

PAYLOAD_HIL_PROTOCOL_VERSION = 1
PAYLOAD_HIL_MAX_MESSAGE_BYTES = 4096


class PayloadHilProtocolError(ValueError):
    """An authenticated HIL payload message violated its protocol contract."""


class PayloadHilResultStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PayloadHilReleaseRequest:
    mission_id: str
    module_id: str
    release_id: str
    payload_slot_id: str
    payload_type: str
    authorization_challenge_id: str
    operator_id: str
    target_id: str
    target_revision: int
    scene_digest: str
    ruleset_version: str
    requested_at_s: float
    expires_at_s: float
    sequence: int
    key_id: str
    protocol_version: int = PAYLOAD_HIL_PROTOCOL_VERSION
    message_type: str = "release_request"
    simulation_only: bool = True
    inert_load_required: bool = True
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "mission_id",
            "module_id",
            "release_id",
            "payload_slot_id",
            "payload_type",
            "authorization_challenge_id",
            "operator_id",
            "target_id",
            "scene_digest",
            "ruleset_version",
            "key_id",
        ):
            _require_text(getattr(self, name), name)
        _require_nonnegative_int(self.target_revision, "target_revision")
        _require_nonnegative_int(self.sequence, "sequence")
        _require_timestamp(self.requested_at_s, "requested_at_s")
        _require_timestamp(self.expires_at_s, "expires_at_s")
        if self.expires_at_s <= self.requested_at_s:
            raise PayloadHilProtocolError("expires_at_s must be after requested_at_s")
        _require_hil_safety_flags(
            protocol_version=self.protocol_version,
            message_type=self.message_type,
            expected_message_type="release_request",
            simulation_only=self.simulation_only,
            inert_load_required=self.inert_load_required,
            physical_release_enabled=self.physical_release_enabled,
        )


@dataclass(frozen=True, slots=True)
class PayloadHilResult:
    mission_id: str
    module_id: str
    release_id: str
    payload_slot_id: str
    status: PayloadHilResultStatus
    observed_at_s: float
    sequence: int
    key_id: str
    controller_healthy: bool
    interlock_healthy: bool
    reason: str | None = None
    protocol_version: int = PAYLOAD_HIL_PROTOCOL_VERSION
    message_type: str = "release_result"
    simulation_only: bool = True
    inert_load_required: bool = True
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        for name in ("mission_id", "module_id", "release_id", "payload_slot_id", "key_id"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.status, PayloadHilResultStatus):
            raise PayloadHilProtocolError("status must be a PayloadHilResultStatus")
        _require_timestamp(self.observed_at_s, "observed_at_s")
        _require_nonnegative_int(self.sequence, "sequence")
        if not isinstance(self.controller_healthy, bool):
            raise PayloadHilProtocolError("controller_healthy must be boolean")
        if not isinstance(self.interlock_healthy, bool):
            raise PayloadHilProtocolError("interlock_healthy must be boolean")
        if self.reason is not None:
            _require_text(self.reason, "reason")
        if self.status in {PayloadHilResultStatus.REJECTED, PayloadHilResultStatus.FAILED}:
            if self.reason is None:
                raise PayloadHilProtocolError("rejected or failed results require a reason")
        _require_hil_safety_flags(
            protocol_version=self.protocol_version,
            message_type=self.message_type,
            expected_message_type="release_result",
            simulation_only=self.simulation_only,
            inert_load_required=self.inert_load_required,
            physical_release_enabled=self.physical_release_enabled,
        )


@dataclass(frozen=True, slots=True)
class PayloadHilVerification:
    valid: bool
    reasons: tuple[str, ...]
    idempotent_replay: bool = False


class PayloadHilCodec:
    """Canonical JSON plus HMAC-SHA256 for inert payload HIL messages."""

    def __init__(self, *, hmac_key: bytes, expected_key_id: str) -> None:
        if len(hmac_key) < 32:
            raise ValueError("payload HIL HMAC key must contain at least 32 bytes")
        _require_text(expected_key_id, "expected_key_id")
        self.hmac_key = hmac_key
        self.expected_key_id = expected_key_id.strip()

    def encode_request(self, request: PayloadHilReleaseRequest) -> bytes:
        return self._encode(asdict(request))

    def decode_request(self, encoded: bytes) -> PayloadHilReleaseRequest:
        document = self._decode_document(encoded, expected_message_type="release_request")
        try:
            return PayloadHilReleaseRequest(**_without_signature(document))
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadHilProtocolError(f"invalid payload HIL request: {exc}") from exc

    def encode_result(self, result: PayloadHilResult) -> bytes:
        document = asdict(result)
        document["status"] = result.status.value
        return self._encode(document)

    def decode_result(self, encoded: bytes) -> PayloadHilResult:
        document = self._decode_document(encoded, expected_message_type="release_result")
        unsigned = _without_signature(document)
        try:
            unsigned["status"] = PayloadHilResultStatus(unsigned["status"])
            return PayloadHilResult(**unsigned)
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadHilProtocolError(f"invalid payload HIL result: {exc}") from exc

    def _encode(self, document: dict[str, Any]) -> bytes:
        if document.get("key_id") != self.expected_key_id:
            raise PayloadHilProtocolError("payload HIL key ID does not match codec")
        unsigned = _canonical_bytes(document)
        signature = hmac.new(self.hmac_key, unsigned, hashlib.sha256).hexdigest()
        encoded = json.dumps(
            {**document, "signature_hmac_sha256": signature},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > PAYLOAD_HIL_MAX_MESSAGE_BYTES:
            raise PayloadHilProtocolError("payload HIL message exceeds the size limit")
        return encoded

    def _decode_document(
        self,
        encoded: bytes,
        *,
        expected_message_type: str,
    ) -> dict[str, Any]:
        if not encoded or len(encoded) > PAYLOAD_HIL_MAX_MESSAGE_BYTES:
            raise PayloadHilProtocolError("payload HIL message size is invalid")
        try:
            document = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayloadHilProtocolError("payload HIL message is not valid UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise PayloadHilProtocolError("payload HIL message must be a JSON object")
        if document.get("message_type") != expected_message_type:
            raise PayloadHilProtocolError("payload HIL message type does not match")
        if document.get("key_id") != self.expected_key_id:
            raise PayloadHilProtocolError("payload HIL key ID does not match")
        signature = document.get("signature_hmac_sha256")
        if not isinstance(signature, str) or len(signature) != 64:
            raise PayloadHilProtocolError("payload HIL signature is missing or invalid")
        expected = hmac.new(
            self.hmac_key,
            _canonical_bytes(document),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected):
            raise PayloadHilProtocolError("payload HIL HMAC verification failed")
        return document


class PayloadHilRequestGuard:
    """Controller-side HIL binding, freshness, replay and idempotency guard."""

    def __init__(
        self,
        *,
        mission_id: str,
        module_id: str,
        installed_slots: dict[str, str],
        maximum_age_s: float,
    ) -> None:
        _require_text(mission_id, "mission_id")
        _require_text(module_id, "module_id")
        if not installed_slots:
            raise ValueError("payload HIL installed slots cannot be empty")
        for slot_id, payload_type in installed_slots.items():
            _require_text(slot_id, "payload_slot_id")
            _require_text(payload_type, "payload_type")
        if not math.isfinite(maximum_age_s) or maximum_age_s <= 0:
            raise ValueError("payload HIL maximum age must be finite and positive")
        self.mission_id = mission_id.strip()
        self.module_id = module_id.strip()
        self.installed_slots = dict(installed_slots)
        self.maximum_age_s = maximum_age_s
        self._highest_sequence: int | None = None
        self._requests_by_release_id: dict[str, bytes] = {}
        self._active_release_id: str | None = None

    def verify(self, request: PayloadHilReleaseRequest, *, now_s: float) -> PayloadHilVerification:
        _require_timestamp(now_s, "now_s")
        reasons: list[str] = []
        if request.mission_id != self.mission_id:
            reasons.append("payload HIL mission ID does not match")
        if request.module_id != self.module_id:
            reasons.append("payload HIL module ID does not match")
        expected_payload_type = self.installed_slots.get(request.payload_slot_id)
        if expected_payload_type is None:
            reasons.append("payload HIL slot is not installed")
        elif request.payload_type != expected_payload_type:
            reasons.append("payload HIL payload type does not match the installed slot")
        age_s = now_s - request.requested_at_s
        if age_s < 0 or age_s > self.maximum_age_s or now_s >= request.expires_at_s:
            reasons.append("payload HIL request is stale or outside its validity window")
        fingerprint = _canonical_bytes(asdict(request))
        existing = self._requests_by_release_id.get(request.release_id)
        if existing is not None:
            if existing != fingerprint:
                reasons.append("payload HIL release ID was reused with different content")
                return PayloadHilVerification(False, tuple(reasons))
            return PayloadHilVerification(not reasons, tuple(reasons), idempotent_replay=True)
        if self._highest_sequence is not None and request.sequence <= self._highest_sequence:
            reasons.append("payload HIL sequence did not increase")
        if self._active_release_id is not None:
            reasons.append("payload HIL interlock already has an active release")
        if reasons:
            return PayloadHilVerification(False, tuple(reasons))
        self._highest_sequence = request.sequence
        self._requests_by_release_id[request.release_id] = fingerprint
        self._active_release_id = request.release_id
        return PayloadHilVerification(True, ())

    def finish(self, *, release_id: str) -> None:
        _require_text(release_id, "release_id")
        if self._active_release_id != release_id:
            raise PayloadHilProtocolError("payload HIL finish does not match the active release")
        self._active_release_id = None


class PayloadHilResultGuard:
    """Aircraft-side correlation and replay guard for authenticated controller results."""

    def __init__(
        self,
        *,
        request: PayloadHilReleaseRequest,
        maximum_age_s: float,
    ) -> None:
        if not math.isfinite(maximum_age_s) or maximum_age_s <= 0:
            raise ValueError("payload HIL result maximum age must be finite and positive")
        self.request = request
        self.maximum_age_s = maximum_age_s
        self._highest_sequence: int | None = None
        self._result_digests: dict[int, bytes] = {}
        self._last_status: PayloadHilResultStatus | None = None

    def verify(self, result: PayloadHilResult, *, now_s: float) -> PayloadHilVerification:
        _require_timestamp(now_s, "now_s")
        reasons: list[str] = []
        request = self.request
        if result.mission_id != request.mission_id:
            reasons.append("payload HIL result mission ID does not match")
        if result.module_id != request.module_id:
            reasons.append("payload HIL result module ID does not match")
        if result.release_id != request.release_id:
            reasons.append("payload HIL result release ID does not match")
        if result.payload_slot_id != request.payload_slot_id:
            reasons.append("payload HIL result slot does not match")
        age_s = now_s - result.observed_at_s
        if age_s < 0 or age_s > self.maximum_age_s:
            reasons.append("payload HIL result is stale")
        if result.observed_at_s < request.requested_at_s:
            reasons.append("payload HIL result predates its request")
        digest = _canonical_bytes({**asdict(result), "status": result.status.value})
        existing = self._result_digests.get(result.sequence)
        if existing is not None:
            if existing != digest:
                reasons.append("payload HIL result changed without a new sequence")
                return PayloadHilVerification(False, tuple(reasons))
            return PayloadHilVerification(not reasons, tuple(reasons), idempotent_replay=True)
        if self._highest_sequence is not None and result.sequence <= self._highest_sequence:
            reasons.append("payload HIL result sequence did not increase")
        if not _result_transition_allowed(self._last_status, result.status):
            reasons.append("payload HIL result status transition is invalid")
        if result.status in {PayloadHilResultStatus.ACCEPTED, PayloadHilResultStatus.EXECUTED}:
            if not result.controller_healthy or not result.interlock_healthy:
                reasons.append("payload HIL result claims success without healthy interlocks")
        if reasons:
            return PayloadHilVerification(False, tuple(reasons))
        self._highest_sequence = result.sequence
        self._result_digests[result.sequence] = digest
        self._last_status = result.status
        return PayloadHilVerification(True, ())


def _without_signature(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "signature_hmac_sha256"}


def _result_transition_allowed(
    previous: PayloadHilResultStatus | None,
    current: PayloadHilResultStatus,
) -> bool:
    if previous is None:
        return True
    allowed = {
        PayloadHilResultStatus.ACCEPTED: frozenset(
            {
                PayloadHilResultStatus.ACCEPTED,
                PayloadHilResultStatus.EXECUTED,
                PayloadHilResultStatus.FAILED,
            }
        ),
        PayloadHilResultStatus.REJECTED: frozenset({PayloadHilResultStatus.REJECTED}),
        PayloadHilResultStatus.EXECUTED: frozenset({PayloadHilResultStatus.EXECUTED}),
        PayloadHilResultStatus.FAILED: frozenset({PayloadHilResultStatus.FAILED}),
    }
    return current in allowed[previous]


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        _without_signature(document),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadHilProtocolError(f"{name} must be a non-empty string")
    return value.strip()


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayloadHilProtocolError(f"{name} must be a non-negative integer")
    return value


def _require_timestamp(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadHilProtocolError(f"{name} must be a finite non-negative number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise PayloadHilProtocolError(f"{name} must be a finite non-negative number")
    return converted


def _require_hil_safety_flags(
    *,
    protocol_version: object,
    message_type: object,
    expected_message_type: str,
    simulation_only: object,
    inert_load_required: object,
    physical_release_enabled: object,
) -> None:
    if protocol_version != PAYLOAD_HIL_PROTOCOL_VERSION:
        raise PayloadHilProtocolError("payload HIL protocol version is unsupported")
    if message_type != expected_message_type:
        raise PayloadHilProtocolError("payload HIL message type is invalid")
    if simulation_only is not True:
        raise PayloadHilProtocolError("payload HIL messages must be simulation-only")
    if inert_load_required is not True:
        raise PayloadHilProtocolError("payload HIL messages require an inert load")
    if physical_release_enabled is not False:
        raise PayloadHilProtocolError("payload HIL messages cannot enable physical release")


__all__ = [
    "PAYLOAD_HIL_MAX_MESSAGE_BYTES",
    "PAYLOAD_HIL_PROTOCOL_VERSION",
    "PayloadHilCodec",
    "PayloadHilProtocolError",
    "PayloadHilReleaseRequest",
    "PayloadHilRequestGuard",
    "PayloadHilResult",
    "PayloadHilResultGuard",
    "PayloadHilResultStatus",
    "PayloadHilVerification",
]

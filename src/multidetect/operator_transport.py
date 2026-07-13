from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from math import isfinite

from .operator_link import (
    AuthorizationDecisionAcceptance,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    SelectionAcceptance,
    SelectionCommandGuard,
    TargetSelectionCommand,
)
from .operator_protocol import (
    AuthorizationDecisionAck,
    AuthorizationDecisionAckReason,
    OperatorProtocolError,
    OperatorTunnelCodec,
    SelectionAck,
    SelectionAckReason,
    WireMessageType,
)


class SelectionDeliveryTimeout(TimeoutError):
    """Raised when a selection command has exhausted its bounded retry budget."""


class AuthorizationDecisionDeliveryTimeout(TimeoutError):
    """Raised when a G20 authorization decision exhausts its bounded retry budget."""


@dataclass(frozen=True, slots=True)
class ServerSelectionResult:
    command: TargetSelectionCommand
    acceptance: SelectionAcceptance
    acknowledgement_payload: bytes
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ServerAuthorizationDecisionResult:
    command: AuthorizationDecisionCommand
    acceptance: AuthorizationDecisionAcceptance
    acknowledgement_payload: bytes
    duplicate: bool


@dataclass(frozen=True, slots=True)
class _CachedSelection:
    fingerprint: bytes
    acceptance: SelectionAcceptance
    acknowledgement: SelectionAck


class SelectionCommandServer:
    """Jetson-side authenticated selection endpoint with idempotent retransmission handling."""

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        guard: SelectionCommandGuard,
        *,
        cache_size: int = 256,
    ) -> None:
        if cache_size <= 0:
            raise ValueError("selection command cache_size must be positive")
        self.codec = codec
        self.guard = guard
        self._cache_size = cache_size
        self._cache: dict[str, _CachedSelection] = {}
        self._order: deque[str] = deque()

    def handle_selection(
        self,
        payload: bytes,
        *,
        received_at_s: float,
        acknowledgement_sequence: int,
    ) -> ServerSelectionResult:
        packet = self.codec.decode(payload)
        if packet.message_type is not WireMessageType.TARGET_SELECTION:
            raise OperatorProtocolError(
                "Jetson selection endpoint received a non-selection message"
            )
        command = packet.message
        if not isinstance(command, TargetSelectionCommand):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded selection message has the wrong type")
        fingerprint = hashlib.sha256(payload).digest()
        cached = self._cache.get(command.command_id)
        duplicate = cached is not None
        if cached is not None and cached.fingerprint == fingerprint:
            acceptance = cached.acceptance
            acknowledgement = cached.acknowledgement
        elif cached is not None:
            acceptance = SelectionAcceptance(
                False,
                ("command ID was reused with different authenticated content",),
            )
            acknowledgement = SelectionAck(
                command.command_id,
                False,
                SelectionAckReason.COMMAND_ID_CONFLICT,
                command.sequence,
            )
        else:
            acceptance = self.guard.evaluate(command, received_at_s=received_at_s)
            acknowledgement = SelectionAck(
                command.command_id,
                acceptance.allowed,
                _ack_reason(acceptance),
                command.sequence,
            )
            self._remember(command.command_id, fingerprint, acceptance, acknowledgement)
        acknowledgement_payload = self.codec.encode_ack(
            acknowledgement,
            sequence=acknowledgement_sequence,
            sent_at_s=received_at_s,
        )
        return ServerSelectionResult(
            command,
            acceptance,
            acknowledgement_payload,
            duplicate,
        )

    def _remember(
        self,
        command_id: str,
        fingerprint: bytes,
        acceptance: SelectionAcceptance,
        acknowledgement: SelectionAck,
    ) -> None:
        if len(self._order) >= self._cache_size:
            oldest = self._order.popleft()
            del self._cache[oldest]
        self._order.append(command_id)
        self._cache[command_id] = _CachedSelection(
            fingerprint,
            acceptance,
            acknowledgement,
        )


class AuthorizationDecisionCommandServer:
    """Jetson-side authenticated decision endpoint bound to the current challenge guard."""

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        guard: AuthorizationDecisionCommandGuard,
    ) -> None:
        self.codec = codec
        self.guard = guard

    def handle_decision(
        self,
        payload: bytes,
        *,
        received_at_s: float,
        acknowledgement_sequence: int,
    ) -> ServerAuthorizationDecisionResult:
        packet = self.codec.decode(payload)
        if packet.message_type is not WireMessageType.AUTHORIZATION_DECISION:
            raise OperatorProtocolError(
                "Jetson authorization endpoint received a non-decision message"
            )
        command = packet.message
        if not isinstance(command, AuthorizationDecisionCommand):
            raise OperatorProtocolError("decoded authorization decision has the wrong type")
        acceptance = self.guard.evaluate(command, received_at_s=received_at_s)
        acknowledgement = AuthorizationDecisionAck(
            command_token=command.command_token,
            accepted=acceptance.allowed,
            reason=_authorization_ack_reason(acceptance),
            acknowledged_sequence=command.sequence,
        )
        acknowledgement_payload = self.codec.encode_authorization_ack(
            acknowledgement,
            sequence=acknowledgement_sequence,
            sent_at_s=received_at_s,
        )
        return ServerAuthorizationDecisionResult(
            command,
            acceptance,
            acknowledgement_payload,
            acceptance.duplicate,
        )


class SelectionRetryClient:
    """G20-side single-command delivery state with bounded identical retransmissions."""

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        command: TargetSelectionCommand,
        *,
        retry_interval_s: float = 0.25,
        maximum_attempts: int = 3,
    ) -> None:
        if not isfinite(retry_interval_s) or retry_interval_s <= 0.0:
            raise ValueError("retry_interval_s must be finite and positive")
        if maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        ttl_s = command.expires_at_s - command.issued_at_s
        if (maximum_attempts - 1) * retry_interval_s >= ttl_s:
            raise ValueError("selection retry budget must fit inside the command TTL")
        self.codec = codec
        self.command = command
        self.retry_interval_s = retry_interval_s
        self.maximum_attempts = maximum_attempts
        self.payload = codec.encode_selection(command)
        self.attempts = 0
        self.next_attempt_at_s = command.issued_at_s
        self.acknowledgement: SelectionAck | None = None
        self.timed_out = False

    @property
    def completed(self) -> bool:
        return self.acknowledgement is not None

    def poll(self, *, now_s: float) -> bytes | None:
        if not isfinite(now_s) or now_s < 0.0:
            raise ValueError("now_s must be finite and non-negative")
        if self.completed:
            return None
        if self.timed_out:
            raise SelectionDeliveryTimeout("selection delivery has already timed out")
        if now_s < self.next_attempt_at_s:
            return None
        if self.attempts >= self.maximum_attempts:
            self.timed_out = True
            raise SelectionDeliveryTimeout("selection acknowledgement retry budget exhausted")
        if now_s > self.command.expires_at_s:
            self.timed_out = True
            raise SelectionDeliveryTimeout("selection command expired before acknowledgement")
        self.attempts += 1
        self.next_attempt_at_s = now_s + self.retry_interval_s
        return self.payload

    def handle_acknowledgement(self, payload: bytes) -> SelectionAck:
        packet = self.codec.decode(payload)
        if packet.message_type is not WireMessageType.SELECTION_ACK:
            raise OperatorProtocolError(
                "G20 selection client received a non-acknowledgement message"
            )
        acknowledgement = packet.message
        if not isinstance(acknowledgement, SelectionAck):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded acknowledgement has the wrong type")
        if acknowledgement.command_id != self.command.command_id:
            raise OperatorProtocolError("selection acknowledgement command ID does not match")
        if acknowledgement.acknowledged_sequence != self.command.sequence:
            raise OperatorProtocolError("selection acknowledgement sequence does not match")
        if self.acknowledgement is not None and acknowledgement != self.acknowledgement:
            raise OperatorProtocolError("selection acknowledgement changed after completion")
        self.acknowledgement = acknowledgement
        return acknowledgement


class AuthorizationDecisionRetryClient:
    """G20-side bounded identical retry state for one authorization decision."""

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        command: AuthorizationDecisionCommand,
        *,
        retry_interval_s: float = 0.25,
        maximum_attempts: int = 3,
    ) -> None:
        if not isfinite(retry_interval_s) or retry_interval_s <= 0:
            raise ValueError("authorization retry interval must be finite and positive")
        if maximum_attempts <= 0:
            raise ValueError("authorization maximum attempts must be positive")
        ttl_s = command.expires_at_s - command.issued_at_s
        if (maximum_attempts - 1) * retry_interval_s >= ttl_s:
            raise ValueError("authorization retry budget must fit inside command TTL")
        self.codec = codec
        self.command = command
        self.retry_interval_s = retry_interval_s
        self.maximum_attempts = maximum_attempts
        self.payload = codec.encode_authorization_decision(command)
        self.attempts = 0
        self.next_attempt_at_s = command.issued_at_s
        self.acknowledgement: AuthorizationDecisionAck | None = None
        self.timed_out = False

    @property
    def completed(self) -> bool:
        return self.acknowledgement is not None

    def poll(self, *, now_s: float) -> bytes | None:
        if not isfinite(now_s) or now_s < 0:
            raise ValueError("authorization retry timestamp must be finite and non-negative")
        if self.completed:
            return None
        if self.timed_out:
            raise AuthorizationDecisionDeliveryTimeout(
                "authorization delivery has already timed out"
            )
        if now_s < self.next_attempt_at_s:
            return None
        if self.attempts >= self.maximum_attempts:
            self.timed_out = True
            raise AuthorizationDecisionDeliveryTimeout(
                "authorization acknowledgement retry budget exhausted"
            )
        if now_s > self.command.expires_at_s:
            self.timed_out = True
            raise AuthorizationDecisionDeliveryTimeout(
                "authorization command expired before acknowledgement"
            )
        self.attempts += 1
        self.next_attempt_at_s = now_s + self.retry_interval_s
        return self.payload

    def handle_acknowledgement(self, payload: bytes) -> AuthorizationDecisionAck:
        packet = self.codec.decode(payload)
        if packet.message_type is not WireMessageType.AUTHORIZATION_ACK:
            raise OperatorProtocolError("G20 authorization client received a non-authorization ACK")
        acknowledgement = packet.message
        if not isinstance(acknowledgement, AuthorizationDecisionAck):
            raise OperatorProtocolError("decoded authorization ACK has the wrong type")
        if acknowledgement.command_token != self.command.command_token:
            raise OperatorProtocolError("authorization ACK command token does not match")
        if acknowledgement.acknowledged_sequence != self.command.sequence:
            raise OperatorProtocolError("authorization ACK sequence does not match")
        if self.acknowledgement is not None and acknowledgement != self.acknowledgement:
            raise OperatorProtocolError("authorization ACK changed after completion")
        self.acknowledgement = acknowledgement
        return acknowledgement


def _ack_reason(acceptance: SelectionAcceptance) -> SelectionAckReason:
    if acceptance.allowed:
        return SelectionAckReason.ACCEPTED
    combined = " ".join(acceptance.reasons).lower()
    if "stale" in combined:
        return SelectionAckReason.STALE
    if "stream" in combined:
        return SelectionAckReason.STREAM_MISMATCH
    if "dimensions" in combined or "rotation" in combined:
        return SelectionAckReason.GEOMETRY_MISMATCH
    if "sequence" in combined:
        return SelectionAckReason.SEQUENCE_REJECTED
    if "future" in combined:
        return SelectionAckReason.FUTURE_TIMESTAMP
    return SelectionAckReason.INVALID


def _authorization_ack_reason(
    acceptance: AuthorizationDecisionAcceptance,
) -> AuthorizationDecisionAckReason:
    if acceptance.allowed:
        return AuthorizationDecisionAckReason.ACCEPTED
    combined = " ".join(acceptance.reasons).lower()
    if "no pending" in combined:
        return AuthorizationDecisionAckReason.NO_ACTIVE_CHALLENGE
    if "does not match" in combined:
        return AuthorizationDecisionAckReason.CHALLENGE_MISMATCH
    if "expired" in combined or "stale" in combined or "outlives" in combined:
        return AuthorizationDecisionAckReason.EXPIRED
    if "sequence" in combined:
        return AuthorizationDecisionAckReason.SEQUENCE_REJECTED
    if "different content" in combined:
        return AuthorizationDecisionAckReason.COMMAND_TOKEN_CONFLICT
    if "already has a decision" in combined:
        return AuthorizationDecisionAckReason.ALREADY_DECIDED
    return AuthorizationDecisionAckReason.INVALID


__all__ = [
    "AuthorizationDecisionCommandServer",
    "AuthorizationDecisionDeliveryTimeout",
    "AuthorizationDecisionRetryClient",
    "SelectionCommandServer",
    "SelectionDeliveryTimeout",
    "SelectionRetryClient",
    "ServerSelectionResult",
    "ServerAuthorizationDecisionResult",
]

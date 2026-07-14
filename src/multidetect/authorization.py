from __future__ import annotations

import hmac
import math
import secrets
import threading
import uuid
from dataclasses import dataclass, replace

from .compat import StrEnum
from .config import MissionConfig
from .domain import AuthorizationChallenge, AuthorizationGrant, DeploymentDecision


class AuthorizationError(RuntimeError):
    """Base error for an invalid or unavailable operator authorization."""


class AuthorizationNotFound(AuthorizationError):
    pass


class AuthorizationExpired(AuthorizationError):
    pass


class AuthorizationDenied(AuthorizationError):
    pass


class AuthorizationConsumed(AuthorizationError):
    pass


class AuthorizationBindingError(AuthorizationError):
    pass


class AuthorizationStateError(AuthorizationError):
    pass


class AuthorizationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ConsumedAuthorization:
    challenge: AuthorizationChallenge
    grant: AuthorizationGrant
    consumed_at_s: float


@dataclass(slots=True)
class _AuthorizationRecord:
    challenge: AuthorizationChallenge
    status: AuthorizationStatus = AuthorizationStatus.PENDING
    grant: AuthorizationGrant | None = None


class AuthorizationService:
    """In-memory, short-lived and single-use authorization registry.

    Challenges are bound to the complete safety decision identity.  The lock is
    part of the safety contract: two concurrent consumers cannot both receive a
    valid authorization from the same challenge.
    """

    def __init__(self, ttl_seconds: float) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")
        self._ttl_seconds = ttl_seconds
        self._records: dict[str, _AuthorizationRecord] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: MissionConfig) -> AuthorizationService:
        return cls(config.safety.authorization_ttl_seconds)

    def create_challenge(
        self,
        *,
        mission_id: str,
        payload_slot_id: str,
        decision: DeploymentDecision,
        now_s: float,
    ) -> AuthorizationChallenge:
        self._validate_time(now_s)
        if not mission_id.strip():
            raise AuthorizationError("mission_id cannot be empty")
        if not payload_slot_id.strip():
            raise AuthorizationError("payload_slot_id cannot be empty")
        if not decision.allowed:
            raise AuthorizationError("cannot authorize a denied safety decision")
        if not decision.target_id or not decision.scene_digest or not decision.ruleset_version:
            raise AuthorizationError("safety decision has incomplete authorization bindings")
        if decision.target_revision < 0:
            raise AuthorizationError("target revision cannot be negative")

        with self._lock:
            challenge_id = uuid.uuid4().hex
            while challenge_id in self._records:
                challenge_id = uuid.uuid4().hex
            challenge = AuthorizationChallenge(
                challenge_id=challenge_id,
                nonce=secrets.token_urlsafe(24),
                mission_id=mission_id,
                target_id=decision.target_id,
                target_revision=decision.target_revision,
                payload_slot_id=payload_slot_id,
                scene_digest=decision.scene_digest,
                ruleset_version=decision.ruleset_version,
                created_at_s=now_s,
                expires_at_s=now_s + self._ttl_seconds,
            )
            self._records[challenge_id] = _AuthorizationRecord(challenge=challenge)
            return challenge

    def approve(
        self,
        *,
        challenge_id: str,
        nonce: str,
        operator_id: str,
        now_s: float,
    ) -> AuthorizationGrant:
        return self._decide(
            challenge_id=challenge_id,
            nonce=nonce,
            operator_id=operator_id,
            approved=True,
            now_s=now_s,
        )

    def deny(
        self,
        *,
        challenge_id: str,
        nonce: str,
        operator_id: str,
        now_s: float,
    ) -> AuthorizationGrant:
        return self._decide(
            challenge_id=challenge_id,
            nonce=nonce,
            operator_id=operator_id,
            approved=False,
            now_s=now_s,
        )

    def consume(
        self,
        *,
        challenge_id: str,
        nonce: str,
        mission_id: str,
        target_id: str,
        target_revision: int,
        payload_slot_id: str,
        scene_digest: str,
        ruleset_version: str,
        now_s: float,
    ) -> ConsumedAuthorization:
        self._validate_time(now_s)
        with self._lock:
            record = self._get_record(challenge_id)
            self._validate_event_time(record.challenge, now_s)
            self._expire_record_if_needed(record, now_s)
            self._raise_unless_approved(record)
            self._verify_nonce(record.challenge, nonce)

            expected = {
                "mission_id": record.challenge.mission_id,
                "target_id": record.challenge.target_id,
                "target_revision": record.challenge.target_revision,
                "payload_slot_id": record.challenge.payload_slot_id,
                "scene_digest": record.challenge.scene_digest,
                "ruleset_version": record.challenge.ruleset_version,
            }
            actual = {
                "mission_id": mission_id,
                "target_id": target_id,
                "target_revision": target_revision,
                "payload_slot_id": payload_slot_id,
                "scene_digest": scene_digest,
                "ruleset_version": ruleset_version,
            }
            mismatches = tuple(key for key in expected if expected[key] != actual[key])
            if mismatches:
                raise AuthorizationBindingError(
                    "authorization binding mismatch: " + ", ".join(mismatches)
                )

            if record.grant is None:  # Defensive; APPROVED always has a grant.
                raise AuthorizationStateError("approved challenge has no grant")
            record.status = AuthorizationStatus.CONSUMED
            return ConsumedAuthorization(
                challenge=record.challenge,
                grant=record.grant,
                consumed_at_s=now_s,
            )

    def expire(self, *, now_s: float) -> tuple[str, ...]:
        self._validate_time(now_s)
        with self._lock:
            expired: list[str] = []
            for challenge_id, record in self._records.items():
                previous = record.status
                self._expire_record_if_needed(record, now_s)
                if (
                    previous is not AuthorizationStatus.EXPIRED
                    and record.status is AuthorizationStatus.EXPIRED
                ):
                    expired.append(challenge_id)
            return tuple(expired)

    def status(self, challenge_id: str, *, now_s: float | None = None) -> AuthorizationStatus:
        with self._lock:
            record = self._get_record(challenge_id)
            if now_s is not None:
                self._validate_time(now_s)
                self._expire_record_if_needed(record, now_s)
            return record.status

    def get_challenge(self, challenge_id: str) -> AuthorizationChallenge:
        with self._lock:
            return self._get_record(challenge_id).challenge

    def refresh_equivalent_binding(
        self,
        *,
        challenge_id: str,
        previous_scene_digest: str,
        decision: DeploymentDecision,
        now_s: float,
    ) -> AuthorizationChallenge:
        """Move an unexpired challenge to a safety-equivalent live snapshot.

        The challenge identity, nonce, target identity, ruleset and original
        expiry remain unchanged. The mission layer must establish semantic
        continuity before calling this optimistic, digest-guarded operation.
        """

        self._validate_time(now_s)
        if not decision.allowed:
            raise AuthorizationBindingError("cannot refresh to a denied decision")
        with self._lock:
            record = self._get_record(challenge_id)
            challenge = record.challenge
            self._validate_event_time(challenge, now_s)
            if now_s >= challenge.expires_at_s:
                if record.status in {
                    AuthorizationStatus.PENDING,
                    AuthorizationStatus.APPROVED,
                }:
                    record.status = AuthorizationStatus.EXPIRED
                raise AuthorizationExpired("authorization challenge has expired")
            if record.status not in {
                AuthorizationStatus.PENDING,
                AuthorizationStatus.APPROVED,
                AuthorizationStatus.CONSUMED,
            }:
                raise AuthorizationStateError(
                    f"cannot refresh challenge while it is {record.status.value}"
                )
            if challenge.scene_digest != previous_scene_digest:
                raise AuthorizationBindingError("authorization refresh used a stale scene digest")
            if decision.target_id != challenge.target_id:
                raise AuthorizationBindingError("authorization refresh changed target identity")
            if decision.ruleset_version != challenge.ruleset_version:
                raise AuthorizationBindingError("authorization refresh changed ruleset version")
            refreshed = replace(
                challenge,
                target_revision=decision.target_revision,
                scene_digest=decision.scene_digest,
            )
            record.challenge = refreshed
            return refreshed

    def _decide(
        self,
        *,
        challenge_id: str,
        nonce: str,
        operator_id: str,
        approved: bool,
        now_s: float,
    ) -> AuthorizationGrant:
        self._validate_time(now_s)
        if not operator_id.strip():
            raise AuthorizationError("operator_id cannot be empty")
        with self._lock:
            record = self._get_record(challenge_id)
            self._validate_event_time(record.challenge, now_s)
            self._expire_record_if_needed(record, now_s)
            if record.status is AuthorizationStatus.EXPIRED:
                raise AuthorizationExpired("authorization challenge has expired")
            if record.status is not AuthorizationStatus.PENDING:
                raise AuthorizationStateError(
                    f"authorization challenge is already {record.status.value}"
                )
            self._verify_nonce(record.challenge, nonce)
            grant = AuthorizationGrant(
                challenge_id=challenge_id,
                operator_id=operator_id,
                approved=approved,
                granted_at_s=now_s,
            )
            record.grant = grant
            record.status = AuthorizationStatus.APPROVED if approved else AuthorizationStatus.DENIED
            return grant

    def _raise_unless_approved(self, record: _AuthorizationRecord) -> None:
        if record.status is AuthorizationStatus.APPROVED:
            return
        if record.status is AuthorizationStatus.EXPIRED:
            raise AuthorizationExpired("authorization challenge has expired")
        if record.status is AuthorizationStatus.DENIED:
            raise AuthorizationDenied("operator denied the authorization challenge")
        if record.status is AuthorizationStatus.CONSUMED:
            raise AuthorizationConsumed("authorization challenge has already been consumed")
        raise AuthorizationStateError("authorization challenge has not been approved")

    def _get_record(self, challenge_id: str) -> _AuthorizationRecord:
        try:
            return self._records[challenge_id]
        except KeyError as exc:
            raise AuthorizationNotFound("authorization challenge was not found") from exc

    @staticmethod
    def _verify_nonce(challenge: AuthorizationChallenge, nonce: str) -> None:
        if not nonce or not hmac.compare_digest(challenge.nonce, nonce):
            raise AuthorizationBindingError("authorization nonce does not match")

    @staticmethod
    def _validate_time(now_s: float) -> None:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")

    @staticmethod
    def _validate_event_time(challenge: AuthorizationChallenge, now_s: float) -> None:
        if now_s < challenge.created_at_s:
            raise AuthorizationError("authorization event predates the challenge")

    @staticmethod
    def _expire_record_if_needed(record: _AuthorizationRecord, now_s: float) -> None:
        if (
            record.status in {AuthorizationStatus.PENDING, AuthorizationStatus.APPROVED}
            and now_s >= record.challenge.expires_at_s
        ):
            record.status = AuthorizationStatus.EXPIRED


__all__ = [
    "AuthorizationBindingError",
    "AuthorizationConsumed",
    "AuthorizationDenied",
    "AuthorizationError",
    "AuthorizationExpired",
    "AuthorizationNotFound",
    "AuthorizationService",
    "AuthorizationStateError",
    "AuthorizationStatus",
    "ConsumedAuthorization",
]

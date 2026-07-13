from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, field

from .authorization import ConsumedAuthorization
from .config import MissionConfig
from .domain import DeploymentDecision, PayloadState, StateTransitionError


class PayloadInterlockError(StateTransitionError):
    """Raised when a release would violate the single-slot interlock."""


class PayloadFeedbackError(StateTransitionError):
    """Raised for missing, contradictory or incorrectly correlated feedback."""


@dataclass(frozen=True, slots=True)
class FakeReleaseRequest:
    release_id: str
    payload_slot_id: str
    requested_at_s: float


class FakePayloadPort:
    """A deterministic simulation sink; this package exposes no hardware port."""

    def __init__(self) -> None:
        self._requests: dict[str, FakeReleaseRequest] = {}
        self._lock = threading.RLock()

    def submit_simulated_release(
        self, *, release_id: str, payload_slot_id: str, requested_at_s: float
    ) -> FakeReleaseRequest:
        request = FakeReleaseRequest(
            release_id=release_id,
            payload_slot_id=payload_slot_id,
            requested_at_s=requested_at_s,
        )
        with self._lock:
            existing = self._requests.get(release_id)
            if existing is not None:
                if existing.payload_slot_id != payload_slot_id:
                    raise PayloadInterlockError(
                        "release_id is already bound to another payload slot"
                    )
                return existing
            self._requests[release_id] = request
            return request

    @property
    def requests(self) -> tuple[FakeReleaseRequest, ...]:
        with self._lock:
            return tuple(self._requests.values())

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)


@dataclass(frozen=True, slots=True)
class PayloadSlotSnapshot:
    payload_slot_id: str
    payload_type: str
    state: PayloadState
    release_id: str | None
    requested_at_s: float | None
    execution_reported_at_s: float | None
    independent_confirmation_sources: tuple[str, ...]
    uncertain_release: bool
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class PayloadReleaseBinding:
    """Immutable authorization and scene identity bound to one simulated release."""

    mission_id: str
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
    authorization_expires_at_s: float


@dataclass(slots=True)
class _PayloadSlotRecord:
    payload_slot_id: str
    payload_type: str
    state: PayloadState = PayloadState.LOCKED
    authorization: ConsumedAuthorization | None = None
    release_id: str | None = None
    requested_at_s: float | None = None
    execution_reported_at_s: float | None = None
    independent_confirmation_sources: set[str] = field(default_factory=set)
    uncertain_release: bool = False
    failure_reason: str | None = None


_ACTIVE_STATES = frozenset(
    {PayloadState.ARMED, PayloadState.RELEASE_REQUESTED, PayloadState.RELEASED}
)


class PayloadController:
    """Strict payload state machine backed exclusively by ``FakePayloadPort``.

    A confirmation requires both an execution report and a separately sourced
    observation.  A timeout or correlation fault is terminal for the controller
    instance, so no retry or next-slot release can happen automatically.
    """

    def __init__(self, config: MissionConfig, port: FakePayloadPort) -> None:
        if type(port) is not FakePayloadPort:
            raise TypeError("PayloadController accepts only FakePayloadPort")
        self._config = config
        self._port = port
        self._records = {
            payload.slot_id: _PayloadSlotRecord(payload.slot_id, payload.payload_type)
            for payload in config.payloads
        }
        self._release_to_slot: dict[str, str] = {}
        self._faulted = False
        self._fault_reason: str | None = None
        self._lock = threading.RLock()

    @property
    def faulted(self) -> bool:
        with self._lock:
            return self._faulted

    @property
    def fault_reason(self) -> str | None:
        with self._lock:
            return self._fault_reason

    @property
    def active_slot_id(self) -> str | None:
        with self._lock:
            active = [
                record.payload_slot_id
                for record in self._records.values()
                if record.state in _ACTIVE_STATES
            ]
            if len(active) > 1:  # Defensive assertion of the core interlock invariant.
                raise PayloadInterlockError("more than one payload slot is active")
            return active[0] if active else None

    @property
    def remaining_payload_count(self) -> int:
        with self._lock:
            return sum(
                record.state in {PayloadState.LOCKED, PayloadState.ARMED}
                for record in self._records.values()
            )

    @property
    def confirmed_release_count(self) -> int:
        with self._lock:
            return sum(
                record.state is PayloadState.RELEASE_CONFIRMED for record in self._records.values()
            )

    def slots(self) -> tuple[PayloadSlotSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshot(record) for record in self._records.values())

    def get_slot(self, payload_slot_id: str) -> PayloadSlotSnapshot:
        with self._lock:
            return self._snapshot(self._get_record(payload_slot_id))

    def release_binding(self, release_id: str) -> PayloadReleaseBinding:
        """Return the exact consumed authorization binding for a submitted release."""

        if not release_id.strip():
            raise ValueError("release_id cannot be empty")
        with self._lock:
            payload_slot_id = self._release_to_slot.get(release_id)
            if payload_slot_id is None:
                raise PayloadFeedbackError("unknown release_id")
            record = self._get_record(payload_slot_id)
            if record.release_id != release_id or record.requested_at_s is None:
                raise PayloadFeedbackError("release record is incomplete")
            if record.authorization is None:
                raise PayloadFeedbackError("release authorization binding is unavailable")
            challenge = record.authorization.challenge
            grant = record.authorization.grant
            return PayloadReleaseBinding(
                mission_id=self._config.mission_id,
                release_id=release_id,
                payload_slot_id=record.payload_slot_id,
                payload_type=record.payload_type,
                authorization_challenge_id=challenge.challenge_id,
                operator_id=grant.operator_id,
                target_id=challenge.target_id,
                target_revision=challenge.target_revision,
                scene_digest=challenge.scene_digest,
                ruleset_version=challenge.ruleset_version,
                requested_at_s=record.requested_at_s,
                authorization_expires_at_s=challenge.expires_at_s,
            )

    def arm(
        self,
        *,
        payload_slot_id: str,
        authorization: ConsumedAuthorization,
        now_s: float,
    ) -> PayloadSlotSnapshot:
        self._validate_time(now_s)
        with self._lock:
            self._ensure_operational()
            record = self._get_record(payload_slot_id)
            if record.state is not PayloadState.LOCKED:
                raise StateTransitionError(f"cannot arm slot from state {record.state.value}")
            if self.active_slot_id is not None:
                raise PayloadInterlockError("another payload slot already holds the interlock")
            self._validate_authorization(
                authorization=authorization,
                payload_slot_id=payload_slot_id,
                now_s=now_s,
            )
            record.authorization = authorization
            record.state = PayloadState.ARMED
            return self._snapshot(record)

    def lock(self, *, payload_slot_id: str) -> PayloadSlotSnapshot:
        """Revoke a pre-release arm; in-flight or terminal releases cannot be relocked."""

        with self._lock:
            record = self._get_record(payload_slot_id)
            if record.state is PayloadState.LOCKED:
                return self._snapshot(record)
            if record.state is not PayloadState.ARMED:
                raise StateTransitionError(f"cannot lock slot from state {record.state.value}")
            record.state = PayloadState.LOCKED
            record.authorization = None
            return self._snapshot(record)

    def refresh_armed_authorization(
        self,
        *,
        payload_slot_id: str,
        previous_scene_digest: str,
        authorization: ConsumedAuthorization,
        now_s: float,
    ) -> PayloadSlotSnapshot:
        """Rebind an armed fake bay to a safety-equivalent live snapshot."""

        self._validate_time(now_s)
        with self._lock:
            self._ensure_operational()
            record = self._get_record(payload_slot_id)
            if record.state is not PayloadState.ARMED or record.authorization is None:
                raise StateTransitionError("only an armed slot authorization can be refreshed")
            previous = record.authorization
            if previous.challenge.scene_digest != previous_scene_digest:
                raise PayloadInterlockError("payload authorization refresh used a stale scene")
            if previous.grant != authorization.grant:
                raise PayloadInterlockError("payload authorization refresh changed operator grant")
            if previous.consumed_at_s != authorization.consumed_at_s:
                raise PayloadInterlockError(
                    "payload authorization refresh changed consumption time"
                )
            old_challenge = previous.challenge
            new_challenge = authorization.challenge
            if (
                old_challenge.challenge_id,
                old_challenge.nonce,
                old_challenge.mission_id,
                old_challenge.target_id,
                old_challenge.payload_slot_id,
                old_challenge.ruleset_version,
                old_challenge.created_at_s,
                old_challenge.expires_at_s,
            ) != (
                new_challenge.challenge_id,
                new_challenge.nonce,
                new_challenge.mission_id,
                new_challenge.target_id,
                new_challenge.payload_slot_id,
                new_challenge.ruleset_version,
                new_challenge.created_at_s,
                new_challenge.expires_at_s,
            ):
                raise PayloadInterlockError(
                    "payload authorization refresh changed immutable bindings"
                )
            self._validate_authorization(
                authorization=authorization,
                payload_slot_id=payload_slot_id,
                now_s=now_s,
            )
            record.authorization = authorization
            return self._snapshot(record)

    def request_release(
        self,
        *,
        payload_slot_id: str,
        decision: DeploymentDecision,
        now_s: float,
        release_id: str | None = None,
    ) -> str:
        self._validate_time(now_s)
        release_id = release_id or uuid.uuid4().hex
        if not release_id.strip():
            raise ValueError("release_id cannot be empty")

        with self._lock:
            existing_slot_id = self._release_to_slot.get(release_id)
            if existing_slot_id is not None:
                if existing_slot_id != payload_slot_id:
                    self._enter_fault(
                        "release_id was reused for a different payload slot",
                        uncertain=True,
                    )
                    raise PayloadInterlockError(
                        "release_id is already bound to another payload slot"
                    )
                return release_id

            self._ensure_operational()
            record = self._get_record(payload_slot_id)
            if record.state is not PayloadState.ARMED:
                raise StateTransitionError(
                    f"cannot request release from state {record.state.value}"
                )
            if record.authorization is None:
                raise PayloadInterlockError("armed slot is missing consumed authorization")
            self._validate_release_decision(
                record=record,
                decision=decision,
                now_s=now_s,
            )

            record.release_id = release_id
            record.requested_at_s = now_s
            record.state = PayloadState.RELEASE_REQUESTED
            self._release_to_slot[release_id] = payload_slot_id
            try:
                self._port.submit_simulated_release(
                    release_id=release_id,
                    payload_slot_id=payload_slot_id,
                    requested_at_s=now_s,
                )
            except Exception as exc:
                self._enter_fault(
                    f"simulated release request failed: {type(exc).__name__}",
                    uncertain=True,
                )
                raise StateTransitionError("simulated release request failed") from exc
            return release_id

    def report_execution(
        self, *, release_id: str, payload_slot_id: str, now_s: float
    ) -> PayloadSlotSnapshot:
        self._validate_time(now_s)
        with self._lock:
            record = self._record_for_feedback(release_id, payload_slot_id)
            if record.state is PayloadState.RELEASE_CONFIRMED:
                return self._snapshot(record)
            if record.state is PayloadState.FAILED:
                raise PayloadFeedbackError("release transaction has already failed")
            if record.state is PayloadState.RELEASED:
                return self._snapshot(record)
            if record.state is not PayloadState.RELEASE_REQUESTED:
                self._enter_fault("execution report arrived in an invalid state", uncertain=True)
                raise PayloadFeedbackError("execution report arrived in an invalid state")
            record.execution_reported_at_s = now_s
            record.state = PayloadState.RELEASED
            self._confirm_if_complete(record)
            return self._snapshot(record)

    def report_independent_confirmation(
        self,
        *,
        release_id: str,
        payload_slot_id: str,
        source_id: str,
        now_s: float,
    ) -> PayloadSlotSnapshot:
        self._validate_time(now_s)
        if not source_id.strip():
            raise ValueError("source_id cannot be empty")
        with self._lock:
            record = self._record_for_feedback(release_id, payload_slot_id)
            if record.state is PayloadState.RELEASE_CONFIRMED:
                return self._snapshot(record)
            if record.state is PayloadState.FAILED:
                raise PayloadFeedbackError("release transaction has already failed")
            if record.state not in {PayloadState.RELEASE_REQUESTED, PayloadState.RELEASED}:
                self._enter_fault(
                    "independent confirmation arrived in an invalid state",
                    uncertain=True,
                )
                raise PayloadFeedbackError("independent confirmation arrived in an invalid state")
            record.independent_confirmation_sources.add(source_id.strip())
            self._confirm_if_complete(record)
            return self._snapshot(record)

    def fail_release(
        self,
        *,
        release_id: str,
        payload_slot_id: str,
        reason: str,
        uncertain: bool = True,
    ) -> PayloadSlotSnapshot:
        if not reason.strip():
            raise ValueError("failure reason cannot be empty")
        with self._lock:
            record = self._record_for_feedback(release_id, payload_slot_id)
            if record.state is PayloadState.RELEASE_CONFIRMED:
                raise PayloadFeedbackError("confirmed release cannot transition to failed")
            if record.state is PayloadState.FAILED:
                return self._snapshot(record)
            if record.state not in {PayloadState.RELEASE_REQUESTED, PayloadState.RELEASED}:
                raise PayloadFeedbackError("release failure arrived in an invalid state")
            self._enter_fault(reason.strip(), uncertain=uncertain)
            return self._snapshot(record)

    def check_timeouts(self, *, now_s: float) -> tuple[PayloadSlotSnapshot, ...]:
        self._validate_time(now_s)
        with self._lock:
            timed_out: list[PayloadSlotSnapshot] = []
            timeout_s = self._config.safety.release_confirmation_timeout_seconds
            for record in self._records.values():
                if record.state not in {
                    PayloadState.RELEASE_REQUESTED,
                    PayloadState.RELEASED,
                }:
                    continue
                if record.requested_at_s is None:
                    self._enter_fault("active release has no request timestamp", uncertain=True)
                    timed_out.append(self._snapshot(record))
                    continue
                if now_s - record.requested_at_s >= timeout_s:
                    self._enter_fault("release confirmation timed out", uncertain=True)
                    timed_out.append(self._snapshot(record))
            return tuple(timed_out)

    def _validate_authorization(
        self,
        *,
        authorization: ConsumedAuthorization,
        payload_slot_id: str,
        now_s: float,
    ) -> None:
        challenge = authorization.challenge
        grant = authorization.grant
        if not grant.approved:
            raise PayloadInterlockError("operator authorization is not approved")
        if grant.challenge_id != challenge.challenge_id:
            raise PayloadInterlockError("authorization grant does not match its challenge")
        if challenge.mission_id != self._config.mission_id:
            raise PayloadInterlockError("authorization belongs to another mission")
        if challenge.payload_slot_id != payload_slot_id:
            raise PayloadInterlockError("authorization belongs to another payload slot")
        if authorization.consumed_at_s > now_s:
            raise PayloadInterlockError("authorization consumption timestamp is in the future")
        if now_s >= challenge.expires_at_s:
            raise PayloadInterlockError("authorization has expired")

    def _validate_release_decision(
        self,
        *,
        record: _PayloadSlotRecord,
        decision: DeploymentDecision,
        now_s: float,
    ) -> None:
        authorization = record.authorization
        if authorization is None:  # Narrowing for type checkers and defensive safety.
            raise PayloadInterlockError("payload slot has no authorization")
        self._validate_authorization(
            authorization=authorization,
            payload_slot_id=record.payload_slot_id,
            now_s=now_s,
        )
        if not decision.allowed:
            raise PayloadInterlockError("latest safety decision denies deployment")
        if decision.evaluated_at_s > now_s:
            raise PayloadInterlockError("safety decision timestamp is in the future")
        if now_s - decision.evaluated_at_s > self._config.safety.sensor_data_max_age_seconds:
            raise PayloadInterlockError("safety decision is stale")

        challenge = authorization.challenge
        expected = (
            challenge.target_id,
            challenge.target_revision,
            challenge.scene_digest,
            challenge.ruleset_version,
        )
        actual = (
            decision.target_id,
            decision.target_revision,
            decision.scene_digest,
            decision.ruleset_version,
        )
        if actual != expected:
            raise PayloadInterlockError("latest safety decision no longer matches authorization")

    def _record_for_feedback(self, release_id: str, payload_slot_id: str) -> _PayloadSlotRecord:
        expected_slot_id = self._release_to_slot.get(release_id)
        if expected_slot_id is None:
            self._enter_fault("feedback referenced an unknown release_id", uncertain=True)
            raise PayloadFeedbackError("unknown release_id")
        if expected_slot_id != payload_slot_id:
            self._enter_fault("feedback referenced the wrong payload slot", uncertain=True)
            raise PayloadFeedbackError("feedback payload slot does not match release_id")
        return self._get_record(expected_slot_id)

    @staticmethod
    def _confirm_if_complete(record: _PayloadSlotRecord) -> None:
        if (
            record.state is PayloadState.RELEASED
            and record.execution_reported_at_s is not None
            and record.independent_confirmation_sources
        ):
            record.state = PayloadState.RELEASE_CONFIRMED
            record.authorization = None

    def _enter_fault(self, reason: str, *, uncertain: bool) -> None:
        self._faulted = True
        self._fault_reason = reason
        for record in self._records.values():
            if record.state is PayloadState.ARMED:
                record.state = PayloadState.LOCKED
                record.authorization = None
            elif record.state in {PayloadState.RELEASE_REQUESTED, PayloadState.RELEASED}:
                record.state = PayloadState.FAILED
                record.uncertain_release = uncertain
                record.failure_reason = reason
                record.authorization = None

    def _ensure_operational(self) -> None:
        if self._faulted:
            raise PayloadInterlockError(
                f"payload controller is faulted: {self._fault_reason or 'unknown reason'}"
            )

    def _get_record(self, payload_slot_id: str) -> _PayloadSlotRecord:
        try:
            return self._records[payload_slot_id]
        except KeyError as exc:
            raise PayloadInterlockError(f"unknown payload slot: {payload_slot_id}") from exc

    @staticmethod
    def _snapshot(record: _PayloadSlotRecord) -> PayloadSlotSnapshot:
        return PayloadSlotSnapshot(
            payload_slot_id=record.payload_slot_id,
            payload_type=record.payload_type,
            state=record.state,
            release_id=record.release_id,
            requested_at_s=record.requested_at_s,
            execution_reported_at_s=record.execution_reported_at_s,
            independent_confirmation_sources=tuple(sorted(record.independent_confirmation_sources)),
            uncertain_release=record.uncertain_release,
            failure_reason=record.failure_reason,
        )

    @staticmethod
    def _validate_time(now_s: float) -> None:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")


__all__ = [
    "FakePayloadPort",
    "FakeReleaseRequest",
    "PayloadController",
    "PayloadFeedbackError",
    "PayloadInterlockError",
    "PayloadReleaseBinding",
    "PayloadSlotSnapshot",
]

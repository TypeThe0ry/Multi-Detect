from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path

from .audit import AuditLog
from .authorization import (
    AuthorizationError,
    AuthorizationExpired,
    AuthorizationService,
    ConsumedAuthorization,
)
from .config import CompletionPolicy, MissionConfig
from .domain import (
    AuthorizationChallenge,
    BoundingBox,
    DeploymentDecision,
    FrameObservation,
    MissionPhase,
    PayloadState,
    SensorKind,
    TrackSnapshot,
)
from .payload import FakePayloadPort, PayloadController, PayloadSlotSnapshot
from .perception import fuse_rgb_thermal
from .ranking import TargetRanker, TargetRiskAssessment
from .safety import SafetyRuleEngine
from .state_machine import MissionStateMachine
from .tracking import IoUMultiObjectTracker


class MissionOperationError(RuntimeError):
    """Raised when an operator action is invalid for the current mission state."""


def _serialized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._operation_lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True, slots=True)
class ObservationOutcome:
    phase: MissionPhase
    tracks: tuple[TrackSnapshot, ...]
    decisions: tuple[DeploymentDecision, ...]
    challenge: AuthorizationChallenge | None


@dataclass(frozen=True, slots=True)
class MissionStatus:
    phase: MissionPhase
    active_target_id: str | None
    pending_challenge_id: str | None
    active_payload_slot_id: str | None
    active_release_id: str | None
    remaining_payload_count: int
    payload_slots: tuple[PayloadSlotSnapshot, ...]
    fault_reason: str | None


@dataclass(slots=True)
class _DeploymentContext:
    track: TrackSnapshot
    frame: FrameObservation
    decision: DeploymentDecision
    payload_slot_id: str
    challenge: AuthorizationChallenge
    authorization: ConsumedAuthorization | None = None
    release_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ServedTargetRegion:
    label: str
    bbox: BoundingBox
    served_at_s: float


class MissionController:
    """Coordinates evidence, authorization and a simulation-only payload transaction.

    No method in this class controls flight or physical hardware. The payload controller
    accepts only ``FakePayloadPort``, making the MVP suitable for replay and fault tests.
    """

    def __init__(self, config: MissionConfig) -> None:
        self.config = config
        self.state = MissionStateMachine()
        self.tracker = IoUMultiObjectTracker(config)
        self.rules = SafetyRuleEngine(config)
        self.authorizations = AuthorizationService.from_config(config)
        self.fake_payload_port = FakePayloadPort()
        self.payload = PayloadController(config, self.fake_payload_port)
        self.ranker = TargetRanker()
        self.audit = AuditLog()
        self._operation_lock = threading.RLock()
        self._context: _DeploymentContext | None = None
        self._served_target_regions: list[_ServedTargetRegion] = []
        self._last_now_s: float | None = None

    @_serialized
    def launch(self, *, now_s: float) -> None:
        self._validate_now(now_s)
        self._transition("launch", now_s)

    @_serialized
    def arrive_task_area(self, *, now_s: float) -> None:
        self._validate_now(now_s)
        self._transition("arrive_task_area", now_s)

    @_serialized
    def process_observation(
        self,
        frame: FrameObservation,
        *,
        now_s: float,
        assessments: Mapping[str, TargetRiskAssessment] | None = None,
    ) -> ObservationOutcome:
        self._validate_now(now_s)
        allowed_input_phases = {
            MissionPhase.SEARCHING,
            MissionPhase.AWAITING_AUTHORIZATION,
            MissionPhase.DEPLOYMENT_READY,
        }
        if self.state.phase not in allowed_input_phases:
            raise MissionOperationError(
                "observations cannot update mission evidence while phase is "
                f"{self.state.phase.value}"
            )
        self._expire_authorization_if_needed(now_s)
        if self.payload.faulted:
            self._transition("fault", now_s, reason=self.payload.fault_reason)
            raise MissionOperationError("payload controller is faulted")
        if self.payload.remaining_payload_count == 0:
            self._transition("request_return", now_s, reason="payload inventory exhausted")
            return ObservationOutcome(self.state.phase, (), (), None)

        fused_frame = self._fuse_frame(frame)
        tracks = self.tracker.update(fused_frame)
        if self.state.phase in {
            MissionPhase.AWAITING_AUTHORIZATION,
            MissionPhase.DEPLOYMENT_READY,
        }:
            refreshed_decision = self._refresh_active_context_if_equivalent(
                tracks=tracks,
                frame=fused_frame,
                now_s=now_s,
            )
            if refreshed_decision is not None:
                context = self._require_any_context()
                return ObservationOutcome(
                    self.state.phase,
                    tracks,
                    (refreshed_decision,),
                    (
                        context.challenge
                        if self.state.phase is MissionPhase.AWAITING_AUTHORIZATION
                        else None
                    ),
                )
            self._invalidate_for_changed_observation(now_s)
        target_labels = set(self.config.target_classes)
        suppressed_recently_served = tuple(
            track
            for track in tracks
            if track.confirmed
            and track.label in target_labels
            and self._is_recently_served_target(track, now_s)
        )
        candidates = tuple(
            track
            for track in tracks
            if track.confirmed
            and track.label in target_labels
            and not self._is_recently_served_target(track, now_s)
        )
        ranked = self.ranker.rank(candidates, assessments)
        score_by_target = {item.track.track_id: item.score for item in ranked}
        track_by_target = {track.track_id: track for track in candidates}

        decisions = tuple(
            replace(
                self.rules.evaluate(
                    track=track_by_target[item.track.track_id],
                    frame=fused_frame,
                    now_s=now_s,
                ),
                priority_score=item.score,
            )
            for item in ranked
        )
        self.audit.append(
            "perception.observation_evaluated",
            now_s,
            {
                "frame_id": frame.frame_id,
                "track_count": len(tracks),
                "confirmed_candidate_count": len(candidates),
                "eligible_candidate_count": sum(decision.allowed for decision in decisions),
                "recently_served_suppressed_count": len(suppressed_recently_served),
                "candidate_scores": score_by_target,
            },
        )
        if not candidates:
            return ObservationOutcome(self.state.phase, tracks, decisions, None)

        self._transition("target_confirmed", now_s)
        selected = next((decision for decision in decisions if decision.allowed), None)
        if selected is None:
            self.audit.append(
                "safety.all_candidates_denied",
                now_s,
                {
                    "frame_id": frame.frame_id,
                    "targets": [
                        {
                            "target_id": decision.target_id,
                            "reasons": decision.denial_reasons,
                        }
                        for decision in decisions
                    ],
                },
            )
            self._transition("safety_invalidated", now_s)
            return ObservationOutcome(self.state.phase, tracks, decisions, None)

        payload_slot_id = self._next_locked_payload_slot()
        if payload_slot_id is None:
            self._transition("fault", now_s, reason="no locked payload slot is available")
            raise MissionOperationError("no locked payload slot is available")
        challenge = self.authorizations.create_challenge(
            mission_id=self.config.mission_id,
            payload_slot_id=payload_slot_id,
            decision=selected,
            now_s=now_s,
        )
        self._context = _DeploymentContext(
            track=track_by_target[selected.target_id],
            frame=fused_frame,
            decision=selected,
            payload_slot_id=payload_slot_id,
            challenge=challenge,
        )
        self._transition("authorization_requested", now_s)
        self.audit.append(
            "authorization.challenge_created",
            now_s,
            {
                "challenge_id": challenge.challenge_id,
                "target_id": challenge.target_id,
                "target_revision": challenge.target_revision,
                "payload_slot_id": challenge.payload_slot_id,
                "expires_at_s": challenge.expires_at_s,
                "scene_digest": challenge.scene_digest,
                "ruleset_version": challenge.ruleset_version,
            },
        )
        return ObservationOutcome(self.state.phase, tracks, decisions, challenge)

    @_serialized
    def approve_authorization(
        self,
        *,
        challenge_id: str,
        nonce: str,
        operator_id: str,
        now_s: float,
    ) -> None:
        self._validate_now(now_s)
        context = self._require_context(MissionPhase.AWAITING_AUTHORIZATION)
        if challenge_id != context.challenge.challenge_id:
            raise MissionOperationError("challenge does not match the active mission context")
        if self._expire_authorization_if_needed(now_s):
            raise AuthorizationExpired("authorization challenge has expired")
        current_decision = self.rules.evaluate(
            track=context.track,
            frame=context.frame,
            now_s=now_s,
        )
        if not self._decision_matches_challenge(current_decision, context.challenge):
            self._invalidate_before_release(now_s, "safety evidence changed before approval")
            raise MissionOperationError("safety evidence changed before approval")
        try:
            self.authorizations.approve(
                challenge_id=challenge_id,
                nonce=nonce,
                operator_id=operator_id,
                now_s=now_s,
            )
            consumed = self.authorizations.consume(
                challenge_id=challenge_id,
                nonce=nonce,
                mission_id=self.config.mission_id,
                target_id=current_decision.target_id,
                target_revision=current_decision.target_revision,
                payload_slot_id=context.payload_slot_id,
                scene_digest=current_decision.scene_digest,
                ruleset_version=current_decision.ruleset_version,
                now_s=now_s,
            )
        except AuthorizationExpired:
            self._transition("authorization_expired", now_s)
            self._context = None
            raise
        except AuthorizationError:
            raise

        context.decision = current_decision
        context.authorization = consumed
        try:
            self.payload.arm(
                payload_slot_id=context.payload_slot_id,
                authorization=consumed,
                now_s=now_s,
            )
            self._transition("authorization_approved", now_s)
        except Exception as exc:
            self._transition("fault", now_s, reason=f"payload arm failed: {type(exc).__name__}")
            raise
        self.audit.append(
            "authorization.approved_and_consumed",
            now_s,
            {
                "challenge_id": challenge_id,
                "operator_id": operator_id,
                "target_id": context.track.track_id,
                "payload_slot_id": context.payload_slot_id,
            },
        )

    @_serialized
    def deny_authorization(
        self,
        *,
        challenge_id: str,
        nonce: str,
        operator_id: str,
        now_s: float,
    ) -> None:
        self._validate_now(now_s)
        context = self._require_context(MissionPhase.AWAITING_AUTHORIZATION)
        if challenge_id != context.challenge.challenge_id:
            raise MissionOperationError("challenge does not match the active mission context")
        if self._expire_authorization_if_needed(now_s):
            raise AuthorizationExpired("authorization challenge has expired")
        try:
            self.authorizations.deny(
                challenge_id=challenge_id,
                nonce=nonce,
                operator_id=operator_id,
                now_s=now_s,
            )
        except AuthorizationExpired:
            self._expire_authorization_if_needed(now_s, force=True)
            raise
        self.audit.append(
            "authorization.denied",
            now_s,
            {"challenge_id": challenge_id, "operator_id": operator_id},
        )
        self._transition("authorization_denied", now_s)
        self._context = None

    @_serialized
    def request_simulated_deployment(self, *, now_s: float) -> str:
        self._validate_now(now_s)
        context = self._require_context(MissionPhase.DEPLOYMENT_READY)
        if self._expire_authorization_if_needed(now_s):
            raise AuthorizationExpired("authorization challenge has expired")
        if context.authorization is None:
            raise MissionOperationError("active context has no consumed authorization")
        latest_decision = self.rules.evaluate(
            track=context.track,
            frame=context.frame,
            now_s=now_s,
        )
        if not self._decision_matches_challenge(latest_decision, context.challenge):
            self.payload.lock(payload_slot_id=context.payload_slot_id)
            self._invalidate_before_release(now_s, "atomic safety recheck failed")
            raise MissionOperationError("atomic safety recheck failed")

        context.decision = latest_decision
        self._transition("deployment_started", now_s)
        try:
            release_id = self.payload.request_release(
                payload_slot_id=context.payload_slot_id,
                decision=latest_decision,
                now_s=now_s,
            )
        except Exception as exc:
            self._transition(
                "deployment_failed",
                now_s,
                reason=f"simulated request failed: {type(exc).__name__}",
            )
            raise
        context.release_id = release_id
        self.audit.append(
            "payload.simulated_release_requested",
            now_s,
            {
                "release_id": release_id,
                "payload_slot_id": context.payload_slot_id,
                "target_id": context.track.track_id,
            },
        )
        return release_id

    @_serialized
    def report_simulated_execution(self, *, release_id: str, now_s: float) -> None:
        self._validate_now(now_s)
        context = self._require_context(MissionPhase.DEPLOYING)
        try:
            snapshot = self.payload.report_execution(
                release_id=release_id,
                payload_slot_id=context.payload_slot_id,
                now_s=now_s,
            )
        except Exception as exc:
            self.audit.append(
                "payload.execution_feedback_rejected",
                now_s,
                {"release_id": release_id, "error_type": type(exc).__name__},
            )
            self._transition(
                "deployment_failed",
                now_s,
                reason=f"execution feedback rejected: {type(exc).__name__}",
            )
            raise
        self._transition("release_execution_reported", now_s)
        self.audit.append(
            "payload.execution_reported",
            now_s,
            {"release_id": release_id, "payload_slot_id": context.payload_slot_id},
        )
        if snapshot.state is PayloadState.RELEASE_CONFIRMED:
            self._finish_confirmed_release(now_s)

    @_serialized
    def report_independent_confirmation(
        self,
        *,
        release_id: str,
        source_id: str,
        now_s: float,
    ) -> None:
        self._validate_now(now_s)
        if self.state.phase not in {MissionPhase.DEPLOYING, MissionPhase.VERIFYING_RELEASE}:
            raise MissionOperationError("mission is not awaiting release evidence")
        context = self._require_any_context()
        try:
            snapshot = self.payload.report_independent_confirmation(
                release_id=release_id,
                payload_slot_id=context.payload_slot_id,
                source_id=source_id,
                now_s=now_s,
            )
        except Exception as exc:
            previous_phase = self.state.phase
            failure_event = (
                "deployment_failed"
                if previous_phase is MissionPhase.DEPLOYING
                else "release_failed"
            )
            self.audit.append(
                "payload.independent_feedback_rejected",
                now_s,
                {
                    "release_id": release_id,
                    "source_id": source_id,
                    "error_type": type(exc).__name__,
                },
            )
            self._transition(
                failure_event,
                now_s,
                reason=f"independent feedback rejected: {type(exc).__name__}",
            )
            raise
        self.audit.append(
            "payload.independent_confirmation_reported",
            now_s,
            {
                "release_id": release_id,
                "payload_slot_id": context.payload_slot_id,
                "source_id": source_id,
            },
        )
        if snapshot.state is PayloadState.RELEASE_CONFIRMED:
            if self.state.phase is MissionPhase.DEPLOYING:
                self._transition("release_execution_reported", now_s)
            self._finish_confirmed_release(now_s)

    @_serialized
    def check_release_timeout(self, *, now_s: float) -> bool:
        self._validate_now(now_s)
        timed_out = self.payload.check_timeouts(now_s=now_s)
        if not timed_out:
            return False
        if self.state.phase is MissionPhase.DEPLOYING:
            self._transition("deployment_failed", now_s, reason="release confirmation timed out")
        elif self.state.phase is MissionPhase.VERIFYING_RELEASE:
            self._transition("release_failed", now_s, reason="release confirmation timed out")
        self.audit.append(
            "payload.release_timeout",
            now_s,
            {
                "slots": [snapshot.payload_slot_id for snapshot in timed_out],
                "automatic_retry": False,
            },
        )
        return True

    @_serialized
    def tick(self, *, now_s: float) -> MissionStatus:
        """Advance authorization and release timeouts without requiring a new frame."""

        self._validate_now(now_s)
        self._expire_authorization_if_needed(now_s)
        if self.state.phase in {MissionPhase.DEPLOYING, MissionPhase.VERIFYING_RELEASE}:
            self.check_release_timeout(now_s=now_s)
        return self.status()

    @_serialized
    def status(self) -> MissionStatus:
        context = self._context
        return MissionStatus(
            phase=self.state.phase,
            active_target_id=context.track.track_id if context else None,
            pending_challenge_id=(
                context.challenge.challenge_id
                if context and self.state.phase is MissionPhase.AWAITING_AUTHORIZATION
                else None
            ),
            active_payload_slot_id=self.payload.active_slot_id,
            active_release_id=context.release_id if context else None,
            remaining_payload_count=self.payload.remaining_payload_count,
            payload_slots=self.payload.slots(),
            fault_reason=self.payload.fault_reason,
        )

    def write_audit_jsonl(self, path: str | Path) -> None:
        self.audit.write_jsonl(path)

    def _fuse_frame(self, frame: FrameObservation) -> FrameObservation:
        rgb = tuple(
            detection for detection in frame.detections if detection.sensor is SensorKind.RGB
        )
        already_fused = tuple(
            detection for detection in frame.detections if detection.sensor is SensorKind.FUSED
        )
        thermal = tuple(
            detection for detection in frame.detections if detection.sensor is SensorKind.THERMAL
        )
        fused = (*fuse_rgb_thermal(rgb, thermal), *already_fused)
        return FrameObservation(
            frame_id=frame.frame_id,
            captured_at_s=frame.captured_at_s,
            detections=fused,
            telemetry=frame.telemetry,
        )

    def _next_locked_payload_slot(self) -> str | None:
        return next(
            (
                slot.payload_slot_id
                for slot in self.payload.slots()
                if slot.state is PayloadState.LOCKED
            ),
            None,
        )

    def _finish_confirmed_release(self, now_s: float) -> None:
        context = self._require_context(MissionPhase.VERIFYING_RELEASE)
        self._transition("release_confirmed", now_s)
        self._served_target_regions.append(
            _ServedTargetRegion(
                label=context.track.label,
                bbox=context.track.bbox,
                served_at_s=now_s,
            )
        )
        self.audit.append(
            "payload.release_confirmed",
            now_s,
            {
                "release_id": context.release_id,
                "payload_slot_id": context.payload_slot_id,
                "target_id": context.track.track_id,
                "remaining_payload_count": self.payload.remaining_payload_count,
            },
        )
        if self.config.completion_policy is CompletionPolicy.TERMINATE_AFTER_FIRST:
            self._transition("terminate", now_s, reason="single-use mission completed")
        elif self.payload.remaining_payload_count > 0:
            self._transition("continue_search", now_s)
        else:
            self._transition("request_return", now_s, reason="payload inventory exhausted")
        self._context = None

    def _invalidate_before_release(self, now_s: float, reason: str) -> None:
        self.audit.append("safety.authorization_invalidated", now_s, {"reason": reason})
        self._transition("safety_invalidated", now_s, reason=reason)
        self._context = None

    def _invalidate_for_changed_observation(self, now_s: float) -> None:
        context = self._require_any_context()
        previous_phase = self.state.phase
        if previous_phase is MissionPhase.DEPLOYMENT_READY:
            self.payload.lock(payload_slot_id=context.payload_slot_id)
        elif previous_phase is MissionPhase.AWAITING_AUTHORIZATION:
            try:
                self.authorizations.deny(
                    challenge_id=context.challenge.challenge_id,
                    nonce=context.challenge.nonce,
                    operator_id="system-safety-refresh",
                    now_s=now_s,
                )
            except AuthorizationError:
                # The state transition and context removal still revoke this
                # mission's ability to consume the old challenge.
                pass
        self.audit.append(
            "safety.authorization_invalidated",
            now_s,
            {
                "reason": "new observation changed authorization safety semantics",
                "previous_challenge_id": context.challenge.challenge_id,
                "payload_relocked": previous_phase is MissionPhase.DEPLOYMENT_READY,
            },
        )
        self._transition(
            "safety_invalidated",
            now_s,
            reason="new observation changed authorization safety semantics",
        )
        self._context = None

    def _refresh_active_context_if_equivalent(
        self,
        *,
        tracks: tuple[TrackSnapshot, ...],
        frame: FrameObservation,
        now_s: float,
    ) -> DeploymentDecision | None:
        context = self._require_any_context()
        current_track = next(
            (track for track in tracks if track.track_id == context.track.track_id),
            None,
        )
        if current_track is None:
            return None
        current_decision = replace(
            self.rules.evaluate(track=current_track, frame=frame, now_s=now_s),
            priority_score=context.decision.priority_score,
        )
        if not self._safety_semantically_equivalent(
            context=context,
            current_track=current_track,
            current_frame=frame,
            current_decision=current_decision,
        ):
            return None

        previous_digest = context.challenge.scene_digest
        refreshed_challenge = self.authorizations.refresh_equivalent_binding(
            challenge_id=context.challenge.challenge_id,
            previous_scene_digest=previous_digest,
            decision=current_decision,
            now_s=now_s,
        )
        if self.state.phase is MissionPhase.DEPLOYMENT_READY:
            if context.authorization is None:
                raise MissionOperationError("ready mission context has no authorization")
            refreshed_authorization = replace(
                context.authorization,
                challenge=refreshed_challenge,
            )
            try:
                self.payload.refresh_armed_authorization(
                    payload_slot_id=context.payload_slot_id,
                    previous_scene_digest=previous_digest,
                    authorization=refreshed_authorization,
                    now_s=now_s,
                )
            except Exception as exc:
                try:
                    if self.payload.get_slot(context.payload_slot_id).state is PayloadState.ARMED:
                        self.payload.lock(payload_slot_id=context.payload_slot_id)
                except Exception:
                    pass
                self._transition(
                    "fault",
                    now_s,
                    reason=f"authorization refresh failed: {type(exc).__name__}",
                )
                raise
            context.authorization = refreshed_authorization
        context.track = current_track
        context.frame = frame
        context.decision = current_decision
        context.challenge = refreshed_challenge
        self.audit.append(
            "authorization.live_safety_binding_refreshed",
            now_s,
            {
                "challenge_id": refreshed_challenge.challenge_id,
                "target_id": refreshed_challenge.target_id,
                "target_revision": refreshed_challenge.target_revision,
                "scene_digest": refreshed_challenge.scene_digest,
                "expires_at_s": refreshed_challenge.expires_at_s,
                "operator_reapproval_required": False,
            },
        )
        return current_decision

    @staticmethod
    def _safety_semantically_equivalent(
        *,
        context: _DeploymentContext,
        current_track: TrackSnapshot,
        current_frame: FrameObservation,
        current_decision: DeploymentDecision,
    ) -> bool:
        if not current_decision.allowed:
            return False
        if (
            current_track.track_id != context.track.track_id
            or current_track.label != context.track.label
            or current_decision.ruleset_version != context.decision.ruleset_version
            or MissionController._perception_model_identity(current_frame)
            != MissionController._perception_model_identity(context.frame)
        ):
            return False
        old_area = context.track.bbox.area
        area_ratio = current_track.bbox.area / old_area if old_area > 0 else float("inf")
        spatially_continuous = (
            context.track.bbox.iou(current_track.bbox) >= 0.4
            or context.track.bbox.center_distance(current_track.bbox) <= 0.04
        )
        old_verdicts = {check.rule_id: check.verdict for check in context.decision.checks}
        new_verdicts = {check.rule_id: check.verdict for check in current_decision.checks}
        return spatially_continuous and 0.5 <= area_ratio <= 2.0 and old_verdicts == new_verdicts

    @staticmethod
    def _perception_model_identity(
        frame: FrameObservation,
    ) -> frozenset[tuple[str, str, str]]:
        return frozenset(
            (
                detection.sensor.value,
                detection.model_version,
                str(detection.metadata.get("thermal_model_version", "")),
            )
            for detection in frame.detections
        )

    def _expire_authorization_if_needed(self, now_s: float, *, force: bool = False) -> bool:
        if self.state.phase not in {
            MissionPhase.AWAITING_AUTHORIZATION,
            MissionPhase.DEPLOYMENT_READY,
        }:
            return False
        context = self._require_any_context()
        if not force and now_s < context.challenge.expires_at_s:
            return False
        self.authorizations.expire(now_s=now_s)
        if self.state.phase is MissionPhase.DEPLOYMENT_READY:
            self.payload.lock(payload_slot_id=context.payload_slot_id)
        self.audit.append(
            "authorization.expired",
            now_s,
            {
                "challenge_id": context.challenge.challenge_id,
                "payload_slot_id": context.payload_slot_id,
            },
        )
        self._transition("authorization_expired", now_s)
        self._context = None
        return True

    def _is_recently_served_target(self, track: TrackSnapshot, now_s: float) -> bool:
        cooldown = self.config.target_reengagement_cooldown_seconds
        self._served_target_regions = [
            region
            for region in self._served_target_regions
            if now_s - region.served_at_s <= cooldown
        ]
        return any(
            region.label == track.label
            and (
                region.bbox.iou(track.bbox) >= 0.25
                or region.bbox.center_distance(track.bbox) <= 0.08
            )
            for region in self._served_target_regions
        )

    def _transition(self, event: str, now_s: float, *, reason: str | None = None) -> None:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("transition timestamp must be a finite non-negative number")
        transition = self.state.apply(event)
        details = {
            "event": event,
            "previous": transition.previous.value,
            "current": transition.current.value,
        }
        if reason is not None:
            details["reason"] = reason
        self.audit.append("mission.transition", now_s, details)

    def _validate_now(self, now_s: float) -> None:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")
        if self._last_now_s is not None and now_s < self._last_now_s:
            raise ValueError("mission event timestamps must be monotonic")
        self._last_now_s = now_s

    def _require_context(self, expected_phase: MissionPhase) -> _DeploymentContext:
        if self.state.phase is not expected_phase:
            raise MissionOperationError(
                f"operation requires phase {expected_phase.value}, "
                f"current phase is {self.state.phase.value}"
            )
        return self._require_any_context()

    def _require_any_context(self) -> _DeploymentContext:
        if self._context is None:
            raise MissionOperationError("mission has no active deployment context")
        return self._context

    @staticmethod
    def _decision_matches_challenge(
        decision: DeploymentDecision, challenge: AuthorizationChallenge
    ) -> bool:
        return decision.allowed and (
            decision.target_id,
            decision.target_revision,
            decision.scene_digest,
            decision.ruleset_version,
        ) == (
            challenge.target_id,
            challenge.target_revision,
            challenge.scene_digest,
            challenge.ruleset_version,
        )


__all__ = [
    "MissionController",
    "MissionOperationError",
    "MissionStatus",
    "ObservationOutcome",
]

"""Adapter-owned task-phase receipts for the realtime onboard loop.

The model proposes a phase; this module does not execute transport.  It only
checks the allowed task sequence and records the truth-free evidence needed to
show that a target was matched, where the qualified GPS fix was taken, and
when the policy moved to return/report.  SIM, HIL and REAL adapters can use the
same receipt contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..mavlink_gps import (
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
)
from ..target_geolocation import wgs84_forward_from_ned
from .contracts import ContextSnapshot, GeoPoint
from .onboard_agent import (
    OnboardDecision,
    OnboardEventKind,
    OnboardResponse,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)


class OnboardTaskStateError(ValueError):
    """Raised when a model phase cannot be accepted by the task state machine."""


MIN_TARGET_MATCH_SCORE = 0.80
# Search may retain low-confidence candidates for reacquisition, but a target
# cannot become actionable from a near-zero detector score.  MATCH/RECORD need
# a calibrated detector confidence in addition to semantic colour/class,
# metric-depth, GPS, and temporal-track gates.  This is deliberately shared by
# the SIM and the future Jetson adapter so the two paths cannot disagree about
# what constitutes a found target.
MIN_TARGET_DETECTOR_CONFIDENCE = 0.25


@dataclass(frozen=True, slots=True)
class OnboardTaskReceipt:
    """One accepted high-level task phase and its optional target GPS fix."""

    sequence: int
    phase: OnboardTaskAction
    event_id: str
    event_sha256: str
    observed_at_s: float
    context_sha256: str
    target_match_score: float | None
    target_class: str | None
    target_color: str | None
    target_class_match: bool | None
    target_color_match: bool | None
    target_position: GeoPoint | None
    model_id: str
    model_version: str
    model_sha256: str
    provider: str
    gps_fix_type: int
    gps_horizontal_accuracy_m: float
    target_north_offset_m: float | None = None
    target_east_offset_m: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, OnboardTaskAction):
            raise TypeError("phase must be an OnboardTaskAction")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be non-empty")
        for name in (
            "event_sha256",
            "context_sha256",
            "model_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"{name} must be a SHA-256 string")
            try:
                int(value, 16)
            except ValueError as exc:
                raise ValueError(f"{name} must be hexadecimal") from exc
        if not math.isfinite(self.observed_at_s) or self.observed_at_s < 0.0:
            raise ValueError("observed_at_s must be finite and non-negative")
        if self.target_match_score is not None and not 0.0 <= self.target_match_score <= 1.0:
            raise ValueError("target_match_score must be within [0, 1]")
        for name in ("target_class", "target_color"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string when provided")
        for name in ("target_class_match", "target_color_match"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be a bool or None")
        if not isinstance(self.target_position, (GeoPoint, type(None))):
            raise TypeError("target_position must be a GeoPoint or None")
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("model_id must be non-empty")
        if not isinstance(self.model_version, str) or not self.model_version:
            raise ValueError("model_version must be non-empty")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider must be non-empty")
        if not isinstance(self.gps_fix_type, int) or isinstance(self.gps_fix_type, bool):
            raise TypeError("gps_fix_type must be an integer")
        if (
            not math.isfinite(self.gps_horizontal_accuracy_m)
            or self.gps_horizontal_accuracy_m < 0.0
        ):
            raise ValueError("gps_horizontal_accuracy_m must be finite and non-negative")
        for name in ("target_north_offset_m", "target_east_offset_m"):
            value = getattr(self, name)
            if value is not None and (
                not math.isfinite(value) or not -5_000.0 <= value <= 5_000.0
            ):
                raise ValueError(f"{name} must be finite within +/-5000 m")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-task-receipt",
            "sequence": self.sequence,
            "phase": self.phase.value,
            "event_id": self.event_id,
            "event_sha256": self.event_sha256,
            "observed_at_s": self.observed_at_s,
            "context_sha256": self.context_sha256,
            "target_match_score": self.target_match_score,
            "target_class": self.target_class,
            "target_color": self.target_color,
            "target_class_match": self.target_class_match,
            "target_color_match": self.target_color_match,
            "target_position": (
                None if self.target_position is None else self.target_position.to_mapping()
            ),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_sha256": self.model_sha256,
            "provider": self.provider,
            "gps_fix_type": self.gps_fix_type,
            "gps_horizontal_accuracy_m": self.gps_horizontal_accuracy_m,
            "target_north_offset_m": self.target_north_offset_m,
            "target_east_offset_m": self.target_east_offset_m,
            "truth_free": True,
            "policy_applied_to_actuator": False,
        }


_ALLOWED_NEXT: dict[OnboardTaskAction, frozenset[OnboardTaskAction]] = {
    OnboardTaskAction.TAKEOFF: frozenset(
        {OnboardTaskAction.TAKEOFF, OnboardTaskAction.SEARCH}
    ),
    OnboardTaskAction.SEARCH: frozenset(
        {
            OnboardTaskAction.SEARCH,
            OnboardTaskAction.INSPECT_TARGET,
            OnboardTaskAction.MATCH_TARGET,
            OnboardTaskAction.RETURN_HOME,
        }
    ),
    OnboardTaskAction.INSPECT_TARGET: frozenset(
        {OnboardTaskAction.INSPECT_TARGET, OnboardTaskAction.MATCH_TARGET, OnboardTaskAction.SEARCH}
    ),
    OnboardTaskAction.MATCH_TARGET: frozenset(
        {
            OnboardTaskAction.MATCH_TARGET,
            OnboardTaskAction.RECORD_LOCATION,
            OnboardTaskAction.SEARCH,
        }
    ),
    OnboardTaskAction.RECORD_LOCATION: frozenset({OnboardTaskAction.RETURN_HOME}),
    OnboardTaskAction.RETURN_HOME: frozenset(
        {OnboardTaskAction.RETURN_HOME, OnboardTaskAction.REPORT}
    ),
    OnboardTaskAction.REPORT: frozenset(
        {OnboardTaskAction.REPORT, OnboardTaskAction.LAND, OnboardTaskAction.HOLD}
    ),
    OnboardTaskAction.HOLD: frozenset({OnboardTaskAction.HOLD, OnboardTaskAction.RETURN_HOME}),
    OnboardTaskAction.LAND: frozenset({OnboardTaskAction.LAND}),
}


class OnboardTaskStateMachine:
    """Validate and record model task phases without transport authority."""

    __slots__ = ("_phase", "_receipts")

    def __init__(self) -> None:
        self._phase = OnboardTaskAction.TAKEOFF
        self._receipts: list[OnboardTaskReceipt] = []

    @property
    def phase(self) -> OnboardTaskAction:
        return self._phase

    @property
    def receipts(self) -> tuple[OnboardTaskReceipt, ...]:
        return tuple(self._receipts)

    def validate(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        decision: OnboardDecision,
    ) -> OnboardTaskAction | None:
        """Validate a proposal before the adapter installs/clears a Mission."""

        proposal = decision.model_proposal
        if proposal is None or proposal.task_action is None:
            return None
        action = proposal.task_action
        if action not in _ALLOWED_NEXT[self._phase]:
            raise OnboardTaskStateError(
                f"task phase {self._phase.value} cannot transition to {action.value}"
            )
        if action in {
            OnboardTaskAction.RECORD_LOCATION,
            OnboardTaskAction.RETURN_HOME,
        } and decision.response is not OnboardResponse.RETURN:
            raise OnboardTaskStateError(
                f"{action.value} requires an accepted RETURN response"
            )
        if action is OnboardTaskAction.MATCH_TARGET:
            if not context.camera_available or not context.perception_available:
                raise OnboardTaskStateError("match_target requires live perception")
            if (
                event.target_confidence is None
                or event.target_confidence < MIN_TARGET_DETECTOR_CONFIDENCE
            ):
                raise OnboardTaskStateError(
                    "match_target requires calibrated detector confidence"
                )
            if (
                event.target_match_score is None
                or event.target_match_score < MIN_TARGET_MATCH_SCORE
            ):
                raise OnboardTaskStateError(
                    "match_target requires a qualified target match score"
                )
            if event.target_class_match is False or event.target_color_match is False:
                raise OnboardTaskStateError("match_target target attributes do not match")
        if action is OnboardTaskAction.RECORD_LOCATION:
            if (
                event.target_confidence is None
                or event.target_confidence < MIN_TARGET_DETECTOR_CONFIDENCE
            ):
                raise OnboardTaskStateError(
                    "record_location requires calibrated detector confidence"
                )
            if context.gps_fix_type < MAVLINK_QUALIFIED_FIX_TYPE_MIN:
                raise OnboardTaskStateError("record_location requires a qualified GPS fix")
            if context.gps_horizontal_accuracy_m > GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M:
                raise OnboardTaskStateError(
                    "record_location GPS accuracy exceeds the canonical gate"
                )
            has_ned_offset = (
                event.target_geometry_frame == "ned"
                and event.target_north_offset_m is not None
                and event.target_east_offset_m is not None
            )
            if not has_ned_offset:
                raise OnboardTaskStateError(
                    "record_location requires calibrated NED offsets; "
                    "camera-relative or legacy polar geometry cannot be recorded"
                )
            if (
                event.target_match_score is None
                or event.target_match_score < MIN_TARGET_MATCH_SCORE
            ):
                raise OnboardTaskStateError(
                    "record_location requires a qualified target match score"
                )
            if event.target_class_match is False or event.target_color_match is False:
                raise OnboardTaskStateError("record_location target attributes do not match")
            if not context.camera_available or not context.perception_available:
                raise OnboardTaskStateError("record_location requires live perception")
        if action is OnboardTaskAction.REPORT:
            if decision.response is not OnboardResponse.HOLD:
                raise OnboardTaskStateError("report requires an accepted HOLD response")
            if event.kind is not OnboardEventKind.MISSION_COMPLETE:
                raise OnboardTaskStateError("report requires a mission-complete event")
            if event.route_progress_fraction is None or event.route_progress_fraction < 0.99:
                raise OnboardTaskStateError("report requires a confirmed return completion")
        if action is OnboardTaskAction.RETURN_HOME:
            if event.kind is not OnboardEventKind.MISSION_COMPLETE:
                raise OnboardTaskStateError("return_home requires a return-completion event")
            if event.route_progress_fraction is None or event.route_progress_fraction < 0.99:
                raise OnboardTaskStateError("return_home requires confirmed home arrival")
        return action

    def mark_takeoff_complete(
        self,
        context: ContextSnapshot,
        *,
        minimum_relative_altitude_m: float = 10.0,
    ) -> bool:
        """Advance ``TAKEOFF`` to ``SEARCH`` from qualified live telemetry.

        A perception frame must not silently advance the mission lifecycle.  A
        SIM, HIL, or REAL adapter calls this method only after it has observed a
        truth-free ``ContextSnapshot`` from the vehicle.  The method is
        idempotent after the first transition and refuses to infer takeoff from
        a planned Mission or from camera pixels alone.
        """

        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if not math.isfinite(minimum_relative_altitude_m) or minimum_relative_altitude_m <= 0.0:
            raise ValueError("minimum_relative_altitude_m must be finite and positive")
        if self._phase is not OnboardTaskAction.TAKEOFF:
            return False
        if context.aircraft_position.altitude_reference != "relative_home":
            raise OnboardTaskStateError(
                "takeoff completion requires a relative_home aircraft position"
            )
        if context.gps_fix_type < MAVLINK_QUALIFIED_FIX_TYPE_MIN:
            raise OnboardTaskStateError("takeoff completion requires a qualified GPS fix")
        if context.gps_horizontal_accuracy_m > GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M:
            raise OnboardTaskStateError(
                "takeoff completion GPS accuracy exceeds the canonical gate"
            )
        altitude_m = context.aircraft_position.altitude_m
        if altitude_m is None or altitude_m < minimum_relative_altitude_m:
            raise OnboardTaskStateError(
                "takeoff completion requires the minimum relative altitude"
            )
        self._phase = OnboardTaskAction.SEARCH
        return True

    def commit(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        decision: OnboardDecision,
        action: OnboardTaskAction | None,
    ) -> OnboardTaskReceipt | None:
        if action is None:
            return None
        proposal = decision.model_proposal
        if proposal is None or proposal.task_action is not action:
            raise OnboardTaskStateError("validated task action no longer matches proposal")
        target_position = None
        if action is OnboardTaskAction.RECORD_LOCATION:
            if (
                event.target_geometry_frame != "ned"
                or event.target_north_offset_m is None
                or event.target_east_offset_m is None
            ):
                raise OnboardTaskStateError(
                    "record_location requires calibrated NED offsets"
                )
            north = event.target_north_offset_m
            east = event.target_east_offset_m
            latitude, longitude = wgs84_forward_from_ned(
                latitude_deg=context.aircraft_position.latitude_deg,
                longitude_deg=context.aircraft_position.longitude_deg,
                north_offset_m=north,
                east_offset_m=east,
            )
            target_position = GeoPoint(latitude, longitude)
        receipt = OnboardTaskReceipt(
            sequence=len(self._receipts),
            phase=action,
            event_id=event.event_id,
            event_sha256=event.canonical_hash,
            observed_at_s=event.observed_at_s,
            context_sha256=context.canonical_hash,
            target_match_score=event.target_match_score,
            target_class=event.target_class,
            target_color=event.target_color,
            target_class_match=event.target_class_match,
            target_color_match=event.target_color_match,
            target_position=target_position,
            model_id=proposal.model_id,
            model_version=proposal.model_version,
            model_sha256=proposal.model_sha256,
            provider=proposal.provider,
            gps_fix_type=context.gps_fix_type,
            gps_horizontal_accuracy_m=context.gps_horizontal_accuracy_m,
            target_north_offset_m=event.target_north_offset_m,
            target_east_offset_m=event.target_east_offset_m,
        )
        self._phase = action
        # RECORD_LOCATION is a target evidence receipt, not proof that the
        # aircraft has completed the return leg.  A later RETURN_HOME event
        # must be accepted from telemetry before REPORT can be committed.
        self._receipts.append(receipt)
        return receipt


__all__ = [
    "OnboardTaskReceipt",
    "OnboardTaskStateError",
    "OnboardTaskStateMachine",
    "MIN_TARGET_DETECTOR_CONFIDENCE",
    "MIN_TARGET_MATCH_SCORE",
]

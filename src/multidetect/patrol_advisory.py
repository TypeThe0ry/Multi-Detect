from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .domain import VehicleTelemetry
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


class PatrolPhase(str, Enum):
    PATROL = "patrol"
    DETECTED = "detected"
    LOCKED_MONITOR = "locked_monitor"
    TRACKING = "tracking"
    OCCLUDED = "occluded"
    REACQUIRING = "reacquiring"
    LOST = "lost"


class AdvisoryValidity(str, Enum):
    VALID = "valid"
    DEGRADED = "degraded"
    INVALID = "invalid"


class ReturnObserveDirection(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    ROUTE_REQUIRED = "route_required"


@dataclass(frozen=True, slots=True)
class PatrolAdvisoryConfig:
    maximum_bank_angle_deg: float = 25.0
    minimum_ground_speed_mps: float = 5.0
    maximum_evidence_age_s: float = 2.0
    centered_deadband_fraction: float = 0.08

    def __post_init__(self) -> None:
        if not math.isfinite(self.maximum_bank_angle_deg) or not (
            1.0 <= self.maximum_bank_angle_deg <= 45.0
        ):
            raise ValueError("maximum_bank_angle_deg must be in [1, 45]")
        if not math.isfinite(self.minimum_ground_speed_mps) or not (
            self.minimum_ground_speed_mps > 0.0
        ):
            raise ValueError("minimum_ground_speed_mps must be positive")
        if not math.isfinite(self.maximum_evidence_age_s) or not (
            self.maximum_evidence_age_s > 0.0
        ):
            raise ValueError("maximum_evidence_age_s must be positive")
        if not math.isfinite(self.centered_deadband_fraction) or not (
            0.0 < self.centered_deadband_fraction <= 0.25
        ):
            raise ValueError("centered_deadband_fraction must be in (0, 0.25]")


@dataclass(frozen=True, slots=True)
class ReturnToObserveAdvisory:
    target_id: str
    generated_at_s: float
    last_seen_at_s: float
    evidence_age_s: float
    direction: ReturnObserveDirection
    estimated_minimum_turn_radius_m: float | None
    validity: AdvisoryValidity
    reasons: tuple[str, ...]
    operator_confirmation_required: bool = True
    sitl_validation_required: bool = True
    advisory_only: bool = True
    flight_control_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.target_id.strip():
            raise ValueError("return-observe target_id cannot be empty")
        if not all(
            math.isfinite(value)
            for value in (self.generated_at_s, self.last_seen_at_s, self.evidence_age_s)
        ):
            raise ValueError("return-observe timestamps must be finite")
        if self.generated_at_s < 0.0 or self.last_seen_at_s < 0.0 or self.evidence_age_s < 0.0:
            raise ValueError("return-observe timestamps cannot be negative")
        if self.estimated_minimum_turn_radius_m is not None and (
            not math.isfinite(self.estimated_minimum_turn_radius_m)
            or self.estimated_minimum_turn_radius_m <= 0.0
        ):
            raise ValueError("turn radius must be finite and positive when available")
        if not self.reasons or any(not reason.strip() for reason in self.reasons):
            raise ValueError("return-observe reasons cannot be empty")
        if (
            not self.operator_confirmation_required
            or not self.sitl_validation_required
            or not self.advisory_only
            or self.flight_control_enabled
        ):
            raise ValueError("return-observe output must remain confirmed, SITL-only advice")


@dataclass(frozen=True, slots=True)
class PatrolModeAssessment:
    phase: PatrolPhase
    evaluated_at_s: float
    primary_target_id: str | None
    target_state: UnifiedTrackState | None
    return_to_observe: ReturnToObserveAdvisory | None
    reason: str
    advisory_only: bool = True
    flight_control_enabled: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.evaluated_at_s) or self.evaluated_at_s < 0.0:
            raise ValueError("patrol assessment time must be finite and non-negative")
        if not self.reason.strip():
            raise ValueError("patrol assessment reason cannot be empty")
        if not self.advisory_only or self.flight_control_enabled:
            raise ValueError("patrol assessment must remain advisory-only")
        if self.phase is PatrolPhase.PATROL and self.primary_target_id is not None:
            raise ValueError("PATROL phase cannot have a primary target")
        if self.return_to_observe is not None and self.phase is not PatrolPhase.LOST:
            raise ValueError("return-to-observe advice is only valid for a LOST target")


class PatrolAdvisoryEngine:
    """Maps the unified target pool to mode-1 state and read-only revisit advice."""

    def __init__(self, config: PatrolAdvisoryConfig | None = None) -> None:
        self.config = config or PatrolAdvisoryConfig()

    def assess(
        self,
        *,
        tracks: Sequence[UnifiedTrackSnapshot],
        primary_target_id: str | None,
        telemetry: VehicleTelemetry,
        now_s: float,
    ) -> PatrolModeAssessment:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("patrol assessment time must be finite and non-negative")
        if primary_target_id is None:
            return PatrolModeAssessment(
                phase=PatrolPhase.PATROL,
                evaluated_at_s=now_s,
                primary_target_id=None,
                target_state=None,
                return_to_observe=None,
                reason="no primary target; continue the approved patrol route",
            )
        matches = tuple(track for track in tracks if track.track_id == primary_target_id)
        if len(matches) != 1:
            return PatrolModeAssessment(
                phase=PatrolPhase.LOST,
                evaluated_at_s=now_s,
                primary_target_id=primary_target_id,
                target_state=None,
                return_to_observe=None,
                reason="primary target record is unavailable; no revisit direction may be inferred",
            )
        target = matches[0]
        phase = self._phase_for(target.state)
        advisory = (
            self._return_to_observe(target=target, telemetry=telemetry, now_s=now_s)
            if target.state is UnifiedTrackState.LOST
            else None
        )
        return PatrolModeAssessment(
            phase=phase,
            evaluated_at_s=now_s,
            primary_target_id=primary_target_id,
            target_state=target.state,
            return_to_observe=advisory,
            reason=self._reason_for(phase, advisory),
        )

    def _return_to_observe(
        self,
        *,
        target: UnifiedTrackSnapshot,
        telemetry: VehicleTelemetry,
        now_s: float,
    ) -> ReturnToObserveAdvisory:
        evidence_age_s = max(0.0, now_s - target.last_seen_at_s)
        center_x, _ = target.bbox.center
        offset = center_x - 0.5
        if offset < -self.config.centered_deadband_fraction:
            direction = ReturnObserveDirection.LEFT
        elif offset > self.config.centered_deadband_fraction:
            direction = ReturnObserveDirection.RIGHT
        else:
            direction = ReturnObserveDirection.ROUTE_REQUIRED

        speed = telemetry.ground_speed_mps
        radius = None
        if math.isfinite(speed) and speed >= self.config.minimum_ground_speed_mps:
            bank_rad = math.radians(self.config.maximum_bank_angle_deg)
            radius = speed * speed / (9.80665 * math.tan(bank_rad))

        invalid_reasons: list[str] = []
        degraded_reasons: list[str] = []
        if evidence_age_s > self.config.maximum_evidence_age_s:
            invalid_reasons.append("last target evidence is stale")
        for name, value in (
            ("position", telemetry.position_healthy),
            ("geofence", telemetry.geofence_healthy),
            ("data link", telemetry.link_healthy),
        ):
            if value is False:
                invalid_reasons.append(f"{name} health is false")
            elif value is None:
                degraded_reasons.append(f"{name} health is unknown")
        if radius is None:
            degraded_reasons.append("ground speed is unavailable or below the planning threshold")
        if direction is ReturnObserveDirection.ROUTE_REQUIRED:
            degraded_reasons.append("last target bearing is inside the center deadband")

        if invalid_reasons:
            validity = AdvisoryValidity.INVALID
            reasons = tuple((*invalid_reasons, *degraded_reasons))
        elif degraded_reasons:
            validity = AdvisoryValidity.DEGRADED
            reasons = tuple(degraded_reasons)
        else:
            validity = AdvisoryValidity.VALID
            reasons = ("fresh target evidence and required navigation health are available",)
        return ReturnToObserveAdvisory(
            target_id=target.track_id,
            generated_at_s=now_s,
            last_seen_at_s=target.last_seen_at_s,
            evidence_age_s=evidence_age_s,
            direction=direction,
            estimated_minimum_turn_radius_m=radius,
            validity=validity,
            reasons=reasons,
        )

    @staticmethod
    def _phase_for(state: UnifiedTrackState) -> PatrolPhase:
        return {
            UnifiedTrackState.DETECTED: PatrolPhase.DETECTED,
            UnifiedTrackState.LOCKED: PatrolPhase.LOCKED_MONITOR,
            UnifiedTrackState.TRACKING: PatrolPhase.TRACKING,
            UnifiedTrackState.RECOVERED: PatrolPhase.TRACKING,
            UnifiedTrackState.OCCLUDED: PatrolPhase.OCCLUDED,
            UnifiedTrackState.REACQUIRING: PatrolPhase.REACQUIRING,
            UnifiedTrackState.LOST: PatrolPhase.LOST,
        }[state]

    @staticmethod
    def _reason_for(
        phase: PatrolPhase,
        advisory: ReturnToObserveAdvisory | None,
    ) -> str:
        if advisory is not None:
            return f"primary target lost; revisit advice is {advisory.validity.value}"
        return {
            PatrolPhase.DETECTED: "target candidate detected but not locked",
            PatrolPhase.LOCKED_MONITOR: "primary target locked for monitoring only",
            PatrolPhase.TRACKING: "primary target is being monitored without route control",
            PatrolPhase.OCCLUDED: "primary target is temporarily occluded",
            PatrolPhase.REACQUIRING: "conservative target reacquisition is in progress",
            PatrolPhase.LOST: "primary target is lost and no revisit advice is available",
            PatrolPhase.PATROL: "continue the approved patrol route",
        }[phase]


__all__ = [
    "AdvisoryValidity",
    "PatrolAdvisoryConfig",
    "PatrolAdvisoryEngine",
    "PatrolModeAssessment",
    "PatrolPhase",
    "ReturnObserveDirection",
    "ReturnToObserveAdvisory",
]

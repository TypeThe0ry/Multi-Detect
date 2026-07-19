from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import uuid4

from .compat import StrEnum
from .domain import BoundingBox, VehicleTelemetry
from .monocular_avoidance import CollisionRiskState, MonocularAvoidanceAssessment
from .multimodal_ranging import CameraCalibration, RangeSolution, RangeValidity
from .unified_tracking import UnifiedTrackState


class ApproachHilPhase(StrEnum):
    SEARCH = "search"
    TARGET_LOCKED = "target_locked"
    SLIDE_CONFIRM_REQUIRED = "slide_confirm_required"
    CORRIDOR_VALID = "corridor_valid"
    CENTERING = "centering"
    CENTERING_SIM = "centering"
    AIMING = "aiming"
    APPROACH_SIM = "aiming"
    COMPLETE = "complete"
    ABORT = "abort"
    ABORT_CLIMB_SIM = "abort"


@dataclass(frozen=True, slots=True)
class ApproachHilConfig:
    slide_confirmation_ttl_s: float = 5.0
    minimum_slide_duration_s: float = 0.6
    maximum_slide_duration_s: float = 4.0
    maximum_evidence_age_s: float = 0.3
    maximum_pose_age_s: float = 0.5
    minimum_range_consistency: float = 0.65
    minimum_approach_range_m: float = 8.0
    maximum_approach_range_m: float = 300.0
    completion_range_m: float = 12.0
    minimum_altitude_agl_m: float = 10.0
    maximum_altitude_agl_m: float = 120.0
    minimum_airspeed_mps: float = 12.0
    maximum_abs_roll_deg: float = 20.0
    maximum_abs_pitch_deg: float = 15.0
    maximum_corridor_angle_deg: float = 18.0
    centering_tolerance_deg: float = 1.5
    centered_frames_required: int = 3
    maximum_yaw_advice_deg: float = 5.0
    maximum_pitch_advice_deg: float = 4.0
    maximum_bank_advice_deg: float = 12.0
    abort_climb_pitch_advice_deg: float = 8.0

    def __post_init__(self) -> None:
        values = (
            self.slide_confirmation_ttl_s,
            self.minimum_slide_duration_s,
            self.maximum_slide_duration_s,
            self.maximum_evidence_age_s,
            self.maximum_pose_age_s,
            self.minimum_range_consistency,
            self.minimum_approach_range_m,
            self.maximum_approach_range_m,
            self.completion_range_m,
            self.minimum_altitude_agl_m,
            self.maximum_altitude_agl_m,
            self.minimum_airspeed_mps,
            self.maximum_abs_roll_deg,
            self.maximum_abs_pitch_deg,
            self.maximum_corridor_angle_deg,
            self.centering_tolerance_deg,
            self.maximum_yaw_advice_deg,
            self.maximum_pitch_advice_deg,
            self.maximum_bank_advice_deg,
            self.abort_climb_pitch_advice_deg,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("approach HIL numeric configuration must be finite and positive")
        if not self.minimum_slide_duration_s < self.maximum_slide_duration_s:
            raise ValueError("slide duration limits are invalid")
        if not self.minimum_approach_range_m < self.maximum_approach_range_m:
            raise ValueError("approach range limits are invalid")
        if not self.minimum_approach_range_m <= self.completion_range_m:
            raise ValueError("completion range cannot be below the minimum approach range")
        if not self.minimum_altitude_agl_m < self.maximum_altitude_agl_m:
            raise ValueError("approach altitude limits are invalid")
        if not 0.0 < self.minimum_range_consistency <= 1.0:
            raise ValueError("minimum range consistency must be in (0, 1]")
        if self.centering_tolerance_deg >= self.maximum_corridor_angle_deg:
            raise ValueError("centering tolerance must be inside the approach corridor")
        if self.centered_frames_required < 1:
            raise ValueError("centered_frames_required must be positive")


@dataclass(frozen=True, slots=True)
class ApproachTargetEvidence:
    target_id: str
    target_revision: int
    frame_id: str
    observed_at_s: float
    label: str
    bbox: BoundingBox
    state: UnifiedTrackState
    locked: bool
    primary: bool

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.frame_id.strip() or not self.label.strip():
            raise ValueError("approach target identifiers and label cannot be empty")
        if self.target_revision < 0:
            raise ValueError("approach target revision cannot be negative")
        if not math.isfinite(self.observed_at_s) or self.observed_at_s < 0.0:
            raise ValueError("approach target timestamp is invalid")
        if not isinstance(self.state, UnifiedTrackState):
            raise ValueError("approach target state is invalid")


@dataclass(frozen=True, slots=True)
class SlideConfirmationChallenge:
    token: str
    target_id: str
    target_revision: int
    issued_at_s: float
    expires_at_s: float
    hil_only: bool = True
    flight_control_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.token.strip() or not self.target_id.strip():
            raise ValueError("slide confirmation identifiers cannot be empty")
        if self.target_revision < 0 or self.expires_at_s <= self.issued_at_s:
            raise ValueError("slide confirmation binding is invalid")
        if not self.hil_only or self.flight_control_enabled:
            raise ValueError("slide confirmation must remain HIL-only")


@dataclass(frozen=True, slots=True)
class ApproachHilInput:
    target: ApproachTargetEvidence
    calibration: CameraCalibration
    ranging: RangeSolution | None
    avoidance: MonocularAvoidanceAssessment | None
    telemetry: VehicleTelemetry
    evaluated_at_s: float


@dataclass(frozen=True, slots=True)
class ApproachHilAssessment:
    phase: ApproachHilPhase
    target_id: str | None
    target_revision: int | None
    evaluated_at_s: float
    reasons: tuple[str, ...]
    yaw_error_deg: float | None = None
    pitch_error_deg: float | None = None
    yaw_advice_deg: float | None = None
    pitch_advice_deg: float | None = None
    bank_advice_deg: float | None = None
    climb_pitch_advice_deg: float | None = None
    ground_range_m: float | None = None
    confirmation_expires_at_s: float | None = None
    advisory_only: bool = True
    sitl_hil_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ApproachHilPhase):
            raise ValueError("approach HIL phase is invalid")
        if not math.isfinite(self.evaluated_at_s) or self.evaluated_at_s < 0.0:
            raise ValueError("approach HIL timestamp is invalid")
        if not self.reasons or any(not reason.strip() for reason in self.reasons):
            raise ValueError("approach HIL reasons cannot be empty")
        numeric = (
            self.yaw_error_deg,
            self.pitch_error_deg,
            self.yaw_advice_deg,
            self.pitch_advice_deg,
            self.bank_advice_deg,
            self.climb_pitch_advice_deg,
            self.ground_range_m,
            self.confirmation_expires_at_s,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("approach HIL numeric fields must be finite")
        if self.phase is ApproachHilPhase.ABORT_CLIMB_SIM and self.climb_pitch_advice_deg is None:
            raise ValueError("abort assessment requires bounded climb advice")
        if (
            not self.advisory_only
            or not self.sitl_hil_only
            or self.flight_control_enabled
            or self.physical_release_enabled
        ):
            raise ValueError("approach HIL assessment must remain advisory-only")


class ApproachHilController:
    """Stateful Mode-3 advice engine with one-time target-bound slide confirmation."""

    _RECOVERABLE_TRACKING_ABORT_REASONS = frozenset(
        {
            "target_occluded",
            "target_reacquiring",
            "target_lost",
            "target_evidence_stale",
        }
    )

    def __init__(self, config: ApproachHilConfig | None = None) -> None:
        self.config = config or ApproachHilConfig()
        self._target_id: str | None = None
        self._target_revision: int | None = None
        self._phase = ApproachHilPhase.SEARCH
        self._challenge: SlideConfirmationChallenge | None = None
        self._confirmation_accepted = False
        self._used_tokens: set[str] = set()
        self._centered_frames = 0
        self._abort_reason: str | None = None

    @property
    def phase(self) -> ApproachHilPhase:
        return self._phase

    @property
    def challenge(self) -> SlideConfirmationChallenge | None:
        return self._challenge

    @property
    def confirmation_accepted(self) -> bool:
        return self._confirmation_accepted

    @property
    def can_rearm_after_tracking_recovery(self) -> bool:
        """Whether a same-binding visual recovery may issue a fresh confirmation.

        This deliberately excludes an operator cancellation and every navigation,
        range, or corridor abort.  A recovered visual track still needs a new
        operator confirmation; this flag only permits publishing that challenge.
        """

        return (
            self._phase is ApproachHilPhase.ABORT_CLIMB_SIM
            and self._abort_reason in self._RECOVERABLE_TRACKING_ABORT_REASONS
        )

    def clear_target(self) -> None:
        """Invalidate all target-bound Mode-3 state without issuing control output."""

        self._target_id = None
        self._target_revision = None
        self._phase = ApproachHilPhase.SEARCH
        self._challenge = None
        self._confirmation_accepted = False
        self._centered_frames = 0
        self._abort_reason = None

    def select_target(self, target: ApproachTargetEvidence, *, now_s: float) -> None:
        self._require_now(now_s)
        if not target.locked or not target.primary:
            raise ValueError("mode-3 target must be the locked primary target")
        self._target_id = target.target_id
        self._target_revision = target.target_revision
        self._phase = ApproachHilPhase.TARGET_LOCKED
        self._challenge = None
        self._confirmation_accepted = False
        self._centered_frames = 0
        self._abort_reason = None

    def issue_slide_challenge(self, *, now_s: float) -> SlideConfirmationChallenge:
        self._require_now(now_s)
        if self._target_id is None or self._target_revision is None:
            raise RuntimeError("no mode-3 target is selected")
        challenge = SlideConfirmationChallenge(
            token=str(uuid4()),
            target_id=self._target_id,
            target_revision=self._target_revision,
            issued_at_s=now_s,
            expires_at_s=now_s + self.config.slide_confirmation_ttl_s,
        )
        self._challenge = challenge
        self._confirmation_accepted = False
        self._phase = ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
        return challenge

    def cancel_execution(self, *, now_s: float) -> ApproachHilAssessment:
        """Latch the current target in ABORT until the operator reselects it."""

        self._require_now(now_s)
        if self._target_id is None or self._target_revision is None:
            return self._assessment(now_s, ApproachHilPhase.SEARCH, ("no_target_selected",))
        self._challenge = None
        return self._abort(now_s, "abort_latched_until_reselection")

    def accept_slide_confirmation(
        self,
        *,
        token: str,
        target_id: str,
        target_revision: int,
        slide_started_at_s: float,
        slide_completed_at_s: float,
        completion_fraction: float,
        continuous: bool,
    ) -> bool:
        challenge = self._challenge
        if challenge is None or token in self._used_tokens:
            return False
        duration_s = slide_completed_at_s - slide_started_at_s
        valid = (
            token == challenge.token
            and target_id == challenge.target_id == self._target_id
            and target_revision == challenge.target_revision == self._target_revision
            and challenge.issued_at_s <= slide_started_at_s <= slide_completed_at_s
            and slide_completed_at_s <= challenge.expires_at_s
            and self.config.minimum_slide_duration_s
            <= duration_s
            <= self.config.maximum_slide_duration_s
            and math.isfinite(completion_fraction)
            and completion_fraction >= 0.98
            and continuous
        )
        self._used_tokens.add(token)
        if valid:
            self._confirmation_accepted = True
            self._phase = ApproachHilPhase.CORRIDOR_VALID
        return valid

    def evaluate(self, input_data: ApproachHilInput) -> ApproachHilAssessment:
        now_s = input_data.evaluated_at_s
        self._require_now(now_s)
        target = input_data.target
        if self._target_id is None or self._target_revision is None:
            return self._assessment(now_s, ApproachHilPhase.SEARCH, ("no_target_selected",))
        if self._phase is ApproachHilPhase.ABORT_CLIMB_SIM:
            return self._assessment(
                now_s,
                ApproachHilPhase.ABORT_CLIMB_SIM,
                ("abort_latched_until_reselection",),
                climb_pitch_advice_deg=self.config.abort_climb_pitch_advice_deg,
            )
        if target.target_id != self._target_id or target.target_revision != self._target_revision:
            return self._abort(now_s, "target_binding_changed")
        if target.state in {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.LOST,
        }:
            return self._abort(now_s, f"target_{target.state.value}")
        if target.state is UnifiedTrackState.RECOVERED:
            self._phase = ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
            return self._assessment(
                now_s,
                self._phase,
                ("target_not_stably_tracking",),
            )
        if (
            target.state is not UnifiedTrackState.TRACKING
            or not target.locked
            or not target.primary
        ):
            return self._assessment(
                now_s,
                ApproachHilPhase.SLIDE_CONFIRM_REQUIRED,
                ("target_not_stably_tracking",),
            )
        if now_s - target.observed_at_s > self.config.maximum_evidence_age_s:
            return self._abort(now_s, "target_evidence_stale")
        if not self._confirmation_accepted or self._challenge is None:
            self._phase = ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
            return self._assessment(now_s, self._phase, ("slide_confirmation_required",))
        if now_s >= self._challenge.expires_at_s:
            return self._abort(now_s, "slide_confirmation_expired")
        invalid_reason = self._invalid_safety_reason(input_data)
        if invalid_reason is not None:
            return self._abort(now_s, invalid_reason)

        yaw_error_deg, pitch_error_deg = self._optical_errors(target.bbox, input_data.calibration)
        if max(abs(yaw_error_deg), abs(pitch_error_deg)) > self.config.maximum_corridor_angle_deg:
            return self._abort(now_s, "target_outside_approach_corridor")

        yaw_advice = self._clamp(yaw_error_deg, self.config.maximum_yaw_advice_deg)
        pitch_advice = self._clamp(pitch_error_deg, self.config.maximum_pitch_advice_deg)
        bank_advice = self._clamp(yaw_error_deg * 0.7, self.config.maximum_bank_advice_deg)
        ground_range = float(input_data.ranging.ground_range_m)
        centered = (
            abs(yaw_error_deg) <= self.config.centering_tolerance_deg
            and abs(pitch_error_deg) <= self.config.centering_tolerance_deg
        )
        self._centered_frames = self._centered_frames + 1 if centered else 0

        if centered and ground_range <= self.config.completion_range_m:
            self._phase = ApproachHilPhase.COMPLETE
            reason = "approach_completion_gate_reached"
        elif self._centered_frames >= self.config.centered_frames_required:
            self._phase = ApproachHilPhase.APPROACH_SIM
            reason = "approach_corridor_centered"
        else:
            self._phase = ApproachHilPhase.CENTERING_SIM
            reason = "centering_advice_only"
        return self._assessment(
            now_s,
            self._phase,
            (reason,),
            yaw_error_deg=yaw_error_deg,
            pitch_error_deg=pitch_error_deg,
            yaw_advice_deg=yaw_advice,
            pitch_advice_deg=pitch_advice,
            bank_advice_deg=bank_advice,
            ground_range_m=ground_range,
        )

    def _invalid_safety_reason(self, data: ApproachHilInput) -> str | None:
        avoidance = data.avoidance
        if avoidance is None:
            return "avoidance_unavailable"
        if data.evaluated_at_s - avoidance.produced_at_s > self.config.maximum_evidence_age_s:
            return "avoidance_stale"
        if avoidance.state in {CollisionRiskState.AVOID, CollisionRiskState.INVALID}:
            return f"avoidance_{avoidance.state.value}"
        ranging = data.ranging
        if ranging is None:
            return "range_unavailable"
        if ranging.target_id != data.target.target_id or ranging.frame_id != data.target.frame_id:
            return "range_target_or_frame_mismatch"
        if ranging.validity is not RangeValidity.VALID or ranging.ground_range_m is None:
            return "range_invalid"
        if (
            ranging.data_freshness_s is None
            or ranging.data_freshness_s > self.config.maximum_evidence_age_s
            or ranging.sensor_consistency < self.config.minimum_range_consistency
        ):
            return "range_freshness_or_consistency_invalid"
        if not (
            self.config.minimum_approach_range_m
            <= ranging.ground_range_m
            <= self.config.maximum_approach_range_m
        ):
            return "range_outside_approach_domain"
        telemetry = data.telemetry
        if not all(
            value is True
            for value in (
                telemetry.in_allowed_zone,
                telemetry.geofence_healthy,
                telemetry.position_healthy,
                telemetry.link_healthy,
            )
        ):
            return "navigation_or_link_unhealthy"
        if not all(
            math.isfinite(value)
            for value in (
                telemetry.altitude_agl_m,
                telemetry.roll_deg,
                telemetry.pitch_deg,
                telemetry.airspeed_mps,
                telemetry.attitude_observed_at_s,
                telemetry.position_observed_at_s,
            )
        ):
            return "required_telemetry_unavailable"
        if (
            data.evaluated_at_s - telemetry.attitude_observed_at_s > self.config.maximum_pose_age_s
            or data.evaluated_at_s - telemetry.position_observed_at_s
            > self.config.maximum_pose_age_s
            or telemetry.attitude_observed_at_s > data.evaluated_at_s
            or telemetry.position_observed_at_s > data.evaluated_at_s
        ):
            return "required_telemetry_stale_or_from_future"
        if not (
            self.config.minimum_altitude_agl_m
            <= telemetry.altitude_agl_m
            <= self.config.maximum_altitude_agl_m
        ):
            return "altitude_outside_approach_domain"
        if telemetry.airspeed_mps < self.config.minimum_airspeed_mps:
            return "airspeed_below_approach_minimum"
        if abs(telemetry.roll_deg) > self.config.maximum_abs_roll_deg:
            return "roll_outside_approach_domain"
        if abs(telemetry.pitch_deg) > self.config.maximum_abs_pitch_deg:
            return "pitch_outside_approach_domain"
        return None

    def _abort(self, now_s: float, reason: str) -> ApproachHilAssessment:
        self._phase = ApproachHilPhase.ABORT_CLIMB_SIM
        # An abort status is produced at ``now_s``.  Retaining a previous
        # challenge here can put its deadline in the past and makes the wire
        # status invalid, which in turn suppresses the next real challenge.
        self._challenge = None
        self._confirmation_accepted = False
        self._centered_frames = 0
        self._abort_reason = reason
        return self._assessment(
            now_s,
            self._phase,
            (reason,),
            climb_pitch_advice_deg=self.config.abort_climb_pitch_advice_deg,
        )

    def _assessment(
        self,
        now_s: float,
        phase: ApproachHilPhase,
        reasons: tuple[str, ...],
        **values: float | None,
    ) -> ApproachHilAssessment:
        return ApproachHilAssessment(
            phase=phase,
            target_id=self._target_id,
            target_revision=self._target_revision,
            evaluated_at_s=now_s,
            reasons=reasons,
            confirmation_expires_at_s=(
                self._challenge.expires_at_s if self._challenge is not None else None
            ),
            **values,
        )

    @staticmethod
    def _optical_errors(
        bbox: BoundingBox,
        calibration: CameraCalibration,
    ) -> tuple[float, float]:
        x_px = (bbox.x1 + bbox.x2) * 0.5 * calibration.width_px
        y_px = (bbox.y1 + bbox.y2) * 0.5 * calibration.height_px
        yaw = math.degrees(math.atan2(x_px - calibration.cx_px, calibration.fx_px))
        pitch = math.degrees(math.atan2(y_px - calibration.cy_px, calibration.fy_px))
        return yaw, pitch

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        return max(-limit, min(limit, value))

    @staticmethod
    def _require_now(now_s: float) -> None:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("approach HIL time must be finite and non-negative")


__all__ = [
    "ApproachHilAssessment",
    "ApproachHilConfig",
    "ApproachHilController",
    "ApproachHilInput",
    "ApproachHilPhase",
    "ApproachTargetEvidence",
    "SlideConfirmationChallenge",
]

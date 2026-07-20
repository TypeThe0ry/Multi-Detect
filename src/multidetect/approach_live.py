from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace

from .approach_hil import (
    ApproachHilAssessment,
    ApproachHilController,
    ApproachHilInput,
    ApproachHilPhase,
    ApproachTargetEvidence,
)
from .domain import VehicleTelemetry
from .monocular_avoidance import MonocularAvoidanceAssessment
from .multimodal_ranging import CameraCalibration, RangeSolution
from .operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachStatusMessage,
    operator_identifier_token,
)
from .operator_status import (
    build_approach_challenge_status_message,
    build_approach_status_message,
)
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


@dataclass(frozen=True, slots=True)
class LiveApproachHilFrame:
    assessment: ApproachHilAssessment
    challenge: ApproachChallengeStatusMessage | None
    status: ApproachStatusMessage


class LiveApproachHilCoordinator:
    """Bind Mode-3 aim state to one operator selection and unified track.

    It emits authenticated metadata and confirmation state. The optional fixed-wing
    executor is the sole owner of Pixhawk setpoints; this coordinator has no direct
    actuator, mission-upload, mode-change, or payload-release interface.
    """

    def __init__(
        self,
        *,
        controller: ApproachHilController,
        calibration: CameraCalibration,
        flight_control_enabled: bool = False,
    ) -> None:
        self.controller = controller
        self.calibration = calibration
        self.flight_control_enabled = flight_control_enabled
        self._binding: tuple[str, str, int] | None = None
        self._challenge_sequence = 0
        self._status_sequence = 0
        self._pilot_input_cancelled = False
        self._vehicle_armed: bool | None = None

    @property
    def active_binding(self) -> tuple[str, str, int] | None:
        return self._binding

    def clear(self) -> None:
        self.controller.clear_target()
        self._binding = None
        self._pilot_input_cancelled = False
        self._vehicle_armed = None

    def cancel_execution(self, *, now_s: float, pilot_input_cancelled: bool) -> None:
        self._require_time(now_s, "Mode-3 cancellation time")
        self.controller.cancel_execution(now_s=now_s)
        self._pilot_input_cancelled = pilot_input_cancelled

    def prepare_frame(
        self,
        *,
        selection_command_id: str | None,
        track: UnifiedTrackSnapshot | None,
        frame_id: str,
        captured_at_s: float,
        ranging: RangeSolution | None,
        avoidance: MonocularAvoidanceAssessment | None,
        telemetry: VehicleTelemetry,
        now_s: float,
        wire_now_s: float,
    ) -> LiveApproachHilFrame:
        self._require_time(now_s, "Mode-3 monotonic time")
        self._require_time(wire_now_s, "Mode-3 wire time")
        if not frame_id.strip():
            raise ValueError("Mode-3 source frame ID cannot be empty")

        if (
            selection_command_id is None
            or not selection_command_id.strip()
            or track is None
            or not track.locked
            or not track.primary
        ):
            if self._binding is not None:
                self.clear()
            assessment = ApproachHilAssessment(
                phase=ApproachHilPhase.SEARCH,
                target_id=None,
                target_revision=None,
                evaluated_at_s=now_s,
                reasons=("no_target_selected",),
            )
            return self._frame(
                assessment, selection_command_id=None, now_s=now_s, wire_now_s=wire_now_s
            )

        revision = self._revision(selection_command_id, track.track_id)
        binding = (selection_command_id, track.track_id, revision)
        evidence = ApproachTargetEvidence(
            target_id=track.track_id,
            target_revision=revision,
            frame_id=frame_id,
            observed_at_s=track.last_seen_at_s,
            label=track.label,
            bbox=track.bbox,
            state=track.state,
            locked=track.locked,
            primary=track.primary,
        )
        vehicle_armed = telemetry.armed is True
        armed_state_changed = self._vehicle_armed is not None and (
            vehicle_armed != self._vehicle_armed
        )
        if binding != self._binding or armed_state_changed:
            # A disarm/arm edge starts a new execution epoch.  Re-selecting the
            # same visual target invalidates any challenge or accepted
            # confirmation from the previous epoch while retaining the LCK
            # identity and its continuously updated ranging evidence.
            self.controller.select_target(evidence, now_s=now_s)
            self._binding = binding
            self._pilot_input_cancelled = False
        elif (
            self.controller.can_rearm_after_tracking_recovery
            and track.state
            in {
                UnifiedTrackState.LOCKED,
                UnifiedTrackState.TRACKING,
            }
        ):
            # Keep the operator's LCK binding, but require a newly issued
            # confirmation after a transient loss caused by camera motion or
            # stale visual evidence.  Operator/pilot cancellation remains
            # latched because it is not a recoverable tracking abort.
            self.controller.select_target(evidence, now_s=now_s)
            self._pilot_input_cancelled = False
        self._vehicle_armed = vehicle_armed

        if not vehicle_armed:
            assessment = ApproachHilAssessment(
                phase=ApproachHilPhase.TARGET_LOCKED,
                target_id=track.track_id,
                target_revision=revision,
                evaluated_at_s=now_s,
                reasons=("required_telemetry_unavailable",),
            )
            return self._frame(
                assessment,
                selection_command_id=selection_command_id,
                now_s=now_s,
                wire_now_s=wire_now_s,
            )

        challenge = self.controller.challenge
        if (
            not self.controller.confirmation_accepted
            and self.controller.phase is not ApproachHilPhase.ABORT_CLIMB_SIM
            and track.state
            in {
                UnifiedTrackState.LOCKED,
                UnifiedTrackState.TRACKING,
            }
            and track.locked
            and track.primary
            and (challenge is None or now_s >= challenge.expires_at_s)
        ):
            challenge = self.controller.issue_slide_challenge(now_s=now_s)

        assessment = self.controller.evaluate(
            ApproachHilInput(
                target=evidence,
                calibration=self.calibration,
                ranging=ranging,
                avoidance=avoidance,
                telemetry=telemetry,
                evaluated_at_s=now_s,
            )
        )
        return self._frame(
            assessment,
            selection_command_id=selection_command_id,
            now_s=now_s,
            wire_now_s=wire_now_s,
        )

    def consume_confirmation(
        self,
        command: ApproachConfirmationCommand,
        *,
        now_s: float,
    ) -> bool:
        self._require_time(now_s, "Mode-3 confirmation receipt time")
        challenge = self.controller.challenge
        binding = self._binding
        if challenge is None or binding is None or now_s >= challenge.expires_at_s:
            return False
        selection_command_id, target_id, target_revision = binding
        if (
            command.selection_command_id != selection_command_id
            or command.challenge_token != operator_identifier_token(challenge.token)
            or command.target_token != operator_identifier_token(target_id)
            or command.target_revision != target_revision
        ):
            return False
        completed_at_s = now_s
        started_at_s = completed_at_s - command.slide_duration_s
        return self.controller.accept_slide_confirmation(
            token=challenge.token,
            target_id=target_id,
            target_revision=target_revision,
            slide_started_at_s=started_at_s,
            slide_completed_at_s=completed_at_s,
            completion_fraction=command.completion_fraction,
            continuous=command.continuous,
        )

    def _frame(
        self,
        assessment: ApproachHilAssessment,
        *,
        selection_command_id: str | None,
        now_s: float,
        wire_now_s: float,
    ) -> LiveApproachHilFrame:
        challenge_message = None
        challenge = self.controller.challenge
        if (
            challenge is not None
            and not self.controller.confirmation_accepted
            and selection_command_id is not None
            and now_s < challenge.expires_at_s
        ):
            self._challenge_sequence = (self._challenge_sequence + 1) & 0xFFFFFFFF
            challenge_message = build_approach_challenge_status_message(
                challenge=challenge,
                selection_command_id=selection_command_id,
                sequence=self._challenge_sequence,
                produced_at_s=wire_now_s,
                challenge_clock_now_s=now_s,
            )
        self._status_sequence = (self._status_sequence + 1) & 0xFFFFFFFF
        status = build_approach_status_message(
            assessment=assessment,
            sequence=self._status_sequence,
            produced_at_s=wire_now_s,
            assessment_clock_now_s=now_s,
            flight_control_enabled=self.flight_control_enabled,
        )
        if self._pilot_input_cancelled:
            status = replace(status, pilot_input_cancelled=True)
        return LiveApproachHilFrame(assessment, challenge_message, status)

    @staticmethod
    def _revision(selection_command_id: str, track_id: str) -> int:
        digest = hashlib.sha256(f"{selection_command_id}\0{track_id}".encode()).digest()
        return int.from_bytes(digest[:4], "big")

    @staticmethod
    def _require_time(value: float, name: str) -> None:
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")


__all__ = ["LiveApproachHilCoordinator", "LiveApproachHilFrame"]

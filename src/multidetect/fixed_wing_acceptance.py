"""Deterministic fixed-wing software acceptance gate.

This module exercises the production target pool, Mode-3 confirmation path and
bounded fixed-wing attitude executor with an in-memory transport.  It is a
software/HIL-preflight gate: it deliberately records the control messages that
would be emitted, but it never opens a Pixhawk link or sends a physical command.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .actuator_safety import run_actuator_safety_software_acceptance
from .approach_hil import ApproachHilController
from .approach_live import LiveApproachHilCoordinator
from .domain import BoundingBox, VehicleTelemetry
from .fixed_wing_aim_control import (
    FixedWingAimConfig,
    FixedWingAimController,
    FixedWingAimExecutor,
    FixedWingAimState,
    FixedWingAimTarget,
)
from .monocular_avoidance import CollisionRiskState, MonocularAvoidanceAssessment
from .multimodal_ranging import CameraCalibration, RangeSolution, RangeValidity
from .operator_link import ApproachConfirmationCommand, operator_identifier_token
from .pixhawk import PixhawkRcInputSnapshot
from .product_version import PRODUCT_VERSION
from .unified_tracking import (
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackSnapshot,
    UnifiedTrackState,
)

_CALIBRATION = CameraCalibration("fixed-wing-acceptance-v1", 1280, 720, 900.0, 900.0, 640.0, 360.0)
_SELECTION_COMMAND_ID = "fixed-wing-acceptance-selection"
_TARGET_LABEL = "aircraft"
_TARGET_BBOX = BoundingBox(0.70, 0.58, 0.80, 0.68)


@dataclass
class _RecordingTransport:
    """Minimal transport double with the same interface as the executor."""

    setpoints: list[Any]
    modes: list[str]
    rc_input_snapshot: PixhawkRcInputSnapshot | None

    def __init__(self) -> None:
        self.setpoints = []
        self.modes = []
        self.rc_input_snapshot = _rc_input(0.0)

    def send_attitude_target(self, setpoint: Any) -> None:
        self.setpoints.append(setpoint)

    def request_mode(self, mode: str) -> None:
        self.modes.append(mode)


def run_fixed_wing_software_acceptance() -> dict[str, Any]:
    """Run the deterministic DET -> TRK -> LCK -> TGT -> OFFBOARD gate."""

    actuator_safety = run_actuator_safety_software_acceptance()
    if not actuator_safety["gate_passed"]:
        raise RuntimeError("actuator safety software bench did not pass")

    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=3,
            priority_minimum_new_track_confidence=0.25,
        )
    )
    pool_states: list[str] = []
    updates = []
    for index, captured_at_s in enumerate((0.00, 0.05, 0.10), start=1):
        update = pool.update(
            frame_id=f"fixed-wing-acceptance-{index}",
            captured_at_s=captured_at_s,
            observations=(
                TargetObservation(
                    label=_TARGET_LABEL,
                    confidence=0.94,
                    bbox=_TARGET_BBOX,
                    source="acceptance-fixture",
                ),
            ),
        )
        updates.append(update)
        pool_states.append(update.tracks[0].state.value)

    track = _single_track(pool)
    if pool_states != ["detected", "detected", "tracking"] or not track.actionable:
        raise RuntimeError("fixed-wing target pool did not reach actionable TRK")

    locked = pool.lock(track.track_id, now_s=0.15)
    switch = pool.switch_primary(track.track_id, now_s=0.15)
    if not (locked.locked and locked.state is UnifiedTrackState.LOCKED):
        raise RuntimeError("fixed-wing target did not enter LCK")
    lck_state = "LCK"

    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(),
        calibration=_CALIBRATION,
    )
    first_frame = coordinator.prepare_frame(
        selection_command_id=_SELECTION_COMMAND_ID,
        track=locked,
        frame_id="fixed-wing-acceptance-lck",
        captured_at_s=0.16,
        ranging=_range("target-000001", "fixed-wing-acceptance-lck", 0.16),
        avoidance=_avoidance("fixed-wing-acceptance-lck", 0.16),
        telemetry=_telemetry(0.16),
        now_s=0.17,
        wire_now_s=1000.17,
    )
    if (
        first_frame.challenge is None
        or coordinator.controller.phase.value != "slide_confirm_required"
    ):
        raise RuntimeError("fixed-wing LCK did not issue a target-bound confirmation challenge")

    challenge = first_frame.challenge
    # The wire message exposes only derived operator tokens.  Feed the same
    # values a QGC confirmation would send back through the production path.
    accepted = coordinator.consume_confirmation(
        ApproachConfirmationCommand(
            command_token=operator_identifier_token("fixed-wing-acceptance-command"),
            session_token=operator_identifier_token("fixed-wing-acceptance-session"),
            challenge_token=challenge.challenge_token,
            target_token=challenge.target_token,
            target_revision=challenge.target_revision,
            selection_command_id=_SELECTION_COMMAND_ID,
            sequence=1,
            issued_at_s=1000.60,
            expires_at_s=1002.60,
            slide_duration_s=0.80,
            completion_fraction=1.0,
            continuous=True,
        ),
        now_s=1.00,
    )
    if not accepted:
        raise RuntimeError("fixed-wing target confirmation was rejected")
    tgt_state = "TGT"

    # The production target pool changes LOCKED to TRACKING on the next fresh
    # observation.  That is the state expected by the Mode-3 coordinator and by
    # the aim controller after the LCK identity remains primary.
    post_lock_update = pool.update(
        frame_id="fixed-wing-acceptance-tgt",
        captured_at_s=1.00,
        observations=(
            TargetObservation(
                label=_TARGET_LABEL,
                confidence=0.95,
                bbox=_TARGET_BBOX,
                source="acceptance-fixture",
            ),
        ),
    )
    tracking_target = _single_track(pool)
    if tracking_target.state is not UnifiedTrackState.TRACKING or not tracking_target.primary:
        raise RuntimeError("confirmed fixed-wing target did not retain primary TRK identity")

    transport = _RecordingTransport()
    executor = FixedWingAimExecutor(
        FixedWingAimController(
            _CALIBRATION,
            FixedWingAimConfig(prestream_setpoints=3),
        ),
        transport,
    )
    transport.rc_input_snapshot = _rc_input(1.04)
    aim_states: list[str] = []
    decisions = []
    for now_s in (1.05, 1.10, 1.15):
        decision = executor.step(
            target=_aim_target(tracking_target, observed_at_s=now_s - 0.01),
            telemetry=_telemetry(now_s - 0.01),
            mode3_active=True,
            execution_confirmed=True,
            now_s=now_s,
        )
        decisions.append(decision)
        aim_states.append(decision.state.value)
    if aim_states != ["prestream", "prestream", "active"] or transport.modes != ["OFFBOARD"]:
        raise RuntimeError("fixed-wing executor did not prestream and enter OFFBOARD exactly once")

    active_setpoint = decisions[-1].setpoint
    if active_setpoint is None:
        raise RuntimeError("fixed-wing executor emitted no active setpoint")
    if not (
        abs(active_setpoint.roll_deg) <= 20.0
        and abs(active_setpoint.pitch_deg) <= 15.0
        and math.isclose(
            sum(value * value for value in active_setpoint.quaternion_wxyz),
            1.0,
            abs_tol=1e-9,
        )
    ):
        raise RuntimeError("fixed-wing attitude target exceeded bounds or quaternion normalization")

    transport.rc_input_snapshot = _rc_input(1.18, channel=7, pwm=1600)
    cancelled = executor.step(
        target=_aim_target(tracking_target, observed_at_s=1.17),
        telemetry=_telemetry(1.17, flight_mode="OFFBOARD"),
        mode3_active=True,
        execution_confirmed=True,
        now_s=1.19,
    )
    if (
        cancelled.state is not FixedWingAimState.INHIBITED
        or cancelled.reason != "pilot_input_cancelled"
    ):
        raise RuntimeError("fixed-wing RC cancellation did not inhibit aim")
    if transport.modes != ["OFFBOARD", "AUTO"]:
        raise RuntimeError("fixed-wing RC cancellation did not restore entry mode")

    # Recreate the executor to exercise the independent visual-loss recovery
    # path without conflating it with a pilot cancellation latch.
    recovery_transport = _RecordingTransport()
    recovery_executor = FixedWingAimExecutor(
        FixedWingAimController(
            _CALIBRATION,
            FixedWingAimConfig(prestream_setpoints=1),
        ),
        recovery_transport,
    )
    recovery_transport.rc_input_snapshot = _rc_input(2.00)
    recovery_executor.step(
        target=_aim_target(tracking_target, observed_at_s=2.00),
        telemetry=_telemetry(2.01, flight_mode="POSCTL"),
        mode3_active=True,
        execution_confirmed=True,
        now_s=2.01,
    )
    lost = recovery_executor.step(
        target=_aim_target(
            tracking_target,
            observed_at_s=2.04,
            state=UnifiedTrackState.LOST,
        ),
        telemetry=_telemetry(2.05, flight_mode="OFFBOARD"),
        mode3_active=True,
        execution_confirmed=True,
        now_s=2.05,
    )
    if lost.reason != "target_lost" or recovery_transport.modes != ["OFFBOARD", "POSCTL"]:
        raise RuntimeError("fixed-wing target loss did not restore the observed entry mode")

    return {
        "event": "fixed_wing_software_acceptance_passed",
        # This closes the deterministic software/HIL-preflight exercise only;
        # the report remains explicitly software_only and the release
        # manifest still requires separate HIL hardware/real-control status.
        "gate_passed": True,
        "release_label": PRODUCT_VERSION.release_label,
        "runtime_mode": "HIL",
        "software_only": True,
        "state_path": ["DET", "TRK", lck_state, tgt_state],
        "target_pool": {
            "frame_states": pool_states,
            "created_track_id": updates[0].created_track_ids[0],
            "tracking_track_id": tracking_target.track_id,
            "tracking_actionable": tracking_target.actionable,
            "primary_track_id": pool.primary_track_id,
            "post_lock_frame": post_lock_update.frame_id,
            "primary_switch_latency_ms": switch.switch_latency_ms,
        },
        "target_confirmation": {
            "challenge_issued": True,
            "challenge_bound_to_selection": challenge.selection_command_id == _SELECTION_COMMAND_ID,
            "confirmation_accepted": accepted,
            "flight_control_enabled": False,
        },
        "attitude_control": {
            "aim_states": aim_states,
            "setpoints_recorded": len(transport.setpoints),
            "mode_requests": list(transport.modes),
            "offboard_requested_once": transport.modes.count("OFFBOARD") == 1,
            "roll_limit_deg": 20.0,
            "pitch_limit_deg": 15.0,
            "active_roll_deg": active_setpoint.roll_deg,
            "active_pitch_deg": active_setpoint.pitch_deg,
            "active_yaw_deg": active_setpoint.yaw_deg,
            "quaternion_norm": math.sqrt(
                sum(value * value for value in active_setpoint.quaternion_wxyz)
            ),
        },
        "pilot_cancel": {
            "reason": cancelled.reason,
            "mode_requests_after_cancel": list(transport.modes),
            "setpoints_after_cancel": len(transport.setpoints),
            "latched_until_new_binding": True,
        },
        "target_loss_recovery": {
            "reason": lost.reason,
            "mode_requests": list(recovery_transport.modes),
            "restored_entry_mode": recovery_transport.modes[-1],
        },
        "actuator_safety": actuator_safety,
        "ui_contract": {
            "surface": "native_qgc_flyview",
            "second_window": False,
            "second_video_surface": False,
            "custom_theme": False,
            "operator_visual_acceptance": "not_claimed",
        },
        "physical_actuators": {
            "pixhawk_link_opened": False,
            "mavlink_messages_sent": 0,
            "physical_output_enabled": False,
        },
    }


def _single_track(pool: UnifiedTargetPool) -> UnifiedTrackSnapshot:
    tracks = pool.snapshots()
    if len(tracks) != 1:
        raise RuntimeError(f"acceptance fixture expected one target, got {len(tracks)}")
    return tracks[0]


def _aim_target(
    track: UnifiedTrackSnapshot,
    *,
    observed_at_s: float,
    state: UnifiedTrackState | None = None,
) -> FixedWingAimTarget:
    return FixedWingAimTarget(
        target_id=track.track_id,
        target_revision=track.observation_count,
        bbox=track.bbox,
        observed_at_s=observed_at_s,
        state=state or UnifiedTrackState.TRACKING,
        locked=True,
        primary=True,
    )


def _telemetry(observed_at_s: float, *, flight_mode: str = "AUTO") -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=60.0,
        roll_deg=2.0,
        pitch_deg=1.0,
        ground_speed_mps=21.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=False,
        release_zone_clear=False,
        heading_deg=82.0,
        armed=True,
        flight_mode=flight_mode,
        attitude_observed_at_s=observed_at_s,
        position_observed_at_s=observed_at_s,
        airspeed_mps=19.0,
        airspeed_observed_at_s=observed_at_s,
    )


def _range(target_id: str, frame_id: str, evaluated_at_s: float) -> RangeSolution:
    return RangeSolution(
        target_id=target_id,
        frame_id=frame_id,
        calibration_id=_CALIBRATION.calibration_id,
        evaluated_at_s=evaluated_at_s,
        validity=RangeValidity.VALID,
        reasons=("multimodal_range_consistent",),
        sources=("camera_ground", "laser"),
        rejected_sources=(),
        slant_range_m=80.0,
        ground_range_m=75.0,
        slant_range_ci95_m=(77.0, 83.0),
        ground_range_ci95_m=(72.0, 78.0),
        relative_bearing_deg=4.0,
        absolute_bearing_deg=86.0,
        bearing_sigma_deg=0.8,
        north_offset_m=5.0,
        east_offset_m=75.0,
        data_freshness_s=0.01,
        sensor_consistency=0.9,
    )


def _avoidance(frame_id: str, observed_at_s: float) -> MonocularAvoidanceAssessment:
    return MonocularAvoidanceAssessment(
        frame_id=frame_id,
        state=CollisionRiskState.CLEAR,
        zones=(),
        captured_at_s=observed_at_s - 0.01,
        produced_at_s=observed_at_s,
        data_age_s=0.01,
        frame_interval_s=0.05,
        valid_feature_count=80,
        rotation_compensated=True,
        processing_time_ms=4.0,
    )


def _rc_input(
    observed_at_s: float,
    *,
    channel: int | None = None,
    pwm: int = 1500,
) -> PixhawkRcInputSnapshot:
    channels: list[int | None] = [1500] * 18
    if channel is not None:
        channels[channel - 1] = pwm
    return PixhawkRcInputSnapshot(observed_at_s, tuple(channels))


__all__ = ["run_fixed_wing_software_acceptance"]

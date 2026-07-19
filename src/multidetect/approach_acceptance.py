from __future__ import annotations

import time
from typing import Any

from .approach_hil import ApproachHilController, ApproachHilPhase
from .approach_live import LiveApproachHilCoordinator
from .domain import BoundingBox, VehicleTelemetry
from .monocular_avoidance import CollisionRiskState, MonocularAvoidanceAssessment
from .multimodal_ranging import CameraCalibration, RangeSolution, RangeValidity
from .operator_link import (
    ApproachConfirmationCommand,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    VideoGeometry,
    operator_identifier_token,
)
from .operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
from .operator_protocol import OperatorTunnelCodec
from .operator_udp import UdpOperatorSelectionServer, UdpOperatorSessionClient
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState

_APPLICATION_KEY = b"mode3-approach-hil-application-key-32-bytes"
_MAVLINK_KEY = b"A" * 32
_GEOMETRY = VideoGeometry("camera-main", 1280, 720)
_CALIBRATION = CameraCalibration("camera-main-hil-v1", 1280, 720, 900.0, 900.0, 640.0, 360.0)


def run_mode3_approach_hil_acceptance() -> dict[str, Any]:
    """Exercise arbitrary-target advisory state, aborts, and visual re-arming."""

    vehicle = _run_signed_target_session(
        label="vehicle",
        target_id="mode3-vehicle-1",
        selection_id="11111111-1111-4111-8111-111111111111",
        session_id="22222222-2222-4222-8222-222222222222",
        logical_base_s=10.0,
    )
    person = _run_signed_target_session(
        label="person",
        target_id="mode3-person-1",
        selection_id="33333333-3333-4333-8333-333333333333",
        session_id="44444444-4444-4444-8444-444444444444",
        logical_base_s=30.0,
    )

    occluded = _abort_case(UnifiedTrackState.OCCLUDED, CollisionRiskState.CLEAR, 50.0)
    lost = _abort_case(UnifiedTrackState.LOST, CollisionRiskState.CLEAR, 70.0)
    avoidance = _abort_case(UnifiedTrackState.TRACKING, CollisionRiskState.AVOID, 90.0)
    avoidance_invalid = _abort_case(
        UnifiedTrackState.TRACKING,
        CollisionRiskState.INVALID,
        110.0,
    )

    switch_coordinator, old_command = _confirmed_coordinator(
        label="vehicle",
        target_id="mode3-switch-target",
        selection_id="55555555-5555-4555-8555-555555555555",
        logical_base_s=130.0,
    )
    switched = switch_coordinator.prepare_frame(
        selection_command_id="66666666-6666-4666-8666-666666666666",
        track=_track("mode3-switch-target", "vehicle", 130.95),
        frame_id="mode3-switch-frame",
        captured_at_s=130.95,
        ranging=_range("mode3-switch-target", "mode3-switch-frame", 130.96),
        avoidance=_avoidance("mode3-switch-frame", 130.96),
        telemetry=_telemetry(130.96),
        now_s=130.97,
        wire_now_s=time.time(),
    )
    old_confirmation_rejected = not switch_coordinator.consume_confirmation(
        old_command,
        now_s=130.98,
    )
    target_switch_reset = bool(
        switched.assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
        and switched.challenge is not None
        and old_confirmation_rejected
    )
    if not target_switch_reset:
        raise RuntimeError("Mode-3 target switch did not require a new slide confirmation")

    return {
        "event": "mode3_approach_hil_acceptance_passed",
        "arbitrary_targets": {
            "vehicle": vehicle,
            "person": person,
        },
        "abort_cases": {
            "occluded": occluded,
            "lost": lost,
            "avoidance_avoid": avoidance,
            "avoidance_invalid": avoidance_invalid,
            "target_switch_requires_new_slide": target_switch_reset,
            "old_confirmation_rejected": old_confirmation_rejected,
        },
        "advisory_only": True,
        "sitl_hil_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "real_actuator_interface_present": False,
        "model_training_executed": False,
        "model_inference_executed": False,
    }


def _run_signed_target_session(
    *,
    label: str,
    target_id: str,
    selection_id: str,
    session_id: str,
    logical_base_s: float,
) -> dict[str, Any]:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(),
        calibration=_CALIBRATION,
    )
    first = _prepare(
        coordinator,
        selection_id=selection_id,
        target_id=target_id,
        label=label,
        now_s=logical_base_s + 0.05,
    )
    if (
        first.assessment.phase is not ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
        or first.challenge is None
        or first.assessment.yaw_advice_deg is not None
    ):
        raise RuntimeError("Mode-3 target produced advice before continuous-slide confirmation")

    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    operator = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))
    started_s = time.perf_counter()
    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(_GEOMETRY),
        receive_timeout_s=0.05,
    ) as server:
        server.start_background()
        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=operator,
            retry_interval_s=0.25,
            maximum_attempts=3,
        ) as client:
            selection_now_s = time.time()
            selection_receipt = client.deliver(
                TargetSelectionCommand(
                    command_id=selection_id,
                    session_id=session_id,
                    sequence=1,
                    action=SelectionAction.SELECT,
                    geometry=_GEOMETRY,
                    issued_at_s=selection_now_s,
                    expires_at_s=selection_now_s + 3.0,
                    bbox=_track(target_id, label, logical_base_s).bbox,
                    displayed_frame_id=f"{target_id}-selection-frame",
                )
            )
            queued_selection = _poll(server.poll_selection)
            if queued_selection is None or not selection_receipt.acknowledgement.accepted:
                raise RuntimeError("signed Mode-3 selection was not acknowledged")
            peer = queued_selection[1]
            server.publish_approach_challenge(first.challenge, peer=peer)
            challenge = client.receive_approach_challenge(timeout_s=1.0)
            issued_at_s = time.time()
            command = ApproachConfirmationCommand(
                command_token=operator_identifier_token(f"{target_id}-slide-command"),
                session_token=operator_identifier_token(session_id),
                challenge_token=challenge.challenge_token,
                target_token=challenge.target_token,
                target_revision=challenge.target_revision,
                selection_command_id=challenge.selection_command_id,
                sequence=2,
                issued_at_s=issued_at_s,
                expires_at_s=min(issued_at_s + 2.0, challenge.expires_at_s),
                slide_duration_s=0.8,
                completion_fraction=1.0,
                continuous=True,
            )
            receipt = client.deliver_approach_confirmation(command)
            queued_confirmation = _poll(server.poll_approach_confirmation)
            if queued_confirmation is None or not receipt.acknowledgement.accepted:
                raise RuntimeError("signed Mode-3 continuous slide was not acknowledged")
            if not coordinator.consume_confirmation(
                queued_confirmation[0],
                now_s=logical_base_s + 0.9,
            ):
                raise RuntimeError("Mode-3 coordinator rejected an authenticated slide")

            phases: list[str] = []
            final = None
            for index in range(3):
                evaluated_at_s = logical_base_s + 0.95 + index * 0.05
                final = _prepare(
                    coordinator,
                    selection_id=selection_id,
                    target_id=target_id,
                    label=label,
                    now_s=evaluated_at_s,
                    frame_id=f"{target_id}-centered-{index}",
                )
                phases.append(final.assessment.phase.value)
            if final is None or final.assessment.phase is not ApproachHilPhase.APPROACH_SIM:
                raise RuntimeError(
                    "confirmed Mode-3 target did not reach advisory approach simulation"
                )
            server.publish_approach_status(final.status, peer=peer)
            received_status = client.receive_approach_status(timeout_s=1.0)
            if (
                received_status.phase is not ApproachHilPhase.APPROACH_SIM
                or not received_status.advisory_only
                or received_status.flight_control_enabled
                or received_status.physical_release_enabled
            ):
                raise RuntimeError("Mode-3 advisory status violated the no-control boundary")
    return {
        "label": label,
        "selection_acknowledged": True,
        "continuous_slide_acknowledged": True,
        "phases_after_slide": phases,
        "final_phase": ApproachHilPhase.APPROACH_SIM.value,
        "status_received": True,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "session_elapsed_ms": (time.perf_counter() - started_s) * 1000.0,
    }


def _abort_case(
    state: UnifiedTrackState,
    collision: CollisionRiskState,
    logical_base_s: float,
) -> dict[str, Any]:
    target_id = f"mode3-abort-{state.value}-{collision.value}"
    selection_id = f"{int(logical_base_s):08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    coordinator, _command = _confirmed_coordinator(
        label="vehicle",
        target_id=target_id,
        selection_id=selection_id,
        logical_base_s=logical_base_s,
    )
    frame_id = f"{target_id}-unsafe"
    evaluated_at_s = logical_base_s + 0.95
    unsafe = coordinator.prepare_frame(
        selection_command_id=selection_id,
        track=_track(target_id, "vehicle", evaluated_at_s - 0.01, state=state),
        frame_id=frame_id,
        captured_at_s=evaluated_at_s - 0.01,
        ranging=_range(target_id, frame_id, evaluated_at_s - 0.005),
        avoidance=_avoidance(frame_id, evaluated_at_s - 0.005, state=collision),
        telemetry=_telemetry(evaluated_at_s - 0.005),
        now_s=evaluated_at_s,
        wire_now_s=time.time(),
    )
    recovered_frame = f"{target_id}-apparently-clear"
    latched = coordinator.prepare_frame(
        selection_command_id=selection_id,
        track=_track(target_id, "vehicle", evaluated_at_s + 0.03),
        frame_id=recovered_frame,
        captured_at_s=evaluated_at_s + 0.03,
        ranging=_range(target_id, recovered_frame, evaluated_at_s + 0.04),
        avoidance=_avoidance(recovered_frame, evaluated_at_s + 0.04),
        telemetry=_telemetry(evaluated_at_s + 0.04),
        now_s=evaluated_at_s + 0.05,
        wire_now_s=time.time(),
    )
    visual_interruption = collision is CollisionRiskState.CLEAR and state in {
        UnifiedTrackState.OCCLUDED,
        UnifiedTrackState.LOST,
    }
    if unsafe.assessment.phase is not ApproachHilPhase.ABORT_CLIMB_SIM:
        raise RuntimeError("Mode-3 unsafe evidence did not emit an advisory abort")
    if unsafe.assessment.climb_pitch_advice_deg is None:
        raise RuntimeError("Mode-3 abort omitted bounded advisory climb guidance")
    if visual_interruption:
        if (
            latched.assessment.phase is not ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
            or latched.assessment.reasons != ("slide_confirmation_required",)
            or latched.challenge is None
            or latched.status.confirmation_expires_at_s is None
            or latched.status.confirmation_expires_at_s <= latched.status.produced_at_s
        ):
            raise RuntimeError("visual Mode-3 recovery did not require a fresh slide challenge")
    elif (
        latched.assessment.phase is not ApproachHilPhase.ABORT_CLIMB_SIM
        or latched.assessment.reasons != ("abort_latched_until_reselection",)
    ):
        raise RuntimeError("non-visual Mode-3 abort did not remain latched")
    return {
        "abort_phase": unsafe.assessment.phase.value,
        "reason": unsafe.assessment.reasons[0],
        "bounded_climb_advice": unsafe.assessment.climb_pitch_advice_deg,
        "abort_latched": not visual_interruption,
        "rearmed_with_fresh_challenge": visual_interruption,
        "recovery_phase": latched.assessment.phase.value,
        "blind_approach_continued": False,
    }


def _confirmed_coordinator(
    *,
    label: str,
    target_id: str,
    selection_id: str,
    logical_base_s: float,
) -> tuple[LiveApproachHilCoordinator, ApproachConfirmationCommand]:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(),
        calibration=_CALIBRATION,
    )
    first = _prepare(
        coordinator,
        selection_id=selection_id,
        target_id=target_id,
        label=label,
        now_s=logical_base_s + 0.05,
    )
    if first.challenge is None:
        raise RuntimeError("Mode-3 abort scenario did not create a slide challenge")
    command = ApproachConfirmationCommand(
        command_token=operator_identifier_token(f"{target_id}-direct-slide"),
        session_token=operator_identifier_token(f"{target_id}-direct-session"),
        challenge_token=first.challenge.challenge_token,
        target_token=first.challenge.target_token,
        target_revision=first.challenge.target_revision,
        selection_command_id=selection_id,
        sequence=1,
        issued_at_s=time.time(),
        expires_at_s=time.time() + 2.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    if not coordinator.consume_confirmation(command, now_s=logical_base_s + 0.9):
        raise RuntimeError("Mode-3 abort scenario could not establish a slide grant")
    return coordinator, command


def _prepare(
    coordinator: LiveApproachHilCoordinator,
    *,
    selection_id: str,
    target_id: str,
    label: str,
    now_s: float,
    frame_id: str | None = None,
):
    source_frame = frame_id or f"{target_id}-frame"
    observed_at_s = now_s - 0.02
    return coordinator.prepare_frame(
        selection_command_id=selection_id,
        track=_track(target_id, label, observed_at_s),
        frame_id=source_frame,
        captured_at_s=observed_at_s,
        ranging=_range(target_id, source_frame, now_s - 0.01),
        avoidance=_avoidance(source_frame, now_s - 0.01),
        telemetry=_telemetry(now_s - 0.01),
        now_s=now_s,
        wire_now_s=time.time(),
    )


def _track(
    target_id: str,
    label: str,
    observed_at_s: float,
    *,
    state: UnifiedTrackState = UnifiedTrackState.TRACKING,
) -> UnifiedTrackSnapshot:
    bbox = BoundingBox(0.46, 0.44, 0.54, 0.56)
    return UnifiedTrackSnapshot(
        track_id=target_id,
        state=state,
        label=label,
        bbox=bbox,
        predicted_bbox=bbox,
        first_seen_at_s=max(0.0, observed_at_s - 2.0),
        last_seen_at_s=observed_at_s,
        state_changed_at_s=max(0.0, observed_at_s - 1.0),
        observation_count=10,
        missed_frame_count=0,
        confidence=0.93,
        tracking_quality=0.92,
        velocity_x_s=0.0,
        velocity_y_s=0.0,
        appearance_sample_count=3,
        last_appearance_distance=0.05,
        reid_confirmed=True,
        locked=True,
        primary=True,
        actionable=True,
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
        relative_bearing_deg=0.0,
        absolute_bearing_deg=90.0,
        bearing_sigma_deg=0.8,
        north_offset_m=0.0,
        east_offset_m=75.0,
        data_freshness_s=0.01,
        sensor_consistency=0.9,
    )


def _avoidance(
    frame_id: str,
    observed_at_s: float,
    *,
    state: CollisionRiskState = CollisionRiskState.CLEAR,
) -> MonocularAvoidanceAssessment:
    return MonocularAvoidanceAssessment(
        frame_id=frame_id,
        state=state,
        zones=(),
        captured_at_s=observed_at_s - 0.01,
        produced_at_s=observed_at_s,
        data_age_s=0.01,
        frame_interval_s=0.05,
        valid_feature_count=80,
        rotation_compensated=True,
        processing_time_ms=4.0,
    )


def _telemetry(observed_at_s: float) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=45.0,
        roll_deg=1.0,
        pitch_deg=0.5,
        ground_speed_mps=18.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=False,
        release_zone_clear=False,
        airspeed_mps=17.0,
        attitude_observed_at_s=observed_at_s,
        position_observed_at_s=observed_at_s,
    )


def _adapter(endpoint: OperatorMavlinkEndpoint) -> OperatorMavlinkTunnelAdapter:
    return OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=_APPLICATION_KEY, geometries=(_GEOMETRY,)),
        endpoint,
        signing_key=_MAVLINK_KEY,
        signing_link_id=endpoint.local_component_id,
        initial_signing_timestamp=5_000_000 + endpoint.local_system_id,
    )


def _poll(callback: Any, *, timeout_s: float = 1.0) -> Any | None:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        value = callback()
        if value is not None:
            return value
        time.sleep(0.005)
    return None


__all__ = ["run_mode3_approach_hil_acceptance"]

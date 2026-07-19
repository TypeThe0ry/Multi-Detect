from __future__ import annotations

from dataclasses import replace

from multidetect.approach_hil import ApproachHilController, ApproachHilPhase
from multidetect.approach_live import LiveApproachHilCoordinator
from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.monocular_avoidance import CollisionRiskState, MonocularAvoidanceAssessment
from multidetect.multimodal_ranging import CameraCalibration, RangeSolution, RangeValidity
from multidetect.operator_link import ApproachConfirmationCommand
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState

SELECTION_ID = "11111111-1111-4111-8111-111111111111"


def _track(**changes) -> UnifiedTrackSnapshot:
    values = dict(
        track_id="manual-vehicle-1",
        state=UnifiedTrackState.TRACKING,
        label="car",
        bbox=BoundingBox(0.46, 0.44, 0.54, 0.56),
        predicted_bbox=BoundingBox(0.46, 0.44, 0.54, 0.56),
        first_seen_at_s=9.0,
        last_seen_at_s=10.0,
        state_changed_at_s=9.5,
        observation_count=10,
        missed_frame_count=0,
        confidence=0.9,
        tracking_quality=0.9,
        velocity_x_s=0.0,
        velocity_y_s=0.0,
        appearance_sample_count=3,
        last_appearance_distance=0.1,
        reid_confirmed=True,
        locked=True,
        primary=True,
        actionable=True,
    )
    values.update(changes)
    return UnifiedTrackSnapshot(**values)


def _calibration() -> CameraCalibration:
    return CameraCalibration("camera-main-v1", 1280, 720, 900.0, 900.0, 640.0, 360.0)


def _range(**changes) -> RangeSolution:
    values = dict(
        target_id="manual-vehicle-1",
        frame_id="frame-10",
        calibration_id="camera-main-v1",
        evaluated_at_s=10.02,
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
        data_freshness_s=0.02,
        sensor_consistency=0.9,
    )
    values.update(changes)
    return RangeSolution(**values)


def _avoidance(**changes) -> MonocularAvoidanceAssessment:
    values = dict(
        frame_id="frame-10",
        state=CollisionRiskState.CLEAR,
        zones=(),
        captured_at_s=10.0,
        produced_at_s=10.02,
        data_age_s=0.02,
        frame_interval_s=0.05,
        valid_feature_count=80,
        rotation_compensated=True,
        processing_time_ms=4.0,
    )
    values.update(changes)
    return MonocularAvoidanceAssessment(**values)


def _telemetry(**changes) -> VehicleTelemetry:
    values = dict(
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
        attitude_observed_at_s=10.0,
        position_observed_at_s=10.0,
    )
    values.update(changes)
    return VehicleTelemetry(**values)


def _prepare(coordinator: LiveApproachHilCoordinator, **changes):
    values = dict(
        selection_command_id=SELECTION_ID,
        track=_track(),
        frame_id="frame-10",
        captured_at_s=10.0,
        ranging=_range(),
        avoidance=_avoidance(),
        telemetry=_telemetry(),
        now_s=10.05,
        wire_now_s=1000.05,
    )
    values.update(changes)
    return coordinator.prepare_frame(**values)


def test_live_coordinator_binds_challenge_and_consumes_continuous_slide() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )
    first = _prepare(coordinator)
    assert first.assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
    assert first.challenge is not None
    assert first.challenge.selection_command_id == SELECTION_ID
    assert first.status.flight_control_enabled is False

    command = ApproachConfirmationCommand(
        command_token=303,
        session_token=404,
        challenge_token=first.challenge.challenge_token,
        target_token=first.challenge.target_token,
        target_revision=first.challenge.target_revision,
        selection_command_id=SELECTION_ID,
        sequence=111,
        issued_at_s=1000.8,
        expires_at_s=1002.8,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    assert coordinator.consume_confirmation(command, now_s=10.9)
    second = _prepare(
        coordinator,
        track=_track(last_seen_at_s=10.92),
        ranging=replace(_range(), evaluated_at_s=10.92, data_freshness_s=0.01),
        avoidance=replace(_avoidance(), captured_at_s=10.92, produced_at_s=10.92),
        telemetry=_telemetry(attitude_observed_at_s=10.92, position_observed_at_s=10.92),
        now_s=10.95,
        wire_now_s=1000.95,
    )
    assert second.challenge is None
    assert second.assessment.phase is ApproachHilPhase.CENTERING_SIM
    assert second.assessment.flight_control_enabled is False
    assert second.assessment.physical_release_enabled is False


def test_live_coordinator_issues_challenge_on_immediate_locked_pool_state() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )

    frame = _prepare(coordinator, track=_track(state=UnifiedTrackState.LOCKED))

    assert frame.challenge is not None
    assert frame.challenge.selection_command_id == SELECTION_ID
    assert frame.assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED


def test_live_coordinator_executes_detector_backed_non_manual_target() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )
    detector_target = _track(
        track_id="target-000123",
        label="person",
        state=UnifiedTrackState.LOCKED,
    )

    frame = _prepare(
        coordinator,
        track=detector_target,
        ranging=_range(target_id=detector_target.track_id),
    )

    assert frame.challenge is not None
    assert frame.challenge.selection_command_id == SELECTION_ID
    assert frame.assessment.target_id == detector_target.track_id
    assert coordinator.active_binding is not None
    assert coordinator.active_binding[1] == detector_target.track_id
    assert frame.assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED


def test_live_coordinator_ignores_trk_only_target_without_processing_error() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )

    frame = _prepare(
        coordinator,
        track=_track(locked=False, primary=False),
    )

    assert coordinator.active_binding is None
    assert frame.challenge is None
    assert frame.assessment.phase is ApproachHilPhase.SEARCH
    assert frame.assessment.reasons == ("no_target_selected",)


def test_live_coordinator_rejects_wrong_selection_and_aborts_on_occlusion() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )
    first = _prepare(coordinator)
    assert first.challenge is not None
    wrong = ApproachConfirmationCommand(
        command_token=305,
        session_token=404,
        challenge_token=first.challenge.challenge_token,
        target_token=first.challenge.target_token,
        target_revision=first.challenge.target_revision,
        selection_command_id="22222222-2222-4222-8222-222222222222",
        sequence=112,
        issued_at_s=1000.8,
        expires_at_s=1002.8,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    assert not coordinator.consume_confirmation(wrong, now_s=10.9)

    occluded = _prepare(
        coordinator,
        track=_track(state=UnifiedTrackState.OCCLUDED, last_seen_at_s=10.1),
        now_s=10.2,
        wire_now_s=1000.2,
    )
    assert occluded.assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert occluded.assessment.reasons == ("target_occluded",)


def test_live_coordinator_rearms_same_lck_after_camera_motion_recovery() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )
    first = _prepare(coordinator)
    assert first.challenge is not None

    interrupted = _prepare(
        coordinator,
        track=_track(state=UnifiedTrackState.OCCLUDED, last_seen_at_s=10.1),
        now_s=10.2,
        wire_now_s=1000.2,
    )
    assert interrupted.assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert interrupted.status.confirmation_expires_at_s is None
    assert interrupted.challenge is None

    resumed_at_s = 10.3
    recovered = _prepare(
        coordinator,
        track=_track(last_seen_at_s=resumed_at_s),
        ranging=replace(_range(), evaluated_at_s=resumed_at_s, data_freshness_s=0.01),
        avoidance=replace(_avoidance(), captured_at_s=resumed_at_s, produced_at_s=resumed_at_s),
        telemetry=_telemetry(
            attitude_observed_at_s=resumed_at_s,
            position_observed_at_s=resumed_at_s,
        ),
        now_s=resumed_at_s + 0.02,
        wire_now_s=1000.32,
    )

    assert recovered.challenge is not None
    assert recovered.challenge.challenge_token != first.challenge.challenge_token
    assert recovered.assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED


def test_live_coordinator_cancel_clears_target_and_publishes_search() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(), calibration=_calibration()
    )
    _prepare(coordinator)
    cleared = _prepare(
        coordinator,
        selection_command_id=None,
        track=None,
        now_s=10.2,
        wire_now_s=1000.2,
    )
    assert coordinator.active_binding is None
    assert cleared.challenge is None
    assert cleared.status.phase is ApproachHilPhase.SEARCH


def test_live_coordinator_latches_pilot_input_cancellation_until_reselection() -> None:
    coordinator = LiveApproachHilCoordinator(
        controller=ApproachHilController(),
        calibration=_calibration(),
        flight_control_enabled=True,
    )
    first = _prepare(coordinator)
    assert first.challenge is not None
    command = ApproachConfirmationCommand(
        command_token=309,
        session_token=404,
        challenge_token=first.challenge.challenge_token,
        target_token=first.challenge.target_token,
        target_revision=first.challenge.target_revision,
        selection_command_id=SELECTION_ID,
        sequence=113,
        issued_at_s=1000.8,
        expires_at_s=1002.8,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    assert coordinator.consume_confirmation(command, now_s=10.9)

    coordinator.cancel_execution(now_s=10.91, pilot_input_cancelled=True)
    cancelled = _prepare(
        coordinator,
        track=_track(last_seen_at_s=10.92),
        now_s=10.95,
        wire_now_s=1000.95,
    )

    assert cancelled.assessment.phase is ApproachHilPhase.ABORT
    assert cancelled.challenge is None
    assert cancelled.status.flight_control_enabled is True
    assert cancelled.status.aim_control_active is False
    assert cancelled.status.pilot_input_cancelled is True

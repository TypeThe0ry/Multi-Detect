from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.approach_hil import (
    ApproachHilConfig,
    ApproachHilController,
    ApproachHilInput,
    ApproachHilPhase,
    ApproachTargetEvidence,
)
from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.monocular_avoidance import (
    CollisionRiskState,
    MonocularAvoidanceAssessment,
)
from multidetect.multimodal_ranging import CameraCalibration, RangeSolution, RangeValidity
from multidetect.unified_tracking import UnifiedTrackState


def _target(**changes) -> ApproachTargetEvidence:
    values = dict(
        target_id="manual-vehicle-1",
        target_revision=7,
        frame_id="frame-10",
        observed_at_s=10.0,
        label="car",
        bbox=BoundingBox(0.46, 0.44, 0.54, 0.56),
        state=UnifiedTrackState.TRACKING,
        locked=True,
        primary=True,
    )
    values.update(changes)
    return ApproachTargetEvidence(**values)


def _calibration() -> CameraCalibration:
    return CameraCalibration("camera-main-v1", 1280, 720, 900.0, 900.0, 640.0, 360.0)


def _ranging(**changes) -> RangeSolution:
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


def _avoidance(state: CollisionRiskState = CollisionRiskState.CLEAR):
    return MonocularAvoidanceAssessment(
        frame_id="frame-10",
        state=state,
        zones=(),
        captured_at_s=10.0,
        produced_at_s=10.02,
        data_age_s=0.02,
        frame_interval_s=0.05,
        valid_feature_count=80,
        rotation_compensated=True,
        processing_time_ms=4.0,
    )


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


def _input(**changes) -> ApproachHilInput:
    values = dict(
        target=_target(),
        calibration=_calibration(),
        ranging=_ranging(),
        avoidance=_avoidance(),
        telemetry=_telemetry(),
        evaluated_at_s=10.05,
    )
    values.update(changes)
    return ApproachHilInput(**values)


def _confirmed_controller() -> ApproachHilController:
    controller = ApproachHilController()
    controller.select_target(_target(), now_s=9.0)
    challenge = controller.issue_slide_challenge(now_s=9.1)
    assert controller.accept_slide_confirmation(
        token=challenge.token,
        target_id=challenge.target_id,
        target_revision=challenge.target_revision,
        slide_started_at_s=9.2,
        slide_completed_at_s=9.9,
        completion_fraction=1.0,
        continuous=True,
    )
    return controller


def test_slide_confirmation_is_continuous_bound_and_one_time() -> None:
    controller = ApproachHilController()
    controller.select_target(_target(), now_s=9.0)
    challenge = controller.issue_slide_challenge(now_s=9.1)

    assert not controller.accept_slide_confirmation(
        token=challenge.token,
        target_id=challenge.target_id,
        target_revision=challenge.target_revision,
        slide_started_at_s=9.2,
        slide_completed_at_s=9.9,
        completion_fraction=1.0,
        continuous=False,
    )
    assert not controller.accept_slide_confirmation(
        token=challenge.token,
        target_id=challenge.target_id,
        target_revision=challenge.target_revision,
        slide_started_at_s=9.2,
        slide_completed_at_s=9.9,
        completion_fraction=1.0,
        continuous=True,
    )


def test_centered_target_advances_only_after_required_frames() -> None:
    controller = _confirmed_controller()
    phases = [controller.evaluate(_input()).phase for _ in range(3)]
    assert phases == [
        ApproachHilPhase.CENTERING_SIM,
        ApproachHilPhase.CENTERING_SIM,
        ApproachHilPhase.APPROACH_SIM,
    ]


def test_unconfirmed_target_produces_no_centering_advice() -> None:
    controller = ApproachHilController()
    controller.select_target(_target(), now_s=9.0)
    assessment = controller.evaluate(_input())
    assert assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
    assert assessment.yaw_advice_deg is None
    assert assessment.flight_control_enabled is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"avoidance": _avoidance(CollisionRiskState.AVOID)}, "avoidance_avoid"),
        ({"avoidance": _avoidance(CollisionRiskState.INVALID)}, "avoidance_invalid"),
        ({"ranging": None}, "range_unavailable"),
        (
            {
                "ranging": _ranging(
                    validity=RangeValidity.INVALID,
                    slant_range_m=None,
                    ground_range_m=None,
                    slant_range_ci95_m=None,
                    ground_range_ci95_m=None,
                )
            },
            "range_invalid",
        ),
        ({"telemetry": _telemetry(link_healthy=False)}, "navigation_or_link_unhealthy"),
    ],
)
def test_unsafe_evidence_aborts_and_latches(changes, reason: str) -> None:
    controller = _confirmed_controller()
    aborted = controller.evaluate(_input(**changes))
    latched = controller.evaluate(_input())
    assert aborted.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert aborted.reasons == (reason,)
    assert aborted.climb_pitch_advice_deg == pytest.approx(8.0)
    assert latched.reasons == ("abort_latched_until_reselection",)


@pytest.mark.parametrize(
    "state",
    [
        UnifiedTrackState.OCCLUDED,
        UnifiedTrackState.REACQUIRING,
        UnifiedTrackState.LOST,
    ],
)
def test_occlusion_recovery_or_loss_invalidates_approach(state: UnifiedTrackState) -> None:
    controller = _confirmed_controller()
    assessment = controller.evaluate(_input(target=_target(state=state)))
    assert assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert assessment.reasons == (f"target_{state.value}",)


def test_recovered_visual_track_waits_for_stable_tracking_without_latching_abort() -> None:
    controller = _confirmed_controller()

    assessment = controller.evaluate(_input(target=_target(state=UnifiedTrackState.RECOVERED)))

    assert assessment.phase is ApproachHilPhase.SLIDE_CONFIRM_REQUIRED
    assert assessment.reasons == ("target_not_stably_tracking",)
    assert controller.can_rearm_after_tracking_recovery is False


def test_target_revision_change_aborts_instead_of_following_last_position() -> None:
    controller = _confirmed_controller()
    assessment = controller.evaluate(_input(target=_target(target_revision=8)))
    assert assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert assessment.reasons == ("target_binding_changed",)


def test_large_optical_error_aborts_outside_corridor() -> None:
    controller = _confirmed_controller()
    assessment = controller.evaluate(
        _input(target=_target(bbox=BoundingBox(0.86, 0.45, 0.96, 0.55)))
    )
    assert assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert assessment.reasons == ("target_outside_approach_corridor",)


def test_centering_advice_is_bounded_and_never_enables_control() -> None:
    controller = _confirmed_controller()
    assessment = controller.evaluate(
        _input(target=_target(bbox=BoundingBox(0.56, 0.50, 0.64, 0.62)))
    )
    assert assessment.phase is ApproachHilPhase.CENTERING_SIM
    assert abs(assessment.yaw_advice_deg) <= 5.0
    assert abs(assessment.pitch_advice_deg) <= 4.0
    assert abs(assessment.bank_advice_deg) <= 12.0
    assert assessment.advisory_only is True
    assert assessment.flight_control_enabled is False
    assert assessment.physical_release_enabled is False


def test_completion_requires_centered_valid_close_range() -> None:
    controller = _confirmed_controller()
    assessment = controller.evaluate(_input(ranging=_ranging(ground_range_m=10.0)))
    assert assessment.phase is ApproachHilPhase.COMPLETE


def test_confirmation_expiry_forces_abort() -> None:
    controller = _confirmed_controller()
    assessment = controller.evaluate(
        _input(
            target=_target(observed_at_s=14.2),
            ranging=replace(_ranging(), evaluated_at_s=14.2, data_freshness_s=0.01),
            avoidance=replace(_avoidance(), captured_at_s=14.2, produced_at_s=14.2),
            telemetry=_telemetry(attitude_observed_at_s=14.2, position_observed_at_s=14.2),
            evaluated_at_s=14.2,
        )
    )
    assert assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert assessment.reasons == ("slide_confirmation_expired",)


def test_confirmation_expires_at_the_exact_deadline() -> None:
    controller = _confirmed_controller()
    challenge = controller.challenge
    assert challenge is not None
    deadline = challenge.expires_at_s
    assessment = controller.evaluate(
        _input(
            target=_target(observed_at_s=deadline),
            ranging=replace(_ranging(), evaluated_at_s=deadline, data_freshness_s=0.0),
            avoidance=replace(_avoidance(), captured_at_s=deadline, produced_at_s=deadline),
            telemetry=_telemetry(
                attitude_observed_at_s=deadline,
                position_observed_at_s=deadline,
            ),
            evaluated_at_s=deadline,
        )
    )

    assert assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert assessment.reasons == ("slide_confirmation_expired",)


def test_abort_discards_expired_challenge_before_publishing_status() -> None:
    controller = _confirmed_controller()
    challenge = controller.challenge
    assert challenge is not None
    now_s = challenge.expires_at_s + 0.1

    assessment = controller.evaluate(
        _input(
            target=_target(state=UnifiedTrackState.OCCLUDED, observed_at_s=now_s),
            evaluated_at_s=now_s,
        )
    )

    assert assessment.phase is ApproachHilPhase.ABORT_CLIMB_SIM
    assert assessment.reasons == ("target_occluded",)
    assert assessment.confirmation_expires_at_s is None
    assert controller.challenge is None
    assert controller.can_rearm_after_tracking_recovery is True


def test_config_rejects_centering_tolerance_outside_corridor() -> None:
    with pytest.raises(ValueError, match="inside the approach corridor"):
        ApproachHilConfig(centering_tolerance_deg=20.0, maximum_corridor_angle_deg=18.0)

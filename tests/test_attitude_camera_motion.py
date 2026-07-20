from __future__ import annotations

import math

import pytest

from multidetect.attitude_camera_motion import (
    AttitudeCameraMotionConfig,
    AttitudeCameraMotionEstimator,
)
from multidetect.domain import VehicleTelemetry
from multidetect.multimodal_ranging import CameraCalibration


def _calibration(**changes: object) -> CameraCalibration:
    values = {
        "calibration_id": "main-camera-v1",
        "width_px": 640,
        "height_px": 360,
        "fx_px": 420.0,
        "fy_px": 420.0,
        "cx_px": 320.0,
        "cy_px": 180.0,
    }
    values.update(changes)
    return CameraCalibration(**values)  # type: ignore[arg-type]


def _telemetry(
    *,
    observed_at_s: float,
    roll_deg: float = 0.0,
    pitch_deg: float = 0.0,
    heading_deg: float = 0.0,
) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=10.0,
        roll_deg=roll_deg,
        pitch_deg=pitch_deg,
        ground_speed_mps=0.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=None,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        heading_deg=heading_deg,
        attitude_observed_at_s=observed_at_s,
    )


def test_attitude_homography_projects_a_yaw_change_into_the_expected_image_pan() -> None:
    estimator = AttitudeCameraMotionEstimator(_calibration())

    assert estimator.update(_telemetry(observed_at_s=10.0), captured_at_s=10.0) is None
    motion = estimator.update(
        _telemetry(observed_at_s=10.05, heading_deg=5.0), captured_at_s=10.05
    )

    assert motion is not None
    # The airframe turns right, so an inertial scene feature moves left in the
    # forward-facing camera.  The calibrated focal length fixes the magnitude.
    assert motion.transform_point(0.5, 0.5)[0] < 0.5
    assert motion.dx == pytest.approx(-math.tan(math.radians(5.0)) * 420.0 / 640.0, abs=0.004)
    assert motion.confidence >= 0.55
    assert motion.homography is not None


def test_attitude_homography_carries_roll_and_pitch_as_a_full_projective_transform() -> None:
    estimator = AttitudeCameraMotionEstimator(
        _calibration(mount_pitch_down_deg=25.0, mount_roll_clockwise_deg=3.0)
    )
    estimator.update(
        _telemetry(observed_at_s=20.0, roll_deg=2.0, pitch_deg=-4.0, heading_deg=10.0),
        captured_at_s=20.0,
    )

    motion = estimator.update(
        _telemetry(observed_at_s=20.04, roll_deg=8.0, pitch_deg=1.0, heading_deg=11.0),
        captured_at_s=20.04,
    )

    assert motion is not None
    assert motion.homography is not None
    assert abs(motion.rotation_deg) > 1.0
    x, y = motion.transform_point(0.15, 0.80)
    assert math.isfinite(x) and math.isfinite(y)


@pytest.mark.parametrize(
    ("telemetry_time", "captured_at_s"),
    ((10.0, 10.30), (10.0, 9.70)),
)
def test_attitude_motion_rejects_image_and_attitude_skew(
    telemetry_time: float,
    captured_at_s: float,
) -> None:
    estimator = AttitudeCameraMotionEstimator(_calibration())

    assert (
        estimator.update(
            _telemetry(observed_at_s=telemetry_time), captured_at_s=captured_at_s
        )
        is None
    )


def test_attitude_motion_rejects_a_large_or_nonmonotonic_pose_step() -> None:
    estimator = AttitudeCameraMotionEstimator(
        _calibration(),
        AttitudeCameraMotionConfig(maximum_rotation_deg=10.0),
    )
    assert estimator.update(_telemetry(observed_at_s=30.0), captured_at_s=30.0) is None

    assert (
        estimator.update(
            _telemetry(observed_at_s=30.05, heading_deg=30.0), captured_at_s=30.05
        )
        is None
    )
    assert (
        estimator.update(
            _telemetry(observed_at_s=30.04, heading_deg=1.0), captured_at_s=30.04
        )
        is None
    )

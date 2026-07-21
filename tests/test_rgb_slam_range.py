from __future__ import annotations

import math

import pytest

from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.multimodal_ranging import CameraCalibration, DirectRangeSource
from multidetect.rgb_slam_range import RgbSlamRangeEstimator
from multidetect.unified_tracking import (
    CameraMotionEstimate,
    UnifiedTrackSnapshot,
    UnifiedTrackState,
)

CALIBRATION = CameraCalibration(
    calibration_id="synthetic-1280",
    width_px=1280,
    height_px=720,
    fx_px=800.0,
    fy_px=800.0,
    cx_px=640.0,
    cy_px=360.0,
)


def _telemetry(*, t: float, east: float) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=float("nan"),
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=10.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        heading_deg=0.0,
        attitude_observed_at_s=t,
        velocity_north_mps=10.0,
        velocity_east_mps=0.0,
        velocity_observed_at_s=t,
        airspeed_mps=10.0,
        airspeed_observed_at_s=t,
        local_north_m=0.0,
        local_east_m=east,
        local_down_m=0.0,
        local_position_observed_at_s=t,
    )


def _track(*, t: float, center_x: float) -> UnifiedTrackSnapshot:
    box = BoundingBox(center_x - 0.04, 0.46, center_x + 0.04, 0.54)
    return UnifiedTrackSnapshot(
        track_id="target-1",
        state=UnifiedTrackState.TRACKING,
        label="person",
        bbox=box,
        predicted_bbox=box,
        first_seen_at_s=10.0,
        last_seen_at_s=t,
        state_changed_at_s=10.0,
        observation_count=10,
        missed_frame_count=0,
        confidence=0.9,
        tracking_quality=0.9,
        velocity_x_s=0.0,
        velocity_y_s=0.0,
        appearance_sample_count=0,
        last_appearance_distance=None,
        reid_confirmed=False,
        locked=False,
        primary=True,
        actionable=True,
    )


def test_rgb_slam_triangulates_calibrated_target_from_keyframes() -> None:
    estimator = RgbSlamRangeEstimator()
    measurement = None
    for index in range(8):
        timestamp = 10.0 + index * 0.1
        east = float(index)
        center_x = (CALIBRATION.cx_px + CALIBRATION.fx_px * (20.0 - east) / 50.0) / 1280.0
        measurement = estimator.observe(
            track=_track(t=timestamp, center_x=center_x),
            telemetry=_telemetry(t=timestamp, east=east),
            calibration=CALIBRATION,
            frame_id=f"frame-{index}",
            captured_at_s=timestamp,
            camera_motion=CameraMotionEstimate(dx=0.005, dy=0.0, confidence=0.95),
        )

    assert measurement is not None
    assert measurement.source is DirectRangeSource.RGB_SLAM
    assert measurement.slant_range_m == pytest.approx(math.hypot(50.0, 13.0), rel=0.03)
    assert measurement.absolute_scale_valid is True


def test_rgb_slam_requires_background_motion_confidence() -> None:
    estimator = RgbSlamRangeEstimator()
    measurement = estimator.observe(
        track=_track(t=10.0, center_x=0.6),
        telemetry=_telemetry(t=10.0, east=0.0),
        calibration=CALIBRATION,
        frame_id="frame-1",
        captured_at_s=10.0,
        camera_motion=CameraMotionEstimate(dx=0.01, dy=0.0, confidence=0.2),
    )

    assert measurement is None

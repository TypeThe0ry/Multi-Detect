from __future__ import annotations

import math

import pytest

from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.multimodal_ranging import CameraCalibration
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState
from multidetect.visual_inertial_range import VisualInertialRangeEstimator

CALIBRATION = CameraCalibration(
    calibration_id="synthetic-1280",
    width_px=1280,
    height_px=720,
    fx_px=800.0,
    fy_px=800.0,
    cx_px=640.0,
    cy_px=360.0,
)


def _telemetry(*, t: float, north: float, east: float, heading: float = 0.0) -> VehicleTelemetry:
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
        heading_deg=heading,
        attitude_observed_at_s=t,
        velocity_north_mps=10.0,
        velocity_east_mps=0.0,
        velocity_observed_at_s=t,
        airspeed_mps=10.0,
        airspeed_observed_at_s=t,
        local_north_m=north,
        local_east_m=east,
        local_down_m=0.0,
        local_position_observed_at_s=t,
    )


def _track(
    *, t: float, center_x: float, center_y: float, size: float, label: str = "flame"
) -> UnifiedTrackSnapshot:
    half = size / 2.0
    bbox = BoundingBox(center_x - half, center_y - half, center_x + half, center_y + half)
    return UnifiedTrackSnapshot(
        track_id="target-1",
        state=UnifiedTrackState.TRACKING,
        label=label,
        bbox=bbox,
        predicted_bbox=bbox,
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


def _image_center_for_world_target(
    *,
    camera_north: float,
    camera_east: float,
    target_north: float,
    target_east: float,
) -> tuple[float, float]:
    north = target_north - camera_north
    east = target_east - camera_east
    return (
        (CALIBRATION.cx_px + CALIBRATION.fx_px * east / north) / CALIBRATION.width_px,
        CALIBRATION.cy_px / CALIBRATION.height_px,
    )


def test_temporal_motion_triangulation_recovers_metric_range() -> None:
    estimator = VisualInertialRangeEstimator()
    measurement = None
    for index in range(8):
        t = 10.0 + index * 0.10
        east = float(index)
        center_x, center_y = _image_center_for_world_target(
            camera_north=0.0,
            camera_east=east,
            target_north=50.0,
            target_east=20.0,
        )
        measurement = estimator.observe(
            track=_track(t=t, center_x=center_x, center_y=center_y, size=0.08),
            telemetry=_telemetry(t=t, north=0.0, east=east),
            calibration=CALIBRATION,
            frame_id=f"frame-{index}",
            captured_at_s=t,
        )

    assert measurement is not None
    expected = math.hypot(50.0, 20.0 - 7.0)
    assert measurement.slant_range_m == pytest.approx(expected, rel=0.03)
    assert measurement.absolute_scale_valid is True


def test_looming_recovers_range_during_forward_approach() -> None:
    estimator = VisualInertialRangeEstimator()
    measurement = None
    for index in range(8):
        t = 10.0 + index * 0.10
        north = float(index)
        distance = 50.0 - north
        measurement = estimator.observe(
            track=_track(t=t, center_x=0.5, center_y=0.5, size=5.0 / distance),
            telemetry=_telemetry(t=t, north=north, east=0.0),
            calibration=CALIBRATION,
            frame_id=f"frame-{index}",
            captured_at_s=t,
        )

    assert measurement is not None
    assert measurement.slant_range_m == pytest.approx(43.0, rel=0.12)


def test_stationary_camera_does_not_create_metric_range() -> None:
    estimator = VisualInertialRangeEstimator()
    measurement = None
    for index in range(8):
        t = 10.0 + index * 0.10
        measurement = estimator.observe(
            track=_track(t=t, center_x=0.55, center_y=0.5, size=0.08, label="flame"),
            telemetry=_telemetry(t=t, north=0.0, east=0.0),
            calibration=CALIBRATION,
            frame_id=f"frame-{index}",
            captured_at_s=t,
        )

    assert measurement is None


def test_stationary_person_uses_wide_uncertainty_size_prior() -> None:
    estimator = VisualInertialRangeEstimator()
    measurement = estimator.observe(
        track=_track(t=10.0, center_x=0.5, center_y=0.5, size=0.20, label="person"),
        telemetry=_telemetry(t=10.0, north=0.0, east=0.0),
        calibration=CALIBRATION,
        frame_id="frame-person",
        captured_at_s=10.0,
    )

    assert measurement is not None
    assert measurement.source.value == "monocular_size"
    assert measurement.slant_range_m == pytest.approx(1.7 * 800.0 / 144.0, rel=0.01)
    assert measurement.sigma_m >= measurement.slant_range_m * 0.40


def test_attitude_compensation_rejects_rotation_only_as_parallax() -> None:
    estimator = VisualInertialRangeEstimator()
    measurement = None
    target_bearing_deg = 20.0
    for index in range(8):
        t = 10.0 + index * 0.10
        heading = float(index * 2)
        relative = math.radians(target_bearing_deg - heading)
        center_x = (
            CALIBRATION.cx_px + CALIBRATION.fx_px * math.tan(relative)
        ) / CALIBRATION.width_px
        measurement = estimator.observe(
            track=_track(t=t, center_x=center_x, center_y=0.5, size=0.08),
            telemetry=_telemetry(t=t, north=0.0, east=0.0, heading=heading),
            calibration=CALIBRATION,
            frame_id=f"frame-{index}",
            captured_at_s=t,
        )

    assert measurement is None

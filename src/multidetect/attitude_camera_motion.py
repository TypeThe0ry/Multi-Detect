from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import VehicleTelemetry
from .multimodal_ranging import CameraCalibration
from .unified_tracking import CameraMotionEstimate


@dataclass(frozen=True, slots=True)
class AttitudeCameraMotionConfig:
    """Freshness and geometric limits for IMU-derived image motion."""

    maximum_attitude_image_skew_s: float = 0.20
    maximum_attitude_interval_s: float = 0.35
    maximum_rotation_deg: float = 35.0
    minimum_confidence: float = 0.55

    def __post_init__(self) -> None:
        for name, value in (
            ("attitude/image skew", self.maximum_attitude_image_skew_s),
            ("attitude interval", self.maximum_attitude_interval_s),
            ("attitude rotation", self.maximum_rotation_deg),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if self.maximum_rotation_deg > 90.0:
            raise ValueError("attitude rotation limit cannot exceed 90 degrees")
        if not math.isfinite(self.minimum_confidence) or not 0.5 <= self.minimum_confidence <= 1.0:
            raise ValueError("attitude camera-motion confidence must be in [0.5, 1]")


@dataclass(frozen=True, slots=True)
class _AttitudeSample:
    observed_at_s: float
    roll_deg: float
    pitch_deg: float
    heading_deg: float


class AttitudeCameraMotionEstimator:
    """Project rigid Pixhawk attitude changes into camera-image homographies.

    The estimator supplies a bounded fallback when target-excluded visual flow
    is sparse, blurred or momentarily dominated by moving subjects.  It is
    intentionally only a prediction source: the short-term tracker continues
    to prefer a fresh target-excluded background homography whenever one is
    available.  Translation/depth remain unobservable from attitude alone and
    are not fabricated here.
    """

    def __init__(
        self,
        calibration: CameraCalibration,
        config: AttitudeCameraMotionConfig | None = None,
    ) -> None:
        self.calibration = calibration
        self.config = config or AttitudeCameraMotionConfig()
        self._previous: _AttitudeSample | None = None

    def update(
        self,
        telemetry: VehicleTelemetry,
        *,
        captured_at_s: float,
    ) -> CameraMotionEstimate | None:
        """Return previous-to-current image motion when two fresh poses exist."""

        if not math.isfinite(captured_at_s) or captured_at_s < 0.0:
            raise ValueError("captured image timestamp must be finite and non-negative")
        values = (
            telemetry.roll_deg,
            telemetry.pitch_deg,
            telemetry.heading_deg,
            telemetry.attitude_observed_at_s,
        )
        if not all(math.isfinite(value) for value in values):
            return None
        if abs(captured_at_s - telemetry.attitude_observed_at_s) > (
            self.config.maximum_attitude_image_skew_s
        ):
            return None
        current = _AttitudeSample(
            observed_at_s=telemetry.attitude_observed_at_s,
            roll_deg=telemetry.roll_deg,
            pitch_deg=telemetry.pitch_deg,
            heading_deg=telemetry.heading_deg % 360.0,
        )
        previous = self._previous
        self._previous = current
        if previous is None:
            return None
        interval_s = current.observed_at_s - previous.observed_at_s
        if interval_s <= 0.0 or interval_s > self.config.maximum_attitude_interval_s:
            return None
        previous_rotation = _camera_to_ned_rotation(self.calibration, previous)
        current_rotation = _camera_to_ned_rotation(self.calibration, current)
        relative_rotation = _matmul(_transpose(current_rotation), previous_rotation)
        rotation_angle_deg = _rotation_angle_deg(relative_rotation)
        if rotation_angle_deg > self.config.maximum_rotation_deg:
            return None
        homography = _image_homography(self.calibration, relative_rotation)
        try:
            estimate = _camera_motion_from_homography(
                homography,
                confidence=self._confidence(captured_at_s, current.observed_at_s),
            )
        except ValueError:
            return None
        return estimate

    def _confidence(self, captured_at_s: float, observed_at_s: float) -> float:
        freshness = max(
            0.0,
            1.0 - abs(captured_at_s - observed_at_s) / self.config.maximum_attitude_image_skew_s,
        )
        return self.config.minimum_confidence + (
            1.0 - self.config.minimum_confidence
        ) * freshness


def _camera_to_ned_rotation(
    calibration: CameraCalibration,
    attitude: _AttitudeSample,
) -> tuple[tuple[float, float, float], ...]:
    # OpenCV camera coordinates are (right, down, forward).  The ranging path
    # uses body (forward, right, down) and the same calibrated mount convention.
    camera_to_body_nominal = ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    mount = _rotation_matrix(
        calibration.mount_roll_clockwise_deg,
        -calibration.mount_pitch_down_deg,
        calibration.mount_yaw_right_deg,
    )
    body_to_ned = _rotation_matrix(
        attitude.roll_deg,
        attitude.pitch_deg,
        attitude.heading_deg,
    )
    return _matmul(_matmul(body_to_ned, mount), camera_to_body_nominal)


def _image_homography(
    calibration: CameraCalibration,
    relative_rotation: tuple[tuple[float, float, float], ...],
) -> tuple[float, float, float, float, float, float, float, float, float]:
    camera_matrix = (
        (calibration.fx_px, 0.0, calibration.cx_px),
        (0.0, calibration.fy_px, calibration.cy_px),
        (0.0, 0.0, 1.0),
    )
    camera_inverse = (
        (1.0 / calibration.fx_px, 0.0, -calibration.cx_px / calibration.fx_px),
        (0.0, 1.0 / calibration.fy_px, -calibration.cy_px / calibration.fy_px),
        (0.0, 0.0, 1.0),
    )
    pixel_motion = _matmul(_matmul(camera_matrix, relative_rotation), camera_inverse)
    normalized_to_pixel = (
        (float(calibration.width_px), 0.0, 0.0),
        (0.0, float(calibration.height_px), 0.0),
        (0.0, 0.0, 1.0),
    )
    pixel_to_normalized = (
        (1.0 / calibration.width_px, 0.0, 0.0),
        (0.0, 1.0 / calibration.height_px, 0.0),
        (0.0, 0.0, 1.0),
    )
    normalized = _matmul(_matmul(pixel_to_normalized, pixel_motion), normalized_to_pixel)
    scale = normalized[2][2]
    if not math.isfinite(scale) or abs(scale) < 1e-9:
        raise ValueError("attitude image homography is singular")
    return tuple(value / scale for row in normalized for value in row)


def _camera_motion_from_homography(
    homography: tuple[float, float, float, float, float, float, float, float, float],
    *,
    confidence: float,
) -> CameraMotionEstimate:
    center_x, center_y = _transform(homography, 0.5, 0.5)
    xx, xy, yx, yy = _jacobian(homography, 0.5, 0.5)
    determinant = xx * yy - xy * yx
    if not math.isfinite(determinant) or not 0.25 <= determinant <= 4.0:
        raise ValueError("attitude image homography is outside the bounded tracking domain")
    scale = math.sqrt(determinant)
    rotation_deg = math.degrees(math.atan2(yx - xy, xx + yy))
    return CameraMotionEstimate(
        dx=center_x - 0.5,
        dy=center_y - 0.5,
        scale=scale,
        confidence=confidence,
        rotation_deg=rotation_deg,
        aspect_ratio=1.0,
        homography=homography,
    )


def _rotation_matrix(
    roll_deg: float,
    pitch_deg: float,
    yaw_deg: float,
) -> tuple[tuple[float, float, float], ...]:
    roll, pitch, yaw = (math.radians(value) for value in (roll_deg, pitch_deg, yaw_deg))
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy),
        (cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy),
        (-sp, sr * cp, cr * cp),
    )


def _matmul(
    left: tuple[tuple[float, float, float], ...],
    right: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )


def _transpose(
    matrix: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(matrix[column][row] for column in range(3)) for row in range(3))


def _rotation_angle_deg(matrix: tuple[tuple[float, float, float], ...]) -> float:
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) * 0.5))))


def _transform(
    homography: tuple[float, float, float, float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    denominator = homography[6] * x + homography[7] * y + homography[8]
    if not math.isfinite(denominator) or abs(denominator) < 1e-9:
        raise ValueError("attitude image homography has a projective pole")
    return (
        (homography[0] * x + homography[1] * y + homography[2]) / denominator,
        (homography[3] * x + homography[4] * y + homography[5]) / denominator,
    )


def _jacobian(
    homography: tuple[float, float, float, float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float, float, float]:
    denominator = homography[6] * x + homography[7] * y + homography[8]
    numerator_x = homography[0] * x + homography[1] * y + homography[2]
    numerator_y = homography[3] * x + homography[4] * y + homography[5]
    denominator_squared = denominator * denominator
    if not math.isfinite(denominator_squared) or denominator_squared < 1e-16:
        raise ValueError("attitude image homography has an invalid Jacobian")
    return (
        (homography[0] * denominator - numerator_x * homography[6]) / denominator_squared,
        (homography[1] * denominator - numerator_x * homography[7]) / denominator_squared,
        (homography[3] * denominator - numerator_y * homography[6]) / denominator_squared,
        (homography[4] * denominator - numerator_y * homography[7]) / denominator_squared,
    )


__all__ = [
    "AttitudeCameraMotionConfig",
    "AttitudeCameraMotionEstimator",
]

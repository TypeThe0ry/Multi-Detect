"""Lightweight calibrated RGB-SLAM target ranging for the Jetson pipeline.

The tracker already derives target-excluded background motion each camera frame.
This estimator turns stable keyframes plus the Pixhawk-scaled platform baseline
into a separate RGB-SLAM range measurement.  It is deliberately independent
from the VI range path at the measurement layer so the fusion filter can expose
their separate values and weights to QGC.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .domain import VehicleTelemetry
from .multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    DirectRangeMeasurement,
    DirectRangeSource,
    MultiModalRangingEngine,
    TargetImageObservation,
)
from .unified_tracking import CameraMotionEstimate, UnifiedTrackSnapshot, UnifiedTrackState


@dataclass(frozen=True, slots=True)
class RgbSlamRangeConfig:
    window_seconds: float = 5.0
    minimum_samples: int = 4
    minimum_baseline_m: float = 0.15
    minimum_ray_separation_deg: float = 0.12
    minimum_camera_motion_confidence: float = 0.35
    maximum_position_age_s: float = 0.60
    minimum_range_m: float = 0.4
    maximum_range_m: float = 800.0

    def __post_init__(self) -> None:
        if self.minimum_samples < 3:
            raise ValueError("RGB-SLAM requires at least three keyframes")
        if not (
            0.0 < self.minimum_baseline_m
            and 0.0 < self.minimum_ray_separation_deg < 20.0
            and 0.0 < self.minimum_camera_motion_confidence <= 1.0
            and 0.0 < self.minimum_range_m < self.maximum_range_m
            and self.window_seconds > 0.0
            and self.maximum_position_age_s > 0.0
        ):
            raise ValueError("RGB-SLAM ranging configuration is invalid")


@dataclass(frozen=True, slots=True)
class _Keyframe:
    captured_at_s: float
    position_ned_m: tuple[float, float, float]
    ray_ned: tuple[float, float, float]
    visual_confidence: float


class RgbSlamRangeEstimator:
    """Keyframe multi-view ray triangulation with image-motion quality gating."""

    def __init__(self, config: RgbSlamRangeConfig | None = None) -> None:
        self.config = config or RgbSlamRangeConfig()
        self._history: dict[str, deque[_Keyframe]] = {}
        self._last_position: tuple[float, float, float] | None = None
        self._last_time_s: float | None = None

    def observe(
        self,
        *,
        track: UnifiedTrackSnapshot,
        telemetry: VehicleTelemetry,
        calibration: CameraCalibration,
        frame_id: str,
        captured_at_s: float,
        camera_motion: CameraMotionEstimate | None,
    ) -> DirectRangeMeasurement | None:
        if (
            camera_motion is None
            or camera_motion.confidence < self.config.minimum_camera_motion_confidence
        ):
            return None
        if track.state not in {
            UnifiedTrackState.TRACKING,
            UnifiedTrackState.RECOVERED,
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
        }:
            return None
        if track.state in {UnifiedTrackState.OCCLUDED, UnifiedTrackState.REACQUIRING} and (
            track.missed_frame_count > 8 or track.tracking_quality < 0.18
        ):
            return None
        pose = self._pose(telemetry)
        if pose is None or abs(captured_at_s - pose.captured_at_s) > 0.35:
            return None
        position = self._position(telemetry, captured_at_s=captured_at_s)
        if position is None:
            return None
        center_x, center_y = track.bbox.center
        target = TargetImageObservation(
            target_id=track.track_id,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            center_x=center_x,
            center_y=center_y,
        )
        ray = MultiModalRangingEngine.target_ray_ned(
            calibration=calibration,
            pose=pose,
            target=target,
        )
        history = self._history.setdefault(track.track_id, deque())
        if history and captured_at_s <= history[-1].captured_at_s:
            return self._estimate(track_id=track.track_id, history=tuple(history))
        # Keyframe admission uses visual transform quality and a bounded amount
        # of rotation/scale evidence.  This suppresses repeated near-identical
        # frames while preserving fixed-wing high-rate baselines.
        visual_change = (
            abs(camera_motion.dx)
            + abs(camera_motion.dy)
            + abs(math.log(max(camera_motion.scale, 1e-4)))
        )
        if not history or visual_change >= 0.0015 or abs(camera_motion.rotation_deg) >= 0.10:
            history.append(
                _Keyframe(
                    captured_at_s=captured_at_s,
                    position_ned_m=position,
                    ray_ned=ray,
                    visual_confidence=camera_motion.confidence,
                )
            )
        cutoff = captured_at_s - self.config.window_seconds
        while history and history[0].captured_at_s < cutoff:
            history.popleft()
        self._expire(captured_at_s)
        return self._estimate(track_id=track.track_id, history=tuple(history))

    def _pose(self, telemetry: VehicleTelemetry) -> AircraftPose | None:
        values = (
            telemetry.roll_deg,
            telemetry.pitch_deg,
            telemetry.heading_deg,
            telemetry.attitude_observed_at_s,
        )
        if not all(math.isfinite(value) for value in values):
            return None
        return AircraftPose(
            captured_at_s=telemetry.attitude_observed_at_s,
            roll_deg=telemetry.roll_deg,
            pitch_deg=telemetry.pitch_deg,
            heading_deg=telemetry.heading_deg % 360.0,
        )

    def _position(
        self, telemetry: VehicleTelemetry, *, captured_at_s: float
    ) -> tuple[float, float, float] | None:
        age = abs(captured_at_s - telemetry.local_position_observed_at_s)
        values = (telemetry.local_north_m, telemetry.local_east_m, telemetry.local_down_m)
        if (
            all(math.isfinite(value) for value in values)
            and age <= self.config.maximum_position_age_s
        ):
            position = tuple(float(value) for value in values)
            self._last_position, self._last_time_s = position, captured_at_s
            return position
        if self._last_position is None or self._last_time_s is None:
            return None
        elapsed = captured_at_s - self._last_time_s
        if elapsed <= 0.0 or elapsed > 0.50:
            return None
        if math.isfinite(telemetry.velocity_north_mps) and math.isfinite(
            telemetry.velocity_east_mps
        ):
            north_v, east_v = telemetry.velocity_north_mps, telemetry.velocity_east_mps
        elif (
            telemetry.armed is True
            and math.isfinite(telemetry.airspeed_mps)
            and telemetry.airspeed_mps > 0.5
            and math.isfinite(telemetry.heading_deg)
        ):
            heading = math.radians(telemetry.heading_deg)
            north_v = telemetry.airspeed_mps * math.cos(heading)
            east_v = telemetry.airspeed_mps * math.sin(heading)
        else:
            return None
        north, east, down = self._last_position
        position = (north + north_v * elapsed, east + east_v * elapsed, down)
        self._last_position, self._last_time_s = position, captured_at_s
        return position

    def _estimate(
        self, *, track_id: str, history: tuple[_Keyframe, ...]
    ) -> DirectRangeMeasurement | None:
        if len(history) < self.config.minimum_samples:
            return None
        current = history[-1]
        baseline = max(
            _distance(current.position_ned_m, sample.position_ned_m) for sample in history[:-1]
        )
        separation = max(_ray_angle(current.ray_ned, sample.ray_ned) for sample in history[:-1])
        if (
            baseline < self.config.minimum_baseline_m
            or separation < self.config.minimum_ray_separation_deg
        ):
            return None
        point = _least_squares_intersection(history)
        if point is None:
            return None
        delta = _subtract(point, current.position_ned_m)
        slant_range = _dot(delta, current.ray_ned)
        residuals = tuple(_ray_residual(point, sample) for sample in history)
        rms = math.sqrt(sum(value * value for value in residuals) / len(residuals))
        if not self.config.minimum_range_m <= slant_range <= self.config.maximum_range_m:
            return None
        if rms > max(4.0, slant_range * 0.15):
            return None
        visual_confidence = sum(sample.visual_confidence for sample in history) / len(history)
        angular_sigma = math.radians(0.35)
        geometry_sigma = angular_sigma * slant_range * slant_range / max(baseline, 0.1)
        sigma = max(1.0, rms * 2.0, geometry_sigma) / max(0.35, visual_confidence)
        return DirectRangeMeasurement(
            source=DirectRangeSource.RGB_SLAM,
            target_id=track_id,
            slant_range_m=slant_range,
            sigma_m=min(slant_range * 0.90, sigma),
            captured_at_s=current.captured_at_s,
            absolute_scale_valid=True,
        )

    def _expire(self, now_s: float) -> None:
        for track_id in tuple(self._history):
            history = self._history[track_id]
            if not history or now_s - history[-1].captured_at_s > self.config.window_seconds:
                del self._history[track_id]


def _least_squares_intersection(
    samples: tuple[_Keyframe, ...],
) -> tuple[float, float, float] | None:
    matrix = [[0.0, 0.0, 0.0] for _ in range(3)]
    rhs = [0.0, 0.0, 0.0]
    for sample in samples:
        ray = sample.ray_ned
        projection = [[float(i == j) - ray[i] * ray[j] for j in range(3)] for i in range(3)]
        for row in range(3):
            rhs[row] += sum(projection[row][col] * sample.position_ned_m[col] for col in range(3))
            for col in range(3):
                matrix[row][col] += projection[row][col]
    return _solve_3x3(matrix, rhs)


def _solve_3x3(matrix: list[list[float]], rhs: list[float]) -> tuple[float, float, float] | None:
    augmented = [row[:] + [rhs[index]] for index, row in enumerate(matrix)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-8:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    point = tuple(augmented[row][3] for row in range(3))
    return point if all(math.isfinite(value) for value in point) else None


def _ray_residual(point: tuple[float, float, float], sample: _Keyframe) -> float:
    delta = _subtract(point, sample.position_ned_m)
    along = _dot(delta, sample.ray_ned)
    return math.sqrt(
        _dot(
            tuple(delta[i] - along * sample.ray_ned[i] for i in range(3)),
            tuple(delta[i] - along * sample.ray_ned[i] for i in range(3)),
        )
    )


def _ray_angle(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(first, second)))))


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(_subtract(first, second), _subtract(first, second)))


def _subtract(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(first[index] - second[index] for index in range(3))


def _dot(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return sum(first[index] * second[index] for index in range(3))


__all__ = ["RgbSlamRangeConfig", "RgbSlamRangeEstimator"]

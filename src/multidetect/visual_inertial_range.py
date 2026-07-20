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
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


@dataclass(frozen=True, slots=True)
class VisualInertialRangeConfig:
    window_seconds: float = 4.0
    minimum_samples: int = 5
    minimum_span_seconds: float = 0.35
    minimum_baseline_m: float = 0.20
    minimum_ray_separation_deg: float = 0.15
    maximum_range_m: float = 5_000.0
    maximum_position_age_s: float = 0.60
    minimum_motion_speed_mps: float = 2.0


@dataclass(frozen=True, slots=True)
class _Sample:
    captured_at_s: float
    position_ned_m: tuple[float, float, float]
    ray_ned: tuple[float, float, float]
    angular_scale: float
    position_source: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    range_m: float
    sigma_m: float
    method: str


class VisualInertialRangeEstimator:
    """Temporal metric ranging from platform motion and calibrated target rays.

    Local-NED positions supplied by the PX4 estimator are preferred.  During a
    short local-position gap, north/east velocity or airspeed+heading is
    integrated into a bounded dead-reckoning frame.  Multi-view ray
    triangulation is combined with bounding-box looming when both geometries are
    observable.  Every result remains a degraded advisory measurement until an
    independent vertical reference is available.
    """

    _DYNAMIC_LABELS = frozenset({"person", "car", "truck", "bus", "motorcycle", "bicycle"})
    # Approximate real-world object heights used only as a wide-uncertainty
    # stationary monocular fallback. Temporal geometry replaces these priors as
    # soon as platform motion provides observable scale.
    _OBJECT_HEIGHT_PRIORS_M = {
        "person": (1.70, 0.45),
        "pedestrian": (1.70, 0.45),
        "people": (1.70, 0.50),
        "firefighter": (1.75, 0.45),
        "car": (1.50, 0.55),
        "van": (2.05, 0.55),
        "truck": (3.00, 0.60),
        "bus": (3.20, 0.55),
        "motorcycle": (1.20, 0.60),
        "bicycle": (1.10, 0.65),
    }

    def __init__(self, config: VisualInertialRangeConfig | None = None) -> None:
        self.config = config or VisualInertialRangeConfig()
        self._samples: dict[str, deque[_Sample]] = {}
        self._dead_reckoned_ned_m = (0.0, 0.0, 0.0)
        self._last_platform_time_s: float | None = None
        self._last_platform_position: tuple[float, float, float] | None = None
        self._last_platform_source: str | None = None

    def observe(
        self,
        *,
        track: UnifiedTrackSnapshot,
        telemetry: VehicleTelemetry,
        calibration: CameraCalibration,
        frame_id: str,
        captured_at_s: float,
    ) -> DirectRangeMeasurement | None:
        predicted_track_usable = (
            track.state in {UnifiedTrackState.OCCLUDED, UnifiedTrackState.REACQUIRING}
            and track.missed_frame_count <= 3
            and track.tracking_quality >= 0.20
        )
        if not track.actionable and not predicted_track_usable:
            return None
        attitude_skew_s = abs(captured_at_s - telemetry.attitude_observed_at_s)
        if not math.isfinite(attitude_skew_s) or attitude_skew_s > 0.35:
            return None
        pose = self._pose(telemetry)
        if pose is None:
            return None
        target = TargetImageObservation(
            target_id=track.track_id,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            center_x=track.bbox.center[0],
            center_y=track.bbox.center[1],
        )
        ray_ned = MultiModalRangingEngine.target_ray_ned(
            calibration=calibration,
            pose=pose,
            target=target,
        )
        platform = self._platform_position(telemetry=telemetry, captured_at_s=captured_at_s)
        if platform is None:
            return None
        position_ned_m, position_source = platform
        angular_scale = math.sqrt(track.bbox.area)
        history = self._samples.setdefault(track.track_id, deque())
        if history and history[-1].position_source != position_source:
            history.clear()
        if history and captured_at_s <= history[-1].captured_at_s:
            if captured_at_s == history[-1].captured_at_s:
                temporal = self._estimate(track=track, samples=tuple(history))
                return temporal or self._object_size_measurement(
                    track=track,
                    calibration=calibration,
                    captured_at_s=captured_at_s,
                )
            history.clear()
        history.append(
            _Sample(
                captured_at_s=captured_at_s,
                position_ned_m=position_ned_m,
                ray_ned=ray_ned,
                angular_scale=angular_scale,
                position_source=position_source,
            )
        )
        cutoff = captured_at_s - self.config.window_seconds
        while history and history[0].captured_at_s < cutoff:
            history.popleft()
        self._expire_tracks(now_s=captured_at_s)
        temporal = self._estimate(track=track, samples=tuple(history))
        return temporal or self._object_size_measurement(
            track=track,
            calibration=calibration,
            captured_at_s=captured_at_s,
        )

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

    def _platform_position(
        self,
        *,
        telemetry: VehicleTelemetry,
        captured_at_s: float,
    ) -> tuple[tuple[float, float, float], str] | None:
        local_values = (telemetry.local_north_m, telemetry.local_east_m, telemetry.local_down_m)
        local_age_s = abs(captured_at_s - telemetry.local_position_observed_at_s)
        if (
            all(math.isfinite(value) for value in local_values)
            and math.isfinite(local_age_s)
            and local_age_s <= self.config.maximum_position_age_s
        ):
            position = tuple(float(value) for value in local_values)
            self._dead_reckoned_ned_m = position
            self._last_platform_time_s = captured_at_s
            self._last_platform_position = position
            self._last_platform_source = "local_ned"
            return position, "local_ned"

        if self._last_platform_time_s is None:
            self._last_platform_time_s = captured_at_s
            return None
        elapsed_s = captured_at_s - self._last_platform_time_s
        if elapsed_s < 0.0 or elapsed_s > 0.50:
            self._last_platform_time_s = captured_at_s
            return None
        if elapsed_s == 0.0 and self._last_platform_position is not None:
            return self._last_platform_position, self._last_platform_source or "dead_reckoned"

        north_velocity = telemetry.velocity_north_mps
        east_velocity = telemetry.velocity_east_mps
        velocity_age_s = abs(captured_at_s - telemetry.velocity_observed_at_s)
        source = "velocity_dead_reckoned"
        if not (
            math.isfinite(north_velocity)
            and math.isfinite(east_velocity)
            and math.isfinite(velocity_age_s)
            and velocity_age_s <= self.config.maximum_position_age_s
        ):
            if not (
                telemetry.armed is True
                and math.isfinite(telemetry.airspeed_mps)
                and telemetry.airspeed_mps >= self.config.minimum_motion_speed_mps
                and math.isfinite(telemetry.heading_deg)
            ):
                self._last_platform_time_s = captured_at_s
                return None
            heading_rad = math.radians(telemetry.heading_deg)
            north_velocity = telemetry.airspeed_mps * math.cos(heading_rad)
            east_velocity = telemetry.airspeed_mps * math.sin(heading_rad)
            source = "airspeed_dead_reckoned"
        north, east, down = self._dead_reckoned_ned_m
        position = (
            north + north_velocity * elapsed_s,
            east + east_velocity * elapsed_s,
            down,
        )
        self._dead_reckoned_ned_m = position
        self._last_platform_time_s = captured_at_s
        self._last_platform_position = position
        self._last_platform_source = source
        return position, source

    def _estimate(
        self,
        *,
        track: UnifiedTrackSnapshot,
        samples: tuple[_Sample, ...],
    ) -> DirectRangeMeasurement | None:
        if len(samples) < self.config.minimum_samples:
            return None
        if samples[-1].captured_at_s - samples[0].captured_at_s < self.config.minimum_span_seconds:
            return None
        baseline_m = max(
            _distance(samples[-1].position_ned_m, sample.position_ned_m) for sample in samples[:-1]
        )
        candidates: list[_Candidate] = []
        if baseline_m >= self.config.minimum_baseline_m:
            triangulated = self._triangulate(samples=samples, baseline_m=baseline_m)
            if triangulated is not None:
                candidates.append(triangulated)
        looming = self._looming(samples=samples)
        if looming is not None:
            candidates.append(looming)
        if not candidates:
            return None
        if len(candidates) == 2:
            high, low = max(c.range_m for c in candidates), min(c.range_m for c in candidates)
            if high / low > 1.65:
                candidates = [min(candidates, key=lambda candidate: candidate.sigma_m)]
        weights = [1.0 / (candidate.sigma_m**2) for candidate in candidates]
        range_m = sum(c.range_m * w for c, w in zip(candidates, weights, strict=True)) / sum(
            weights
        )
        sigma_m = math.sqrt(1.0 / sum(weights))
        if track.label in self._DYNAMIC_LABELS:
            sigma_m *= 1.75
        sigma_m = max(1.0, sigma_m, range_m * 0.10)
        if not 1.0 <= range_m <= self.config.maximum_range_m:
            return None
        return DirectRangeMeasurement(
            source=DirectRangeSource.VIO,
            target_id=track.track_id,
            slant_range_m=range_m,
            sigma_m=min(range_m * 0.85, sigma_m),
            captured_at_s=samples[-1].captured_at_s,
            absolute_scale_valid=True,
        )

    def _object_size_measurement(
        self,
        *,
        track: UnifiedTrackSnapshot,
        calibration: CameraCalibration,
        captured_at_s: float,
    ) -> DirectRangeMeasurement | None:
        prior = self._OBJECT_HEIGHT_PRIORS_M.get(track.label)
        if prior is None or track.tracking_quality < 0.20:
            return None
        physical_height_m, relative_sigma = prior
        pixel_height = track.bbox.y2 * calibration.height_px - track.bbox.y1 * calibration.height_px
        if pixel_height < 8.0:
            return None
        axial_range_m = physical_height_m * calibration.fy_px / pixel_height
        center_x, center_y = track.bbox.center
        normalized_x = (center_x * calibration.width_px - calibration.cx_px) / calibration.fx_px
        normalized_y = (center_y * calibration.height_px - calibration.cy_px) / calibration.fy_px
        slant_range_m = axial_range_m * math.sqrt(
            1.0 + normalized_x * normalized_x + normalized_y * normalized_y
        )
        if not 1.0 <= slant_range_m <= min(500.0, self.config.maximum_range_m):
            return None
        return DirectRangeMeasurement(
            source=DirectRangeSource.MONOCULAR_SIZE,
            target_id=track.track_id,
            slant_range_m=slant_range_m,
            sigma_m=max(2.0, slant_range_m * relative_sigma),
            captured_at_s=captured_at_s,
            absolute_scale_valid=True,
        )

    def _triangulate(
        self,
        *,
        samples: tuple[_Sample, ...],
        baseline_m: float,
    ) -> _Candidate | None:
        maximum_angle_deg = max(
            _ray_angle_deg(samples[-1].ray_ned, sample.ray_ned) for sample in samples[:-1]
        )
        if maximum_angle_deg < self.config.minimum_ray_separation_deg:
            return None
        point = _least_squares_ray_intersection(samples)
        if point is None:
            return None
        residuals = [_ray_residual(point, sample) for sample in samples]
        median_residual = _median(residuals)
        retained = tuple(
            sample
            for sample, residual in zip(samples, residuals, strict=True)
            if residual <= max(0.75, median_residual * 3.0)
        )
        if len(retained) >= 4 and len(retained) < len(samples):
            refined = _least_squares_ray_intersection(retained)
            if refined is not None:
                point = refined
                residuals = [_ray_residual(point, sample) for sample in retained]
        current = samples[-1]
        delta = _subtract(point, current.position_ned_m)
        slant_range_m = _dot(delta, current.ray_ned)
        rms_residual = math.sqrt(sum(value * value for value in residuals) / len(residuals))
        if slant_range_m <= 0.0 or rms_residual > max(2.5, slant_range_m * 0.10):
            return None
        angular_sigma = math.radians(0.25)
        geometry_sigma = angular_sigma * slant_range_m * slant_range_m / max(baseline_m, 0.1)
        sigma_m = max(1.0, rms_residual * 2.0, geometry_sigma)
        return _Candidate(slant_range_m, sigma_m, "motion_triangulation")

    def _looming(self, *, samples: tuple[_Sample, ...]) -> _Candidate | None:
        times = tuple(sample.captured_at_s for sample in samples)
        log_scales = tuple(math.log(max(sample.angular_scale, 1e-6)) for sample in samples)
        growth_rate, fit_rms = _linear_slope_and_rms(times, log_scales)
        elapsed_s = times[-1] - times[0]
        if growth_rate <= 0.012 or fit_rms > 0.08 or elapsed_s <= 0.0:
            return None
        displacement = _subtract(samples[-1].position_ned_m, samples[0].position_ned_m)
        closing_speed_mps = _dot(displacement, samples[-1].ray_ned) / elapsed_s
        if closing_speed_mps < self.config.minimum_motion_speed_mps:
            return None
        range_m = closing_speed_mps / growth_rate
        if not 1.0 <= range_m <= self.config.maximum_range_m:
            return None
        relative_error = min(0.80, max(0.20, fit_rms / max(growth_rate * elapsed_s, 1e-3)))
        return _Candidate(range_m, max(1.5, range_m * relative_error), "looming")

    def _expire_tracks(self, *, now_s: float) -> None:
        stale = [
            track_id
            for track_id, history in self._samples.items()
            if not history or now_s - history[-1].captured_at_s > self.config.window_seconds
        ]
        for track_id in stale:
            del self._samples[track_id]


def _least_squares_ray_intersection(
    samples: tuple[_Sample, ...],
) -> tuple[float, float, float] | None:
    matrix = [[0.0, 0.0, 0.0] for _ in range(3)]
    rhs = [0.0, 0.0, 0.0]
    for sample in samples:
        ray = sample.ray_ned
        projection = [
            [float(row == column) - ray[row] * ray[column] for column in range(3)]
            for row in range(3)
        ]
        for row in range(3):
            rhs[row] += sum(
                projection[row][column] * sample.position_ned_m[column] for column in range(3)
            )
            for column in range(3):
                matrix[row][column] += projection[row][column]
    return _solve_3x3(matrix, rhs)


def _solve_3x3(matrix: list[list[float]], rhs: list[float]) -> tuple[float, float, float] | None:
    augmented = [matrix[row][:] + [rhs[row]] for row in range(3)]
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
    solution = tuple(augmented[row][3] for row in range(3))
    return solution if all(math.isfinite(value) for value in solution) else None


def _ray_residual(point: tuple[float, float, float], sample: _Sample) -> float:
    delta = _subtract(point, sample.position_ned_m)
    along = _dot(delta, sample.ray_ned)
    perpendicular = tuple(delta[index] - along * sample.ray_ned[index] for index in range(3))
    return math.sqrt(_dot(perpendicular, perpendicular))


def _linear_slope_and_rms(xs: tuple[float, ...], ys: tuple[float, ...]) -> tuple[float, float]:
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator <= 1e-12:
        return 0.0, float("inf")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator
    intercept = mean_y - slope * mean_x
    rms = math.sqrt(
        sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True)) / len(xs)
    )
    return slope, rms


def _ray_angle_deg(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(first, second)))))


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    delta = _subtract(first, second)
    return math.sqrt(_dot(delta, delta))


def _subtract(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(first[index] - second[index] for index in range(3))


def _dot(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return sum(first[index] * second[index] for index in range(3))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


__all__ = ["VisualInertialRangeConfig", "VisualInertialRangeEstimator"]

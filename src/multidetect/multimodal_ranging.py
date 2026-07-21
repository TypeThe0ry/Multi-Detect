from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .compat import StrEnum


class RangeValidity(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    INVALID = "invalid"


class VerticalSource(StrEnum):
    PIXHAWK_AGL = "pixhawk_agl"
    DEM_GPS = "dem_gps"
    GROUND_PLANE = "ground_plane"


class DirectRangeSource(StrEnum):
    LASER = "laser"
    VIO = "vio"
    RGB_SLAM = "rgb_slam"
    MONOCULAR_SIZE = "monocular_size"
    MONOCULAR_METRIC = "monocular_metric"


@dataclass(frozen=True, slots=True)
class CameraCalibration:
    calibration_id: str
    width_px: int
    height_px: int
    fx_px: float
    fy_px: float
    cx_px: float
    cy_px: float
    mount_pitch_down_deg: float = 0.0
    mount_yaw_right_deg: float = 0.0
    mount_roll_clockwise_deg: float = 0.0
    k1: float = 0.0
    k2: float = 0.0
    p1: float = 0.0
    p2: float = 0.0
    k3: float = 0.0
    boresight_sigma_deg: float = 0.25

    def __post_init__(self) -> None:
        if not isinstance(self.calibration_id, str) or not self.calibration_id.strip():
            raise ValueError("camera calibration ID cannot be empty")
        if (
            isinstance(self.width_px, bool)
            or not isinstance(self.width_px, int)
            or isinstance(self.height_px, bool)
            or not isinstance(self.height_px, int)
            or self.width_px <= 0
            or self.height_px <= 0
        ):
            raise ValueError("camera dimensions must be positive")
        numeric = (
            self.fx_px,
            self.fy_px,
            self.cx_px,
            self.cy_px,
            self.mount_pitch_down_deg,
            self.mount_yaw_right_deg,
            self.mount_roll_clockwise_deg,
            self.k1,
            self.k2,
            self.p1,
            self.p2,
            self.k3,
            self.boresight_sigma_deg,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
            raise ValueError("camera calibration values must be numeric")
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("camera calibration values must be finite")
        if self.fx_px <= 0.0 or self.fy_px <= 0.0:
            raise ValueError("camera focal lengths must be positive")
        if not 0.0 <= self.cx_px <= self.width_px or not 0.0 <= self.cy_px <= self.height_px:
            raise ValueError("camera principal point must lie inside the image")
        if not 0.0 <= self.boresight_sigma_deg <= 10.0:
            raise ValueError("camera boresight uncertainty must be in [0, 10] degrees")


@dataclass(frozen=True, slots=True)
class AircraftPose:
    captured_at_s: float
    roll_deg: float
    pitch_deg: float
    heading_deg: float
    roll_sigma_deg: float = 0.3
    pitch_sigma_deg: float = 0.3
    heading_sigma_deg: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.captured_at_s,
            self.roll_deg,
            self.pitch_deg,
            self.heading_deg,
            self.roll_sigma_deg,
            self.pitch_sigma_deg,
            self.heading_sigma_deg,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("aircraft pose values must be finite")
        if self.captured_at_s < 0.0:
            raise ValueError("aircraft pose timestamp cannot be negative")
        if not -180.0 <= self.roll_deg <= 180.0 or not -90.0 <= self.pitch_deg <= 90.0:
            raise ValueError("aircraft attitude is outside its physical domain")
        if not 0.0 <= self.heading_deg < 360.0:
            raise ValueError("aircraft heading must be in [0, 360)")
        if any(value < 0.0 for value in values[4:]):
            raise ValueError("aircraft pose uncertainty cannot be negative")


@dataclass(frozen=True, slots=True)
class TargetImageObservation:
    target_id: str
    frame_id: str
    captured_at_s: float
    center_x: float
    center_y: float
    center_sigma_px: float = 2.0

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.frame_id.strip():
            raise ValueError("target and frame IDs cannot be empty")
        values = (self.captured_at_s, self.center_x, self.center_y, self.center_sigma_px)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("target image values must be finite")
        if self.captured_at_s < 0.0:
            raise ValueError("target image timestamp cannot be negative")
        if not 0.0 <= self.center_x <= 1.0 or not 0.0 <= self.center_y <= 1.0:
            raise ValueError("target center must be normalized to [0, 1]")
        if self.center_sigma_px <= 0.0:
            raise ValueError("target center uncertainty must be positive")


@dataclass(frozen=True, slots=True)
class VerticalMeasurement:
    source: VerticalSource
    height_m: float
    sigma_m: float
    captured_at_s: float

    def __post_init__(self) -> None:
        if not isinstance(self.source, VerticalSource):
            raise ValueError("vertical source is invalid")
        if not all(
            math.isfinite(value) for value in (self.height_m, self.sigma_m, self.captured_at_s)
        ):
            raise ValueError("vertical measurement values must be finite")
        if self.height_m <= 0.0 or self.sigma_m <= 0.0 or self.captured_at_s < 0.0:
            raise ValueError("vertical measurement height/sigma must be positive")


@dataclass(frozen=True, slots=True)
class DirectRangeMeasurement:
    source: DirectRangeSource
    target_id: str
    slant_range_m: float
    sigma_m: float
    captured_at_s: float
    absolute_scale_valid: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.source, DirectRangeSource):
            raise ValueError("direct range source is invalid")
        if not self.target_id.strip():
            raise ValueError("direct range target ID cannot be empty")
        if not all(
            math.isfinite(value) for value in (self.slant_range_m, self.sigma_m, self.captured_at_s)
        ):
            raise ValueError("direct range values must be finite")
        if self.slant_range_m <= 0.0 or self.sigma_m <= 0.0 or self.captured_at_s < 0.0:
            raise ValueError("direct range distance/sigma must be positive")
        if not isinstance(self.absolute_scale_valid, bool):
            raise ValueError("direct range scale-valid flag must be boolean")


@dataclass(frozen=True, slots=True)
class RangingFusionConfig:
    maximum_pose_age_s: float = 0.20
    maximum_image_age_s: float = 0.35
    maximum_vertical_age_s: float = 0.50
    maximum_direct_range_age_s: float = 0.35
    maximum_pose_image_skew_s: float = 0.10
    consistency_gate_sigma: float = 3.5
    minimum_range_sigma_m: float = 0.15
    minimum_slant_range_m: float = 0.4
    maximum_slant_range_m: float = 800.0
    minimum_downward_ray_component: float = 0.03

    def __post_init__(self) -> None:
        values = (
            self.maximum_pose_age_s,
            self.maximum_image_age_s,
            self.maximum_vertical_age_s,
            self.maximum_direct_range_age_s,
            self.maximum_pose_image_skew_s,
            self.consistency_gate_sigma,
            self.minimum_range_sigma_m,
            self.minimum_slant_range_m,
            self.maximum_slant_range_m,
            self.minimum_downward_ray_component,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("ranging fusion limits must be finite and positive")
        if self.minimum_downward_ray_component >= 1.0:
            raise ValueError("minimum downward ray component must be below one")
        if self.minimum_slant_range_m >= self.maximum_slant_range_m:
            raise ValueError("ranging slant-range limits are reversed")


@dataclass(frozen=True, slots=True)
class RangeSourceContribution:
    """One accepted measurement exposed to the ground UI and audit stream."""

    source: str
    range_m: float
    sigma_m: float
    weight: float
    freshness_s: float

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("range contribution source cannot be empty")
        if not all(
            math.isfinite(value) and value >= 0.0
            for value in (self.range_m, self.sigma_m, self.weight, self.freshness_s)
        ):
            raise ValueError("range contribution fields must be finite and non-negative")
        if self.sigma_m <= 0.0:
            raise ValueError("range contribution sigma must be positive")


@dataclass(frozen=True, slots=True)
class RangeSolution:
    target_id: str
    frame_id: str
    calibration_id: str
    evaluated_at_s: float
    validity: RangeValidity
    reasons: tuple[str, ...]
    sources: tuple[str, ...]
    rejected_sources: tuple[str, ...]
    slant_range_m: float | None = None
    ground_range_m: float | None = None
    slant_range_ci95_m: tuple[float, float] | None = None
    ground_range_ci95_m: tuple[float, float] | None = None
    relative_bearing_deg: float | None = None
    absolute_bearing_deg: float | None = None
    bearing_sigma_deg: float | None = None
    north_offset_m: float | None = None
    east_offset_m: float | None = None
    data_freshness_s: float | None = None
    sensor_consistency: float = 0.0
    source_contributions: tuple[RangeSourceContribution, ...] = ()
    fusion_profile: str = "outdoor-multimodal-v1"
    vehicle_profile: str = "auto"
    navigation_state: str = "unknown"
    motion_regime: str = "unknown"
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            not self.target_id.strip()
            or not self.frame_id.strip()
            or not self.calibration_id.strip()
        ):
            raise ValueError("range solution identifiers cannot be empty")
        if not math.isfinite(self.evaluated_at_s) or self.evaluated_at_s < 0.0:
            raise ValueError("range solution evaluation time is invalid")
        if not isinstance(self.validity, RangeValidity):
            raise ValueError("range solution validity is invalid")
        if not self.reasons or any(not reason.strip() for reason in self.reasons):
            raise ValueError("range solution reasons cannot be empty")
        numeric = (
            self.slant_range_m,
            self.ground_range_m,
            self.relative_bearing_deg,
            self.absolute_bearing_deg,
            self.bearing_sigma_deg,
            self.north_offset_m,
            self.east_offset_m,
            self.data_freshness_s,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("range solution numeric values must be finite when supplied")
        if not math.isfinite(self.sensor_consistency) or not 0.0 <= self.sensor_consistency <= 1.0:
            raise ValueError("sensor consistency must be in [0, 1]")
        if any(
            not isinstance(value, RangeSourceContribution) for value in self.source_contributions
        ):
            raise ValueError("range source contributions are invalid")
        if self.source_contributions and not math.isclose(
            sum(value.weight for value in self.source_contributions),
            1.0,
            abs_tol=0.015,
        ):
            raise ValueError("accepted range contribution weights must sum to one")
        for name, value in (
            ("fusion profile", self.fusion_profile),
            ("vehicle profile", self.vehicle_profile),
            ("navigation state", self.navigation_state),
            ("motion regime", self.motion_regime),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if self.validity is RangeValidity.INVALID and any(
            value is not None
            for value in (
                self.slant_range_m,
                self.ground_range_m,
                self.slant_range_ci95_m,
                self.ground_range_ci95_m,
            )
        ):
            raise ValueError("invalid range solutions cannot publish distance")
        for interval in (self.slant_range_ci95_m, self.ground_range_ci95_m):
            if interval is not None and (
                len(interval) != 2
                or not all(math.isfinite(value) and value >= 0.0 for value in interval)
                or interval[1] < interval[0]
            ):
                raise ValueError("range confidence interval is invalid")
        if not self.advisory_only or self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("range solution must remain advisory-only")


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: str
    value_m: float
    sigma_m: float
    age_s: float


@dataclass(frozen=True, slots=True)
class _Fusion:
    value_m: float
    sigma_m: float
    accepted: tuple[_Candidate, ...]
    rejected: tuple[_Candidate, ...]
    consistency: float


class MultiModalRangingEngine:
    """Read-only target range fusion with explicit freshness and consistency gates."""

    def __init__(self, config: RangingFusionConfig | None = None) -> None:
        self.config = config or RangingFusionConfig()

    @staticmethod
    def relative_bearing_deg(
        *,
        calibration: CameraCalibration,
        target: TargetImageObservation,
    ) -> float:
        """Project one calibrated image point into a body-relative bearing.

        This remains useful when metric depth is temporarily absent: it uses the
        same distortion and camera-mount model as the full range solution rather
        than a raw screen-coordinate approximation.
        """

        neutral_pose = AircraftPose(
            captured_at_s=target.captured_at_s,
            roll_deg=0.0,
            pitch_deg=0.0,
            heading_deg=0.0,
        )
        ray_body, _ = _target_rays(calibration, neutral_pose, target)
        return _wrap_signed_degrees(math.degrees(math.atan2(ray_body[1], ray_body[0])))

    @staticmethod
    def target_ray_ned(
        *,
        calibration: CameraCalibration,
        pose: AircraftPose,
        target: TargetImageObservation,
    ) -> tuple[float, float, float]:
        """Return the calibrated target ray in the navigation NED frame."""

        return _target_rays(calibration, pose, target)[1]

    def solve(
        self,
        *,
        calibration: CameraCalibration,
        pose: AircraftPose,
        target: TargetImageObservation,
        vertical_measurements: tuple[VerticalMeasurement, ...],
        direct_measurements: tuple[DirectRangeMeasurement, ...] = (),
        source_weight_multipliers: Mapping[str, float] | None = None,
        fusion_profile: str = "outdoor-multimodal-v1",
        vehicle_profile: str = "auto",
        navigation_state: str = "unknown",
        motion_regime: str = "unknown",
        now_s: float,
    ) -> RangeSolution:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("ranging evaluation time must be finite and non-negative")
        base = {
            "target_id": target.target_id,
            "frame_id": target.frame_id,
            "calibration_id": calibration.calibration_id,
            "evaluated_at_s": now_s,
            "fusion_profile": fusion_profile,
            "vehicle_profile": vehicle_profile,
            "navigation_state": navigation_state,
            "motion_regime": motion_regime,
        }
        reasons: list[str] = []
        pose_age = now_s - pose.captured_at_s
        image_age = now_s - target.captured_at_s
        if pose_age < 0.0 or pose_age > self.config.maximum_pose_age_s:
            return self._invalid(base, "pose_stale_or_from_future")
        if image_age < 0.0 or image_age > self.config.maximum_image_age_s:
            return self._invalid(base, "target_image_stale_or_from_future")
        if abs(pose.captured_at_s - target.captured_at_s) > self.config.maximum_pose_image_skew_s:
            return self._invalid(base, "pose_image_time_skew_exceeded")

        if _has_duplicate_sources(measurement.source for measurement in vertical_measurements):
            return self._invalid(base, "duplicate_vertical_source")
        vertical_candidates = tuple(
            _weighted_candidate(
                _Candidate(
                measurement.source.value,
                measurement.height_m,
                measurement.sigma_m,
                now_s - measurement.captured_at_s,
                ),
                source_weight_multipliers,
            )
            for measurement in vertical_measurements
            if 0.0 <= now_s - measurement.captured_at_s <= self.config.maximum_vertical_age_s
        )
        if not vertical_candidates:
            return self._invalid(base, "vertical_reference_unavailable_or_stale")
        vertical_fusion = _fuse_consistent(
            vertical_candidates,
            gate_sigma=self.config.consistency_gate_sigma,
            minimum_sigma=self.config.minimum_range_sigma_m,
        )
        if vertical_fusion is None:
            return self._invalid(base, "vertical_references_inconsistent")
        if vertical_fusion.rejected:
            reasons.append("vertical_reference_outlier_rejected")

        ray_body, ray_ned = _target_rays(calibration, pose, target)
        if ray_ned[2] < self.config.minimum_downward_ray_component:
            return self._invalid(base, "target_ray_does_not_intersect_ground_safely")

        camera_slant = vertical_fusion.value_m / ray_ned[2]
        if not (
            self.config.minimum_slant_range_m
            <= camera_slant
            <= self.config.maximum_slant_range_m
        ):
            return self._invalid(base, "camera_ground_intersection_out_of_range")
        camera_sigma = self._camera_range_sigma(
            calibration=calibration,
            pose=pose,
            target=target,
            height_m=vertical_fusion.value_m,
            height_sigma_m=vertical_fusion.sigma_m,
        )
        range_candidates: list[_Candidate] = [
            _weighted_candidate(
                _Candidate(
                "camera_ground",
                camera_slant,
                max(self.config.minimum_range_sigma_m, camera_sigma),
                max(
                    pose_age,
                    image_age,
                    *(candidate.age_s for candidate in vertical_fusion.accepted),
                ),
                ),
                source_weight_multipliers,
            )
        ]

        if _has_duplicate_sources(measurement.source for measurement in direct_measurements):
            return self._invalid(base, "duplicate_direct_range_source")
        for measurement in direct_measurements:
            age_s = now_s - measurement.captured_at_s
            if measurement.target_id != target.target_id:
                reasons.append(_direct_rejection_reason(measurement.source, "target_mismatch"))
                continue
            if not measurement.absolute_scale_valid:
                reasons.append(
                    _direct_rejection_reason(measurement.source, "absolute_scale_invalid")
                )
                continue
            if age_s < 0.0 or age_s > self.config.maximum_direct_range_age_s:
                reasons.append(
                    _direct_rejection_reason(measurement.source, "stale_or_from_future")
                )
                continue
            if not (
                self.config.minimum_slant_range_m
                <= measurement.slant_range_m
                <= self.config.maximum_slant_range_m
            ):
                reasons.append(_direct_rejection_reason(measurement.source, "out_of_range"))
                continue
            range_candidates.append(
                _weighted_candidate(
                    _Candidate(
                    measurement.source.value,
                    measurement.slant_range_m,
                    max(self.config.minimum_range_sigma_m, measurement.sigma_m),
                    age_s,
                    ),
                    source_weight_multipliers,
                )
            )

        range_fusion = _fuse_consistent(
            tuple(range_candidates),
            gate_sigma=self.config.consistency_gate_sigma,
            minimum_sigma=self.config.minimum_range_sigma_m,
        )
        if range_fusion is None:
            return self._invalid(base, "absolute_range_sources_inconsistent")
        if range_fusion.rejected:
            reasons.append("absolute_range_outlier_rejected")

        slant_range_m = range_fusion.value_m
        horizontal_fraction = math.hypot(ray_ned[0], ray_ned[1])
        ground_range_m = slant_range_m * horizontal_fraction
        north_offset_m = slant_range_m * ray_ned[0]
        east_offset_m = slant_range_m * ray_ned[1]
        relative_bearing_deg = _wrap_signed_degrees(
            math.degrees(math.atan2(ray_body[1], ray_body[0]))
        )
        absolute_bearing_deg = _wrap_unsigned_degrees(
            math.degrees(math.atan2(ray_ned[1], ray_ned[0]))
        )
        bearing_sigma_deg = math.sqrt(
            pose.heading_sigma_deg**2
            + calibration.boresight_sigma_deg**2
            + math.degrees(target.center_sigma_px / max(calibration.fx_px, 1.0)) ** 2
        )
        slant_ci = _ci95(slant_range_m, range_fusion.sigma_m)
        ground_ci = _ci95(ground_range_m, range_fusion.sigma_m * horizontal_fraction)
        accepted_sources = tuple(
            candidate.source for candidate in vertical_fusion.accepted
        ) + tuple(candidate.source for candidate in range_fusion.accepted)
        rejected_sources = tuple(
            candidate.source for candidate in vertical_fusion.rejected
        ) + tuple(candidate.source for candidate in range_fusion.rejected)
        all_ages = [pose_age, image_age]
        all_ages.extend(candidate.age_s for candidate in vertical_fusion.accepted)
        all_ages.extend(candidate.age_s for candidate in range_fusion.accepted)

        validity = RangeValidity.VALID
        if len(range_fusion.accepted) < 2 or range_fusion.rejected or vertical_fusion.rejected:
            validity = RangeValidity.DEGRADED
        if len(range_fusion.accepted) < 2:
            reasons.append("single_absolute_range_method")
        if not reasons:
            reasons.append("multimodal_range_consistent")
        sensor_consistency = range_fusion.consistency
        if len(vertical_fusion.accepted) > 1:
            sensor_consistency = min(sensor_consistency, vertical_fusion.consistency)
        return RangeSolution(
            **base,
            validity=validity,
            reasons=tuple(dict.fromkeys(reasons)),
            sources=accepted_sources,
            rejected_sources=rejected_sources,
            slant_range_m=slant_range_m,
            ground_range_m=ground_range_m,
            slant_range_ci95_m=slant_ci,
            ground_range_ci95_m=ground_ci,
            relative_bearing_deg=relative_bearing_deg,
            absolute_bearing_deg=absolute_bearing_deg,
            bearing_sigma_deg=bearing_sigma_deg,
            north_offset_m=north_offset_m,
            east_offset_m=east_offset_m,
            data_freshness_s=max(all_ages),
            sensor_consistency=sensor_consistency,
            source_contributions=_source_contributions(range_fusion),
        )

    def solve_direct(
        self,
        *,
        calibration: CameraCalibration,
        pose: AircraftPose,
        target: TargetImageObservation,
        direct_measurements: tuple[DirectRangeMeasurement, ...],
        source_weight_multipliers: Mapping[str, float] | None = None,
        fusion_profile: str = "outdoor-multimodal-v1",
        vehicle_profile: str = "auto",
        navigation_state: str = "unknown",
        motion_regime: str = "unknown",
        now_s: float,
    ) -> RangeSolution:
        """Produce a degraded metric solution from scaled visual-inertial range.

        This is used while an aircraft is moving when a terrain/home height is
        temporarily absent.  It remains explicitly degraded and carries a wide
        uncertainty interval; later vertical ranging replaces it automatically.
        """
        base = {
            "target_id": target.target_id,
            "frame_id": target.frame_id,
            "calibration_id": calibration.calibration_id,
            "evaluated_at_s": now_s,
            "fusion_profile": fusion_profile,
            "vehicle_profile": vehicle_profile,
            "navigation_state": navigation_state,
            "motion_regime": motion_regime,
        }
        pose_age, image_age = now_s - pose.captured_at_s, now_s - target.captured_at_s
        if pose_age < 0 or pose_age > self.config.maximum_pose_age_s:
            return self._invalid(base, "pose_stale_or_from_future")
        if image_age < 0 or image_age > self.config.maximum_image_age_s:
            return self._invalid(base, "target_image_stale_or_from_future")
        # Direct metric methods do not derive their scale from the instantaneous
        # aircraft pose. Keep bearing metadata usable across the bounded RTSP
        # capture/telemetry skew seen on the live Jetson pipeline.
        if abs(pose.captured_at_s - target.captured_at_s) > (
            self.config.maximum_pose_age_s + self.config.maximum_image_age_s + 1e-6
        ):
            return self._invalid(base, "pose_image_time_skew_exceeded")
        candidates = tuple(
            _weighted_candidate(
                _Candidate(
                m.source.value,
                m.slant_range_m,
                max(self.config.minimum_range_sigma_m, m.sigma_m),
                now_s - m.captured_at_s,
                ),
                source_weight_multipliers,
            )
            for m in direct_measurements
            if m.target_id == target.target_id
            and m.absolute_scale_valid
            and 0 <= now_s - m.captured_at_s <= self.config.maximum_direct_range_age_s
            and self.config.minimum_slant_range_m
            <= m.slant_range_m
            <= self.config.maximum_slant_range_m
        )
        fusion = _fuse_consistent(
            candidates,
            gate_sigma=self.config.consistency_gate_sigma,
            minimum_sigma=self.config.minimum_range_sigma_m,
        )
        if fusion is None:
            return self._invalid(base, "direct_range_unavailable")
        ray_body, ray_ned = _target_rays(calibration, pose, target)
        slant_range_m = fusion.value_m
        horizontal_fraction = math.hypot(ray_ned[0], ray_ned[1])
        return RangeSolution(
            **base,
            validity=RangeValidity.DEGRADED,
            reasons=("direct_degraded_metric_range", "vertical_reference_unavailable"),
            sources=tuple(c.source for c in fusion.accepted),
            rejected_sources=tuple(c.source for c in fusion.rejected),
            slant_range_m=slant_range_m,
            ground_range_m=slant_range_m * horizontal_fraction,
            slant_range_ci95_m=_ci95(slant_range_m, fusion.sigma_m),
            ground_range_ci95_m=_ci95(
                slant_range_m * horizontal_fraction, fusion.sigma_m * horizontal_fraction
            ),
            relative_bearing_deg=_wrap_signed_degrees(
                math.degrees(math.atan2(ray_body[1], ray_body[0]))
            ),
            absolute_bearing_deg=_wrap_unsigned_degrees(
                math.degrees(math.atan2(ray_ned[1], ray_ned[0]))
            ),
            bearing_sigma_deg=math.sqrt(
                pose.heading_sigma_deg**2
                + calibration.boresight_sigma_deg**2
                + math.degrees(target.center_sigma_px / max(calibration.fx_px, 1.0)) ** 2
            ),
            north_offset_m=slant_range_m * ray_ned[0],
            east_offset_m=slant_range_m * ray_ned[1],
            data_freshness_s=max(pose_age, image_age, *(c.age_s for c in fusion.accepted)),
            sensor_consistency=fusion.consistency,
            source_contributions=_source_contributions(fusion),
        )

    def _camera_range_sigma(
        self,
        *,
        calibration: CameraCalibration,
        pose: AircraftPose,
        target: TargetImageObservation,
        height_m: float,
        height_sigma_m: float,
    ) -> float:
        base_ray = _target_rays(calibration, pose, target)[1]
        components = [height_sigma_m / base_ray[2]]
        perturbations: tuple[tuple[object, str, float], ...] = (
            (pose, "roll_deg", pose.roll_sigma_deg),
            (pose, "pitch_deg", pose.pitch_sigma_deg),
            (calibration, "mount_pitch_down_deg", calibration.boresight_sigma_deg),
            (target, "center_x", target.center_sigma_px / calibration.width_px),
            (target, "center_y", target.center_sigma_px / calibration.height_px),
        )
        for instance, field_name, sigma in perturbations:
            if sigma <= 0.0:
                continue
            try:
                plus = replace(instance, **{field_name: getattr(instance, field_name) + sigma})
                minus = replace(instance, **{field_name: getattr(instance, field_name) - sigma})
                plus_ray = _target_rays(
                    plus if isinstance(plus, CameraCalibration) else calibration,
                    plus if isinstance(plus, AircraftPose) else pose,
                    plus if isinstance(plus, TargetImageObservation) else target,
                )[1]
                minus_ray = _target_rays(
                    minus if isinstance(minus, CameraCalibration) else calibration,
                    minus if isinstance(minus, AircraftPose) else pose,
                    minus if isinstance(minus, TargetImageObservation) else target,
                )[1]
                if plus_ray[2] > 0.0 and minus_ray[2] > 0.0:
                    delta = abs(height_m / plus_ray[2] - height_m / minus_ray[2]) / 2.0
                    components.append(delta)
            except ValueError:
                continue
        return math.sqrt(sum(component * component for component in components))

    @staticmethod
    def _invalid(base: dict[str, object], reason: str) -> RangeSolution:
        return RangeSolution(
            **base,
            validity=RangeValidity.INVALID,
            reasons=(reason,),
            sources=(),
            rejected_sources=(),
        )


_CALIBRATION_FIELDS = frozenset(CameraCalibration.__dataclass_fields__)


def load_camera_calibration(path: str | Path) -> CameraCalibration:
    """Load a strict, explicit camera calibration document.

    The loader deliberately has no guessed/default camera profile. A live ranging
    session must name a versioned calibration file, and unknown fields are rejected
    so misspelled intrinsics or installation angles cannot silently pass through.
    """

    calibration_path = Path(path)
    try:
        document: Any = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"camera calibration could not be loaded: {calibration_path}") from exc
    if not isinstance(document, dict):
        raise ValueError("camera calibration document must be an object")
    unknown_document_fields = set(document) - {"schema_version", "calibration"}
    if unknown_document_fields:
        raise ValueError(
            "camera calibration document contains unknown fields: "
            + ", ".join(sorted(unknown_document_fields))
        )
    if document.get("schema_version") != 1:
        raise ValueError("camera calibration schema_version must be 1")
    raw_calibration = document.get("calibration")
    if not isinstance(raw_calibration, dict):
        raise ValueError("camera calibration field must be an object")
    unknown_calibration_fields = set(raw_calibration) - _CALIBRATION_FIELDS
    if unknown_calibration_fields:
        raise ValueError(
            "camera calibration contains unknown fields: "
            + ", ".join(sorted(unknown_calibration_fields))
        )
    required = {
        "calibration_id",
        "width_px",
        "height_px",
        "fx_px",
        "fy_px",
        "cx_px",
        "cy_px",
    }
    missing = required - set(raw_calibration)
    if missing:
        raise ValueError("camera calibration is missing fields: " + ", ".join(sorted(missing)))
    try:
        return CameraCalibration(**raw_calibration)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"camera calibration is invalid: {exc}") from exc


def _target_rays(
    calibration: CameraCalibration,
    pose: AircraftPose,
    target: TargetImageObservation,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    u = target.center_x * calibration.width_px
    v = target.center_y * calibration.height_px
    distorted_x = (u - calibration.cx_px) / calibration.fx_px
    distorted_y = (v - calibration.cy_px) / calibration.fy_px
    x, y = _undistort(distorted_x, distorted_y, calibration)
    camera_ray = _normalize((x, y, 1.0))
    nominal_body = (camera_ray[2], camera_ray[0], camera_ray[1])
    mount_rotation = _rotation_matrix(
        calibration.mount_roll_clockwise_deg,
        -calibration.mount_pitch_down_deg,
        calibration.mount_yaw_right_deg,
    )
    ray_body = _normalize(_matvec(mount_rotation, nominal_body))
    body_to_ned = _rotation_matrix(pose.roll_deg, pose.pitch_deg, pose.heading_deg)
    ray_ned = _normalize(_matvec(body_to_ned, ray_body))
    return ray_body, ray_ned


def _undistort(xd: float, yd: float, calibration: CameraCalibration) -> tuple[float, float]:
    x, y = xd, yd
    for _ in range(8):
        radius2 = x * x + y * y
        radial = (
            1.0
            + calibration.k1 * radius2
            + calibration.k2 * radius2**2
            + calibration.k3 * radius2**3
        )
        if not math.isfinite(radial) or abs(radial) < 1e-9:
            raise ValueError("camera distortion model is singular")
        tangential_x = 2.0 * calibration.p1 * x * y + calibration.p2 * (radius2 + 2.0 * x * x)
        tangential_y = calibration.p1 * (radius2 + 2.0 * y * y) + 2.0 * calibration.p2 * x * y
        x = (xd - tangential_x) / radial
        y = (yd - tangential_y) / radial
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("camera distortion inversion failed")
    return x, y


def _rotation_matrix(
    roll_deg: float,
    pitch_deg: float,
    yaw_deg: float,
) -> tuple[tuple[float, float, float], ...]:
    roll = math.radians(roll_deg)
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy),
        (cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy),
        (-sp, sr * cp, cr * cp),
    )


def _matvec(
    matrix: tuple[tuple[float, float, float], ...],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        sum(matrix[0][index] * vector[index] for index in range(3)),
        sum(matrix[1][index] * vector[index] for index in range(3)),
        sum(matrix[2][index] * vector[index] for index in range(3)),
    )


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("camera ray is degenerate")
    return (vector[0] / norm, vector[1] / norm, vector[2] / norm)


def _has_duplicate_sources(sources: Iterable[object]) -> bool:
    values = tuple(sources)
    return len(values) != len(set(values))


def _weighted_candidate(
    candidate: _Candidate,
    source_weight_multipliers: Mapping[str, float] | None,
) -> _Candidate:
    """Apply a bounded information prior without obscuring the raw measurement.

    The fusion engine stays deterministic: policy weights only rescale the
    covariance before the existing consistency gate and inverse-variance fusion.
    """

    if not source_weight_multipliers:
        return candidate
    multiplier = source_weight_multipliers.get(candidate.source, 1.0)
    if not isinstance(multiplier, (int, float)) or not math.isfinite(multiplier):
        return candidate
    multiplier = min(8.0, max(0.05, float(multiplier)))
    return _Candidate(
        source=candidate.source,
        value_m=candidate.value_m,
        sigma_m=candidate.sigma_m / math.sqrt(multiplier),
        age_s=candidate.age_s,
    )


def _source_contributions(fusion: _Fusion) -> tuple[RangeSourceContribution, ...]:
    if not fusion.accepted:
        return ()
    information = tuple(1.0 / candidate.sigma_m**2 for candidate in fusion.accepted)
    total = sum(information)
    return tuple(
        RangeSourceContribution(
            source=candidate.source,
            range_m=candidate.value_m,
            sigma_m=candidate.sigma_m,
            weight=value / total,
            freshness_s=candidate.age_s,
        )
        for candidate, value in zip(fusion.accepted, information, strict=True)
    )


def _direct_rejection_reason(source: DirectRangeSource, suffix: str) -> str:
    if source in {
        DirectRangeSource.LASER,
        DirectRangeSource.VIO,
    }:
        return f"{source.value}_{suffix}"
    return "direct_range_unavailable"


def _fuse_consistent(
    candidates: tuple[_Candidate, ...],
    *,
    gate_sigma: float,
    minimum_sigma: float,
) -> _Fusion | None:
    if not candidates:
        return None
    accepted = candidates
    rejected: tuple[_Candidate, ...] = ()
    if len(candidates) > 1 and not _all_consistent(candidates, gate_sigma):
        accepted = ()
        for size in range(len(candidates) - 1, 1, -1):
            subsets = tuple(
                subset
                for subset in itertools.combinations(candidates, size)
                if _all_consistent(subset, gate_sigma)
            )
            if subsets:
                accepted = min(subsets, key=lambda subset: _weighted_sigma(subset, minimum_sigma))
                break
        if not accepted:
            return None
        rejected = tuple(candidate for candidate in candidates if candidate not in accepted)
    weights = tuple(1.0 / max(candidate.sigma_m, minimum_sigma) ** 2 for candidate in accepted)
    weight_sum = sum(weights)
    value_m = (
        sum(candidate.value_m * weight for candidate, weight in zip(accepted, weights, strict=True))
        / weight_sum
    )
    sigma_m = math.sqrt(1.0 / weight_sum)
    max_z = _maximum_pairwise_z(accepted)
    consistency = 0.5 if len(accepted) == 1 else math.exp(-0.5 * (max_z / gate_sigma) ** 2)
    return _Fusion(value_m, sigma_m, tuple(accepted), rejected, consistency)


def _weighted_sigma(candidates: tuple[_Candidate, ...], minimum_sigma: float) -> float:
    return math.sqrt(
        1.0 / sum(1.0 / max(candidate.sigma_m, minimum_sigma) ** 2 for candidate in candidates)
    )


def _all_consistent(candidates: tuple[_Candidate, ...], gate_sigma: float) -> bool:
    return _maximum_pairwise_z(candidates) <= gate_sigma


def _maximum_pairwise_z(candidates: tuple[_Candidate, ...]) -> float:
    maximum = 0.0
    for left, right in itertools.combinations(candidates, 2):
        denominator = math.hypot(left.sigma_m, right.sigma_m)
        maximum = max(maximum, abs(left.value_m - right.value_m) / denominator)
    return maximum


def _ci95(value_m: float, sigma_m: float) -> tuple[float, float]:
    margin = 1.96 * sigma_m
    return (max(0.0, value_m - margin), value_m + margin)


def _wrap_unsigned_degrees(value: float) -> float:
    return value % 360.0


def _wrap_signed_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


__all__ = [
    "AircraftPose",
    "CameraCalibration",
    "DirectRangeMeasurement",
    "DirectRangeSource",
    "MultiModalRangingEngine",
    "RangeSolution",
    "RangeSourceContribution",
    "RangeValidity",
    "RangingFusionConfig",
    "TargetImageObservation",
    "VerticalMeasurement",
    "VerticalSource",
    "load_camera_calibration",
]

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .autonomy.serialization import AutonomyContractError, strict_json_loads
from .compat import StrEnum
from .terrain_dem import DemGpsTerrainObservation


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
    # Common one-sigma survey uncertainty for each independently measured
    # mount Euler angle.  The ray solver perturbs pitch, yaw and roll instead
    # of treating this value as a pitch-only range term plus a generic bearing
    # scalar.  That matters for banked aircraft and off-axis detections, where
    # every mount axis can change the NED ground intersection.
    boresight_sigma_deg: float = 0.25
    # Translation from the navigation/GNSS reference point to the camera
    # optical centre, expressed in the aircraft body NED frame (forward,
    # right, down).  Older calibration documents omit these optional fields;
    # zero keeps their historical origin at the navigation reference point.
    camera_offset_forward_m: float = 0.0
    camera_offset_right_m: float = 0.0
    camera_offset_down_m: float = 0.0
    # Conservative isotropic one-sigma survey uncertainty for the three
    # translation components.  It is used in the range/height/bearing error
    # budget rather than silently treating an un-surveyed lever arm as exact.
    camera_offset_sigma_m: float = 0.0

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
            self.camera_offset_forward_m,
            self.camera_offset_right_m,
            self.camera_offset_down_m,
            self.camera_offset_sigma_m,
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
        if self.camera_offset_sigma_m < 0.0:
            raise ValueError("camera offset uncertainty cannot be negative")
        if any(
            abs(value) > 100.0
            for value in (
                self.camera_offset_forward_m,
                self.camera_offset_right_m,
                self.camera_offset_down_m,
            )
        ):
            raise ValueError("camera offset is outside the supported 100 m domain")
        if self.camera_offset_sigma_m > 100.0:
            raise ValueError("camera offset uncertainty is outside the supported 100 m domain")


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
    # A pair can be inside the hard consistency gate while its residuals are
    # still larger than the declared sensor covariance.  Keep that fact
    # explicit so callers can surface the conservative uncertainty budget and
    # mark the result degraded instead of presenting an over-confident point.
    sigma_inflated: bool = False


_Ray = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class _RayUncertainty:
    """Nominal NED ray plus one-at-a-time perturbation endpoints.

    The ranging result is only useful when every one-sigma perturbation still
    intersects the ground.  Keeping the perturbation endpoints together lets
    slant and ground-range uncertainty use the exact same calibrated ray
    samples instead of silently dropping a near-horizon derivative.
    """

    base_body: _Ray
    base_ned: _Ray
    perturbations: tuple[tuple[_Ray, _Ray], ...]
    # Pose roll/pitch and all three surveyed mount angles rotate the NED ray as
    # well as changing its ground-range projection.  Keep their independent
    # central-difference azimuth terms so the target-geolocation error budget
    # does not silently omit this coupling in a banked or pitched aircraft.
    attitude_bearing_sigma_deg: float = 0.0
    boresight_bearing_sigma_deg: float = 0.0


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
        terrain_observation: DemGpsTerrainObservation | None = None,
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

        if terrain_observation is not None and not isinstance(
            terrain_observation, DemGpsTerrainObservation
        ):
            raise TypeError("terrain_observation must be DemGpsTerrainObservation or None")
        combined_vertical_measurements = vertical_measurements
        if terrain_observation is not None:
            combined_vertical_measurements += (
                VerticalMeasurement(
                    source=VerticalSource.DEM_GPS,
                    height_m=terrain_observation.height_m,
                    sigma_m=terrain_observation.height_sigma_m,
                    captured_at_s=terrain_observation.captured_at_s,
                ),
            )
        if _has_duplicate_sources(
            measurement.source for measurement in combined_vertical_measurements
        ):
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
            for measurement in combined_vertical_measurements
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

        ray_uncertainty = _build_ray_uncertainty(calibration, pose, target)
        if ray_uncertainty is None:
            return self._invalid(base, "target_ray_uncertainty_unavailable")
        ray_body, ray_ned = ray_uncertainty.base_body, ray_uncertainty.base_ned
        if ray_ned[2] < self.config.minimum_downward_ray_component:
            return self._invalid(base, "target_ray_does_not_intersect_ground_safely")

        # Vertical measurements are reported at the navigation/GNSS reference
        # point.  Move the ground-plane origin to the optical centre before
        # intersecting the ray; otherwise even a modest forward/downward
        # camera installation creates a repeatable outdoor map bias.
        camera_offset_ned = _camera_offset_ned(calibration, pose)
        terrain_active = terrain_observation is not None and any(
            candidate.source == VerticalSource.DEM_GPS.value
            for candidate in vertical_fusion.accepted
        )
        if terrain_active:
            assert terrain_observation is not None
            intersection = terrain_observation.grid.intersect_ray(
                reference_north_m=terrain_observation.reference_north_m,
                reference_east_m=terrain_observation.reference_east_m,
                height_m=vertical_fusion.value_m,
                camera_offset_ned=camera_offset_ned,
                ray_ned=ray_ned,
                maximum_slant_range_m=self.config.maximum_slant_range_m,
                minimum_incidence_component=self.config.minimum_downward_ray_component,
            )
            if intersection is None:
                return self._invalid(base, "terrain_intersection_unavailable")
            camera_slant = intersection.slant_range_m
            camera_sigma = self._terrain_range_sigma(
                calibration=calibration,
                terrain=terrain_observation,
                height_m=vertical_fusion.value_m,
                height_sigma_m=vertical_fusion.sigma_m,
                camera_offset_ned=camera_offset_ned,
                ray_uncertainty=ray_uncertainty,
            )
            if camera_sigma is None:
                return self._invalid(base, "terrain_uncertainty_unbounded")
        else:
            camera_height_m = vertical_fusion.value_m - camera_offset_ned[2]
            if camera_height_m <= 0.0 or not math.isfinite(camera_height_m):
                return self._invalid(base, "camera_ground_intersection_out_of_range")
            camera_height_sigma_m = math.hypot(
                vertical_fusion.sigma_m,
                calibration.camera_offset_sigma_m,
            )
            camera_slant = camera_height_m / ray_ned[2]
            camera_sigma = self._camera_range_sigma(
                calibration=calibration,
                pose=pose,
                target=target,
                height_m=camera_height_m,
                height_sigma_m=camera_height_sigma_m,
                ray_uncertainty=ray_uncertainty,
            )
            if camera_sigma is None:
                return self._invalid(base, "target_ray_uncertainty_crosses_horizon")
        if not (
            self.config.minimum_slant_range_m <= camera_slant <= self.config.maximum_slant_range_m
        ):
            return self._invalid(base, "camera_ground_intersection_out_of_range")
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
                reasons.append(_direct_rejection_reason(measurement.source, "stale_or_from_future"))
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
        if range_fusion.sigma_inflated:
            reasons.append("absolute_range_uncertainty_inflated")

        slant_range_m = range_fusion.value_m
        north_offset_m = camera_offset_ned[0] + slant_range_m * ray_ned[0]
        east_offset_m = camera_offset_ned[1] + slant_range_m * ray_ned[1]
        ground_range_m = math.hypot(north_offset_m, east_offset_m)
        relative_bearing_deg = _wrap_signed_degrees(
            math.degrees(math.atan2(ray_body[1], ray_body[0]))
        )
        absolute_bearing_deg = _wrap_unsigned_degrees(
            math.degrees(math.atan2(east_offset_m, north_offset_m))
        )
        bearing_sigma_deg = _bearing_sigma_from_ray_uncertainty(
            calibration=calibration,
            pose=pose,
            target=target,
            ray_uncertainty=ray_uncertainty,
        )
        if bearing_sigma_deg is None:
            return self._invalid(base, "target_ray_uncertainty_unavailable")
        camera_offset_sigma = calibration.camera_offset_sigma_m
        if camera_offset_sigma > 0.0:
            if ground_range_m > 1.0e-9:
                lever_arm_bearing_sigma_deg = math.degrees(camera_offset_sigma / ground_range_m)
                if lever_arm_bearing_sigma_deg > 90.0:
                    return self._invalid(base, "camera_offset_uncertainty_unbounded")
                bearing_sigma_deg = math.hypot(
                    bearing_sigma_deg,
                    lever_arm_bearing_sigma_deg,
                )
        slant_ci = _ci95(slant_range_m, range_fusion.sigma_m)
        ground_sigma = self._ground_range_sigma(
            ray_uncertainty=ray_uncertainty,
            slant_range_m=slant_range_m,
            range_sigma_m=range_fusion.sigma_m,
            camera_offset_ned=camera_offset_ned,
            camera_offset_sigma_m=calibration.camera_offset_sigma_m,
        )
        if ground_sigma is None:
            return self._invalid(base, "target_ray_uncertainty_crosses_horizon")
        ground_ci = _ci95(ground_range_m, ground_sigma)
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
        if (
            len(range_fusion.accepted) < 2
            or range_fusion.rejected
            or range_fusion.sigma_inflated
            or vertical_fusion.rejected
        ):
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
        if _has_duplicate_sources(measurement.source for measurement in direct_measurements):
            return self._invalid(base, "duplicate_direct_range_source")
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
        ray_uncertainty = _build_ray_uncertainty(calibration, pose, target)
        if ray_uncertainty is None:
            return self._invalid(base, "target_ray_uncertainty_unavailable")
        ray_body, ray_ned = ray_uncertainty.base_body, ray_uncertainty.base_ned
        if ray_ned[2] < self.config.minimum_downward_ray_component:
            return self._invalid(base, "target_ray_does_not_intersect_ground_safely")
        slant_range_m = fusion.value_m
        camera_offset_ned = _camera_offset_ned(calibration, pose)
        north_offset_m = camera_offset_ned[0] + slant_range_m * ray_ned[0]
        east_offset_m = camera_offset_ned[1] + slant_range_m * ray_ned[1]
        ground_range_m = math.hypot(north_offset_m, east_offset_m)
        ground_sigma = self._ground_range_sigma(
            ray_uncertainty=ray_uncertainty,
            slant_range_m=slant_range_m,
            range_sigma_m=fusion.sigma_m,
            camera_offset_ned=camera_offset_ned,
            camera_offset_sigma_m=calibration.camera_offset_sigma_m,
        )
        if ground_sigma is None:
            return self._invalid(base, "target_ray_uncertainty_crosses_horizon")
        bearing_sigma_deg = _bearing_sigma_from_ray_uncertainty(
            calibration=calibration,
            pose=pose,
            target=target,
            ray_uncertainty=ray_uncertainty,
        )
        if bearing_sigma_deg is None:
            return self._invalid(base, "target_ray_uncertainty_unavailable")
        camera_offset_sigma = calibration.camera_offset_sigma_m
        if camera_offset_sigma > 0.0:
            if ground_range_m > 1.0e-9:
                lever_arm_bearing_sigma_deg = math.degrees(camera_offset_sigma / ground_range_m)
                if lever_arm_bearing_sigma_deg > 90.0:
                    return self._invalid(base, "camera_offset_uncertainty_unbounded")
                bearing_sigma_deg = math.hypot(
                    bearing_sigma_deg,
                    lever_arm_bearing_sigma_deg,
                )
        return RangeSolution(
            **base,
            validity=RangeValidity.DEGRADED,
            reasons=tuple(
                dict.fromkeys(
                    (
                        "direct_degraded_metric_range",
                        "vertical_reference_unavailable",
                        *(("direct_range_uncertainty_inflated",) if fusion.sigma_inflated else ()),
                    )
                )
            ),
            sources=tuple(c.source for c in fusion.accepted),
            rejected_sources=tuple(c.source for c in fusion.rejected),
            slant_range_m=slant_range_m,
            ground_range_m=ground_range_m,
            slant_range_ci95_m=_ci95(slant_range_m, fusion.sigma_m),
            ground_range_ci95_m=_ci95(ground_range_m, ground_sigma),
            relative_bearing_deg=_wrap_signed_degrees(
                math.degrees(math.atan2(ray_body[1], ray_body[0]))
            ),
            absolute_bearing_deg=_wrap_unsigned_degrees(
                math.degrees(math.atan2(east_offset_m, north_offset_m))
            ),
            bearing_sigma_deg=bearing_sigma_deg,
            north_offset_m=north_offset_m,
            east_offset_m=east_offset_m,
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
        ray_uncertainty: _RayUncertainty | None = None,
    ) -> float | None:
        uncertainty = ray_uncertainty or _build_ray_uncertainty(calibration, pose, target)
        if uncertainty is None:
            return None
        base_ray = uncertainty.base_ned
        if base_ray[2] <= 0.0:
            return None
        components = [height_sigma_m / base_ray[2]]
        for plus_ray, minus_ray in uncertainty.perturbations:
            # A skipped endpoint used to make near-horizon estimates look more
            # certain than they are.  Once a one-sigma ray crosses the horizon
            # the ground intersection is unbounded, so publish no distance.
            if plus_ray[2] <= 0.0 or minus_ray[2] <= 0.0:
                return None
            delta = abs(height_m / plus_ray[2] - height_m / minus_ray[2]) / 2.0
            components.append(delta)
        sigma = math.sqrt(sum(component * component for component in components))
        return sigma if math.isfinite(sigma) else None

    def _terrain_range_sigma(
        self,
        *,
        calibration: CameraCalibration,
        terrain: DemGpsTerrainObservation,
        height_m: float,
        height_sigma_m: float,
        camera_offset_ned: tuple[float, float, float],
        ray_uncertainty: _RayUncertainty,
    ) -> float | None:
        """Propagate GPS/DEM, pose, lever-arm and slope uncertainty by endpoints."""

        grid = terrain.grid

        def evaluate(
            *,
            height: float = height_m,
            offset: tuple[float, float, float] = camera_offset_ned,
            ray: tuple[float, float, float] = ray_uncertainty.base_ned,
            reference_north: float = terrain.reference_north_m,
            reference_east: float = terrain.reference_east_m,
            slope_bias_north: float = 0.0,
            slope_bias_east: float = 0.0,
        ) -> float | None:
            result = grid.intersect_ray(
                reference_north_m=reference_north,
                reference_east_m=reference_east,
                height_m=height,
                camera_offset_ned=offset,
                ray_ned=ray,
                maximum_slant_range_m=self.config.maximum_slant_range_m,
                minimum_incidence_component=self.config.minimum_downward_ray_component,
                slope_bias_north_m_per_m=slope_bias_north,
                slope_bias_east_m_per_m=slope_bias_east,
            )
            if result is None or not (
                self.config.minimum_slant_range_m
                <= result.slant_range_m
                <= self.config.maximum_slant_range_m
            ):
                return None
            return result.slant_range_m

        components: list[float] = []

        def add_pair(plus: float | None, minus: float | None) -> bool:
            if plus is None or minus is None:
                return False
            component = abs(plus - minus) / 2.0
            if not math.isfinite(component):
                return False
            components.append(component)
            return True

        if not add_pair(
            evaluate(height=height_m + height_sigma_m),
            evaluate(height=height_m - height_sigma_m),
        ):
            return None
        for plus_ray, minus_ray in ray_uncertainty.perturbations:
            if not add_pair(evaluate(ray=plus_ray), evaluate(ray=minus_ray)):
                return None
        offset_sigma = calibration.camera_offset_sigma_m
        if offset_sigma > 0.0:
            for axis in range(3):
                plus_offset = list(camera_offset_ned)
                minus_offset = list(camera_offset_ned)
                plus_offset[axis] += offset_sigma
                minus_offset[axis] -= offset_sigma
                if not add_pair(
                    evaluate(offset=tuple(plus_offset)),
                    evaluate(offset=tuple(minus_offset)),
                ):
                    return None
        horizontal_sigma = terrain.horizontal_sigma_m
        if horizontal_sigma > 0.0:
            if not add_pair(
                evaluate(reference_north=terrain.reference_north_m + horizontal_sigma),
                evaluate(reference_north=terrain.reference_north_m - horizontal_sigma),
            ):
                return None
            if not add_pair(
                evaluate(reference_east=terrain.reference_east_m + horizontal_sigma),
                evaluate(reference_east=terrain.reference_east_m - horizontal_sigma),
            ):
                return None
        slope_sigma = grid.slope_sigma_m_per_m
        if slope_sigma > 0.0:
            if not add_pair(
                evaluate(slope_bias_north=slope_sigma),
                evaluate(slope_bias_north=-slope_sigma),
            ):
                return None
            if not add_pair(
                evaluate(slope_bias_east=slope_sigma),
                evaluate(slope_bias_east=-slope_sigma),
            ):
                return None
        sigma = math.sqrt(sum(component * component for component in components))
        return sigma if math.isfinite(sigma) and sigma > 0.0 else None

    @staticmethod
    def _ground_range_sigma(
        *,
        ray_uncertainty: _RayUncertainty,
        slant_range_m: float,
        range_sigma_m: float,
        camera_offset_ned: tuple[float, float, float] = (0.0, 0.0, 0.0),
        camera_offset_sigma_m: float = 0.0,
    ) -> float | None:
        """Propagate range and calibrated line-of-sight uncertainty to ground range."""

        if (
            not all(
                math.isfinite(value) and value >= 0.0 for value in (slant_range_m, range_sigma_m)
            )
            or slant_range_m <= 0.0
            or range_sigma_m <= 0.0
        ):
            return None
        if (
            len(camera_offset_ned) != 3
            or not all(math.isfinite(value) for value in camera_offset_ned)
            or not math.isfinite(camera_offset_sigma_m)
            or camera_offset_sigma_m < 0.0
        ):
            return None
        base_ray = ray_uncertainty.base_ned
        if base_ray[2] <= 0.0:
            return None
        horizontal_fraction = math.hypot(base_ray[0], base_ray[1])
        components = [range_sigma_m * horizontal_fraction]
        if camera_offset_sigma_m > 0.0:
            # The calibration exposes one isotropic survey sigma for the
            # three translation components.  Add it as a radial horizontal
            # term so an uncertain lever arm can only widen, never shrink,
            # the published confidence interval.
            components.append(camera_offset_sigma_m)
        for plus_ray, minus_ray in ray_uncertainty.perturbations:
            if plus_ray[2] <= 0.0 or minus_ray[2] <= 0.0:
                return None
            plus_ground = math.hypot(
                camera_offset_ned[0] + slant_range_m * plus_ray[0],
                camera_offset_ned[1] + slant_range_m * plus_ray[1],
            )
            minus_ground = math.hypot(
                camera_offset_ned[0] + slant_range_m * minus_ray[0],
                camera_offset_ned[1] + slant_range_m * minus_ray[1],
            )
            components.append(abs(plus_ground - minus_ground) / 2.0)
        sigma = math.sqrt(sum(component * component for component in components))
        return sigma if math.isfinite(sigma) else None

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
        document: Any = strict_json_loads(
            calibration_path.read_bytes(),
            name="camera calibration document",
        )
    except (OSError, AutonomyContractError, TypeError) as exc:
        raise ValueError(
            f"camera calibration could not be loaded: {calibration_path}: {exc}"
        ) from exc
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


def _camera_offset_ned(
    calibration: CameraCalibration,
    pose: AircraftPose,
) -> tuple[float, float, float]:
    """Rotate the surveyed camera-to-navigation lever arm into NED.

    The calibration vector is defined in body NED (forward/right/down), while
    target offsets and WGS-84 projection are expressed in navigation NED.  A
    zero vector is the backwards-compatible result for legacy calibrations.
    """

    offset_body = (
        calibration.camera_offset_forward_m,
        calibration.camera_offset_right_m,
        calibration.camera_offset_down_m,
    )
    offset_ned = _matvec(
        _rotation_matrix(pose.roll_deg, pose.pitch_deg, pose.heading_deg),
        offset_body,
    )
    if not all(math.isfinite(value) for value in offset_ned):
        raise ValueError("camera offset rotation returned non-finite NED values")
    return offset_ned


def _build_ray_uncertainty(
    calibration: CameraCalibration,
    pose: AircraftPose,
    target: TargetImageObservation,
) -> _RayUncertainty | None:
    """Build deterministic one-sigma NED ray perturbations.

    Pixel, attitude and all three mount-axis uncertainties are sampled one at
    a time.  A malformed endpoint (for example a center sigma that leaves the
    image domain) makes the geometry unavailable instead of silently shrinking
    the reported error budget.
    """

    try:
        base_body, base_ned = _target_rays(calibration, pose, target)
    except ValueError:
        return None
    perturbations: tuple[tuple[object, str, float, str | None], ...] = (
        (pose, "roll_deg", pose.roll_sigma_deg, "attitude"),
        (pose, "pitch_deg", pose.pitch_sigma_deg, "attitude"),
        (
            calibration,
            "mount_pitch_down_deg",
            calibration.boresight_sigma_deg,
            "boresight",
        ),
        (
            calibration,
            "mount_yaw_right_deg",
            calibration.boresight_sigma_deg,
            "boresight",
        ),
        (
            calibration,
            "mount_roll_clockwise_deg",
            calibration.boresight_sigma_deg,
            "boresight",
        ),
        (target, "center_x", target.center_sigma_px / calibration.width_px, None),
        (target, "center_y", target.center_sigma_px / calibration.height_px, None),
    )
    endpoints: list[tuple[_Ray, _Ray]] = []
    attitude_bearing_components: list[float] = []
    boresight_bearing_components: list[float] = []
    for instance, field_name, sigma, bearing_group in perturbations:
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
        except (TypeError, ValueError):
            return None
        endpoints.append((plus_ray, minus_ray))
        if bearing_group is not None:
            component = _bearing_sigma_from_ray_pair(plus_ray, minus_ray)
            if component is None:
                return None
            if bearing_group == "attitude":
                attitude_bearing_components.append(component)
            else:
                boresight_bearing_components.append(component)
    return _RayUncertainty(
        base_body,
        base_ned,
        tuple(endpoints),
        math.hypot(*attitude_bearing_components) if attitude_bearing_components else 0.0,
        math.hypot(*boresight_bearing_components) if boresight_bearing_components else 0.0,
    )


def _bearing_sigma_from_ray_uncertainty(
    *,
    calibration: CameraCalibration,
    pose: AircraftPose,
    target: TargetImageObservation,
    ray_uncertainty: _RayUncertainty,
) -> float | None:
    """Return the absolute-bearing one-sigma budget for a calibrated ray.

    Heading and image-centre terms remain independent inputs for wire/backwards
    compatibility.  Pose roll/pitch and mount pitch/yaw/roll terms come from
    the same NED endpoint perturbations used by ground-range propagation, so a
    banked fixed-wing frame cannot publish a deceptively narrow geolocation
    circle simply because its horizontal bearing changed through attitude or
    mount-survey uncertainty.
    """

    pixel_sigma_deg = math.degrees(target.center_sigma_px / max(calibration.fx_px, 1.0))
    components = (
        pose.heading_sigma_deg,
        pixel_sigma_deg,
        ray_uncertainty.attitude_bearing_sigma_deg,
        ray_uncertainty.boresight_bearing_sigma_deg,
    )
    if not all(math.isfinite(value) and value >= 0.0 for value in components):
        return None
    sigma = math.hypot(*components)
    return sigma if math.isfinite(sigma) else None


def _bearing_sigma_from_ray_pair(plus_ray: _Ray, minus_ray: _Ray) -> float | None:
    """Estimate one angular sigma from plus/minus NED ray endpoints."""

    plus_horizontal = math.hypot(plus_ray[0], plus_ray[1])
    minus_horizontal = math.hypot(minus_ray[0], minus_ray[1])
    if (
        not math.isfinite(plus_horizontal)
        or not math.isfinite(minus_horizontal)
        or plus_horizontal <= 1.0e-12
        or minus_horizontal <= 1.0e-12
    ):
        return None
    plus_bearing_deg = math.degrees(math.atan2(plus_ray[1], plus_ray[0]))
    minus_bearing_deg = math.degrees(math.atan2(minus_ray[1], minus_ray[0]))
    delta_deg = abs(_wrap_signed_degrees(plus_bearing_deg - minus_bearing_deg)) / 2.0
    return delta_deg if math.isfinite(delta_deg) else None


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
    sigma_inflated = False
    if len(accepted) > 1:
        # The hard pairwise gate answers “are these measurements plausibly the
        # same quantity?”.  It does not answer “are the declared sigmas large
        # enough for the observed spread?”.  Use the weighted residual
        # chi-square (Birge ratio) to conservatively widen the fused sigma when
        # the latter is false.  This prevents a camera/laser pair that is just
        # inside the gate from producing an unrealistically tiny geolocation
        # circle.  One accepted source keeps the historical sigma unchanged.
        residual_chi2 = math.fsum(
            ((candidate.value_m - value_m) / max(candidate.sigma_m, minimum_sigma)) ** 2
            for candidate in accepted
        )
        degrees_of_freedom = len(accepted) - 1
        if (
            degrees_of_freedom > 0
            and math.isfinite(residual_chi2)
            and residual_chi2 > degrees_of_freedom
        ):
            sigma_m *= math.sqrt(residual_chi2 / degrees_of_freedom)
            sigma_inflated = True
    max_z = _maximum_pairwise_z(accepted)
    consistency = 0.5 if len(accepted) == 1 else math.exp(-0.5 * (max_z / gate_sigma) ** 2)
    return _Fusion(
        value_m,
        sigma_m,
        tuple(accepted),
        rejected,
        consistency,
        sigma_inflated,
    )


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

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .config import FixedWingReleaseWindowConfig
from .domain import (
    BoundingBox,
    DeploymentWindowSolution,
    DeploymentWindowStatus,
    FrameObservation,
    ReleaseTimingStatus,
    TrackSnapshot,
    VehicleTelemetry,
)
from .multimodal_ranging import RangeSolution, RangeValidity


@dataclass(frozen=True, slots=True)
class PrimaryRangeEvidence:
    """Bind a range solution to the exact unified target and source frame."""

    source_target_id: str
    source_frame_id: str
    source_captured_at_s: float
    source_label: str
    source_bbox: BoundingBox
    solution: RangeSolution

    def __post_init__(self) -> None:
        if not self.source_target_id.strip() or not self.source_frame_id.strip():
            raise ValueError("range-evidence identifiers cannot be empty")
        if not math.isfinite(self.source_captured_at_s) or self.source_captured_at_s < 0.0:
            raise ValueError("range-evidence capture timestamp is invalid")
        normalized_label = self.source_label.strip().lower()
        if not normalized_label:
            raise ValueError("range-evidence label cannot be empty")
        if self.solution.target_id != self.source_target_id:
            raise ValueError("range solution is not bound to the source target")
        if self.solution.frame_id != self.source_frame_id:
            raise ValueError("range solution is not bound to the source frame")
        object.__setattr__(self, "source_label", normalized_label)


@dataclass(frozen=True, slots=True)
class _Impact:
    north_m: float
    east_m: float
    descent_time_s: float


class FixedWingReleaseWindowPlanner:
    """Compute a read-only Mode 2 HIL release window.

    When ``require_multimodal_range`` is enabled, the solver requires a fresh,
    identity-bound VALID range solution plus synchronized ground velocity,
    airspeed and wind. A quadratic-drag point-mass model estimates the impact
    point and a finite-difference covariance produces a 95% error ellipse.
    The class has no actuator, MAVLink transmit, servo or payload interface.
    """

    _PROHIBITED_TARGET_LABELS = frozenset({"person", "firefighter", "vehicle", "car"})

    def __init__(
        self,
        config: FixedWingReleaseWindowConfig,
        *,
        allowed_target_labels: Iterable[str],
    ) -> None:
        self._config = config
        self._allowed_target_labels = frozenset(
            label.strip().lower() for label in allowed_target_labels
        )

    def plan(
        self,
        *,
        track: TrackSnapshot,
        frame: FrameObservation,
        now_s: float,
        ranging_evidence: PrimaryRangeEvidence | None = None,
    ) -> DeploymentWindowSolution:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("now_s must be a finite non-negative number")
        label = track.label.strip().lower()
        if label in self._PROHIBITED_TARGET_LABELS or label not in self._allowed_target_labels:
            return self._unavailable(track, now_s, "target_class_not_eligible")
        if self._config.require_multimodal_range:
            return self._plan_multimodal(
                track=track,
                frame=frame,
                now_s=now_s,
                evidence=ranging_evidence,
            )
        return self._plan_legacy_projection(track=track, frame=frame, now_s=now_s)

    def _plan_multimodal(
        self,
        *,
        track: TrackSnapshot,
        frame: FrameObservation,
        now_s: float,
        evidence: PrimaryRangeEvidence | None,
    ) -> DeploymentWindowSolution:
        if evidence is None:
            return self._unavailable(track, now_s, "multimodal_range_evidence_unavailable")
        if evidence.source_label != track.label.strip().lower():
            return self._unavailable(track, now_s, "range_target_class_mismatch", evidence)
        if track.bbox.iou(evidence.source_bbox) < self._config.minimum_range_target_iou:
            return self._unavailable(track, now_s, "range_target_spatial_binding_failed", evidence)
        solution = evidence.solution
        solution_age_s = now_s - solution.evaluated_at_s
        source_age_s = now_s - evidence.source_captured_at_s
        if (
            solution_age_s < 0.0
            or source_age_s < 0.0
            or solution_age_s > self._config.maximum_range_age_s
            or source_age_s > self._config.maximum_range_age_s
        ):
            return self._unavailable(track, now_s, "multimodal_range_evidence_stale", evidence)
        if solution.validity is not RangeValidity.VALID:
            return self._unavailable(track, now_s, "multimodal_range_not_valid", evidence)
        if solution.sensor_consistency < self._config.minimum_range_sensor_consistency:
            return self._unavailable(track, now_s, "multimodal_range_consistency_too_low", evidence)
        if solution.data_freshness_s is None or (
            solution.data_freshness_s > self._config.maximum_range_age_s
        ):
            return self._unavailable(track, now_s, "multimodal_range_freshness_invalid", evidence)
        if any(
            value is None
            for value in (
                solution.ground_range_m,
                solution.ground_range_ci95_m,
                solution.relative_bearing_deg,
                solution.bearing_sigma_deg,
                solution.north_offset_m,
                solution.east_offset_m,
            )
        ):
            return self._unavailable(track, now_s, "multimodal_range_geometry_incomplete", evidence)

        telemetry = frame.telemetry
        telemetry_reason = self._validate_ballistic_telemetry(telemetry, now_s)
        if telemetry_reason is not None:
            return self._unavailable(track, now_s, telemetry_reason, evidence)
        air_relative_speed = math.hypot(
            telemetry.velocity_north_mps - telemetry.wind_north_mps,
            telemetry.velocity_east_mps - telemetry.wind_east_mps,
        )
        if abs(air_relative_speed - telemetry.airspeed_mps) > (
            self._config.maximum_air_data_disagreement_mps
        ):
            return self._unavailable(
                track,
                now_s,
                "airspeed_groundspeed_wind_inconsistent",
                evidence,
            )

        impact = self._simulate_impact(
            telemetry=telemetry,
            mass_kg=self._config.payload_mass_kg,
            drag_coefficient=self._config.drag_coefficient,
            wind_north_mps=telemetry.wind_north_mps,
            wind_east_mps=telemetry.wind_east_mps,
            velocity_north_mps=telemetry.velocity_north_mps,
            velocity_east_mps=telemetry.velocity_east_mps,
            altitude_m=telemetry.altitude_agl_m,
            release_latency_s=self._config.command_to_release_latency_seconds,
        )
        if impact is None:
            return self._unavailable(track, now_s, "ballistic_integration_failed", evidence)

        target_north = float(solution.north_offset_m)
        target_east = float(solution.east_offset_m)
        error_north = target_north - impact.north_m
        error_east = target_east - impact.east_m
        heading_rad = math.radians(telemetry.heading_deg)
        forward_north = math.cos(heading_rad)
        forward_east = math.sin(heading_rad)
        right_north = -forward_east
        right_east = forward_north
        along_error = error_north * forward_north + error_east * forward_east
        cross_error = error_north * right_north + error_east * right_east

        covariance = self._combined_error_covariance(
            telemetry=telemetry,
            solution=solution,
            baseline=impact,
        )
        ellipse_major, ellipse_minor, ellipse_orientation = _ellipse95(covariance)
        common = {
            "target_id": track.track_id,
            "target_revision": track.revision,
            "calibration_id": self._config.calibration_id,
            "evaluated_at_s": now_s,
            "relative_bearing_deg": solution.relative_bearing_deg,
            "estimated_ground_range_m": solution.ground_range_m,
            "cross_track_error_m": cross_error,
            "along_track_error_m": along_error,
            "payload_descent_time_s": impact.descent_time_s,
            "release_lead_distance_m": math.hypot(impact.north_m, impact.east_m),
            "target_north_offset_m": target_north,
            "target_east_offset_m": target_east,
            "impact_north_offset_m": impact.north_m,
            "impact_east_offset_m": impact.east_m,
            "error_ellipse_major_m": ellipse_major,
            "error_ellipse_minor_m": ellipse_minor,
            "error_ellipse_orientation_deg": ellipse_orientation,
            "ground_range_ci95_m": solution.ground_range_ci95_m,
            "range_target_id": evidence.source_target_id,
            "range_frame_id": evidence.source_frame_id,
            "range_sensor_consistency": solution.sensor_consistency,
        }
        if (
            ellipse_major > self._config.maximum_error_ellipse_major_m
            or ellipse_minor > self._config.maximum_error_ellipse_minor_m
        ):
            return DeploymentWindowSolution(
                status=DeploymentWindowStatus.UNAVAILABLE,
                timing_status=ReleaseTimingStatus.INVALID,
                reasons=("impact_uncertainty_exceeds_limit",),
                **common,
            )
        if abs(cross_error) > self._config.maximum_cross_track_error_m:
            return DeploymentWindowSolution(
                status=DeploymentWindowStatus.WAIT,
                timing_status=ReleaseTimingStatus.INVALID,
                reasons=("target_outside_cross_track_corridor",),
                **common,
            )
        if along_error > self._config.release_window_half_length_m:
            return DeploymentWindowSolution(
                status=DeploymentWindowStatus.WAIT,
                timing_status=ReleaseTimingStatus.TOO_EARLY,
                reasons=("before_release_window",),
                **common,
            )
        if along_error < -self._config.release_window_half_length_m:
            return DeploymentWindowSolution(
                status=DeploymentWindowStatus.WAIT,
                timing_status=ReleaseTimingStatus.TOO_LATE,
                reasons=("release_window_passed",),
                **common,
            )
        return DeploymentWindowSolution(
            status=DeploymentWindowStatus.READY,
            timing_status=ReleaseTimingStatus.WINDOW,
            reasons=("multimodal_release_window_ready",),
            **common,
        )

    def _validate_ballistic_telemetry(
        self,
        telemetry: VehicleTelemetry,
        now_s: float,
    ) -> str | None:
        required = (
            telemetry.altitude_agl_m,
            telemetry.heading_deg,
            telemetry.velocity_north_mps,
            telemetry.velocity_east_mps,
            telemetry.airspeed_mps,
            telemetry.wind_north_mps,
            telemetry.wind_east_mps,
            telemetry.velocity_observed_at_s,
            telemetry.airspeed_observed_at_s,
            telemetry.wind_observed_at_s,
        )
        if not all(math.isfinite(value) for value in required):
            return "ballistic_telemetry_unavailable"
        if telemetry.altitude_agl_m <= 0.0 or telemetry.airspeed_mps < 0.0:
            return "ballistic_telemetry_out_of_domain"
        for timestamp in (
            telemetry.velocity_observed_at_s,
            telemetry.airspeed_observed_at_s,
            telemetry.wind_observed_at_s,
        ):
            age_s = now_s - timestamp
            if age_s < 0.0 or age_s > self._config.maximum_range_age_s:
                return "ballistic_telemetry_stale_or_from_future"
        return None

    def _simulate_impact(
        self,
        *,
        telemetry: VehicleTelemetry,
        mass_kg: float,
        drag_coefficient: float,
        wind_north_mps: float,
        wind_east_mps: float,
        velocity_north_mps: float,
        velocity_east_mps: float,
        altitude_m: float,
        release_latency_s: float,
    ) -> _Impact | None:
        if mass_kg <= 0.0 or drag_coefficient <= 0.0 or altitude_m <= 0.0:
            return None
        north = velocity_north_mps * release_latency_s
        east = velocity_east_mps * release_latency_s
        down = 0.0
        down_velocity = 0.0
        elapsed = 0.0
        step = self._config.integration_step_seconds
        drag_factor = (
            0.5
            * self._config.air_density_kg_m3
            * drag_coefficient
            * self._config.reference_area_m2
            / mass_kg
        )
        while down < altitude_m and elapsed < self._config.maximum_flight_time_seconds:
            relative_north = velocity_north_mps - wind_north_mps
            relative_east = velocity_east_mps - wind_east_mps
            relative_speed = math.sqrt(
                relative_north * relative_north
                + relative_east * relative_east
                + down_velocity * down_velocity
            )
            velocity_north_mps += -drag_factor * relative_speed * relative_north * step
            velocity_east_mps += -drag_factor * relative_speed * relative_east * step
            down_velocity += (
                self._config.gravity_mps2 - drag_factor * relative_speed * down_velocity
            ) * step
            north += velocity_north_mps * step
            east += velocity_east_mps * step
            down += down_velocity * step
            elapsed += step
        if down < altitude_m or not all(
            math.isfinite(value) for value in (north, east, elapsed, down_velocity)
        ):
            return None
        return _Impact(north_m=north, east_m=east, descent_time_s=elapsed)

    def _combined_error_covariance(
        self,
        *,
        telemetry: VehicleTelemetry,
        solution: RangeSolution,
        baseline: _Impact,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        range_low, range_high = solution.ground_range_ci95_m or (0.0, 0.0)
        range_sigma = max(0.0, range_high - range_low) / 3.92
        bearing_sigma_rad = math.radians(solution.bearing_sigma_deg or 0.0)
        bearing_rad = math.atan2(float(solution.east_offset_m), float(solution.north_offset_m))
        radial_north = math.cos(bearing_rad)
        radial_east = math.sin(bearing_rad)
        tangent_north = -radial_east
        tangent_east = radial_north
        tangent_sigma = float(solution.ground_range_m) * bearing_sigma_rad
        cov_nn = (range_sigma * radial_north) ** 2 + (tangent_sigma * tangent_north) ** 2
        cov_ee = (range_sigma * radial_east) ** 2 + (tangent_sigma * tangent_east) ** 2
        cov_ne = (
            range_sigma * range_sigma * radial_north * radial_east
            + tangent_sigma * tangent_sigma * tangent_north * tangent_east
        )

        perturbations = (
            {"mass_kg": self._config.payload_mass_kg + self._config.payload_mass_sigma_kg},
            {
                "drag_coefficient": self._config.drag_coefficient
                + self._config.drag_coefficient_sigma
            },
            {"wind_north_mps": telemetry.wind_north_mps + self._config.wind_sigma_mps},
            {"wind_east_mps": telemetry.wind_east_mps + self._config.wind_sigma_mps},
            {
                "velocity_north_mps": telemetry.velocity_north_mps
                + self._config.ground_velocity_sigma_mps
            },
            {
                "velocity_east_mps": telemetry.velocity_east_mps
                + self._config.ground_velocity_sigma_mps
            },
            {"altitude_m": telemetry.altitude_agl_m + self._config.altitude_sigma_m},
            {
                "release_latency_s": self._config.command_to_release_latency_seconds
                + self._config.release_latency_sigma_s
            },
        )
        base_parameters = {
            "mass_kg": self._config.payload_mass_kg,
            "drag_coefficient": self._config.drag_coefficient,
            "wind_north_mps": telemetry.wind_north_mps,
            "wind_east_mps": telemetry.wind_east_mps,
            "velocity_north_mps": telemetry.velocity_north_mps,
            "velocity_east_mps": telemetry.velocity_east_mps,
            "altitude_m": telemetry.altitude_agl_m,
            "release_latency_s": self._config.command_to_release_latency_seconds,
        }
        for change in perturbations:
            if next(iter(change.values())) == base_parameters[next(iter(change))]:
                continue
            candidate = self._simulate_impact(
                telemetry=telemetry,
                **(base_parameters | change),
            )
            if candidate is None:
                continue
            delta_north = candidate.north_m - baseline.north_m
            delta_east = candidate.east_m - baseline.east_m
            cov_nn += delta_north * delta_north
            cov_ee += delta_east * delta_east
            cov_ne += delta_north * delta_east
        return ((cov_nn, cov_ne), (cov_ne, cov_ee))

    def _plan_legacy_projection(
        self,
        *,
        track: TrackSnapshot,
        frame: FrameObservation,
        now_s: float,
    ) -> DeploymentWindowSolution:
        telemetry = frame.telemetry
        required = (
            telemetry.altitude_agl_m,
            telemetry.ground_speed_mps,
            telemetry.pitch_deg,
        )
        if not all(math.isfinite(value) for value in required):
            return self._unavailable(track, now_s, "required_telemetry_unavailable")
        if telemetry.altitude_agl_m <= 0.0 or telemetry.ground_speed_mps < 0.0:
            return self._unavailable(track, now_s, "required_telemetry_out_of_domain")

        center_x, center_y = track.bbox.center
        relative_bearing_deg = (center_x - 0.5) * self._config.camera_horizontal_fov_deg
        depression_angle_deg = (
            self._config.camera_mount_down_angle_deg
            - telemetry.pitch_deg
            + (center_y - 0.5) * self._config.camera_vertical_fov_deg
        )
        if not (
            self._config.minimum_depression_angle_deg
            <= depression_angle_deg
            <= self._config.maximum_depression_angle_deg
        ):
            return DeploymentWindowSolution(
                status=DeploymentWindowStatus.WAIT,
                timing_status=ReleaseTimingStatus.INVALID,
                target_id=track.track_id,
                target_revision=track.revision,
                calibration_id=self._config.calibration_id,
                evaluated_at_s=now_s,
                reasons=("target_outside_calibrated_ground_projection",),
                relative_bearing_deg=relative_bearing_deg,
                depression_angle_deg=depression_angle_deg,
            )

        depression_rad = math.radians(depression_angle_deg)
        bearing_rad = math.radians(relative_bearing_deg)
        ground_range_m = telemetry.altitude_agl_m / math.tan(depression_rad)
        along_track_range_m = ground_range_m * math.cos(bearing_rad)
        cross_track_error_m = ground_range_m * math.sin(bearing_rad)
        descent_time_s = self._config.payload_descent_time_factor * math.sqrt(
            2.0 * telemetry.altitude_agl_m / self._config.gravity_mps2
        )
        release_lead_distance_m = telemetry.ground_speed_mps * (
            descent_time_s + self._config.command_to_release_latency_seconds
        )
        along_track_error_m = along_track_range_m - release_lead_distance_m

        reasons: list[str] = []
        timing_status = ReleaseTimingStatus.INVALID
        if abs(cross_track_error_m) > self._config.maximum_cross_track_error_m:
            reasons.append("target_outside_cross_track_corridor")
        if along_track_error_m > self._config.release_window_half_length_m:
            reasons.append("before_release_window")
            timing_status = ReleaseTimingStatus.TOO_EARLY
        elif along_track_error_m < -self._config.release_window_half_length_m:
            reasons.append("release_window_passed")
            timing_status = ReleaseTimingStatus.TOO_LATE
        status = DeploymentWindowStatus.WAIT
        if not reasons:
            reasons.append("release_window_ready")
            status = DeploymentWindowStatus.READY
            timing_status = ReleaseTimingStatus.WINDOW

        return DeploymentWindowSolution(
            status=status,
            timing_status=timing_status,
            target_id=track.track_id,
            target_revision=track.revision,
            calibration_id=self._config.calibration_id,
            evaluated_at_s=now_s,
            reasons=tuple(reasons),
            relative_bearing_deg=relative_bearing_deg,
            depression_angle_deg=depression_angle_deg,
            estimated_ground_range_m=ground_range_m,
            cross_track_error_m=cross_track_error_m,
            along_track_error_m=along_track_error_m,
            payload_descent_time_s=descent_time_s,
            release_lead_distance_m=release_lead_distance_m,
        )

    def _unavailable(
        self,
        track: TrackSnapshot,
        now_s: float,
        reason: str,
        evidence: PrimaryRangeEvidence | None = None,
    ) -> DeploymentWindowSolution:
        return DeploymentWindowSolution(
            status=DeploymentWindowStatus.UNAVAILABLE,
            timing_status=ReleaseTimingStatus.INVALID,
            target_id=track.track_id,
            target_revision=track.revision,
            calibration_id=self._config.calibration_id,
            evaluated_at_s=now_s,
            reasons=(reason,),
            range_target_id=(evidence.source_target_id if evidence is not None else None),
            range_frame_id=(evidence.source_frame_id if evidence is not None else None),
            range_sensor_consistency=(
                evidence.solution.sensor_consistency if evidence is not None else None
            ),
        )


def _ellipse95(
    covariance: tuple[tuple[float, float], tuple[float, float]],
) -> tuple[float, float, float]:
    a = max(0.0, covariance[0][0])
    b = covariance[0][1]
    d = max(0.0, covariance[1][1])
    discriminant = math.sqrt(max(0.0, (a - d) * (a - d) + 4.0 * b * b))
    major_variance = max(0.0, 0.5 * (a + d + discriminant))
    minor_variance = max(0.0, 0.5 * (a + d - discriminant))
    scale95 = math.sqrt(5.991464547)
    major = scale95 * math.sqrt(major_variance)
    minor = scale95 * math.sqrt(minor_variance)
    orientation = 0.5 * math.degrees(math.atan2(2.0 * b, a - d))
    return major, minor, orientation


__all__ = ["FixedWingReleaseWindowPlanner", "PrimaryRangeEvidence"]

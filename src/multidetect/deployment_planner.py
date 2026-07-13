from __future__ import annotations

import math
from collections.abc import Iterable

from .config import FixedWingReleaseWindowConfig
from .domain import (
    DeploymentWindowSolution,
    DeploymentWindowStatus,
    FrameObservation,
    TrackSnapshot,
)


class FixedWingReleaseWindowPlanner:
    """Compute a still-air HIL release window without issuing control commands.

    The projection deliberately stops at an advisory WAIT/READY result. It does
    not model wind, terrain, payload aerodynamics or aircraft control response,
    so it is not evidence for a physical release.
    """

    _PROHIBITED_TARGET_LABELS = frozenset({"person", "firefighter", "vehicle"})

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
    ) -> DeploymentWindowSolution:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("now_s must be a finite non-negative number")
        label = track.label.strip().lower()
        if label in self._PROHIBITED_TARGET_LABELS or label not in self._allowed_target_labels:
            return self._unavailable(track, now_s, "target_class_not_eligible")

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
        if abs(cross_track_error_m) > self._config.maximum_cross_track_error_m:
            reasons.append("target_outside_cross_track_corridor")
        if along_track_error_m > self._config.release_window_half_length_m:
            reasons.append("before_release_window")
        elif along_track_error_m < -self._config.release_window_half_length_m:
            reasons.append("release_window_passed")
        status = DeploymentWindowStatus.WAIT
        if not reasons:
            reasons.append("release_window_ready")
            status = DeploymentWindowStatus.READY

        return DeploymentWindowSolution(
            status=status,
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
    ) -> DeploymentWindowSolution:
        return DeploymentWindowSolution(
            status=DeploymentWindowStatus.UNAVAILABLE,
            target_id=track.track_id,
            target_revision=track.revision,
            calibration_id=self._config.calibration_id,
            evaluated_at_s=now_s,
            reasons=(reason,),
        )


__all__ = ["FixedWingReleaseWindowPlanner"]

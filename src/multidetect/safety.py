from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .config import MissionConfig
from .deployment_planner import FixedWingReleaseWindowPlanner, PrimaryRangeEvidence
from .domain import (
    DeploymentDecision,
    DeploymentWindowSolution,
    DeploymentWindowStatus,
    FrameObservation,
    RuleCheck,
    TrackSnapshot,
    Verdict,
)


def _canonical_float(value: float, digits: int = 5) -> float | str:
    """Return a deterministic JSON value, including for invalid sensor numbers."""

    if not math.isfinite(value):
        return "non-finite"
    return round(value, digits)


def _canonical_optional_float(value: float | None) -> float | str | None:
    return None if value is None else _canonical_float(value)


def _check(rule_id: str, condition: bool | None, pass_reason: str, deny_reason: str) -> RuleCheck:
    if condition is None:
        return RuleCheck(rule_id, Verdict.UNKNOWN, f"{deny_reason}: value is unavailable")
    if condition:
        return RuleCheck(rule_id, Verdict.PASS, pass_reason)
    return RuleCheck(rule_id, Verdict.DENY, deny_reason)


class SafetyRuleEngine:
    """Fail-closed, deny-overrides rules for a deployment recommendation.

    The engine is deliberately side-effect free.  It accepts an already tracked
    target and a synchronized observation and returns evidence only; it has no
    reference to a payload controller or flight-control system.
    """

    def __init__(self, config: MissionConfig) -> None:
        self._config = config
        self._release_window_planner = (
            FixedWingReleaseWindowPlanner(
                config.fixed_wing_release_window,
                allowed_target_labels=config.target_classes,
            )
            if config.fixed_wing_release_window is not None
            else None
        )

    @property
    def ruleset_version(self) -> str:
        return self._config.ruleset_version

    def evaluate(
        self,
        *,
        track: TrackSnapshot,
        frame: FrameObservation,
        now_s: float,
        ranging_evidence: PrimaryRangeEvidence | None = None,
    ) -> DeploymentDecision:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")

        checks: list[RuleCheck] = []
        checks.append(
            _check(
                "target.confirmed_track",
                track.confirmed,
                "target track is confirmed",
                "target track is not confirmed",
            )
        )

        normalized_label = track.label.strip().lower()
        target_classes = {label.strip().lower() for label in self._config.target_classes}
        checks.append(
            _check(
                "target.allowed_class",
                normalized_label in target_classes,
                "target class is allowed by the mission",
                f"target class {normalized_label!r} is not allowed by the mission",
            )
        )
        confidence_is_valid = math.isfinite(track.confidence_floor)
        checks.append(
            RuleCheck(
                "target.minimum_confidence",
                (
                    Verdict.UNKNOWN
                    if not confidence_is_valid
                    else (
                        Verdict.PASS
                        if track.confidence_floor >= self._config.minimum_confidence
                        else Verdict.DENY
                    )
                ),
                (
                    "target confidence is unavailable"
                    if not confidence_is_valid
                    else (
                        "target confidence meets the configured minimum"
                        if track.confidence_floor >= self._config.minimum_confidence
                        else "target confidence is below the configured minimum"
                    )
                ),
            )
        )

        checks.append(
            self._freshness_check("sensor.frame_freshness", frame.captured_at_s, now_s, "frame")
        )
        checks.append(
            self._freshness_check(
                "sensor.track_freshness", track.last_seen_at_s, now_s, "target track"
            )
        )

        telemetry = frame.telemetry
        checks.extend(
            (
                _check(
                    "navigation.allowed_zone",
                    telemetry.in_allowed_zone,
                    "vehicle is inside the allowed mission zone",
                    "vehicle is outside the allowed mission zone",
                ),
                _check(
                    "navigation.geofence_health",
                    telemetry.geofence_healthy,
                    "geofence status is healthy",
                    "geofence status is not healthy",
                ),
                _check(
                    "navigation.position_health",
                    telemetry.position_healthy,
                    "position estimate is healthy",
                    "position estimate is not healthy",
                ),
                _check(
                    "communications.link_health",
                    telemetry.link_healthy,
                    "operator link is healthy",
                    "operator link is not healthy",
                ),
                _check(
                    "flight.allowed_mode",
                    telemetry.flight_mode_allows_deploy,
                    "current flight mode permits deployment",
                    "current flight mode does not permit deployment",
                ),
                _check(
                    "deployment.release_zone_clear",
                    telemetry.release_zone_clear,
                    "release zone is reported clear",
                    "release zone is not clear",
                ),
            )
        )

        limits = self._config.safety
        checks.append(
            self._range_check(
                "flight.altitude",
                telemetry.altitude_agl_m,
                limits.minimum_altitude_agl_m,
                limits.maximum_altitude_agl_m,
                "altitude",
            )
        )
        checks.append(
            self._absolute_limit_check(
                "flight.roll",
                telemetry.roll_deg,
                limits.maximum_abs_roll_deg,
                "roll",
            )
        )
        checks.append(
            self._absolute_limit_check(
                "flight.pitch",
                telemetry.pitch_deg,
                limits.maximum_abs_pitch_deg,
                "pitch",
            )
        )
        checks.append(
            self._range_check(
                "flight.ground_speed",
                telemetry.ground_speed_mps,
                limits.minimum_ground_speed_mps,
                limits.maximum_ground_speed_mps,
                "ground speed",
            )
        )

        deployment_window = (
            self._release_window_planner.plan(
                track=track,
                frame=frame,
                now_s=now_s,
                ranging_evidence=ranging_evidence,
            )
            if self._release_window_planner is not None
            else None
        )
        if deployment_window is not None:
            checks.append(self._deployment_window_check(deployment_window))

        checks.append(
            _check(
                "sensor.person_detector_health",
                (
                    telemetry.person_detector_healthy
                    if self._config.person_exclusion_enabled
                    else True
                ),
                (
                    "person-safety detector is healthy"
                    if self._config.person_exclusion_enabled
                    else "person exclusion is disabled for this mission"
                ),
                "person-safety detector is not healthy",
            )
        )
        checks.append(self._person_exclusion_check(track=track, frame=frame))
        checks.append(
            _check(
                "sensor.independent_rgb_fire_consistency",
                (
                    track.independent_rgb_corroborated
                    if self._config.require_independent_rgb_corroboration
                    else True
                ),
                (
                    "an independent RGB fire verifier corroborates the target"
                    if self._config.require_independent_rgb_corroboration
                    else "independent RGB fire corroboration is not required for this mission"
                ),
                "the independent RGB fire verifier does not corroborate the target",
            )
        )
        if self._config.require_thermal_corroboration:
            checks.append(
                _check(
                    "sensor.thermal_consistency",
                    track.thermal_corroborated,
                    "thermal observation corroborates the target",
                    "thermal observation does not corroborate the target",
                )
            )

        immutable_checks = tuple(checks)
        allowed = bool(immutable_checks) and all(
            check.verdict is Verdict.PASS for check in immutable_checks
        )
        return DeploymentDecision(
            allowed=allowed,
            target_id=track.track_id,
            target_revision=track.revision,
            frame_id=frame.frame_id,
            scene_digest=self.scene_digest(
                track=track,
                frame=frame,
                deployment_window=deployment_window,
            ),
            ruleset_version=self._config.ruleset_version,
            evaluated_at_s=now_s,
            checks=immutable_checks,
            deployment_window=deployment_window,
        )

    def scene_digest(
        self,
        *,
        track: TrackSnapshot,
        frame: FrameObservation,
        deployment_window: DeploymentWindowSolution | None = None,
    ) -> str:
        """Hash safety-relevant state using a stable order and float quantization.

        Frame identifiers, wall-clock evaluation time and arbitrary detection
        metadata are intentionally excluded.  Reordering equivalent detections
        therefore cannot invalidate an authorization challenge.
        """

        detections: list[dict[str, Any]] = []
        for detection in frame.detections:
            detections.append(
                {
                    "bbox": detection.bbox.rounded(),
                    "confidence": _canonical_float(detection.confidence),
                    "label": detection.label.strip().lower(),
                    "model_version": detection.model_version,
                    "sensor": detection.sensor.value,
                }
            )
        detections.sort(
            key=lambda item: json.dumps(
                item, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
        )

        telemetry = frame.telemetry
        document = {
            "mission": {
                "id": self._config.mission_id,
                "ruleset": self._config.ruleset_version,
                "target_classes": sorted(
                    label.strip().lower() for label in self._config.target_classes
                ),
            },
            "target": {
                "bbox": track.bbox.rounded(),
                "confidence_floor": _canonical_float(track.confidence_floor),
                "confirmed": track.confirmed,
                "id": track.track_id,
                "label": track.label.strip().lower(),
                "revision": track.revision,
                "independent_rgb_corroborated": track.independent_rgb_corroborated,
                "thermal_corroborated": track.thermal_corroborated,
            },
            "telemetry": {
                "altitude_agl_m": _canonical_float(telemetry.altitude_agl_m),
                "flight_mode_allows_deploy": telemetry.flight_mode_allows_deploy,
                "geofence_healthy": telemetry.geofence_healthy,
                "ground_speed_mps": _canonical_float(telemetry.ground_speed_mps),
                "velocity_north_mps": _canonical_float(telemetry.velocity_north_mps),
                "velocity_east_mps": _canonical_float(telemetry.velocity_east_mps),
                "airspeed_mps": _canonical_float(telemetry.airspeed_mps),
                "wind_north_mps": _canonical_float(telemetry.wind_north_mps),
                "wind_east_mps": _canonical_float(telemetry.wind_east_mps),
                "in_allowed_zone": telemetry.in_allowed_zone,
                "link_healthy": telemetry.link_healthy,
                "pitch_deg": _canonical_float(telemetry.pitch_deg),
                "person_detector_healthy": telemetry.person_detector_healthy,
                "position_healthy": telemetry.position_healthy,
                "release_zone_clear": telemetry.release_zone_clear,
                "roll_deg": _canonical_float(telemetry.roll_deg),
            },
            "deployment_window": (
                None
                if deployment_window is None
                else {
                    "status": deployment_window.status.value,
                    "timing_status": deployment_window.timing_status.value,
                    "calibration_id": deployment_window.calibration_id,
                    "reasons": deployment_window.reasons,
                    "relative_bearing_deg": _canonical_optional_float(
                        deployment_window.relative_bearing_deg
                    ),
                    "depression_angle_deg": _canonical_optional_float(
                        deployment_window.depression_angle_deg
                    ),
                    "estimated_ground_range_m": _canonical_optional_float(
                        deployment_window.estimated_ground_range_m
                    ),
                    "cross_track_error_m": _canonical_optional_float(
                        deployment_window.cross_track_error_m
                    ),
                    "along_track_error_m": _canonical_optional_float(
                        deployment_window.along_track_error_m
                    ),
                    "payload_descent_time_s": _canonical_optional_float(
                        deployment_window.payload_descent_time_s
                    ),
                    "release_lead_distance_m": _canonical_optional_float(
                        deployment_window.release_lead_distance_m
                    ),
                    "target_north_offset_m": _canonical_optional_float(
                        deployment_window.target_north_offset_m
                    ),
                    "target_east_offset_m": _canonical_optional_float(
                        deployment_window.target_east_offset_m
                    ),
                    "impact_north_offset_m": _canonical_optional_float(
                        deployment_window.impact_north_offset_m
                    ),
                    "impact_east_offset_m": _canonical_optional_float(
                        deployment_window.impact_east_offset_m
                    ),
                    "error_ellipse_major_m": _canonical_optional_float(
                        deployment_window.error_ellipse_major_m
                    ),
                    "error_ellipse_minor_m": _canonical_optional_float(
                        deployment_window.error_ellipse_minor_m
                    ),
                    "error_ellipse_orientation_deg": _canonical_optional_float(
                        deployment_window.error_ellipse_orientation_deg
                    ),
                    "ground_range_ci95_m": (
                        None
                        if deployment_window.ground_range_ci95_m is None
                        else tuple(
                            _canonical_float(value)
                            for value in deployment_window.ground_range_ci95_m
                        )
                    ),
                    "range_target_id": deployment_window.range_target_id,
                    "range_frame_id": deployment_window.range_frame_id,
                    "range_sensor_consistency": _canonical_optional_float(
                        deployment_window.range_sensor_consistency
                    ),
                    "advisory_only": deployment_window.advisory_only,
                    "flight_control_enabled": deployment_window.flight_control_enabled,
                    "physical_release_enabled": deployment_window.physical_release_enabled,
                }
            ),
            "detections": detections,
        }
        encoded = json.dumps(
            document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _deployment_window_check(solution: DeploymentWindowSolution) -> RuleCheck:
        if solution.status is DeploymentWindowStatus.READY:
            return RuleCheck(
                "deployment.fixed_wing_release_window",
                Verdict.PASS,
                "fixed-wing HIL release window is ready",
            )
        if solution.status is DeploymentWindowStatus.UNAVAILABLE:
            return RuleCheck(
                "deployment.fixed_wing_release_window",
                Verdict.UNKNOWN,
                f"fixed-wing HIL release window is unavailable: {', '.join(solution.reasons)}",
            )
        return RuleCheck(
            "deployment.fixed_wing_release_window",
            Verdict.DENY,
            f"fixed-wing HIL release window is not ready: {', '.join(solution.reasons)}",
        )

    def _freshness_check(
        self, rule_id: str, observed_at_s: float, now_s: float, description: str
    ) -> RuleCheck:
        if not math.isfinite(observed_at_s):
            return RuleCheck(rule_id, Verdict.UNKNOWN, f"{description} timestamp is unavailable")
        age_s = now_s - observed_at_s
        if age_s < 0:
            return RuleCheck(rule_id, Verdict.UNKNOWN, f"{description} timestamp is in the future")
        if age_s > self._config.safety.sensor_data_max_age_seconds:
            return RuleCheck(rule_id, Verdict.DENY, f"{description} is stale")
        return RuleCheck(rule_id, Verdict.PASS, f"{description} is fresh")

    @staticmethod
    def _range_check(
        rule_id: str,
        value: float,
        minimum: float,
        maximum: float,
        description: str,
    ) -> RuleCheck:
        if not math.isfinite(value):
            return RuleCheck(rule_id, Verdict.UNKNOWN, f"{description} is unavailable")
        if minimum <= value <= maximum:
            return RuleCheck(rule_id, Verdict.PASS, f"{description} is within limits")
        return RuleCheck(rule_id, Verdict.DENY, f"{description} is outside limits")

    @staticmethod
    def _absolute_limit_check(
        rule_id: str, value: float, maximum_absolute: float, description: str
    ) -> RuleCheck:
        if not math.isfinite(value):
            return RuleCheck(rule_id, Verdict.UNKNOWN, f"{description} is unavailable")
        if abs(value) <= maximum_absolute:
            return RuleCheck(rule_id, Verdict.PASS, f"{description} is within limits")
        return RuleCheck(rule_id, Verdict.DENY, f"{description} is outside limits")

    def _person_exclusion_check(
        self, *, track: TrackSnapshot, frame: FrameObservation
    ) -> RuleCheck:
        if not self._config.person_exclusion_enabled:
            return RuleCheck(
                "deployment.person_exclusion",
                Verdict.PASS,
                "person exclusion is disabled by mission configuration",
            )

        person_labels = {label.strip().lower() for label in self._config.person_labels}
        exclusion_zone = track.bbox.expanded(self._config.safety.person_exclusion_margin_normalized)
        nearby = tuple(
            detection
            for detection in frame.detections
            if detection.label.strip().lower() in person_labels
            and exclusion_zone.intersects(detection.bbox)
        )
        if nearby:
            return RuleCheck(
                "deployment.person_exclusion",
                Verdict.DENY,
                f"{len(nearby)} person-safety detection(s) intersect the exclusion zone",
            )
        return RuleCheck(
            "deployment.person_exclusion",
            Verdict.PASS,
            "no person-safety detection intersects the exclusion zone",
        )


__all__ = ["SafetyRuleEngine"]

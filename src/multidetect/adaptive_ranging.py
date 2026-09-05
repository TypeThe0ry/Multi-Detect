"""Adaptive source priors for outdoor fixed-wing and multirotor ranging.

This module contains only deterministic state classification and covariance
priors.  The final weights remain the information weights calculated by the
multimodal fusion engine after freshness and consistency gating.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .compat import StrEnum
from .domain import VehicleTelemetry
from .mavlink_gps import (
    GNSS_CANONICAL_MAX_GPS_AGE_S,
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_GPS_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_SATELLITES_MIN,
    MAVLINK_SATELLITES_MAX,
    MAVLINK_SATELLITES_MIN,
    MAVLINK_SATELLITES_UNKNOWN,
)


class VehicleProfile(StrEnum):
    AUTO = "auto"
    FIXED_WING = "fixed-wing"
    MULTIROTOR = "multirotor"


class NavigationState(StrEnum):
    GPS_AIDED = "gps-aided"
    LOCAL_NED = "local-ned"
    AIRSPEED_DR = "airspeed-dr"
    VISION_ONLY = "vision-only"


class MotionRegime(StrEnum):
    STATIC = "static"
    LOW_SPEED = "low-speed"
    CRUISE = "cruise"
    HIGH_SPEED = "high-speed"


@dataclass(frozen=True, slots=True)
class AdaptiveRangingConfig:
    vehicle_profile: VehicleProfile = VehicleProfile.AUTO
    static_speed_mps: float = 0.5
    low_speed_mps: float = 5.0
    cruise_speed_mps: float = 12.0
    minimum_gps_fix_type: int = MAVLINK_QUALIFIED_FIX_TYPE_MIN
    minimum_gps_satellites: int = MAVLINK_QUALIFIED_SATELLITES_MIN
    maximum_gps_horizontal_accuracy_m: float = GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
    maximum_gps_vertical_accuracy_m: float = GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M
    maximum_gps_age_s: float = GNSS_CANONICAL_MAX_GPS_AGE_S
    maximum_local_position_age_s: float = 0.60
    maximum_velocity_age_s: float = 0.60
    maximum_airspeed_age_s: float = 0.80

    def __post_init__(self) -> None:
        if not (0.0 < self.static_speed_mps < self.low_speed_mps < self.cruise_speed_mps):
            raise ValueError("adaptive ranging speed thresholds are not ordered")
        if (
            self.minimum_gps_fix_type < MAVLINK_QUALIFIED_FIX_TYPE_MIN
            or self.minimum_gps_fix_type > 6
        ):
            raise ValueError("adaptive ranging GPS minimum fix type must be in [3, 6]")
        if self.minimum_gps_satellites < MAVLINK_QUALIFIED_SATELLITES_MIN:
            raise ValueError("adaptive ranging GPS minimum must be at least six satellites")
        for value in (
            self.maximum_gps_horizontal_accuracy_m,
            self.maximum_gps_vertical_accuracy_m,
            self.maximum_gps_age_s,
            self.maximum_local_position_age_s,
            self.maximum_velocity_age_s,
            self.maximum_airspeed_age_s,
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError("adaptive ranging freshness limits must be finite and positive")
        if self.maximum_gps_horizontal_accuracy_m > GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M:
            raise ValueError(
                "adaptive ranging horizontal GPS accuracy cannot relax the canonical 6 m gate"
            )
        if self.maximum_gps_vertical_accuracy_m > GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M:
            raise ValueError(
                "adaptive ranging vertical GPS accuracy cannot relax the canonical 10 m gate"
            )
        if self.maximum_gps_age_s > GNSS_CANONICAL_MAX_GPS_AGE_S:
            raise ValueError("adaptive ranging GPS age cannot relax the canonical 1.5 s gate")


@dataclass(frozen=True, slots=True)
class AdaptiveRangingDecision:
    vehicle_profile: VehicleProfile
    navigation_state: NavigationState
    motion_regime: MotionRegime
    ground_speed_mps: float
    source_weight_priors: Mapping[str, float]

    def __post_init__(self) -> None:
        if not math.isfinite(self.ground_speed_mps) or self.ground_speed_mps < 0.0:
            raise ValueError("adaptive ranging ground speed must be finite and non-negative")
        if not self.source_weight_priors:
            raise ValueError("adaptive ranging source priors cannot be empty")
        if any(
            not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0.0
            for value in self.source_weight_priors.values()
        ):
            raise ValueError("adaptive ranging source priors must be finite and positive")


@dataclass(frozen=True, slots=True)
class GpsQualityAssessment:
    """Fail-closed assessment of one aircraft GPS sample.

    ``RangeSolution.navigation_state`` is derived metadata and can be
    reconstructed by a replay producer or a caller.  Consumers that publish
    WGS-84 coordinates therefore use this value together with the live
    :class:`VehicleTelemetry` snapshot instead of trusting that string alone.
    ``age_s`` is ``None`` when the source timestamp is missing, invalid, or in
    the future; a finite value is always non-negative.
    """

    qualified: bool
    reason: str
    age_s: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.qualified, bool):
            raise ValueError("GPS quality qualified flag must be boolean")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("GPS quality reason cannot be empty")
        if self.age_s is not None and (
            not isinstance(self.age_s, (int, float))
            or isinstance(self.age_s, bool)
            or not math.isfinite(self.age_s)
            or self.age_s < 0.0
        ):
            raise ValueError("GPS quality age must be finite and non-negative when present")
        if self.qualified and self.reason != "gps_qualified":
            raise ValueError("qualified GPS assessment must use gps_qualified reason")
        if not self.qualified and self.reason == "gps_qualified":
            raise ValueError("unqualified GPS assessment cannot use gps_qualified reason")


class AdaptiveRangingPolicy:
    """Classify vehicle/navigation motion and return bounded source priors."""

    _BASE_BY_REGIME: dict[MotionRegime, dict[str, float]] = {
        MotionRegime.STATIC: {
            "monocular_metric": 1.55,
            "monocular_size": 0.80,
            "rgb_slam": 0.25,
            "vio": 0.35,
            "camera_ground": 0.85,
        },
        MotionRegime.LOW_SPEED: {
            "monocular_metric": 1.35,
            "monocular_size": 0.50,
            "rgb_slam": 0.95,
            "vio": 1.10,
            "camera_ground": 1.00,
        },
        MotionRegime.CRUISE: {
            "monocular_metric": 0.78,
            "monocular_size": 0.20,
            "rgb_slam": 1.45,
            "vio": 1.55,
            "camera_ground": 1.10,
        },
        MotionRegime.HIGH_SPEED: {
            "monocular_metric": 0.42,
            "monocular_size": 0.10,
            "rgb_slam": 1.75,
            "vio": 1.95,
            "camera_ground": 1.15,
        },
    }

    def __init__(self, config: AdaptiveRangingConfig | None = None) -> None:
        self.config = config or AdaptiveRangingConfig()

    def decide(self, telemetry: VehicleTelemetry, *, now_s: float) -> AdaptiveRangingDecision:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("adaptive ranging evaluation time must be finite and non-negative")
        speed = self._ground_speed(telemetry, now_s=now_s)
        vehicle = self._vehicle_profile(telemetry, speed)
        navigation = self._navigation_state(telemetry, now_s=now_s)
        regime = self._motion_regime(speed)
        priors = dict(self._BASE_BY_REGIME[regime])

        if vehicle is VehicleProfile.FIXED_WING:
            priors["rgb_slam"] *= 1.15
            priors["vio"] *= 1.20
            priors["monocular_metric"] *= 0.85
        else:
            # Multirotors spend more time hovering and sidestepping, where a
            # current dense depth observation carries high scale value.
            priors["monocular_metric"] *= 1.20
            priors["monocular_size"] *= 1.10
            if regime in {MotionRegime.STATIC, MotionRegime.LOW_SPEED}:
                priors["rgb_slam"] *= 0.82
                priors["vio"] *= 0.82

        if navigation is NavigationState.GPS_AIDED:
            priors["camera_ground"] *= 1.45
            priors["rgb_slam"] *= 1.08
            priors["vio"] *= 1.12
            priors["pixhawk_agl"] = 1.30
            # DEM/GPS uncertainty is already propagated from the receiver,
            # datum grid and local slope.  Do not make that physical covariance
            # look tighter merely because the navigation mode is GPS-aided.
            priors["dem_gps"] = 1.0
        elif navigation is NavigationState.LOCAL_NED:
            priors["camera_ground"] *= 0.95
            priors["rgb_slam"] *= 1.04
            priors["vio"] *= 1.08
            priors["pixhawk_agl"] = 1.0
            priors["dem_gps"] = 0.05
        elif navigation is NavigationState.AIRSPEED_DR:
            priors["camera_ground"] *= 0.55
            priors["rgb_slam"] *= 1.18
            priors["vio"] *= 1.22
            priors["pixhawk_agl"] = 0.75
            priors["dem_gps"] = 0.05
        else:
            priors["camera_ground"] *= 0.35
            priors["rgb_slam"] *= 1.22
            priors["vio"] *= 1.12
            priors["pixhawk_agl"] = 0.60
            priors["dem_gps"] = 0.05

        return AdaptiveRangingDecision(
            vehicle_profile=vehicle,
            navigation_state=navigation,
            motion_regime=regime,
            ground_speed_mps=speed,
            source_weight_priors={
                source: min(8.0, max(0.05, value)) for source, value in priors.items()
            },
        )

    def assess_gps_quality(
        self,
        telemetry: VehicleTelemetry,
        *,
        now_s: float,
    ) -> GpsQualityAssessment:
        """Assess GPS with this policy's exact thresholds.

        Keeping this method on the policy prevents geolocation publishers from
        copying thresholds that could drift from the ranging classifier.
        """

        return assess_gps_quality(telemetry, now_s=now_s, config=self.config)

    def _vehicle_profile(self, telemetry: VehicleTelemetry, speed: float) -> VehicleProfile:
        if self.config.vehicle_profile is not VehicleProfile.AUTO:
            return self.config.vehicle_profile
        if math.isfinite(telemetry.airspeed_mps) and telemetry.airspeed_mps >= 8.0:
            return VehicleProfile.FIXED_WING
        if speed >= self.config.cruise_speed_mps:
            return VehicleProfile.FIXED_WING
        return VehicleProfile.MULTIROTOR

    def _navigation_state(self, telemetry: VehicleTelemetry, *, now_s: float) -> NavigationState:
        gps_quality = self.assess_gps_quality(telemetry, now_s=now_s)
        if gps_quality.qualified:
            return NavigationState.GPS_AIDED
        local_age = _non_negative_age(now_s, telemetry.local_position_observed_at_s)
        if (
            all(
                math.isfinite(value)
                for value in (
                    telemetry.local_north_m,
                    telemetry.local_east_m,
                    telemetry.local_down_m,
                )
            )
            and math.isfinite(local_age)
            and local_age <= self.config.maximum_local_position_age_s
        ):
            return NavigationState.LOCAL_NED
        airspeed_age = _non_negative_age(now_s, telemetry.airspeed_observed_at_s)
        if (
            telemetry.armed is True
            and math.isfinite(telemetry.airspeed_mps)
            and telemetry.airspeed_mps >= self.config.static_speed_mps
            and math.isfinite(airspeed_age)
            and airspeed_age <= self.config.maximum_airspeed_age_s
        ):
            return NavigationState.AIRSPEED_DR
        return NavigationState.VISION_ONLY

    def _ground_speed(self, telemetry: VehicleTelemetry, *, now_s: float) -> float:
        if math.isfinite(telemetry.ground_speed_mps) and telemetry.ground_speed_mps >= 0.0:
            return telemetry.ground_speed_mps
        velocity_age = _non_negative_age(now_s, telemetry.velocity_observed_at_s)
        if (
            math.isfinite(telemetry.velocity_north_mps)
            and math.isfinite(telemetry.velocity_east_mps)
            and math.isfinite(velocity_age)
            and velocity_age <= self.config.maximum_velocity_age_s
        ):
            return math.hypot(telemetry.velocity_north_mps, telemetry.velocity_east_mps)
        if math.isfinite(telemetry.airspeed_mps) and telemetry.airspeed_mps >= 0.0:
            return telemetry.airspeed_mps
        return 0.0

    def _motion_regime(self, speed: float) -> MotionRegime:
        if speed < self.config.static_speed_mps:
            return MotionRegime.STATIC
        if speed < self.config.low_speed_mps:
            return MotionRegime.LOW_SPEED
        if speed < self.config.cruise_speed_mps:
            return MotionRegime.CRUISE
        return MotionRegime.HIGH_SPEED


def assess_gps_quality(
    telemetry: VehicleTelemetry,
    *,
    now_s: float,
    config: AdaptiveRangingConfig | None = None,
) -> GpsQualityAssessment:
    """Return the canonical, fail-closed GPS qualification decision.

    This is intentionally shared by the ranging classifier and every target
    geolocation publisher.  A caller-supplied ``RangeSolution`` can carry a
    stale or forged ``navigation_state`` string, so coordinate publication
    must always re-check the raw telemetry through this function.
    """

    if not _finite_real(now_s) or now_s < 0.0:
        raise ValueError("adaptive ranging evaluation time must be finite and non-negative")
    active_config = config or AdaptiveRangingConfig()

    observed_at_s = telemetry.gps_observed_at_s
    if not _finite_real(observed_at_s):
        gps_age_s: float | None = None
        timestamp_reason = "gps_timestamp_unavailable"
    else:
        raw_age_s = float(now_s) - float(observed_at_s)
        if raw_age_s < 0.0:
            gps_age_s = None
            timestamp_reason = "gps_timestamp_future"
        else:
            gps_age_s = raw_age_s
            timestamp_reason = ""

    if telemetry.position_healthy is not True:
        return GpsQualityAssessment(False, "gps_position_unhealthy", gps_age_s)
    if (
        not _finite_real(telemetry.latitude_deg)
        or not _finite_real(telemetry.longitude_deg)
        or not -90.0 < float(telemetry.latitude_deg) < 90.0
        or not -180.0 <= float(telemetry.longitude_deg) <= 180.0
    ):
        return GpsQualityAssessment(False, "gps_coordinates_invalid", gps_age_s)
    if (
        isinstance(telemetry.gps_fix_type, bool)
        or not isinstance(telemetry.gps_fix_type, int)
        or not active_config.minimum_gps_fix_type
        <= telemetry.gps_fix_type
        <= MAVLINK_GPS_FIX_TYPE_MAX
    ):
        return GpsQualityAssessment(False, "gps_fix_unqualified", gps_age_s)
    if (
        isinstance(telemetry.satellites_visible, bool)
        or not isinstance(telemetry.satellites_visible, int)
        or not MAVLINK_SATELLITES_MIN <= telemetry.satellites_visible <= MAVLINK_SATELLITES_MAX
        or telemetry.satellites_visible < active_config.minimum_gps_satellites
    ):
        return GpsQualityAssessment(False, "gps_satellites_insufficient", gps_age_s)

    horizontal_accuracy_m = telemetry.gps_horizontal_accuracy_m
    if not _finite_real(horizontal_accuracy_m) or horizontal_accuracy_m < 0.0:
        return GpsQualityAssessment(False, "gps_horizontal_accuracy_invalid", gps_age_s)
    if horizontal_accuracy_m > active_config.maximum_gps_horizontal_accuracy_m:
        return GpsQualityAssessment(False, "gps_horizontal_accuracy_exceeded", gps_age_s)

    vertical_accuracy_m = telemetry.gps_vertical_accuracy_m
    if not _finite_real(vertical_accuracy_m) or vertical_accuracy_m < 0.0:
        return GpsQualityAssessment(False, "gps_vertical_accuracy_invalid", gps_age_s)
    if vertical_accuracy_m > active_config.maximum_gps_vertical_accuracy_m:
        return GpsQualityAssessment(False, "gps_vertical_accuracy_exceeded", gps_age_s)

    if timestamp_reason:
        return GpsQualityAssessment(False, timestamp_reason, gps_age_s)
    assert gps_age_s is not None  # timestamp_reason is empty only for finite age
    if gps_age_s > active_config.maximum_gps_age_s:
        return GpsQualityAssessment(False, "gps_stale", gps_age_s)
    return GpsQualityAssessment(True, "gps_qualified", gps_age_s)


def _finite_real(value: object) -> bool:
    """Whether ``value`` is a non-bool finite real number."""

    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _non_negative_age(now_s: float, observed_at_s: float) -> float:
    """Return a bounded-source age, treating future/invalid time as unusable.

    ``math.inf`` is intentionally used as the sentinel because every caller
    compares the result with a finite freshness limit.  Keeping this helper
    local makes the rule identical for GPS, local position, air-data and
    velocity fallback paths.
    """

    if not math.isfinite(observed_at_s):
        return math.inf
    age_s = now_s - observed_at_s
    return age_s if age_s >= 0.0 else math.inf


__all__ = [
    "AdaptiveRangingConfig",
    "AdaptiveRangingDecision",
    "AdaptiveRangingPolicy",
    "GNSS_CANONICAL_MAX_GPS_AGE_S",
    "GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M",
    "GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M",
    "GpsQualityAssessment",
    "MAVLINK_GPS_FIX_TYPE_MAX",
    "MAVLINK_GPS_FIX_TYPE_MIN",
    "MAVLINK_QUALIFIED_FIX_TYPE_MIN",
    "MAVLINK_QUALIFIED_SATELLITES_MIN",
    "MAVLINK_SATELLITES_MAX",
    "MAVLINK_SATELLITES_MIN",
    "MAVLINK_SATELLITES_UNKNOWN",
    "MotionRegime",
    "NavigationState",
    "VehicleProfile",
    "assess_gps_quality",
]

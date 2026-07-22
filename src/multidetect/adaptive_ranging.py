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
    minimum_gps_fix_type: int = 3
    minimum_gps_satellites: int = 6
    maximum_gps_horizontal_accuracy_m: float = 6.0
    maximum_gps_vertical_accuracy_m: float = 10.0
    maximum_gps_age_s: float = 1.50
    maximum_local_position_age_s: float = 0.60
    maximum_velocity_age_s: float = 0.60
    maximum_airspeed_age_s: float = 0.80

    def __post_init__(self) -> None:
        if not (0.0 < self.static_speed_mps < self.low_speed_mps < self.cruise_speed_mps):
            raise ValueError("adaptive ranging speed thresholds are not ordered")
        if self.minimum_gps_fix_type < 3 or self.minimum_gps_fix_type > 6:
            raise ValueError("adaptive ranging GPS minimum fix type must be in [3, 6]")
        if self.minimum_gps_satellites < 4:
            raise ValueError("adaptive ranging GPS minimum must be at least four satellites")
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
        elif navigation is NavigationState.LOCAL_NED:
            priors["camera_ground"] *= 0.95
            priors["rgb_slam"] *= 1.04
            priors["vio"] *= 1.08
            priors["pixhawk_agl"] = 1.0
        elif navigation is NavigationState.AIRSPEED_DR:
            priors["camera_ground"] *= 0.55
            priors["rgb_slam"] *= 1.18
            priors["vio"] *= 1.22
            priors["pixhawk_agl"] = 0.75
        else:
            priors["camera_ground"] *= 0.35
            priors["rgb_slam"] *= 1.22
            priors["vio"] *= 1.12
            priors["pixhawk_agl"] = 0.60

        return AdaptiveRangingDecision(
            vehicle_profile=vehicle,
            navigation_state=navigation,
            motion_regime=regime,
            ground_speed_mps=speed,
            source_weight_priors={
                source: min(8.0, max(0.05, value)) for source, value in priors.items()
            },
        )

    def _vehicle_profile(self, telemetry: VehicleTelemetry, speed: float) -> VehicleProfile:
        if self.config.vehicle_profile is not VehicleProfile.AUTO:
            return self.config.vehicle_profile
        if math.isfinite(telemetry.airspeed_mps) and telemetry.airspeed_mps >= 8.0:
            return VehicleProfile.FIXED_WING
        if speed >= self.config.cruise_speed_mps:
            return VehicleProfile.FIXED_WING
        return VehicleProfile.MULTIROTOR

    def _navigation_state(self, telemetry: VehicleTelemetry, *, now_s: float) -> NavigationState:
        gps_age = abs(now_s - telemetry.gps_observed_at_s)
        global_position = (
            telemetry.position_healthy is True
            and math.isfinite(telemetry.latitude_deg)
            and math.isfinite(telemetry.longitude_deg)
            and isinstance(telemetry.gps_fix_type, int)
            and telemetry.gps_fix_type >= self.config.minimum_gps_fix_type
            and telemetry.satellites_visible is not None
            and telemetry.satellites_visible >= self.config.minimum_gps_satellites
            and math.isfinite(telemetry.gps_horizontal_accuracy_m)
            and telemetry.gps_horizontal_accuracy_m
            <= self.config.maximum_gps_horizontal_accuracy_m
            and math.isfinite(telemetry.gps_vertical_accuracy_m)
            and telemetry.gps_vertical_accuracy_m <= self.config.maximum_gps_vertical_accuracy_m
            and math.isfinite(gps_age)
            and gps_age <= self.config.maximum_gps_age_s
        )
        if global_position:
            return NavigationState.GPS_AIDED
        local_age = abs(now_s - telemetry.local_position_observed_at_s)
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
        airspeed_age = abs(now_s - telemetry.airspeed_observed_at_s)
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
        velocity_age = abs(now_s - telemetry.velocity_observed_at_s)
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


__all__ = [
    "AdaptiveRangingConfig",
    "AdaptiveRangingDecision",
    "AdaptiveRangingPolicy",
    "MotionRegime",
    "NavigationState",
    "VehicleProfile",
]

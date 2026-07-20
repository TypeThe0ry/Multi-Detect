from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from .compat import StrEnum


class NavigationFusionValidity(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    INVALID = "invalid"


class NavigationVelocitySource(StrEnum):
    GPS = "gps"
    VIO = "vio"
    AIR_DATA = "air_data"


class NavigationFusionMode(StrEnum):
    """Declared source mode for the GPS-first navigation fallback chain."""

    GPS_FUSED = "gps_fused"
    GPS_SUSPECT = "gps_suspect"
    VIO_AIRDATA = "vio_airdata"
    GPS_REACQUIRE = "gps_reacquire"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class GpsVelocityMeasurement:
    north_mps: float
    east_mps: float
    sigma_mps: float
    captured_at_s: float
    fix_type: int
    satellites_visible: int

    def __post_init__(self) -> None:
        _validate_velocity(self.north_mps, self.east_mps, self.sigma_mps, self.captured_at_s)
        if isinstance(self.fix_type, bool) or not isinstance(self.fix_type, int):
            raise ValueError("GPS fix type must be an integer")
        if not 0 <= self.fix_type <= 8:
            raise ValueError("GPS fix type is outside the MAVLink domain")
        if isinstance(self.satellites_visible, bool) or not isinstance(
            self.satellites_visible, int
        ):
            raise ValueError("GPS satellite count must be an integer")
        if not 0 <= self.satellites_visible <= 255:
            raise ValueError("GPS satellite count is outside the MAVLink domain")


@dataclass(frozen=True, slots=True)
class VisualOdometryVelocityMeasurement:
    north_mps: float
    east_mps: float
    sigma_mps: float
    captured_at_s: float
    absolute_scale_valid: bool

    def __post_init__(self) -> None:
        _validate_velocity(self.north_mps, self.east_mps, self.sigma_mps, self.captured_at_s)
        if not isinstance(self.absolute_scale_valid, bool):
            raise ValueError("VIO absolute-scale flag must be boolean")


@dataclass(frozen=True, slots=True)
class AirspeedMeasurement:
    airspeed_mps: float
    heading_deg: float
    sigma_mps: float
    heading_sigma_deg: float
    captured_at_s: float
    calibrated: bool = True
    sensor_present_enabled: bool = True

    def __post_init__(self) -> None:
        values = (
            self.airspeed_mps,
            self.heading_deg,
            self.sigma_mps,
            self.heading_sigma_deg,
            self.captured_at_s,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError("air-data values must be numeric")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("air-data values must be finite")
        if self.airspeed_mps < 0.0:
            raise ValueError("airspeed cannot be negative")
        if not 0.0 <= self.heading_deg < 360.0:
            raise ValueError("air-data heading must be in [0, 360)")
        if self.sigma_mps <= 0.0 or self.heading_sigma_deg < 0.0:
            raise ValueError("air-data uncertainty is invalid")
        if self.captured_at_s < 0.0:
            raise ValueError("air-data timestamp cannot be negative")
        if not isinstance(self.calibrated, bool) or not isinstance(
            self.sensor_present_enabled, bool
        ):
            raise ValueError("air-data readiness flags must be boolean")


@dataclass(frozen=True, slots=True)
class WindVelocityMeasurement:
    north_mps: float
    east_mps: float
    sigma_mps: float
    captured_at_s: float

    def __post_init__(self) -> None:
        _validate_velocity(self.north_mps, self.east_mps, self.sigma_mps, self.captured_at_s)


@dataclass(frozen=True, slots=True)
class NavigationFusionConfig:
    maximum_gps_age_s: float = 0.75
    maximum_vio_age_s: float = 0.25
    maximum_airspeed_age_s: float = 0.75
    maximum_wind_age_s: float = 2.0
    maximum_airdata_wind_skew_s: float = 0.50
    minimum_gps_fix_type: int = 3
    minimum_gps_satellites: int = 6
    consistency_gate_sigma: float = 3.5
    minimum_velocity_sigma_mps: float = 0.15
    maximum_ground_speed_mps: float = 120.0
    minimum_independent_sources: int = 2

    def __post_init__(self) -> None:
        positive = (
            self.maximum_gps_age_s,
            self.maximum_vio_age_s,
            self.maximum_airspeed_age_s,
            self.maximum_wind_age_s,
            self.maximum_airdata_wind_skew_s,
            self.consistency_gate_sigma,
            self.minimum_velocity_sigma_mps,
            self.maximum_ground_speed_mps,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0.0
            for value in positive
        ):
            raise ValueError("navigation fusion limits must be finite and positive")
        if (
            isinstance(self.minimum_gps_fix_type, bool)
            or not isinstance(self.minimum_gps_fix_type, int)
            or not 2 <= self.minimum_gps_fix_type <= 6
        ):
            raise ValueError("minimum GPS fix type must be an integer in [2, 6]")
        if (
            isinstance(self.minimum_gps_satellites, bool)
            or not isinstance(self.minimum_gps_satellites, int)
            or not 0 <= self.minimum_gps_satellites <= 64
        ):
            raise ValueError("minimum GPS satellite count must be an integer in [0, 64]")
        if (
            isinstance(self.minimum_independent_sources, bool)
            or not isinstance(self.minimum_independent_sources, int)
            or not 2 <= self.minimum_independent_sources <= 3
        ):
            raise ValueError("minimum independent source count must be 2 or 3")


@dataclass(frozen=True, slots=True)
class NavigationVelocitySolution:
    evaluated_at_s: float
    validity: NavigationFusionValidity
    reasons: tuple[str, ...]
    source_diagnostics: tuple[str, ...]
    sources: tuple[str, ...]
    rejected_sources: tuple[str, ...]
    north_mps: float | None = None
    east_mps: float | None = None
    ground_speed_mps: float | None = None
    track_deg: float | None = None
    velocity_sigma_mps: float | None = None
    data_freshness_s: float | None = None
    sensor_consistency: float = 0.0
    absolute_scale_valid: bool = False
    advisory_only: bool = True
    flight_control_enabled: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.evaluated_at_s) or self.evaluated_at_s < 0.0:
            raise ValueError("navigation fusion evaluation time is invalid")
        if not isinstance(self.validity, NavigationFusionValidity):
            raise ValueError("navigation fusion validity is invalid")
        if not self.reasons or any(not reason.strip() for reason in self.reasons):
            raise ValueError("navigation fusion reasons cannot be empty")
        if any(not diagnostic.strip() for diagnostic in self.source_diagnostics):
            raise ValueError("navigation source diagnostics cannot be empty")
        numeric = (
            self.north_mps,
            self.east_mps,
            self.ground_speed_mps,
            self.track_deg,
            self.velocity_sigma_mps,
            self.data_freshness_s,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("navigation fusion outputs must be finite when supplied")
        if not math.isfinite(self.sensor_consistency) or not 0.0 <= self.sensor_consistency <= 1.0:
            raise ValueError("navigation sensor consistency must be in [0, 1]")
        velocity_values = (
            self.north_mps,
            self.east_mps,
            self.ground_speed_mps,
            self.velocity_sigma_mps,
            self.data_freshness_s,
        )
        if self.validity is NavigationFusionValidity.INVALID:
            if any(value is not None for value in velocity_values) or self.absolute_scale_valid:
                raise ValueError("invalid navigation solutions cannot publish metric velocity")
        elif any(value is None for value in velocity_values) or not self.absolute_scale_valid:
            raise ValueError("usable navigation solutions require metric velocity and scale")
        if self.track_deg is not None and not 0.0 <= self.track_deg < 360.0:
            raise ValueError("navigation track must be in [0, 360)")
        if not self.advisory_only or self.flight_control_enabled:
            raise ValueError("navigation fusion output must remain advisory-only")

    def to_document(self) -> dict[str, object]:
        return {
            "evaluated_at_s": self.evaluated_at_s,
            "validity": self.validity.value,
            "reasons": self.reasons,
            "source_diagnostics": self.source_diagnostics,
            "sources": self.sources,
            "rejected_sources": self.rejected_sources,
            "north_mps": self.north_mps,
            "east_mps": self.east_mps,
            "ground_speed_mps": self.ground_speed_mps,
            "track_deg": self.track_deg,
            "velocity_sigma_mps": self.velocity_sigma_mps,
            "data_freshness_s": self.data_freshness_s,
            "sensor_consistency": self.sensor_consistency,
            "absolute_scale_valid": self.absolute_scale_valid,
            "advisory_only": self.advisory_only,
            "flight_control_enabled": self.flight_control_enabled,
        }


@dataclass(frozen=True, slots=True)
class NavigationFusionState:
    """State-machine result layered on the metric velocity fusion solution.

    This is deliberately a source supervisor, not a claim that unscaled
    monocular optical flow is SLAM.  A VIO measurement reaches this layer only
    after it has metric scale and passes the existing freshness/consistency
    gates.  The mode gives QGC and audit consumers a stable indication of the
    GPS-first / local-VIO-airdata fallback path.
    """

    mode: NavigationFusionMode
    solution: NavigationVelocitySolution
    consecutive_gps_samples: int

    def __post_init__(self) -> None:
        if not isinstance(self.mode, NavigationFusionMode):
            raise ValueError("navigation fusion mode is invalid")
        if self.consecutive_gps_samples < 0:
            raise ValueError("GPS sample counter cannot be negative")


class MultimodalNavigationSupervisor:
    """GPS-first mode supervisor with VIO/air-data continuity and reacquisition.

    The supervisor is stateful only for GPS re-acquisition hysteresis.  Metric
    velocity and all uncertainty calculations remain owned by
    :class:`NavigationVelocityFusionEngine`.
    """

    def __init__(
        self,
        engine: NavigationVelocityFusionEngine | None = None,
        *,
        gps_reacquire_samples: int = 3,
    ) -> None:
        if (
            isinstance(gps_reacquire_samples, bool)
            or not isinstance(gps_reacquire_samples, int)
            or gps_reacquire_samples < 1
        ):
            raise ValueError("GPS reacquisition sample count must be a positive integer")
        self.engine = engine or NavigationVelocityFusionEngine()
        self.gps_reacquire_samples = gps_reacquire_samples
        self._gps_was_healthy = False
        self._consecutive_gps_samples = 0

    def solve(
        self,
        *,
        now_s: float,
        gps: GpsVelocityMeasurement | None = None,
        visual_odometry: VisualOdometryVelocityMeasurement | None = None,
        airspeed: AirspeedMeasurement | None = None,
        wind: WindVelocityMeasurement | None = None,
    ) -> NavigationFusionState:
        solution = self.engine.solve(
            now_s=now_s,
            gps=gps,
            visual_odometry=visual_odometry,
            airspeed=airspeed,
            wind=wind,
        )
        gps_accepted = "gps:accepted" in solution.source_diagnostics
        had_gps = self._gps_was_healthy
        if gps_accepted:
            self._consecutive_gps_samples += 1
            self._gps_was_healthy = True
            mode = (
                NavigationFusionMode.GPS_REACQUIRE
                if not had_gps and self._consecutive_gps_samples < self.gps_reacquire_samples
                else NavigationFusionMode.GPS_FUSED
            )
        else:
            self._gps_was_healthy = False
            self._consecutive_gps_samples = 0
            sources = set(solution.sources)
            fallback_sources = {
                NavigationVelocitySource.VIO.value,
                NavigationVelocitySource.AIR_DATA.value,
            }
            if fallback_sources.issubset(sources):
                mode = NavigationFusionMode.VIO_AIRDATA
            elif gps is not None:
                mode = NavigationFusionMode.GPS_SUSPECT
            else:
                mode = NavigationFusionMode.INSUFFICIENT
        return NavigationFusionState(
            mode=mode,
            solution=solution,
            consecutive_gps_samples=self._consecutive_gps_samples,
        )


@dataclass(frozen=True, slots=True)
class _VelocityCandidate:
    source: NavigationVelocitySource
    north_mps: float
    east_mps: float
    sigma_mps: float
    age_s: float


class NavigationVelocityFusionEngine:
    """Fuse independent metric velocity sources with freshness and scale gates.

    Camera optical flow or monocular SLAM is accepted only after its metric scale
    has been established. Airspeed is converted to ground velocity only when a
    synchronized wind estimate is present. The output is an observation and does
    not send a flight-control command.
    """

    def __init__(self, config: NavigationFusionConfig | None = None) -> None:
        self.config = config or NavigationFusionConfig()

    def solve(
        self,
        *,
        now_s: float,
        gps: GpsVelocityMeasurement | None = None,
        visual_odometry: VisualOdometryVelocityMeasurement | None = None,
        airspeed: AirspeedMeasurement | None = None,
        wind: WindVelocityMeasurement | None = None,
    ) -> NavigationVelocitySolution:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("navigation fusion evaluation time must be finite and non-negative")

        candidates: list[_VelocityCandidate] = []
        diagnostics: list[str] = []
        rejected_sources: list[str] = []

        if gps is None:
            diagnostics.append("gps:missing")
        else:
            gps_reason = self._gps_rejection_reason(gps, now_s=now_s)
            if gps_reason is None:
                candidates.append(
                    _VelocityCandidate(
                        NavigationVelocitySource.GPS,
                        gps.north_mps,
                        gps.east_mps,
                        max(gps.sigma_mps, self.config.minimum_velocity_sigma_mps),
                        now_s - gps.captured_at_s,
                    )
                )
                diagnostics.append("gps:accepted")
            else:
                diagnostics.append(f"gps:{gps_reason}")
                rejected_sources.append(NavigationVelocitySource.GPS.value)

        if visual_odometry is None:
            diagnostics.append("vio:missing")
        else:
            vio_reason = self._vio_rejection_reason(visual_odometry, now_s=now_s)
            if vio_reason is None:
                candidates.append(
                    _VelocityCandidate(
                        NavigationVelocitySource.VIO,
                        visual_odometry.north_mps,
                        visual_odometry.east_mps,
                        max(
                            visual_odometry.sigma_mps,
                            self.config.minimum_velocity_sigma_mps,
                        ),
                        now_s - visual_odometry.captured_at_s,
                    )
                )
                diagnostics.append("vio:accepted")
            else:
                diagnostics.append(f"vio:{vio_reason}")
                rejected_sources.append(NavigationVelocitySource.VIO.value)

        air_candidate, air_reason = self._air_data_candidate(
            airspeed=airspeed,
            wind=wind,
            now_s=now_s,
        )
        if air_candidate is None:
            diagnostics.append(f"air_data:{air_reason}")
            if airspeed is not None:
                rejected_sources.append(NavigationVelocitySource.AIR_DATA.value)
        else:
            candidates.append(air_candidate)
            diagnostics.append("air_data:accepted")

        if not candidates:
            return self._invalid(
                now_s=now_s,
                reasons=("no_metric_navigation_velocity_source",),
                diagnostics=diagnostics,
                rejected_sources=rejected_sources,
            )

        if len(candidates) == 1:
            candidate = candidates[0]
            return self._solution_from_candidates(
                now_s=now_s,
                accepted=(candidate,),
                rejected=(),
                reasons=("single_navigation_velocity_source",),
                diagnostics=diagnostics,
                pre_rejected_sources=rejected_sources,
            )

        accepted = _largest_consistent_subset(
            tuple(candidates),
            gate_sigma=self.config.consistency_gate_sigma,
        )
        if accepted is None:
            return self._invalid(
                now_s=now_s,
                reasons=("navigation_velocity_sources_inconsistent",),
                diagnostics=diagnostics,
                rejected_sources=[
                    *rejected_sources,
                    *(candidate.source.value for candidate in candidates),
                ],
            )

        rejected = tuple(candidate for candidate in candidates if candidate not in accepted)
        reasons: tuple[str, ...]
        if rejected:
            reasons = ("navigation_velocity_outlier_rejected",)
        elif len(accepted) < self.config.minimum_independent_sources:
            reasons = ("navigation_velocity_redundancy_below_requirement",)
        else:
            reasons = ("navigation_velocity_sources_consistent",)
        return self._solution_from_candidates(
            now_s=now_s,
            accepted=accepted,
            rejected=rejected,
            reasons=reasons,
            diagnostics=diagnostics,
            pre_rejected_sources=rejected_sources,
        )

    def _gps_rejection_reason(
        self,
        measurement: GpsVelocityMeasurement,
        *,
        now_s: float,
    ) -> str | None:
        if measurement.fix_type < self.config.minimum_gps_fix_type:
            return "fix_invalid"
        if measurement.satellites_visible < self.config.minimum_gps_satellites:
            return "satellites_insufficient"
        age_s = now_s - measurement.captured_at_s
        if age_s < 0.0 or age_s > self.config.maximum_gps_age_s:
            return "stale_or_from_future"
        if math.hypot(measurement.north_mps, measurement.east_mps) > (
            self.config.maximum_ground_speed_mps
        ):
            return "velocity_out_of_range"
        return None

    def _vio_rejection_reason(
        self,
        measurement: VisualOdometryVelocityMeasurement,
        *,
        now_s: float,
    ) -> str | None:
        if not measurement.absolute_scale_valid:
            return "absolute_scale_invalid"
        age_s = now_s - measurement.captured_at_s
        if age_s < 0.0 or age_s > self.config.maximum_vio_age_s:
            return "stale_or_from_future"
        if math.hypot(measurement.north_mps, measurement.east_mps) > (
            self.config.maximum_ground_speed_mps
        ):
            return "velocity_out_of_range"
        return None

    def _air_data_candidate(
        self,
        *,
        airspeed: AirspeedMeasurement | None,
        wind: WindVelocityMeasurement | None,
        now_s: float,
    ) -> tuple[_VelocityCandidate | None, str]:
        if airspeed is None:
            return None, "missing"
        if not airspeed.sensor_present_enabled:
            return None, "sensor_not_present_enabled"
        if not airspeed.calibrated:
            return None, "uncalibrated"
        air_age_s = now_s - airspeed.captured_at_s
        if air_age_s < 0.0 or air_age_s > self.config.maximum_airspeed_age_s:
            return None, "stale_or_from_future"
        if wind is None:
            return None, "wind_missing"
        wind_age_s = now_s - wind.captured_at_s
        if wind_age_s < 0.0 or wind_age_s > self.config.maximum_wind_age_s:
            return None, "wind_stale_or_from_future"
        if abs(airspeed.captured_at_s - wind.captured_at_s) > (
            self.config.maximum_airdata_wind_skew_s
        ):
            return None, "wind_time_skew_exceeded"

        heading_rad = math.radians(airspeed.heading_deg)
        north_mps = airspeed.airspeed_mps * math.cos(heading_rad) + wind.north_mps
        east_mps = airspeed.airspeed_mps * math.sin(heading_rad) + wind.east_mps
        if math.hypot(north_mps, east_mps) > self.config.maximum_ground_speed_mps:
            return None, "velocity_out_of_range"
        heading_sigma_mps = airspeed.airspeed_mps * math.radians(
            airspeed.heading_sigma_deg
        )
        sigma_mps = max(
            self.config.minimum_velocity_sigma_mps,
            math.sqrt(
                airspeed.sigma_mps**2 + wind.sigma_mps**2 + heading_sigma_mps**2
            ),
        )
        return (
            _VelocityCandidate(
                NavigationVelocitySource.AIR_DATA,
                north_mps,
                east_mps,
                sigma_mps,
                max(air_age_s, wind_age_s),
            ),
            "accepted",
        )

    def _solution_from_candidates(
        self,
        *,
        now_s: float,
        accepted: tuple[_VelocityCandidate, ...],
        rejected: tuple[_VelocityCandidate, ...],
        reasons: tuple[str, ...],
        diagnostics: list[str],
        pre_rejected_sources: list[str],
    ) -> NavigationVelocitySolution:
        weights = tuple(1.0 / candidate.sigma_mps**2 for candidate in accepted)
        weight_sum = sum(weights)
        north_mps = sum(
            weight * candidate.north_mps
            for weight, candidate in zip(weights, accepted, strict=True)
        ) / weight_sum
        east_mps = sum(
            weight * candidate.east_mps
            for weight, candidate in zip(weights, accepted, strict=True)
        ) / weight_sum
        ground_speed_mps = math.hypot(north_mps, east_mps)
        track_deg = None
        if ground_speed_mps > 1e-9:
            track_deg = math.degrees(math.atan2(east_mps, north_mps)) % 360.0
        sigma_mps = math.sqrt(1.0 / weight_sum)
        pairwise = [
            _normalized_distance(first, second)
            for first, second in itertools.combinations(accepted, 2)
        ]
        maximum_pairwise = max(pairwise, default=0.0)
        consistency = max(
            0.0,
            min(1.0, 1.0 - maximum_pairwise / self.config.consistency_gate_sigma),
        )
        validity = NavigationFusionValidity.VALID
        if rejected or len(accepted) < self.config.minimum_independent_sources:
            validity = NavigationFusionValidity.DEGRADED
        return NavigationVelocitySolution(
            evaluated_at_s=now_s,
            validity=validity,
            reasons=reasons,
            source_diagnostics=tuple(diagnostics),
            sources=tuple(candidate.source.value for candidate in accepted),
            rejected_sources=tuple(
                dict.fromkeys(
                    [
                        *pre_rejected_sources,
                        *(candidate.source.value for candidate in rejected),
                    ]
                )
            ),
            north_mps=north_mps,
            east_mps=east_mps,
            ground_speed_mps=ground_speed_mps,
            track_deg=track_deg,
            velocity_sigma_mps=sigma_mps,
            data_freshness_s=max(candidate.age_s for candidate in accepted),
            sensor_consistency=consistency,
            absolute_scale_valid=True,
        )

    @staticmethod
    def _invalid(
        *,
        now_s: float,
        reasons: tuple[str, ...],
        diagnostics: list[str],
        rejected_sources: list[str],
    ) -> NavigationVelocitySolution:
        return NavigationVelocitySolution(
            evaluated_at_s=now_s,
            validity=NavigationFusionValidity.INVALID,
            reasons=reasons,
            source_diagnostics=tuple(diagnostics),
            sources=(),
            rejected_sources=tuple(dict.fromkeys(rejected_sources)),
        )


def _validate_velocity(
    north_mps: float,
    east_mps: float,
    sigma_mps: float,
    captured_at_s: float,
) -> None:
    values = (north_mps, east_mps, sigma_mps, captured_at_s)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("velocity measurement values must be numeric")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("velocity measurement values must be finite")
    if sigma_mps <= 0.0:
        raise ValueError("velocity uncertainty must be positive")
    if captured_at_s < 0.0:
        raise ValueError("velocity timestamp cannot be negative")


def _normalized_distance(first: _VelocityCandidate, second: _VelocityCandidate) -> float:
    separation_mps = math.hypot(
        first.north_mps - second.north_mps,
        first.east_mps - second.east_mps,
    )
    combined_sigma_mps = math.hypot(first.sigma_mps, second.sigma_mps)
    return separation_mps / max(combined_sigma_mps, 1e-9)


def _largest_consistent_subset(
    candidates: tuple[_VelocityCandidate, ...],
    *,
    gate_sigma: float,
) -> tuple[_VelocityCandidate, ...] | None:
    for size in range(len(candidates), 1, -1):
        consistent: list[tuple[_VelocityCandidate, ...]] = []
        for subset in itertools.combinations(candidates, size):
            if all(
                _normalized_distance(first, second) <= gate_sigma
                for first, second in itertools.combinations(subset, 2)
            ):
                consistent.append(subset)
        if consistent:
            return max(
                consistent,
                key=lambda subset: (
                    sum(1.0 / candidate.sigma_mps**2 for candidate in subset),
                    tuple(candidate.source.value for candidate in subset),
                ),
            )
    return None


__all__ = [
    "AirspeedMeasurement",
    "GpsVelocityMeasurement",
    "NavigationFusionConfig",
    "NavigationFusionMode",
    "NavigationFusionState",
    "NavigationFusionValidity",
    "NavigationVelocityFusionEngine",
    "NavigationVelocitySolution",
    "NavigationVelocitySource",
    "MultimodalNavigationSupervisor",
    "VisualOdometryVelocityMeasurement",
    "WindVelocityMeasurement",
]

from __future__ import annotations

import math

import pytest

from multidetect.navigation_fusion import (
    AirspeedMeasurement,
    GpsVelocityMeasurement,
    MultimodalNavigationSupervisor,
    NavigationFusionConfig,
    NavigationFusionMode,
    NavigationFusionValidity,
    NavigationVelocityFusionEngine,
    VisualOdometryVelocityMeasurement,
    WindVelocityMeasurement,
)


def _gps(
    *,
    north_mps: float = 15.0,
    east_mps: float = 2.0,
    sigma_mps: float = 0.5,
    captured_at_s: float = 10.0,
    fix_type: int = 3,
    satellites_visible: int = 12,
) -> GpsVelocityMeasurement:
    return GpsVelocityMeasurement(
        north_mps=north_mps,
        east_mps=east_mps,
        sigma_mps=sigma_mps,
        captured_at_s=captured_at_s,
        fix_type=fix_type,
        satellites_visible=satellites_visible,
    )


def _vio(
    *,
    north_mps: float = 15.2,
    east_mps: float = 1.8,
    sigma_mps: float = 0.4,
    captured_at_s: float = 10.02,
    absolute_scale_valid: bool = True,
) -> VisualOdometryVelocityMeasurement:
    return VisualOdometryVelocityMeasurement(
        north_mps=north_mps,
        east_mps=east_mps,
        sigma_mps=sigma_mps,
        captured_at_s=captured_at_s,
        absolute_scale_valid=absolute_scale_valid,
    )


def _airspeed(
    *,
    airspeed_mps: float = 14.0,
    heading_deg: float = 0.0,
    captured_at_s: float = 10.01,
    calibrated: bool = True,
    sensor_present_enabled: bool = True,
) -> AirspeedMeasurement:
    return AirspeedMeasurement(
        airspeed_mps=airspeed_mps,
        heading_deg=heading_deg,
        sigma_mps=0.35,
        heading_sigma_deg=1.0,
        captured_at_s=captured_at_s,
        calibrated=calibrated,
        sensor_present_enabled=sensor_present_enabled,
    )


def _wind(
    *,
    north_mps: float = 1.0,
    east_mps: float = 2.0,
    captured_at_s: float = 10.0,
) -> WindVelocityMeasurement:
    return WindVelocityMeasurement(
        north_mps=north_mps,
        east_mps=east_mps,
        sigma_mps=0.4,
        captured_at_s=captured_at_s,
    )


def test_consistent_gps_scaled_vio_and_air_data_produce_metric_velocity() -> None:
    solution = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        gps=_gps(),
        visual_odometry=_vio(),
        airspeed=_airspeed(),
        wind=_wind(),
    )

    assert solution.validity is NavigationFusionValidity.VALID
    assert solution.sources == ("gps", "vio", "air_data")
    assert solution.rejected_sources == ()
    assert solution.absolute_scale_valid is True
    assert solution.north_mps == pytest.approx(15.08, abs=0.15)
    assert solution.east_mps == pytest.approx(1.91, abs=0.15)
    assert solution.ground_speed_mps == pytest.approx(
        math.hypot(solution.north_mps, solution.east_mps)
    )
    assert solution.track_deg is not None and 0.0 < solution.track_deg < 15.0
    assert solution.sensor_consistency > 0.8
    assert solution.advisory_only is True
    assert solution.flight_control_enabled is False


def test_unscaled_monocular_vio_is_excluded_and_gps_remains_degraded() -> None:
    solution = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        gps=_gps(),
        visual_odometry=_vio(absolute_scale_valid=False),
    )

    assert solution.validity is NavigationFusionValidity.DEGRADED
    assert solution.sources == ("gps",)
    assert solution.rejected_sources == ("vio",)
    assert "vio:absolute_scale_invalid" in solution.source_diagnostics
    assert solution.reasons == ("single_navigation_velocity_source",)


def test_two_conflicting_metric_sources_fail_closed_without_velocity() -> None:
    solution = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        gps=_gps(north_mps=15.0, east_mps=0.0, sigma_mps=0.2),
        visual_odometry=_vio(north_mps=-15.0, east_mps=0.0, sigma_mps=0.2),
    )

    assert solution.validity is NavigationFusionValidity.INVALID
    assert solution.reasons == ("navigation_velocity_sources_inconsistent",)
    assert solution.sources == ()
    assert solution.rejected_sources == ("gps", "vio")
    assert solution.north_mps is None
    assert solution.ground_speed_mps is None
    assert solution.absolute_scale_valid is False


def test_consistent_pair_rejects_air_data_outlier() -> None:
    solution = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        gps=_gps(north_mps=10.0, east_mps=0.0, sigma_mps=0.3),
        visual_odometry=_vio(north_mps=10.1, east_mps=0.1, sigma_mps=0.3),
        airspeed=_airspeed(airspeed_mps=35.0),
        wind=_wind(north_mps=0.0, east_mps=0.0),
    )

    assert solution.validity is NavigationFusionValidity.DEGRADED
    assert solution.sources == ("gps", "vio")
    assert solution.rejected_sources == ("air_data",)
    assert solution.reasons == ("navigation_velocity_outlier_rejected",)
    assert solution.north_mps == pytest.approx(10.05, abs=0.1)


def test_invalid_gps_and_missing_metric_vio_publish_no_velocity() -> None:
    solution = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        gps=_gps(fix_type=0, satellites_visible=0),
        visual_odometry=_vio(absolute_scale_valid=False),
        airspeed=_airspeed(sensor_present_enabled=False),
    )

    assert solution.validity is NavigationFusionValidity.INVALID
    assert solution.sources == ()
    assert solution.rejected_sources == ("gps", "vio", "air_data")
    assert "gps:fix_invalid" in solution.source_diagnostics
    assert "vio:absolute_scale_invalid" in solution.source_diagnostics
    assert "air_data:sensor_not_present_enabled" in solution.source_diagnostics


def test_air_data_requires_synchronized_wind_before_ground_velocity_fusion() -> None:
    missing_wind = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        airspeed=_airspeed(),
    )
    stale_wind = NavigationVelocityFusionEngine().solve(
        now_s=10.05,
        airspeed=_airspeed(),
        wind=_wind(captured_at_s=7.0),
    )

    assert missing_wind.validity is NavigationFusionValidity.INVALID
    assert "air_data:wind_missing" in missing_wind.source_diagnostics
    assert stale_wind.validity is NavigationFusionValidity.INVALID
    assert "air_data:wind_stale_or_from_future" in stale_wind.source_diagnostics


def test_navigation_supervisor_falls_back_and_smoothly_reacquires_gps() -> None:
    supervisor = MultimodalNavigationSupervisor(gps_reacquire_samples=2)

    fallback = supervisor.solve(
        now_s=10.05,
        visual_odometry=_vio(),
        airspeed=_airspeed(),
        wind=_wind(),
    )
    first_gps = supervisor.solve(
        now_s=11.05,
        gps=_gps(captured_at_s=11.0),
        visual_odometry=_vio(captured_at_s=11.0),
        airspeed=_airspeed(captured_at_s=11.0),
        wind=_wind(captured_at_s=11.0),
    )
    settled_gps = supervisor.solve(
        now_s=11.15,
        gps=_gps(captured_at_s=11.1),
        visual_odometry=_vio(captured_at_s=11.1),
        airspeed=_airspeed(captured_at_s=11.1),
        wind=_wind(captured_at_s=11.1),
    )

    assert fallback.mode is NavigationFusionMode.VIO_AIRDATA
    assert fallback.solution.absolute_scale_valid is True
    assert first_gps.mode is NavigationFusionMode.GPS_REACQUIRE
    assert first_gps.consecutive_gps_samples == 1
    assert settled_gps.mode is NavigationFusionMode.GPS_FUSED
    assert settled_gps.consecutive_gps_samples == 2


def test_navigation_supervisor_marks_rejected_gps_as_suspect() -> None:
    supervisor = MultimodalNavigationSupervisor()

    state = supervisor.solve(now_s=10.05, gps=_gps(fix_type=1, satellites_visible=0))

    assert state.mode is NavigationFusionMode.GPS_SUSPECT
    assert state.solution.validity is NavigationFusionValidity.INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("maximum_gps_age_s", 0.0),
        ("maximum_vio_age_s", float("nan")),
        ("minimum_gps_fix_type", 1),
        ("minimum_gps_satellites", 65),
        ("minimum_independent_sources", 1),
    ),
)
def test_navigation_fusion_config_rejects_invalid_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        NavigationFusionConfig(**{field: value})

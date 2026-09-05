from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.domain import VehicleTelemetry
from multidetect.multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    DirectRangeMeasurement,
    DirectRangeSource,
    MultiModalRangingEngine,
    RangeValidity,
    RangingFusionConfig,
    TargetImageObservation,
    VerticalMeasurement,
    VerticalSource,
    aircraft_pose_from_telemetry,
    load_camera_calibration,
)
from multidetect.terrain_dem import DemGpsTerrainObservation, LocalDemGrid


def _calibration(**changes: object) -> CameraCalibration:
    calibration = CameraCalibration(
        calibration_id="camera-main-v1",
        width_px=1280,
        height_px=720,
        fx_px=800.0,
        fy_px=800.0,
        cx_px=640.0,
        cy_px=360.0,
        mount_pitch_down_deg=30.0,
    )
    return replace(calibration, **changes)


def _pose(**changes: object) -> AircraftPose:
    return replace(
        AircraftPose(
            captured_at_s=10.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            heading_deg=90.0,
        ),
        **changes,
    )


def _target(**changes: object) -> TargetImageObservation:
    return replace(
        TargetImageObservation(
            target_id="fire-1",
            frame_id="frame-10",
            captured_at_s=10.0,
            center_x=0.5,
            center_y=0.5,
        ),
        **changes,
    )


def _agl(*, height_m: float = 50.0, sigma_m: float = 0.5) -> VerticalMeasurement:
    return VerticalMeasurement(VerticalSource.PIXHAWK_AGL, height_m, sigma_m, 10.0)


def _telemetry_pose_input(**changes: object) -> VehicleTelemetry:
    values: dict[str, object] = {
        "altitude_agl_m": 50.0,
        "roll_deg": 1.0,
        "pitch_deg": -2.0,
        "ground_speed_mps": 12.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": None,
        "heading_deg": 91.0,
        "attitude_observed_at_s": 10.0,
    }
    values.update(changes)
    return VehicleTelemetry(**values)


def test_pose_resolver_prefers_complete_telemetry_uncertainty_and_binds_source() -> None:
    pose = aircraft_pose_from_telemetry(
        _telemetry_pose_input(
            attitude_roll_sigma_deg=0.12,
            attitude_pitch_sigma_deg=0.18,
            attitude_heading_sigma_deg=0.7,
            attitude_uncertainty_source="estimator_covariance",
        ),
        fallback_roll_sigma_deg=1.0,
        fallback_pitch_sigma_deg=1.0,
        fallback_heading_sigma_deg=2.0,
    )
    assert pose is not None
    assert (pose.roll_sigma_deg, pose.pitch_sigma_deg, pose.heading_sigma_deg) == pytest.approx(
        (0.12, 0.18, 0.7)
    )
    assert pose.uncertainty_source == "estimator_covariance"


def test_pose_resolver_uses_explicit_complete_fallback_when_tuple_is_absent() -> None:
    pose = aircraft_pose_from_telemetry(
        _telemetry_pose_input(),
        fallback_roll_sigma_deg=0.4,
        fallback_pitch_sigma_deg=0.5,
        fallback_heading_sigma_deg=1.2,
        fallback_source="field_profile_fallback",
    )
    assert pose is not None
    assert (pose.roll_sigma_deg, pose.pitch_sigma_deg, pose.heading_sigma_deg) == pytest.approx(
        (0.4, 0.5, 1.2)
    )
    assert pose.uncertainty_source == "field_profile_fallback"


def test_pose_resolver_rejects_partial_or_unattributed_uncertainty() -> None:
    assert (
        aircraft_pose_from_telemetry(
            _telemetry_pose_input(attitude_roll_sigma_deg=0.1),
            fallback_roll_sigma_deg=0.4,
            fallback_pitch_sigma_deg=0.5,
            fallback_heading_sigma_deg=1.2,
        )
        is None
    )
    assert (
        aircraft_pose_from_telemetry(
            _telemetry_pose_input(
                attitude_roll_sigma_deg=0.1,
                attitude_pitch_sigma_deg=0.1,
                attitude_heading_sigma_deg=0.5,
            ),
            fallback_roll_sigma_deg=0.4,
            fallback_pitch_sigma_deg=0.5,
            fallback_heading_sigma_deg=1.2,
        )
        is None
    )


def _terrain_grid(
    *,
    east_slope: float = 0.10,
    slope_sigma_m_per_m: float = 0.002,
    half_extent_m: float = 300.0,
) -> LocalDemGrid:
    axis = (-half_extent_m, 0.0, half_extent_m)
    return LocalDemGrid(
        scenario_id="ranging-slope-test",
        origin_latitude_deg=1.3,
        origin_longitude_deg=103.8,
        origin_elevation_m=5.0,
        north_origin_m=-half_extent_m,
        east_origin_m=-half_extent_m,
        north_spacing_m=half_extent_m,
        east_spacing_m=half_extent_m,
        elevations_m=tuple(tuple(5.0 + east_slope * east for east in axis) for _north in axis),
        vertical_sigma_m=0.35,
        slope_sigma_m_per_m=slope_sigma_m_per_m,
        source_sha256="b" * 64,
    )


def _terrain_observation(
    grid: LocalDemGrid,
    *,
    height_m: float = 50.0,
    height_sigma_m: float = 0.5,
    horizontal_sigma_m: float = 0.2,
) -> DemGpsTerrainObservation:
    return DemGpsTerrainObservation(
        grid=grid,
        reference_north_m=0.0,
        reference_east_m=0.0,
        height_m=height_m,
        height_sigma_m=height_sigma_m,
        horizontal_sigma_m=horizontal_sigma_m,
        captured_at_s=10.0,
    )


def test_camera_ground_projection_returns_distance_bearing_and_ci() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.DEGRADED
    assert solution.sources == ("pixhawk_agl", "camera_ground")
    assert solution.slant_range_m == pytest.approx(100.0, rel=0.01)
    assert solution.ground_range_m == pytest.approx(86.6025, rel=0.01)
    assert solution.relative_bearing_deg == pytest.approx(0.0, abs=0.01)
    assert solution.absolute_bearing_deg == pytest.approx(90.0, abs=0.01)
    assert solution.north_offset_m == pytest.approx(0.0, abs=0.01)
    assert solution.east_offset_m == pytest.approx(86.6025, rel=0.01)
    assert solution.slant_range_ci95_m is not None
    assert solution.slant_range_ci95_m[0] < solution.slant_range_m
    assert solution.slant_range_ci95_m[1] > solution.slant_range_m
    assert solution.data_freshness_s == pytest.approx(0.05)
    assert (
        solution.pose_roll_sigma_deg,
        solution.pose_pitch_sigma_deg,
        solution.pose_heading_sigma_deg,
    ) == pytest.approx((0.3, 0.3, 1.0))
    assert solution.pose_uncertainty_source == "configured_default"
    assert solution.advisory_only is True
    assert solution.flight_control_enabled is False
    assert solution.physical_release_enabled is False


def test_dem_terrain_intersection_changes_slant_range_on_uphill_ground() -> None:
    flat = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(),
        terrain_observation=_terrain_observation(_terrain_grid(east_slope=0.0)),
        now_s=10.05,
    )
    uphill = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(),
        terrain_observation=_terrain_observation(_terrain_grid(east_slope=0.10)),
        now_s=10.05,
    )

    assert flat.validity is RangeValidity.DEGRADED
    assert uphill.validity is RangeValidity.DEGRADED
    assert uphill.sources == ("dem_gps", "camera_ground")
    assert flat.slant_range_m == pytest.approx(100.0, rel=1.0e-6)
    expected_uphill = 50.0 / (0.5 + 0.10 * math.cos(math.radians(30.0)))
    assert uphill.slant_range_m == pytest.approx(expected_uphill, rel=1.0e-6)
    assert uphill.slant_range_m < flat.slant_range_m


def test_dem_slope_uncertainty_widens_range_interval() -> None:
    certain = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(),
        terrain_observation=_terrain_observation(_terrain_grid(slope_sigma_m_per_m=0.0)),
        now_s=10.05,
    )
    uncertain = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(),
        terrain_observation=_terrain_observation(_terrain_grid(slope_sigma_m_per_m=0.03)),
        now_s=10.05,
    )

    assert certain.slant_range_ci95_m is not None
    assert uncertain.slant_range_ci95_m is not None
    certain_width = certain.slant_range_ci95_m[1] - certain.slant_range_ci95_m[0]
    uncertain_width = uncertain.slant_range_ci95_m[1] - uncertain.slant_range_ci95_m[0]
    assert uncertain_width > certain_width


def test_dem_intersection_fails_closed_when_target_ray_leaves_coverage() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(),
        terrain_observation=_terrain_observation(_terrain_grid(east_slope=0.0, half_extent_m=20.0)),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("terrain_intersection_unavailable",)
    assert solution.slant_range_m is None


def test_explicit_dem_vertical_measurement_cannot_duplicate_terrain_source() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(VerticalMeasurement(VerticalSource.DEM_GPS, 50.0, 0.5, 10.0),),
        terrain_observation=_terrain_observation(_terrain_grid()),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("duplicate_vertical_source",)


def test_camera_ground_projection_applies_gnss_to_camera_lever_arm() -> None:
    baseline = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        now_s=10.05,
    )
    shifted = MultiModalRangingEngine().solve(
        calibration=_calibration(
            camera_offset_forward_m=2.0,
            camera_offset_right_m=1.0,
        ),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        now_s=10.05,
    )

    assert baseline.north_offset_m == pytest.approx(0.0, abs=0.01)
    # Heading 90 degrees rotates body-forward to east and body-right to south.
    assert shifted.north_offset_m == pytest.approx(-1.0, abs=0.01)
    assert shifted.east_offset_m == pytest.approx(baseline.east_offset_m + 2.0, abs=0.01)
    # A one-metre downward camera displacement reduces the optical-centre
    # height and therefore the inferred camera-to-ground slant range.
    shifted_down = MultiModalRangingEngine().solve(
        calibration=_calibration(camera_offset_down_m=1.0),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        now_s=10.05,
    )
    assert shifted_down.slant_range_m is not None
    assert baseline.slant_range_m is not None
    assert shifted_down.slant_range_m < baseline.slant_range_m


def test_camera_offset_survey_uncertainty_widens_ground_and_bearing_budget() -> None:
    nominal = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(center_x=0.7, center_y=0.55),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )
    surveyed = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(camera_offset_sigma_m=0.75),
        pose=_pose(),
        target=_target(center_x=0.7, center_y=0.55),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert nominal.ground_range_ci95_m is not None
    assert surveyed.ground_range_ci95_m is not None
    assert surveyed.ground_range_ci95_m[1] - surveyed.ground_range_ci95_m[0] > (
        nominal.ground_range_ci95_m[1] - nominal.ground_range_ci95_m[0]
    )
    assert nominal.bearing_sigma_deg is not None
    assert surveyed.bearing_sigma_deg is not None
    assert surveyed.bearing_sigma_deg > nominal.bearing_sigma_deg


def test_bearing_uncertainty_uses_perturbed_lever_arm_target_endpoints() -> None:
    calibration = _calibration(mount_pitch_down_deg=45.0, boresight_sigma_deg=1.0)
    pose = _pose(
        roll_deg=15.0,
        pitch_deg=10.0,
        heading_deg=0.0,
        roll_sigma_deg=1.0,
        pitch_sigma_deg=1.0,
        heading_sigma_deg=0.0,
    )
    target = _target(center_x=0.65, center_y=0.55, center_sigma_px=0.001)
    measurement = (DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 10.0, 0.2, 10.0),)
    datum_at_camera = MultiModalRangingEngine().solve_direct(
        calibration=calibration,
        pose=pose,
        target=target,
        direct_measurements=measurement,
        now_s=10.05,
    )
    surveyed_lever_arm = MultiModalRangingEngine().solve_direct(
        calibration=replace(
            calibration,
            camera_offset_forward_m=5.0,
            camera_offset_right_m=3.0,
            camera_offset_down_m=2.0,
        ),
        pose=pose,
        target=target,
        direct_measurements=measurement,
        now_s=10.05,
    )

    assert datum_at_camera.bearing_sigma_deg is not None
    assert surveyed_lever_arm.bearing_sigma_deg is not None
    # The old ray-only derivative returned the same value for both solutions.
    # At this short range the GNSS-to-camera translation changes the complete
    # target-vector azimuth derivative and therefore must change its sigma.
    assert surveyed_lever_arm.bearing_sigma_deg != pytest.approx(
        datum_at_camera.bearing_sigma_deg,
        abs=0.05,
    )


def test_calibrated_relative_bearing_is_available_without_metric_depth() -> None:
    bearing = MultiModalRangingEngine.relative_bearing_deg(
        calibration=_calibration(mount_pitch_down_deg=0.0),
        target=_target(center_x=0.75),
    )

    assert bearing == pytest.approx(21.801, abs=0.01)


def test_consistent_laser_promotes_multimodal_solution_to_valid() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        direct_measurements=(
            DirectRangeMeasurement(
                DirectRangeSource.LASER,
                "fire-1",
                99.5,
                0.3,
                10.01,
            ),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.VALID
    assert solution.sources == ("pixhawk_agl", "camera_ground", "laser")
    assert solution.slant_range_m == pytest.approx(99.5, abs=0.5)
    assert solution.sensor_consistency > 0.95
    assert solution.reasons == ("multimodal_range_consistent",)


def test_unscaled_vio_is_excluded_without_claiming_absolute_distance() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        direct_measurements=(
            DirectRangeMeasurement(
                DirectRangeSource.VIO,
                "fire-1",
                100.0,
                1.0,
                10.01,
                absolute_scale_valid=False,
            ),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.DEGRADED
    assert solution.sources == ("pixhawk_agl", "camera_ground")
    assert "vio_absolute_scale_invalid" in solution.reasons
    assert "single_absolute_range_method" in solution.reasons


def test_two_conflicting_absolute_methods_fail_closed() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(sigma_m=0.2),),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 170.0, 0.2, 10.01),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("absolute_range_sources_inconsistent",)
    assert solution.slant_range_m is None
    assert solution.slant_range_ci95_m is None


def test_consistent_pair_rejects_one_outlier_and_remains_degraded() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.5, 0.5, 10.01),
            DirectRangeMeasurement(DirectRangeSource.VIO, "fire-1", 250.0, 0.5, 10.01),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.DEGRADED
    assert solution.sources == ("pixhawk_agl", "camera_ground", "laser")
    assert solution.rejected_sources == ("vio",)
    assert "absolute_range_outlier_rejected" in solution.reasons


def test_consistent_but_spread_direct_ranges_inflate_uncertainty() -> None:
    nominal = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 5.0, 10.0),
            DirectRangeMeasurement(DirectRangeSource.VIO, "fire-1", 100.0, 5.0, 10.0),
        ),
        now_s=10.05,
    )
    spread = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 5.0, 10.0),
            DirectRangeMeasurement(DirectRangeSource.VIO, "fire-1", 110.0, 5.0, 10.0),
        ),
        now_s=10.05,
    )

    assert nominal.validity is RangeValidity.DEGRADED
    assert spread.validity is RangeValidity.DEGRADED
    assert "direct_range_uncertainty_inflated" in spread.reasons
    assert "direct_range_uncertainty_inflated" not in nominal.reasons
    assert nominal.slant_range_ci95_m is not None
    assert spread.slant_range_ci95_m is not None
    nominal_width = nominal.slant_range_ci95_m[1] - nominal.slant_range_ci95_m[0]
    spread_width = spread.slant_range_ci95_m[1] - spread.slant_range_ci95_m[0]
    assert spread_width > nominal_width


def test_dem_and_pixhawk_vertical_disagreement_fails_closed() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(
            _agl(height_m=50.0, sigma_m=0.2),
            VerticalMeasurement(VerticalSource.DEM_GPS, 80.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("vertical_references_inconsistent",)


@pytest.mark.parametrize(
    ("pose", "target", "reason"),
    [
        (_pose(captured_at_s=9.0), _target(), "pose_stale_or_from_future"),
        (_pose(), _target(captured_at_s=9.0), "target_image_stale_or_from_future"),
        (_pose(captured_at_s=10.0), _target(captured_at_s=10.15), "pose_image_time_skew_exceeded"),
    ],
)
def test_stale_or_unsynchronized_inputs_are_invalid(
    pose: AircraftPose,
    target: TargetImageObservation,
    reason: str,
) -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=pose,
        target=target,
        vertical_measurements=(_agl(),),
        now_s=10.16,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == (reason,)


def test_horizon_or_upward_ray_never_publishes_ground_range() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(mount_pitch_down_deg=0.0),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("target_ray_does_not_intersect_ground_safely",)
    assert solution.ground_range_m is None


def test_target_mismatched_direct_measurement_is_not_fused() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(),),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "other-target", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.DEGRADED
    assert "laser_target_mismatch" in solution.reasons
    assert solution.sources == ("pixhawk_agl", "camera_ground")


def test_direct_solution_rejects_duplicate_sources() -> None:
    solution = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 101.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("duplicate_direct_range_source",)
    assert solution.slant_range_m is None


def test_direct_solution_rejects_non_finite_evaluation_time() -> None:
    with pytest.raises(ValueError, match="evaluation time"):
        MultiModalRangingEngine().solve_direct(
            calibration=_calibration(),
            pose=_pose(),
            target=_target(),
            direct_measurements=(
                DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
            ),
            now_s=float("nan"),
        )


def test_direct_solution_does_not_publish_ground_range_for_horizon_ray() -> None:
    solution = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(mount_pitch_down_deg=0.0),
        pose=_pose(),
        target=_target(),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("target_ray_does_not_intersect_ground_safely",)
    assert solution.ground_range_m is None


def test_near_horizon_ray_uncertainty_fails_closed() -> None:
    solution = MultiModalRangingEngine(
        RangingFusionConfig(minimum_downward_ray_component=0.001)
    ).solve(
        calibration=_calibration(mount_pitch_down_deg=0.1),
        pose=_pose(),
        target=_target(),
        vertical_measurements=(_agl(height_m=1.0),),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("target_ray_uncertainty_crosses_horizon",)
    assert solution.slant_range_m is None
    assert solution.ground_range_ci95_m is None


def test_direct_near_horizon_ray_uncertainty_fails_closed() -> None:
    solution = MultiModalRangingEngine(
        RangingFusionConfig(minimum_downward_ray_component=0.001)
    ).solve_direct(
        calibration=_calibration(mount_pitch_down_deg=0.1),
        pose=_pose(),
        target=_target(),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("target_ray_uncertainty_crosses_horizon",)
    assert solution.slant_range_m is None
    assert solution.ground_range_ci95_m is None


def test_mount_yaw_uncertainty_crossing_horizon_fails_closed() -> None:
    solution = MultiModalRangingEngine(
        RangingFusionConfig(minimum_downward_ray_component=0.001)
    ).solve(
        calibration=_calibration(
            mount_pitch_down_deg=5.0,
            mount_yaw_right_deg=0.0,
            mount_roll_clockwise_deg=-30.0,
            boresight_sigma_deg=2.0,
        ),
        pose=_pose(
            roll_deg=-45.0,
            pitch_deg=-20.0,
            roll_sigma_deg=0.0,
            pitch_sigma_deg=0.0,
            heading_sigma_deg=0.0,
        ),
        target=_target(center_x=0.8, center_y=0.8, center_sigma_px=1.0e-6),
        vertical_measurements=(_agl(height_m=1.0, sigma_m=0.05),),
        now_s=10.05,
    )

    # The nominal and mount-pitch one-sigma rays still point down, but the
    # positive mount-yaw endpoint crosses the horizon on this banked frame.
    # Omitting the yaw axis would publish a finite yet unsafe ground range.
    assert solution.validity is RangeValidity.INVALID
    assert solution.reasons == ("target_ray_uncertainty_crosses_horizon",)
    assert solution.slant_range_m is None
    assert solution.ground_range_ci95_m is None


def test_ground_range_ci_includes_line_of_sight_uncertainty() -> None:
    narrow = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(center_sigma_px=0.5),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )
    wide = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(),
        target=_target(center_sigma_px=8.0),
        direct_measurements=(
            DirectRangeMeasurement(DirectRangeSource.LASER, "fire-1", 100.0, 0.5, 10.0),
        ),
        now_s=10.05,
    )

    assert narrow.validity is RangeValidity.DEGRADED
    assert wide.validity is RangeValidity.DEGRADED
    assert narrow.ground_range_ci95_m is not None
    assert wide.ground_range_ci95_m is not None
    narrow_width = narrow.ground_range_ci95_m[1] - narrow.ground_range_ci95_m[0]
    wide_width = wide.ground_range_ci95_m[1] - wide.ground_range_ci95_m[0]
    assert wide_width > narrow_width


def test_bearing_sigma_includes_roll_pitch_ray_coupling() -> None:
    direct_measurement = DirectRangeMeasurement(
        DirectRangeSource.LASER,
        "fire-1",
        100.0,
        0.5,
        10.0,
    )
    nominal = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(roll_sigma_deg=0.0, pitch_sigma_deg=0.0),
        target=_target(center_x=0.7, center_y=0.55),
        direct_measurements=(direct_measurement,),
        now_s=10.05,
    )
    banked = MultiModalRangingEngine().solve_direct(
        calibration=_calibration(),
        pose=_pose(
            roll_sigma_deg=5.0,
            pitch_sigma_deg=3.0,
        ),
        target=_target(center_x=0.7, center_y=0.55),
        direct_measurements=(direct_measurement,),
        now_s=10.05,
    )

    assert nominal.bearing_sigma_deg is not None
    assert banked.bearing_sigma_deg is not None
    assert banked.bearing_sigma_deg > nominal.bearing_sigma_deg


def test_boresight_sigma_propagates_all_three_mount_axes() -> None:
    direct_measurement = DirectRangeMeasurement(
        DirectRangeSource.LASER,
        "fire-1",
        100.0,
        0.5,
        10.0,
    )
    calibration = _calibration(
        mount_pitch_down_deg=35.0,
        mount_yaw_right_deg=12.0,
        mount_roll_clockwise_deg=8.0,
        boresight_sigma_deg=2.0,
    )
    pose = _pose(
        roll_deg=18.0,
        pitch_deg=-7.0,
        heading_deg=123.0,
        roll_sigma_deg=0.0,
        pitch_sigma_deg=0.0,
        heading_sigma_deg=0.0,
    )
    target = _target(center_x=0.72, center_y=0.61, center_sigma_px=1.0e-6)

    baseline = MultiModalRangingEngine().solve_direct(
        calibration=replace(calibration, boresight_sigma_deg=0.0),
        pose=pose,
        target=target,
        direct_measurements=(direct_measurement,),
        now_s=10.05,
    )
    surveyed = MultiModalRangingEngine().solve_direct(
        calibration=calibration,
        pose=pose,
        target=target,
        direct_measurements=(direct_measurement,),
        now_s=10.05,
    )

    assert baseline.validity is RangeValidity.DEGRADED
    assert surveyed.validity is RangeValidity.DEGRADED
    assert baseline.ground_range_ci95_m is not None
    assert surveyed.ground_range_ci95_m is not None
    baseline_width = baseline.ground_range_ci95_m[1] - baseline.ground_range_ci95_m[0]
    surveyed_width = surveyed.ground_range_ci95_m[1] - surveyed.ground_range_ci95_m[0]
    assert surveyed_width > baseline_width
    assert surveyed.bearing_sigma_deg is not None
    # Pitch-only propagation plus a generic 2-degree bearing scalar produced
    # exactly 2 degrees for this geometry.  The banked/off-axis ray has real
    # yaw and roll sensitivity, so all three independent mount-axis terms must
    # widen the geometry-derived bearing budget.
    assert surveyed.bearing_sigma_deg > 2.5


def test_off_axis_distorted_projection_produces_finite_bearing() -> None:
    solution = MultiModalRangingEngine().solve(
        calibration=_calibration(k1=-0.12, k2=0.03),
        pose=_pose(heading_deg=350.0),
        target=_target(center_x=0.7, center_y=0.55),
        vertical_measurements=(_agl(),),
        now_s=10.05,
    )

    assert solution.validity is RangeValidity.DEGRADED
    assert solution.relative_bearing_deg is not None
    assert solution.relative_bearing_deg > 0.0
    assert solution.absolute_bearing_deg is not None
    assert 0.0 <= solution.absolute_bearing_deg < 360.0
    assert solution.bearing_sigma_deg is not None
    assert solution.bearing_sigma_deg > 0.0


def test_camera_calibration_loader_requires_explicit_strict_schema(tmp_path: Path) -> None:
    path = tmp_path / "camera.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "calibration": {
                    "calibration_id": "camera-main-v2",
                    "width_px": 1280,
                    "height_px": 720,
                    "fx_px": 810.0,
                    "fy_px": 811.0,
                    "cx_px": 640.0,
                    "cy_px": 360.0,
                    "mount_pitch_down_deg": 25.0,
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_camera_calibration(path)

    assert loaded.calibration_id == "camera-main-v2"
    assert loaded.fx_px == 810.0
    assert loaded.mount_pitch_down_deg == 25.0


def test_camera_calibration_loader_accepts_optional_lever_arm_fields(tmp_path: Path) -> None:
    path = tmp_path / "camera-lever-arm.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "calibration": {
                    "calibration_id": "camera-main-lever-arm-v1",
                    "width_px": 1280,
                    "height_px": 720,
                    "fx_px": 810.0,
                    "fy_px": 811.0,
                    "cx_px": 640.0,
                    "cy_px": 360.0,
                    "camera_offset_forward_m": 0.42,
                    "camera_offset_right_m": -0.08,
                    "camera_offset_down_m": 0.16,
                    "camera_offset_sigma_m": 0.03,
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_camera_calibration(path)

    assert loaded.camera_offset_forward_m == pytest.approx(0.42)
    assert loaded.camera_offset_right_m == pytest.approx(-0.08)
    assert loaded.camera_offset_down_m == pytest.approx(0.16)
    assert loaded.camera_offset_sigma_m == pytest.approx(0.03)


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 2, "calibration": {}},
        {"schema_version": 1, "calibration": {"calibration_id": "incomplete"}},
        {
            "schema_version": 1,
            "calibration": {
                "calibration_id": "camera-main-v2",
                "width_px": 1280,
                "height_px": 720,
                "fx_px": 810.0,
                "fy_px": 811.0,
                "cx_px": 640.0,
                "cy_px": 360.0,
                "misspelled_mount_pitch": 25.0,
            },
        },
    ],
)
def test_camera_calibration_loader_rejects_unknown_or_incomplete_documents(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    path = tmp_path / "camera.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_camera_calibration(path)


def test_camera_calibration_loader_rejects_ambiguous_json_and_accepts_windows_bom(
    tmp_path: Path,
) -> None:
    document = {
        "schema_version": 1,
        "calibration": {
            "calibration_id": "camera-main-v2",
            "width_px": 1280,
            "height_px": 720,
            "fx_px": 810.0,
            "fy_px": 811.0,
            "cx_px": 640.0,
            "cy_px": 360.0,
        },
    }
    valid = tmp_path / "valid-bom.json"
    valid.write_bytes(("\ufeff" + json.dumps(document)).encode("utf-8"))
    assert load_camera_calibration(valid).calibration_id == "camera-main-v2"

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_camera_calibration(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"schema_version":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON number NaN"):
        load_camera_calibration(nonfinite)

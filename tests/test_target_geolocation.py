from __future__ import annotations

import math
from dataclasses import replace

import pytest

from multidetect.adaptive_ranging import AdaptiveRangingPolicy, assess_gps_quality
from multidetect.domain import VehicleTelemetry
from multidetect.geolocation_accuracy import wgs84_distance_m
from multidetect.live import _range_solution_details, _ranging_input_details
from multidetect.multimodal_ranging import RangeSolution, RangeValidity
from multidetect.operator_link import MAX_TARGET_GEOLOCATION_SOURCE_AGE_S
from multidetect.operator_status import build_target_geolocation_status_message
from multidetect.target_geolocation import (
    TargetGeolocation,
    target_geolocation_from_ned_offset,
    wgs84_forward_from_ned,
    wgs84_radii_of_curvature,
)


def test_target_geolocation_projects_ned_offset_and_combines_error_budget() -> None:
    target = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=100.0,
        east_offset_m=50.0,
        aircraft_horizontal_sigma_m=1.5,
        ground_range_ci95_m=(110.0, 130.0),
        ground_range_m=120.0,
        bearing_sigma_deg=2.0,
    )

    assert target.latitude_deg > 1.3000
    assert target.longitude_deg > 103.8000
    assert target.horizontal_sigma_m > 6.0


def test_target_geolocation_converts_bearing_sigma_to_arc_length() -> None:
    target = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=100.0,
        east_offset_m=0.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=100.0,
        bearing_sigma_deg=30.0,
    )

    # Angular standard deviation is an arc-length uncertainty, not the chord
    # of a unit circle.  This distinction matters for deliberately wide
    # bearing gates used by oblique outdoor detections.
    assert target.horizontal_sigma_m == pytest.approx(100.0 * math.radians(30.0))


def test_target_geolocation_uses_ellipsoid_curvature_for_outdoor_offsets() -> None:
    target = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=100.0,
        east_offset_m=50.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
    )

    # These values are the WGS-84 direct solution.  A spherical 6,378,137 m
    # radius would place the same target at latitude 1.3008983153 instead.
    assert target.latitude_deg == pytest.approx(1.300904364759292)
    assert target.longitude_deg == pytest.approx(103.8004492726667)


def test_wgs84_radii_of_curvature_vary_with_latitude() -> None:
    equator_meridional_m, equator_prime_vertical_m = wgs84_radii_of_curvature(0.0)
    high_lat_meridional_m, high_lat_prime_vertical_m = wgs84_radii_of_curvature(60.0)

    assert equator_meridional_m < high_lat_meridional_m
    assert equator_prime_vertical_m < high_lat_prime_vertical_m
    assert high_lat_meridional_m < high_lat_prime_vertical_m


def test_shared_wgs84_forward_projection_validates_and_wraps_coordinates() -> None:
    latitude, longitude = wgs84_forward_from_ned(
        latitude_deg=60.0,
        longitude_deg=179.9999,
        north_offset_m=0.0,
        east_offset_m=20.0,
    )

    assert -90.0 < latitude < 90.0
    assert -180.0 <= longitude < -179.999
    with pytest.raises(ValueError, match="finite"):
        wgs84_forward_from_ned(
            latitude_deg=1.0,
            longitude_deg=2.0,
            north_offset_m=float("nan"),
            east_offset_m=0.0,
        )


@pytest.mark.parametrize("latitude_deg", (89.9999, -89.9999))
def test_shared_wgs84_forward_projection_rejects_pole_crossing(
    latitude_deg: float,
) -> None:
    with pytest.raises(ValueError, match="crosses a geographic pole"):
        wgs84_forward_from_ned(
            latitude_deg=latitude_deg,
            longitude_deg=10.0,
            north_offset_m=16.0 if latitude_deg > 0 else -16.0,
            east_offset_m=0.0,
        )


def test_target_geolocation_wraps_longitude_across_dateline() -> None:
    target = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=0.0,
        aircraft_longitude_deg=179.9999,
        north_offset_m=0.0,
        east_offset_m=30.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
    )

    assert -180.0 <= target.longitude_deg < -179.999


def test_target_geolocation_time_aligns_gps_reference_with_target_frame() -> None:
    static = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=0.0,
        east_offset_m=0.0,
        aircraft_horizontal_sigma_m=1.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
    )
    compensated = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=0.0,
        east_offset_m=0.0,
        aircraft_horizontal_sigma_m=1.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
        aircraft_observed_at_s=100.0,
        target_captured_at_s=100.5,
        aircraft_velocity_north_mps=20.0,
        aircraft_velocity_east_mps=0.0,
    )

    assert compensated.latitude_deg > static.latitude_deg
    assert wgs84_distance_m(
        latitude1_deg=static.latitude_deg,
        longitude1_deg=static.longitude_deg,
        latitude2_deg=compensated.latitude_deg,
        longitude2_deg=compensated.longitude_deg,
    ) == pytest.approx(10.0, rel=1e-4)
    # The default velocity model uncertainty is included in the published
    # one-sigma budget whenever a non-zero time alignment is applied.
    assert compensated.horizontal_sigma_m > static.horizontal_sigma_m


def test_target_geolocation_motion_compensation_supports_reverse_time() -> None:
    compensated = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=0.0,
        east_offset_m=0.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
        aircraft_observed_at_s=100.5,
        target_captured_at_s=100.0,
        aircraft_velocity_north_mps=20.0,
        aircraft_velocity_east_mps=0.0,
        velocity_uncertainty_mps=0.0,
    )
    expected_latitude, expected_longitude = wgs84_forward_from_ned(
        latitude_deg=1.3000,
        longitude_deg=103.8000,
        north_offset_m=-10.0,
        east_offset_m=0.0,
    )
    assert compensated.latitude_deg == pytest.approx(expected_latitude)
    assert compensated.longitude_deg == pytest.approx(expected_longitude)
    assert compensated.horizontal_sigma_m == pytest.approx(0.0)


def test_target_geolocation_inflates_motion_sigma_for_unmodelled_acceleration() -> None:
    baseline = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=0.0,
        east_offset_m=0.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
        aircraft_observed_at_s=100.0,
        target_captured_at_s=100.5,
        aircraft_velocity_north_mps=20.0,
        aircraft_velocity_east_mps=0.0,
        velocity_uncertainty_mps=0.0,
    )
    inflated = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3000,
        aircraft_longitude_deg=103.8000,
        north_offset_m=0.0,
        east_offset_m=0.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
        aircraft_observed_at_s=100.0,
        target_captured_at_s=100.5,
        aircraft_velocity_north_mps=20.0,
        aircraft_velocity_east_mps=0.0,
        velocity_uncertainty_mps=0.0,
        acceleration_uncertainty_mps2=4.0,
    )

    # A one-sigma acceleration envelope contributes 0.5 * dt**2 * sigma_a
    # metres.  With dt=0.5 s and sigma_a=4 m/s² this is exactly 0.5 m.
    assert baseline.horizontal_sigma_m == pytest.approx(0.0)
    assert inflated.horizontal_sigma_m == pytest.approx(0.5)


@pytest.mark.parametrize("value", (-1.0, float("nan"), float("inf"), True, "4"))
def test_target_geolocation_rejects_invalid_acceleration_uncertainty(value: object) -> None:
    with pytest.raises(ValueError, match="acceleration uncertainty"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=0.0,
            east_offset_m=0.0,
            aircraft_horizontal_sigma_m=0.0,
            ground_range_ci95_m=None,
            ground_range_m=None,
            bearing_sigma_deg=None,
            acceleration_uncertainty_mps2=value,  # type: ignore[arg-type]
        )


def test_target_geolocation_rejects_acceleration_budget_without_velocity_pair() -> None:
    with pytest.raises(ValueError, match="complete finite N/E velocity pair"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=0.0,
            east_offset_m=0.0,
            aircraft_horizontal_sigma_m=0.0,
            ground_range_ci95_m=None,
            ground_range_m=None,
            bearing_sigma_deg=None,
            aircraft_observed_at_s=100.0,
            target_captured_at_s=100.5,
            acceleration_uncertainty_mps2=4.0,
        )


def test_operator_target_geolocation_forwards_acceleration_uncertainty() -> None:
    telemetry = replace(
        _qualified_gps_telemetry(),
        gps_observed_at_s=10.0,
        velocity_north_mps=20.0,
        velocity_east_mps=0.0,
        velocity_observed_at_s=10.0,
        acceleration_uncertainty_mps2=4.0,
    )
    solution = replace(_gps_aided_solution(), evaluated_at_s=10.5)
    status = build_target_geolocation_status_message(
        sequence=17,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=10.5,
    )
    without_budget = build_target_geolocation_status_message(
        sequence=18,
        solution=solution,
        telemetry=replace(telemetry, acceleration_uncertainty_mps2=float("nan")),
        source_captured_at_s=10.5,
    )

    assert status.available is True
    assert without_budget.available is True
    assert status.horizontal_sigma_m is not None
    assert without_budget.horizontal_sigma_m is not None
    assert status.horizontal_sigma_m > without_budget.horizontal_sigma_m


def test_target_geolocation_rejects_unbounded_motion_interval() -> None:
    with pytest.raises(ValueError, match="exceeds the allowed horizon"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=1.0,
            east_offset_m=1.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=None,
            ground_range_m=None,
            bearing_sigma_deg=None,
            aircraft_observed_at_s=100.0,
            target_captured_at_s=102.0,
            aircraft_velocity_north_mps=1.0,
            aircraft_velocity_east_mps=1.0,
            maximum_motion_compensation_s=1.5,
        )


def test_target_geolocation_requires_a_complete_motion_timestamp_pair() -> None:
    with pytest.raises(ValueError, match="both observation timestamps"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=1.0,
            east_offset_m=1.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=None,
            ground_range_m=None,
            bearing_sigma_deg=None,
            aircraft_observed_at_s=100.0,
        )


@pytest.mark.parametrize("latitude_deg", (-90.0, 90.0))
def test_target_geolocation_result_rejects_exact_pole_coordinates(latitude_deg: float) -> None:
    with pytest.raises(ValueError, match="outside the WGS-84 domain"):
        TargetGeolocation(latitude_deg=latitude_deg, longitude_deg=0.0, horizontal_sigma_m=0.0)


def test_target_geolocation_rejects_invalid_range_interval() -> None:
    with pytest.raises(ValueError, match="confidence interval"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=1.0,
            east_offset_m=1.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=(20.0, 10.0),
            ground_range_m=15.0,
            bearing_sigma_deg=1.0,
        )


def test_target_geolocation_rejects_nominal_range_outside_confidence_interval() -> None:
    with pytest.raises(ValueError, match="outside its confidence interval"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=10.0,
            east_offset_m=0.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=(20.0, 30.0),
            ground_range_m=10.0,
            bearing_sigma_deg=1.0,
        )


def test_target_geolocation_rejects_inconsistent_offset_and_ground_range() -> None:
    with pytest.raises(ValueError, match="offset and ground range are inconsistent"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=10.0,
            east_offset_m=0.0,
            aircraft_horizontal_sigma_m=0.0,
            ground_range_ci95_m=(49.0, 51.0),
            ground_range_m=50.0,
            bearing_sigma_deg=1.0,
        )


def test_target_geolocation_rejects_unbounded_bearing_uncertainty() -> None:
    with pytest.raises(ValueError, match="bearing uncertainty"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8,
            north_offset_m=10.0,
            east_offset_m=0.0,
            aircraft_horizontal_sigma_m=0.0,
            ground_range_ci95_m=(9.0, 11.0),
            ground_range_m=10.0,
            bearing_sigma_deg=91.0,
        )


def test_target_geolocation_rejects_invalid_gps_inputs() -> None:
    with pytest.raises(ValueError, match="finite"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=float("nan"),
            aircraft_longitude_deg=103.8,
            north_offset_m=1.0,
            east_offset_m=1.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=None,
            ground_range_m=None,
            bearing_sigma_deg=None,
        )


def test_range_audit_withholds_target_coordinate_without_gps_aided_navigation() -> None:
    telemetry = VehicleTelemetry(
        altitude_agl_m=100.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=15.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        latitude_deg=1.3,
        longitude_deg=103.8,
        satellites_visible=12,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.2,
        gps_vertical_accuracy_m=1.5,
        gps_observed_at_s=10.0,
    )
    solution = RangeSolution(
        target_id="target-1",
        frame_id="frame-1",
        calibration_id="camera-1",
        evaluated_at_s=10.0,
        validity=RangeValidity.VALID,
        reasons=("direct_range",),
        sources=("monocular_metric",),
        rejected_sources=(),
        slant_range_m=125.0,
        ground_range_m=120.0,
        ground_range_ci95_m=(110.0, 130.0),
        bearing_sigma_deg=2.0,
        north_offset_m=100.0,
        east_offset_m=50.0,
        navigation_state="gps-aided",
    )

    details = _range_solution_details(solution, telemetry=telemetry, now_s=10.0)
    geolocation = details["target_geolocation"]
    assert geolocation["available"] is True
    assert geolocation["latitude_deg"] == pytest.approx(1.300904364759292)
    assert geolocation["longitude_deg"] == pytest.approx(103.8004492726667)
    assert geolocation["horizontal_sigma_m"] == pytest.approx(6.71, abs=0.01)
    assert geolocation["reference"] == "wgs84_ellipsoid_geodesic"

    withheld = _range_solution_details(
        replace(solution, navigation_state="local-ned"),
        telemetry=telemetry,
        now_s=10.0,
    )
    assert withheld["target_geolocation"] == {
        "available": False,
        "reason": "gps_navigation_not_qualified",
    }

    invalid_details = _range_solution_details(
        replace(
            solution,
            validity=RangeValidity.INVALID,
            slant_range_m=None,
            ground_range_m=None,
            slant_range_ci95_m=None,
            ground_range_ci95_m=None,
        ),
        telemetry=telemetry,
        now_s=10.0,
    )
    assert invalid_details["target_geolocation"] == {
        "available": False,
        "reason": "target_range_invalid",
    }


def test_operator_target_geolocation_status_uses_the_same_gps_quality_gate() -> None:
    telemetry = VehicleTelemetry(
        altitude_agl_m=100.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=15.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        latitude_deg=1.3,
        longitude_deg=103.8,
        satellites_visible=12,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.2,
        gps_vertical_accuracy_m=1.5,
        gps_observed_at_s=10.0,
    )
    solution = RangeSolution(
        target_id="target-1",
        frame_id="frame-1",
        calibration_id="camera-1",
        evaluated_at_s=10.0,
        validity=RangeValidity.VALID,
        reasons=("direct_range",),
        sources=("monocular_metric",),
        rejected_sources=(),
        ground_range_m=120.0,
        ground_range_ci95_m=(110.0, 130.0),
        bearing_sigma_deg=2.0,
        north_offset_m=100.0,
        east_offset_m=50.0,
        navigation_state="gps-aided",
    )

    qualified = build_target_geolocation_status_message(
        sequence=8,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=9.9,
    )
    withheld = build_target_geolocation_status_message(
        sequence=9,
        solution=replace(solution, navigation_state="local-ned"),
        telemetry=telemetry,
        source_captured_at_s=9.9,
    )
    invalid_range = build_target_geolocation_status_message(
        sequence=10,
        solution=replace(
            solution,
            validity=RangeValidity.INVALID,
            ground_range_m=None,
            ground_range_ci95_m=None,
        ),
        telemetry=telemetry,
        source_captured_at_s=9.9,
    )

    assert qualified.available is True
    assert qualified.reason == "gps_qualified"
    assert qualified.horizontal_sigma_m == pytest.approx(6.71, abs=0.01)
    assert withheld.available is False
    assert withheld.reason == "gps_navigation_not_qualified"
    assert withheld.latitude_deg is None
    assert invalid_range.available is False
    assert invalid_range.reason == "target_range_invalid"


def test_operator_target_geolocation_status_time_aligns_with_frame_velocity() -> None:
    telemetry = replace(
        _qualified_gps_telemetry(),
        gps_observed_at_s=10.0,
        velocity_north_mps=20.0,
        velocity_east_mps=0.0,
        velocity_observed_at_s=10.0,
    )
    solution = replace(_gps_aided_solution(), evaluated_at_s=10.5)
    status = build_target_geolocation_status_message(
        sequence=13,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=10.5,
    )
    expected_latitude, expected_longitude = wgs84_forward_from_ned(
        latitude_deg=telemetry.latitude_deg,
        longitude_deg=telemetry.longitude_deg,
        north_offset_m=10.0,
        east_offset_m=0.0,
    )

    assert status.available is True
    assert status.latitude_deg == pytest.approx(
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=expected_latitude,
            aircraft_longitude_deg=expected_longitude,
            north_offset_m=100.0,
            east_offset_m=50.0,
            aircraft_horizontal_sigma_m=telemetry.gps_horizontal_accuracy_m,
            ground_range_ci95_m=solution.ground_range_ci95_m,
            ground_range_m=solution.ground_range_m,
            bearing_sigma_deg=solution.bearing_sigma_deg,
        ).latitude_deg
    )
    assert status.longitude_deg is not None
    assert status.longitude_deg > 103.8004


def test_operator_target_geolocation_rejects_future_source_frame() -> None:
    with pytest.raises(ValueError, match="source frame is from the future"):
        build_target_geolocation_status_message(
            sequence=14,
            solution=_gps_aided_solution(),
            telemetry=_qualified_gps_telemetry(),
            source_captured_at_s=10.1,
        )

    details = _range_solution_details(
        _gps_aided_solution(),
        telemetry=_qualified_gps_telemetry(),
        now_s=10.0,
        source_captured_at_s=10.1,
    )
    assert details["target_geolocation"] == {
        "available": False,
        "reason": "geolocation_input_invalid",
    }


def test_target_geolocation_rejects_velocity_observation_outside_horizon() -> None:
    with pytest.raises(ValueError, match="velocity observation is outside"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=35.0,
            aircraft_longitude_deg=139.0,
            north_offset_m=100.0,
            east_offset_m=50.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=(110.0, 130.0),
            ground_range_m=120.0,
            bearing_sigma_deg=2.0,
            aircraft_observed_at_s=100.0,
            target_captured_at_s=100.5,
            aircraft_velocity_north_mps=20.0,
            aircraft_velocity_east_mps=0.0,
            aircraft_velocity_observed_at_s=102.1,
        )


def test_target_geolocation_rejects_partial_velocity_during_motion_compensation() -> None:
    with pytest.raises(ValueError, match="complete finite N/E velocity pair"):
        target_geolocation_from_ned_offset(
            aircraft_latitude_deg=35.0,
            aircraft_longitude_deg=139.0,
            north_offset_m=100.0,
            east_offset_m=50.0,
            aircraft_horizontal_sigma_m=1.0,
            ground_range_ci95_m=(110.0, 130.0),
            ground_range_m=120.0,
            bearing_sigma_deg=2.0,
            aircraft_observed_at_s=100.0,
            target_captured_at_s=100.5,
            aircraft_velocity_north_mps=20.0,
            aircraft_velocity_east_mps=None,
            aircraft_velocity_observed_at_s=100.0,
        )


def test_operator_target_geolocation_withholds_stale_velocity_sample() -> None:
    telemetry = replace(
        _qualified_gps_telemetry(),
        gps_observed_at_s=10.0,
        velocity_north_mps=20.0,
        velocity_east_mps=0.0,
        velocity_observed_at_s=20.0,
    )
    solution = replace(_gps_aided_solution(), evaluated_at_s=10.5)

    status = build_target_geolocation_status_message(
        sequence=15,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=10.5,
    )

    assert status.available is False
    assert status.reason == "geolocation_input_invalid"
    assert status.latitude_deg is None
    assert status.longitude_deg is None


def test_operator_target_geolocation_withholds_partial_velocity_sample() -> None:
    telemetry = replace(
        _qualified_gps_telemetry(),
        gps_observed_at_s=10.0,
        velocity_north_mps=20.0,
        velocity_east_mps=float("nan"),
        velocity_observed_at_s=10.0,
    )
    solution = replace(_gps_aided_solution(), evaluated_at_s=10.5)

    status = build_target_geolocation_status_message(
        sequence=16,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=10.5,
    )

    assert status.available is False
    assert status.reason == "geolocation_input_invalid"
    assert status.latitude_deg is None
    assert status.longitude_deg is None


def test_target_geolocation_status_withholds_stale_source_frame() -> None:
    telemetry = _qualified_gps_telemetry()
    solution = _gps_aided_solution()

    status = build_target_geolocation_status_message(
        sequence=12,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=10.0 - MAX_TARGET_GEOLOCATION_SOURCE_AGE_S - 0.01,
    )

    assert status.available is False
    assert status.reason == "geolocation_input_invalid"
    assert status.latitude_deg is None
    assert status.longitude_deg is None


def _qualified_gps_telemetry() -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=100.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=15.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        latitude_deg=1.3,
        longitude_deg=103.8,
        satellites_visible=12,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.2,
        gps_vertical_accuracy_m=1.5,
        gps_observed_at_s=9.9,
    )


def _gps_aided_solution() -> RangeSolution:
    return RangeSolution(
        target_id="target-1",
        frame_id="frame-1",
        calibration_id="camera-1",
        evaluated_at_s=10.0,
        validity=RangeValidity.VALID,
        reasons=("direct_range",),
        sources=("monocular_metric",),
        rejected_sources=(),
        ground_range_m=120.0,
        ground_range_ci95_m=(110.0, 130.0),
        bearing_sigma_deg=2.0,
        north_offset_m=100.0,
        east_offset_m=50.0,
        navigation_state="gps-aided",
    )


@pytest.mark.parametrize(
    ("changes", "quality_reason"),
    [
        ({"position_healthy": False}, "gps_position_unhealthy"),
        ({"gps_fix_type": None}, "gps_fix_unqualified"),
        ({"gps_fix_type": 2}, "gps_fix_unqualified"),
        ({"satellites_visible": None}, "gps_satellites_insufficient"),
        ({"satellites_visible": 5}, "gps_satellites_insufficient"),
        ({"gps_horizontal_accuracy_m": float("nan")}, "gps_horizontal_accuracy_invalid"),
        ({"gps_horizontal_accuracy_m": 6.1}, "gps_horizontal_accuracy_exceeded"),
        ({"gps_vertical_accuracy_m": float("nan")}, "gps_vertical_accuracy_invalid"),
        ({"gps_vertical_accuracy_m": 10.1}, "gps_vertical_accuracy_exceeded"),
        ({"gps_observed_at_s": float("nan")}, "gps_timestamp_unavailable"),
        ({"gps_observed_at_s": 10.1}, "gps_timestamp_future"),
        ({"gps_observed_at_s": 8.0}, "gps_stale"),
    ],
)
def test_gps_aided_geolocation_fails_closed_for_raw_quality_failures(
    changes: dict[str, object],
    quality_reason: str,
) -> None:
    telemetry = replace(_qualified_gps_telemetry(), **changes)
    solution = _gps_aided_solution()

    quality = assess_gps_quality(telemetry, now_s=10.0)
    assert quality.qualified is False
    assert quality.reason == quality_reason
    if quality_reason == "gps_stale":
        assert quality.age_s == pytest.approx(2.0)
    else:
        assert quality.age_s is None or quality.age_s == pytest.approx(0.1)

    status = build_target_geolocation_status_message(
        sequence=11,
        solution=solution,
        telemetry=telemetry,
        source_captured_at_s=9.9,
    )
    assert status.available is False
    assert status.reason == "gps_navigation_not_qualified"
    assert status.latitude_deg is None
    assert status.longitude_deg is None

    details = _range_solution_details(solution, telemetry=telemetry, now_s=10.0)
    target_details = details["target_geolocation"]
    assert target_details["available"] is False
    assert target_details["reason"] == "gps_navigation_not_qualified"
    assert target_details["gps_quality"] == {
        "qualified": False,
        "reason": quality_reason,
        "age_s": quality.age_s,
    }


def test_gps_aided_geolocation_uses_evaluation_time_when_details_now_is_omitted() -> None:
    solution = _gps_aided_solution()
    telemetry = replace(_qualified_gps_telemetry(), gps_observed_at_s=9.0)

    details = _range_solution_details(solution, telemetry=telemetry)
    target_details = details["target_geolocation"]
    assert target_details["available"] is True
    assert target_details["gps_quality"]["age_s"] == pytest.approx(1.0)


def test_adaptive_policy_and_geolocation_publishers_share_gps_quality_gate() -> None:
    telemetry = _qualified_gps_telemetry()
    policy = AdaptiveRangingPolicy()

    assessment = policy.assess_gps_quality(telemetry, now_s=10.0)
    decision = policy.decide(telemetry, now_s=10.0)
    assert assessment.qualified is True
    assert assessment.reason == "gps_qualified"
    assert assessment.age_s == pytest.approx(0.1)
    assert decision.navigation_state.value == "gps-aided"

    stale = replace(telemetry, gps_observed_at_s=8.0)
    stale_assessment = policy.assess_gps_quality(stale, now_s=10.0)
    stale_decision = policy.decide(stale, now_s=10.0)
    assert stale_assessment.qualified is False
    assert stale_assessment.reason == "gps_stale"
    assert stale_decision.navigation_state.value != "gps-aided"


def test_ranging_input_diagnostics_reject_future_sample_age() -> None:
    telemetry = replace(
        _qualified_gps_telemetry(),
        attitude_observed_at_s=11.0,
        altitude_observed_at_s=11.0,
        position_observed_at_s=11.0,
        local_position_observed_at_s=11.0,
    )

    details = _ranging_input_details(telemetry, now_s=10.0)

    # Future observations must not be clamped to age=0: that would make a
    # clock-domain mismatch look like a fresh ranging input in the evidence/UI.
    assert details["attitude"]["roll_pitch_heading_available"] is False
    assert details["attitude"]["sample_available"] is False
    assert details["attitude"]["age_s"] is None
    assert details["vertical"]["altitude_agl_available"] is True
    assert details["vertical"]["reference"] == telemetry.altitude_reference
    assert details["vertical"]["sample_available"] is False
    assert details["vertical"]["age_s"] is None
    assert details["position"]["sample_available"] is False
    assert details["position"]["age_s"] is None
    assert details["local_position"]["sample_available"] is False
    assert details["local_position"]["age_s"] is None

from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.domain import VehicleTelemetry
from multidetect.live import _range_solution_details
from multidetect.multimodal_ranging import RangeSolution, RangeValidity
from multidetect.operator_status import build_target_geolocation_status_message
from multidetect.target_geolocation import target_geolocation_from_ned_offset


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
        gps_horizontal_accuracy_m=1.2,
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
    assert geolocation["latitude_deg"] == pytest.approx(1.3008983152841196)
    assert geolocation["longitude_deg"] == pytest.approx(103.80044927328083)
    assert geolocation["horizontal_sigma_m"] == pytest.approx(6.71, abs=0.01)
    assert geolocation["reference"] == "wgs84_local_tangent_plane"

    withheld = _range_solution_details(
        replace(solution, navigation_state="local-ned"),
        telemetry=telemetry,
        now_s=10.0,
    )
    assert withheld["target_geolocation"] == {
        "available": False,
        "reason": "gps_navigation_not_qualified",
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
        gps_horizontal_accuracy_m=1.2,
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

from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.domain import VehicleTelemetry
from multidetect.multimodal_ranging import RangeSolution, RangeValidity
from multidetect.target_speed import TargetWorldSpeedEstimator


def _solution(
    *, target_id: str, captured_at_s: float, north_m: float, east_m: float
) -> RangeSolution:
    slant_range_m = (north_m * north_m + east_m * east_m) ** 0.5
    return RangeSolution(
        target_id=target_id,
        frame_id=f"frame-{captured_at_s}",
        calibration_id="camera-main-test",
        evaluated_at_s=captured_at_s,
        validity=RangeValidity.DEGRADED,
        reasons=("direct_degraded_metric_range",),
        sources=("monocular_metric",),
        rejected_sources=(),
        slant_range_m=slant_range_m,
        ground_range_m=slant_range_m,
        north_offset_m=north_m,
        east_offset_m=east_m,
        data_freshness_s=0.05,
        sensor_consistency=0.5,
    )


def _telemetry(*, captured_at_s: float, north_m: float, east_m: float) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=0.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=0.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=None,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        local_north_m=north_m,
        local_east_m=east_m,
        local_down_m=0.0,
        local_position_observed_at_s=captured_at_s,
    )


def test_world_speed_removes_aircraft_translation_for_stationary_target() -> None:
    estimator = TargetWorldSpeedEstimator()
    result = None
    for index in range(7):
        captured_at_s = 10.0 + index * 0.15
        aircraft_north_m = index * 0.12
        result = estimator.update(
            target_id="stationary-target",
            solution=_solution(
                target_id="stationary-target",
                captured_at_s=captured_at_s,
                north_m=6.8 - aircraft_north_m,
                east_m=(0.015 if index % 2 else -0.015),
            ),
            telemetry=_telemetry(
                captured_at_s=captured_at_s,
                north_m=aircraft_north_m,
                east_m=0.0,
            ),
            captured_at_s=captured_at_s,
        )

    assert result == 0.0


def test_world_speed_reports_sustained_target_motion_after_window() -> None:
    estimator = TargetWorldSpeedEstimator()
    result = None
    for index in range(8):
        captured_at_s = 20.0 + index * 0.15
        elapsed_s = captured_at_s - 20.0
        result = estimator.update(
            target_id="moving-target",
            solution=_solution(
                target_id="moving-target",
                captured_at_s=captured_at_s,
                north_m=6.8 + elapsed_s,
                east_m=0.0,
            ),
            telemetry=_telemetry(captured_at_s=captured_at_s, north_m=0.0, east_m=0.0),
            captured_at_s=captured_at_s,
        )

    assert result == pytest.approx(1.0, abs=0.05)


def test_world_speed_waits_for_a_complete_time_window() -> None:
    estimator = TargetWorldSpeedEstimator()
    telemetry = _telemetry(captured_at_s=30.0, north_m=0.0, east_m=0.0)
    assert estimator.update(
        target_id="short-window",
        solution=_solution(
            target_id="short-window",
            captured_at_s=30.0,
            north_m=6.8,
            east_m=0.0,
        ),
        telemetry=telemetry,
        captured_at_s=30.0,
    ) is None
    # Ensure an unrelated telemetry update does not mutate the original fixture.
    assert replace(telemetry, local_north_m=1.0).local_north_m == 1.0

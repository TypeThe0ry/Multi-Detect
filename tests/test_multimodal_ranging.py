from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    DirectRangeMeasurement,
    DirectRangeSource,
    MultiModalRangingEngine,
    RangeValidity,
    TargetImageObservation,
    VerticalMeasurement,
    VerticalSource,
    load_camera_calibration,
)


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
    assert solution.advisory_only is True
    assert solution.flight_control_enabled is False
    assert solution.physical_release_enabled is False


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

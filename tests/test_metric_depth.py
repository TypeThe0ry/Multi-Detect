from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pytest

from multidetect.domain import BoundingBox
from multidetect.metric_depth import (
    DEPTH_ANYTHING_V2_METRIC_OUTDOOR_SMALL_ONNX_SHA256,
    AsyncMetricDepthRunner,
    MetricDepthConfig,
    MetricDepthEstimator,
)
from multidetect.multimodal_ranging import CameraCalibration, DirectRangeSource

CALIBRATION = CameraCalibration(
    calibration_id="metric-test",
    width_px=1280,
    height_px=720,
    fx_px=800.0,
    fy_px=800.0,
    cx_px=640.0,
    cy_px=360.0,
)


def test_repository_outdoor_metric_depth_export_digest_is_pinned() -> None:
    assert DEPTH_ANYTHING_V2_METRIC_OUTDOOR_SMALL_ONNX_SHA256 == (
        "36b38a0b34f5431a9bbcbedca7d72b440006204910d13227bc844641b0b23e52"
    )


@dataclass(frozen=True)
class _Tensor:
    name: str
    shape: tuple[object, ...]


class _DepthSession:
    def __init__(self, depth: np.ndarray) -> None:
        self.depth = depth
        self.calls = 0

    def get_inputs(self):
        return (_Tensor("image", ("batch", 3, "height", "width")),)

    def get_providers(self):
        return ("TestExecutionProvider",)

    def run(self, _outputs, feeds):
        assert feeds["image"].shape == (1, 3, 518, 518)
        self.calls += 1
        return [self.depth]


class _ProviderSpecificFailure(LookupError):
    pass


class _FailingDepthSession(_DepthSession):
    def run(self, _outputs, _feeds):
        raise _ProviderSpecificFailure("synthetic provider failure")


def _estimator(depth: np.ndarray) -> MetricDepthEstimator:
    return MetricDepthEstimator(
        MetricDepthConfig(model_path=Path("unused.onnx")),
        session=_DepthSession(depth),
    )


def test_metric_depth_uses_robust_center_box_and_publishes_absolute_measurement() -> None:
    depth = np.full((1, 518, 518), 7.5, dtype=np.float32)
    depth[:, :120, :] = 18.0
    estimator = _estimator(depth)

    result = estimator.estimate(
        calibration=CALIBRATION,
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
        target_id="manual-1",
        bbox=BoundingBox(0.25, 0.25, 0.75, 0.75),
        frame_id="frame-1",
        captured_at_s=10.0,
    )

    assert result.slant_range_m == pytest.approx(7.5)
    assert result.sigma_m >= 1.875
    assert result.valid_pixel_count > 10_000
    assert result.provider_names == ("TestExecutionProvider",)
    measurement = result.measurement()
    assert measurement.source is DirectRangeSource.MONOCULAR_METRIC
    assert measurement.target_id == "manual-1"
    assert measurement.absolute_scale_valid is True


def test_async_metric_depth_holds_last_manual_lck_result_without_blocking_submit() -> None:
    estimator = _estimator(np.full((1, 518, 518), 3.25, dtype=np.float32))
    runner = AsyncMetricDepthRunner(estimator)
    try:
        started = time.perf_counter()
        assert runner.submit(
            calibration=CALIBRATION,
            image_bgr=np.zeros((480, 640, 3), dtype=np.uint8),
            target_id="manual-2",
            bbox=BoundingBox(0.2, 0.2, 0.8, 0.8),
            frame_id="frame-2",
            captured_at_s=started,
            now_s=started,
        )
        assert time.perf_counter() - started < 0.10
        measurement = None
        deadline = time.monotonic() + 2.0
        while measurement is None and time.monotonic() < deadline:
            measurement = runner.measurement_for(
                target_id="manual-2",
                bbox=BoundingBox(0.2, 0.2, 0.8, 0.8),
                now_s=time.perf_counter(),
            )
            time.sleep(0.005)
        assert measurement is not None
        assert measurement.slant_range_m == pytest.approx(3.25)
        assert runner.inference_count == 1
        assert runner.failure_count == 0
    finally:
        runner.close()


def test_async_metric_depth_rejects_result_when_same_track_moves_substantially() -> None:
    estimator = _estimator(np.full((1, 518, 518), 4.0, dtype=np.float32))
    runner = AsyncMetricDepthRunner(estimator)
    source_bbox = BoundingBox(0.10, 0.10, 0.30, 0.30)
    try:
        started = time.perf_counter()
        assert runner.submit(
            calibration=CALIBRATION,
            image_bgr=np.zeros((480, 640, 3), dtype=np.uint8),
            target_id="moving-track",
            bbox=source_bbox,
            frame_id="frame-moving-track",
            captured_at_s=started,
            now_s=started,
        )
        deadline = time.monotonic() + 2.0
        while runner.latest_result() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        accepted = runner.measurement_association_for(
            target_id="moving-track",
            bbox=source_bbox,
            now_s=time.perf_counter(),
        )
        assert accepted.status == "accepted"
        assert accepted.measurement is not None
        assert accepted.measurement.slant_range_m == pytest.approx(
            4.0 * math.sqrt(1 + 0.48**2 + 0.27**2)
        )
        assert accepted.source_iou == pytest.approx(1.0)
        assert accepted.center_distance == pytest.approx(0.0)
        assert accepted.compatible_sample_count == 1
        rejected = runner.measurement_association_for(
            target_id="moving-track",
            bbox=BoundingBox(0.65, 0.65, 0.85, 0.85),
            now_s=time.perf_counter(),
        )
        assert rejected.measurement is None
        assert rejected.status == "geometry_rejected"
        assert rejected.source_result is not None
        assert rejected.source_iou == pytest.approx(0.0)
        assert rejected.center_distance is not None
        assert rejected.center_distance > 0.15
    finally:
        runner.close()


def test_async_metric_depth_isolates_provider_specific_failure() -> None:
    estimator = MetricDepthEstimator(
        MetricDepthConfig(model_path=Path("unused.onnx")),
        session=_FailingDepthSession(np.zeros((1, 518, 518), dtype=np.float32)),
    )
    runner = AsyncMetricDepthRunner(estimator)
    try:
        started = time.perf_counter()
        assert runner.submit(
            calibration=CALIBRATION,
            image_bgr=np.zeros((480, 640, 3), dtype=np.uint8),
            target_id="manual-provider-failure",
            bbox=BoundingBox(0.2, 0.2, 0.8, 0.8),
            frame_id="frame-provider-failure",
            captured_at_s=started,
            now_s=started,
        )
        deadline = time.monotonic() + 2.0
        while runner.failure_count == 0 and time.monotonic() < deadline:
            assert runner.latest_result() is None
            time.sleep(0.005)
        assert runner.failure_count == 1
        assert runner.last_error == ("_ProviderSpecificFailure: synthetic provider failure")
    finally:
        runner.close()


def test_metric_depth_rejects_invalid_or_out_of_domain_dense_output() -> None:
    estimator = _estimator(np.full((1, 518, 518), np.nan, dtype=np.float32))
    with pytest.raises(RuntimeError, match="too few valid"):
        estimator.estimate(
            calibration=CALIBRATION,
            image_bgr=np.zeros((32, 32, 3), dtype=np.uint8),
            target_id="manual-3",
            bbox=BoundingBox(0.2, 0.2, 0.8, 0.8),
            frame_id="frame-3",
            captured_at_s=10.0,
        )


def test_metric_depth_applies_single_anchor_calibration_and_exports_grid() -> None:
    estimator = MetricDepthEstimator(
        MetricDepthConfig(
            model_path=Path("unused.onnx"),
            calibration_scale=0.85,
            calibration_profile="indoor-single-anchor-6.8m",
            grid_width=160,
            grid_height=90,
        ),
        session=_DepthSession(np.full((1, 518, 518), 8.0, dtype=np.float32)),
    )

    result = estimator.estimate(
        calibration=CALIBRATION,
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
        target_id="manual-anchor",
        bbox=BoundingBox(0.25, 0.25, 0.75, 0.75),
        frame_id="frame-anchor",
        captured_at_s=20.0,
    )

    assert result.raw_slant_range_m == pytest.approx(8.0)
    assert result.slant_range_m == pytest.approx(6.8)
    assert result.calibration_scale == pytest.approx(0.85)
    assert result.calibration_profile == "indoor-single-anchor-6.8m"
    assert (result.depth_grid.width, result.depth_grid.height) == (160, 90)
    assert result.depth_grid.encoding == "logarithmic"
    assert result.depth_grid.depth_at(0.5, 0.5) == pytest.approx(6.8, abs=0.12)


def test_metric_depth_applies_piecewise_outdoor_curve_and_reports_mode() -> None:
    estimator = MetricDepthEstimator(
        MetricDepthConfig(
            model_path=Path("unused.onnx"),
            calibration_profile="outdoor-piecewise-v1",
            calibration_curve_raw_m=(4.0, 8.0, 14.0, 20.0),
            calibration_curve_actual_m=(2.0, 4.5, 8.5, 12.0),
            calibration_extrapolation_sigma_m=4.0,
        ),
        session=_DepthSession(np.full((1, 518, 518), 14.0, dtype=np.float32)),
    )
    result = estimator.estimate(
        calibration=CALIBRATION,
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
        target_id="outdoor-piecewise-target",
        bbox=BoundingBox(0.25, 0.25, 0.75, 0.75),
        frame_id="frame-piecewise",
        captured_at_s=40.0,
    )

    assert result.raw_slant_range_m == pytest.approx(14.0)
    assert result.slant_range_m == pytest.approx(8.5)
    assert result.calibration_mode == "piecewise_linear"
    assert result.calibration_curve_raw_range_m == (4.0, 20.0)
    assert result.calibration_extrapolated is False


def test_metric_depth_inflates_sigma_for_curve_extrapolation() -> None:
    estimator = MetricDepthEstimator(
        MetricDepthConfig(
            model_path=Path("unused.onnx"),
            calibration_curve_raw_m=(4.0, 8.0, 14.0, 20.0),
            calibration_curve_actual_m=(2.0, 4.5, 8.5, 12.0),
            calibration_extrapolation_sigma_m=4.0,
        ),
        session=_DepthSession(np.full((1, 518, 518), 30.0, dtype=np.float32)),
    )
    result = estimator.estimate(
        calibration=CALIBRATION,
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
        target_id="outdoor-extrapolated-target",
        bbox=BoundingBox(0.25, 0.25, 0.75, 0.75),
        frame_id="frame-extrapolated",
        captured_at_s=41.0,
    )

    assert result.calibration_extrapolated is True
    assert result.slant_range_m > 12.0
    assert result.sigma_m >= 4.0


def test_metric_depth_samples_fire_base_instead_of_translucent_plume_center() -> None:
    depth = np.full((1, 518, 518), 10.0, dtype=np.float32)
    depth[:, 330:, :] = 4.0
    estimator = _estimator(depth)
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    bbox = BoundingBox(0.2, 0.2, 0.8, 0.8)

    manual = estimator.estimate(
        calibration=CALIBRATION,
        image_bgr=image,
        target_id="manual-plume",
        bbox=bbox,
        target_label="manual",
        frame_id="frame-manual-plume",
        captured_at_s=30.0,
    )
    flame = estimator.estimate(
        calibration=CALIBRATION,
        image_bgr=image,
        target_id="flame-base",
        bbox=bbox,
        target_label="flame",
        frame_id="frame-flame-base",
        captured_at_s=30.1,
    )

    assert manual.slant_range_m == pytest.approx(10.0)
    assert flame.slant_range_m == pytest.approx(4.0)


@pytest.mark.parametrize("x, y", [(0.0, 0.0), (0.4, 0.0), (0.3, -0.2)])
@pytest.mark.parametrize("distorted", [False, True])
def test_metric_depth_converts_z_using_undistorted_target_center_ray(
    x: float,
    y: float,
    distorted: bool,
) -> None:
    calibration = replace(
        CALIBRATION,
        k1=0.15 if distorted else 0.0,
        k2=-0.02 if distorted else 0.0,
        k3=0.005 if distorted else 0.0,
        p1=0.01 if distorted else 0.0,
        p2=-0.008 if distorted else 0.0,
    )
    # Forward-project a known camera ray, independently of the inversion under test.
    r2 = x * x + y * y
    radial = 1 + calibration.k1 * r2 + calibration.k2 * r2**2 + calibration.k3 * r2**3
    xd = x * radial + 2 * calibration.p1 * x * y + calibration.p2 * (r2 + 2 * x * x)
    yd = y * radial + calibration.p1 * (r2 + 2 * y * y) + 2 * calibration.p2 * x * y
    u = (calibration.cx_px + calibration.fx_px * xd) / calibration.width_px
    v = (calibration.cy_px + calibration.fy_px * yd) / calibration.height_px
    estimator = MetricDepthEstimator(
        MetricDepthConfig(
            model_path=Path("unused.onnx"),
            calibration_scale=0.75,
            calibration_offset_m=0.5,
        ),
        session=_DepthSession(np.full((1, 518, 518), 8.0, dtype=np.float32)),
    )
    result = estimator.estimate(
        image_bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        calibration=calibration,
        target_id="off-axis",
        bbox=BoundingBox(u - 0.05, v - 0.05, u + 0.05, v + 0.05),
        frame_id="ray-frame",
        captured_at_s=50.0,
    )
    factor = math.sqrt(1 + x * x + y * y)
    assert result.optical_axis_depth_m == pytest.approx(6.5)
    assert result.raw_optical_axis_depth_m == pytest.approx(8.0)
    assert result.slant_range_m == pytest.approx(6.5 * factor, rel=1e-7)
    assert result.raw_slant_range_m == pytest.approx(8.0 * factor, rel=1e-7)
    assert result.sigma_m == pytest.approx(1.625 * factor, rel=1e-7)
    assert result.measurement().slant_range_m == pytest.approx(6.5 * factor, rel=1e-7)
    assert result.depth_grid.depth_at(u, v) == pytest.approx(6.5, rel=0.02)


def test_metric_depth_requires_intrinsics_instead_of_publishing_z_as_slant() -> None:
    estimator = _estimator(np.full((1, 518, 518), 8.0, dtype=np.float32))
    with pytest.raises(RuntimeError, match="requires camera calibration"):
        estimator.estimate(
            image_bgr=np.zeros((360, 640, 3), dtype=np.uint8),
            target_id="missing-intrinsics",
            bbox=BoundingBox(0.6, 0.4, 0.8, 0.6),
            frame_id="missing-intrinsics",
            captured_at_s=50.0,
        )

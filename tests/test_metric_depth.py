from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from multidetect.domain import BoundingBox
from multidetect.metric_depth import (
    AsyncMetricDepthRunner,
    MetricDepthConfig,
    MetricDepthEstimator,
)
from multidetect.multimodal_ranging import DirectRangeSource


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
            measurement = runner.measurement_for(target_id="manual-2", now_s=time.perf_counter())
            time.sleep(0.005)
        assert measurement is not None
        assert measurement.slant_range_m == pytest.approx(3.25)
        assert runner.inference_count == 1
        assert runner.failure_count == 0
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
        assert runner.last_error == (
            "_ProviderSpecificFailure: synthetic provider failure"
        )
    finally:
        runner.close()


def test_metric_depth_rejects_invalid_or_out_of_domain_dense_output() -> None:
    estimator = _estimator(np.full((1, 518, 518), np.nan, dtype=np.float32))
    with pytest.raises(RuntimeError, match="too few valid"):
        estimator.estimate(
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


def test_metric_depth_samples_fire_base_instead_of_translucent_plume_center() -> None:
    depth = np.full((1, 518, 518), 10.0, dtype=np.float32)
    depth[:, 330:, :] = 4.0
    estimator = _estimator(depth)
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    bbox = BoundingBox(0.2, 0.2, 0.8, 0.8)

    manual = estimator.estimate(
        image_bgr=image,
        target_id="manual-plume",
        bbox=bbox,
        target_label="manual",
        frame_id="frame-manual-plume",
        captured_at_s=30.0,
    )
    flame = estimator.estimate(
        image_bgr=image,
        target_id="flame-base",
        bbox=bbox,
        target_label="flame",
        frame_id="frame-flame-base",
        captured_at_s=30.1,
    )

    assert manual.slant_range_m == pytest.approx(10.0)
    assert flame.slant_range_m == pytest.approx(4.0)

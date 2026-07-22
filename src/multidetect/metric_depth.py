from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .domain import BoundingBox
from .multimodal_ranging import DirectRangeMeasurement, DirectRangeSource
from .tensorrt_session import TensorRtDepthSession
from .vision import VisionDependencyError

DEPTH_ANYTHING_V2_METRIC_INDOOR_SMALL_ONNX_SHA256 = (
    "2e25a3f332b34d885a1b3059cab471f7916a36099325810821b2d6e0471f74ad"
)


class MetricDepthContractError(RuntimeError):
    """Raised when the depth model or its dense output violates the metric contract."""


@dataclass(frozen=True, slots=True)
class MetricDepthConfig:
    model_path: Path
    input_size: int = 518
    minimum_interval_s: float = 0.20
    maximum_result_age_s: float = 1.00
    center_fraction: float = 0.50
    minimum_depth_m: float = 0.4
    maximum_depth_m: float = 800.0
    minimum_valid_pixels: int = 64
    minimum_sigma_m: float = 0.50
    relative_sigma: float = 0.25
    calibration_scale: float = 1.0
    calibration_offset_m: float = 0.0
    calibration_profile: str = "uncalibrated"
    grid_width: int = 160
    grid_height: int = 90
    temporal_window_size: int = 5
    grid_encoding: str = "logarithmic"
    providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.input_size <= 0 or self.input_size % 14 != 0:
            raise ValueError("metric-depth input size must be positive and divisible by 14")
        numeric = (
            self.minimum_interval_s,
            self.maximum_result_age_s,
            self.center_fraction,
            self.minimum_depth_m,
            self.maximum_depth_m,
            self.minimum_sigma_m,
            self.relative_sigma,
            self.calibration_scale,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in numeric):
            raise ValueError("metric-depth limits must be finite and positive")
        if self.minimum_interval_s >= self.maximum_result_age_s:
            raise ValueError("metric-depth result age must exceed its inference interval")
        if not 0.1 <= self.center_fraction <= 1.0:
            raise ValueError("metric-depth center fraction must be in [0.1, 1]")
        if self.minimum_depth_m >= self.maximum_depth_m:
            raise ValueError("metric-depth range limits are reversed")
        if self.minimum_valid_pixels < 16:
            raise ValueError("metric-depth minimum valid pixels must be at least 16")
        if not math.isfinite(self.calibration_offset_m):
            raise ValueError("metric-depth calibration offset must be finite")
        if not self.calibration_profile.strip():
            raise ValueError("metric-depth calibration profile cannot be empty")
        if not 16 <= self.grid_width <= 640 or not 16 <= self.grid_height <= 360:
            raise ValueError("metric-depth grid dimensions are outside the supported range")
        if not 1 <= self.temporal_window_size <= 15:
            raise ValueError("metric-depth temporal window must be in [1, 15]")
        if self.grid_encoding not in {"linear", "logarithmic"}:
            raise ValueError("metric-depth grid encoding must be linear or logarithmic")


@dataclass(frozen=True, slots=True)
class MetricDepthGrid:
    """One calibrated, quantized full-frame depth map for QGC visualization."""

    width: int
    height: int
    minimum_depth_m: float
    maximum_depth_m: float
    quantized_depth: bytes
    encoding: str = "logarithmic"

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("metric-depth grid dimensions must be positive")
        if len(self.quantized_depth) != self.width * self.height:
            raise ValueError("metric-depth grid payload size is inconsistent")
        if not (
            math.isfinite(self.minimum_depth_m)
            and math.isfinite(self.maximum_depth_m)
            and 0.0 < self.minimum_depth_m < self.maximum_depth_m
        ):
            raise ValueError("metric-depth grid range is invalid")
        if self.encoding not in {"linear", "logarithmic"}:
            raise ValueError("metric-depth grid encoding is invalid")

    def depth_at(self, normalized_x: float, normalized_y: float) -> float | None:
        if not 0.0 <= normalized_x <= 1.0 or not 0.0 <= normalized_y <= 1.0:
            raise ValueError("metric-depth grid lookup must be normalized")
        x = min(self.width - 1, int(normalized_x * self.width))
        y = min(self.height - 1, int(normalized_y * self.height))
        encoded = self.quantized_depth[y * self.width + x]
        if encoded == 0:
            return None
        fraction = (encoded - 1) / 254.0
        if self.encoding == "logarithmic":
            return math.exp(
                math.log(self.minimum_depth_m)
                + fraction * math.log(self.maximum_depth_m / self.minimum_depth_m)
            )
        return self.minimum_depth_m + fraction * (self.maximum_depth_m - self.minimum_depth_m)


@dataclass(frozen=True, slots=True)
class MetricDepthResult:
    target_id: str
    frame_id: str
    captured_at_s: float
    produced_at_s: float
    slant_range_m: float
    raw_slant_range_m: float
    sigma_m: float
    valid_pixel_count: int
    processing_time_ms: float
    provider_names: tuple[str, ...]
    calibration_scale: float
    calibration_offset_m: float
    calibration_profile: str
    depth_grid: MetricDepthGrid

    def measurement(self) -> DirectRangeMeasurement:
        return DirectRangeMeasurement(
            source=DirectRangeSource.MONOCULAR_METRIC,
            target_id=self.target_id,
            slant_range_m=self.slant_range_m,
            sigma_m=self.sigma_m,
            captured_at_s=self.captured_at_s,
            absolute_scale_valid=True,
        )


class MetricDepthEstimator:
    """Metric depth inference and robust target-box sampling."""

    def __init__(self, config: MetricDepthConfig, *, session: Any | None = None) -> None:
        self.config = config
        self._np, self._cv2 = self._dependencies()
        if session is None:
            session = self._load_session()
        self._session = session
        inputs = tuple(session.get_inputs())
        if len(inputs) != 1:
            raise MetricDepthContractError("metric-depth model must expose exactly one input")
        self._input_name = inputs[0].name
        shape = tuple(inputs[0].shape)
        if len(shape) != 4 or shape[1] not in (3, "3", None):
            raise MetricDepthContractError("metric-depth input must be NCHW RGB")
        self.provider_names = tuple(
            str(value) for value in getattr(session, "get_providers", lambda: ())()
        )

    def estimate(
        self,
        *,
        image_bgr: Any,
        target_id: str,
        bbox: BoundingBox,
        target_label: str = "manual",
        frame_id: str,
        captured_at_s: float,
    ) -> MetricDepthResult:
        if not target_id.strip() or not frame_id.strip():
            raise ValueError("metric-depth target and frame IDs cannot be empty")
        started_s = time.perf_counter()
        image = self._np.asarray(image_bgr)
        if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
            raise MetricDepthContractError("metric-depth image must be non-empty BGR")
        size = self.config.input_size
        rgb = self._cv2.cvtColor(image, self._cv2.COLOR_BGR2RGB)
        resized = self._cv2.resize(rgb, (size, size), interpolation=self._cv2.INTER_CUBIC)
        tensor = resized.astype(self._np.float32) / 255.0
        tensor = (tensor - self._np.asarray((0.485, 0.456, 0.406), dtype=self._np.float32)) / (
            self._np.asarray((0.229, 0.224, 0.225), dtype=self._np.float32)
        )
        tensor = self._np.ascontiguousarray(self._np.transpose(tensor, (2, 0, 1))[None])
        outputs = self._session.run(None, {self._input_name: tensor})
        if len(outputs) != 1:
            raise MetricDepthContractError("metric-depth model must expose one dense output")
        raw_depth = self._np.asarray(outputs[0], dtype=self._np.float32).squeeze()
        if raw_depth.ndim != 2 or raw_depth.size < self.config.minimum_valid_pixels:
            raise MetricDepthContractError("metric-depth output must be a dense HxW map")
        depth = raw_depth * self.config.calibration_scale + self.config.calibration_offset_m
        values = self._target_values(depth, bbox, target_label=target_label)
        raw_values = self._target_values(raw_depth, bbox, target_label=target_label)
        if values.size < self.config.minimum_valid_pixels:
            raise MetricDepthContractError("metric-depth target has too few valid depth pixels")
        slant_range_m = float(self._np.median(values))
        raw_slant_range_m = float(self._np.median(raw_values))
        mad = float(self._np.median(self._np.abs(values - slant_range_m)))
        sigma_m = max(
            self.config.minimum_sigma_m,
            slant_range_m * self.config.relative_sigma,
            mad * 1.4826 * 2.0,
        )
        return MetricDepthResult(
            target_id=target_id,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            produced_at_s=time.monotonic(),
            slant_range_m=slant_range_m,
            raw_slant_range_m=raw_slant_range_m,
            sigma_m=sigma_m,
            valid_pixel_count=int(values.size),
            processing_time_ms=(time.perf_counter() - started_s) * 1_000.0,
            provider_names=self.provider_names,
            calibration_scale=self.config.calibration_scale,
            calibration_offset_m=self.config.calibration_offset_m,
            calibration_profile=self.config.calibration_profile,
            depth_grid=self._quantize_grid(depth),
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def _target_values(self, depth: Any, bbox: BoundingBox, *, target_label: str) -> Any:
        height, width = depth.shape
        center_x, center_y = bbox.center
        normalized_label = target_label.strip().lower()
        fire_region = normalized_label in {
            "fire",
            "flame",
            "smoke",
            "hotspot",
            "smoldering_area",
            "smolder_area",
        }
        half_width = (bbox.x2 - bbox.x1) * self.config.center_fraction * 0.5
        # Fire and smoke are non-rigid volumes. Sample the lower source/base
        # region rather than the translucent centre or plume background.
        if fire_region:
            center_y = bbox.y1 + 0.82 * (bbox.y2 - bbox.y1)
            half_height = (bbox.y2 - bbox.y1) * 0.16
        else:
            half_height = (bbox.y2 - bbox.y1) * self.config.center_fraction * 0.5
        x1 = max(0, min(width - 1, int(math.floor((center_x - half_width) * width))))
        x2 = max(x1 + 1, min(width, int(math.ceil((center_x + half_width) * width))))
        y1 = max(0, min(height - 1, int(math.floor((center_y - half_height) * height))))
        y2 = max(y1 + 1, min(height, int(math.ceil((center_y + half_height) * height))))
        crop = depth[y1:y2, x1:x2]
        mask = (
            self._np.isfinite(crop)
            & (crop >= self.config.minimum_depth_m)
            & (crop <= self.config.maximum_depth_m)
        )
        return crop[mask]

    def _quantize_grid(self, depth: Any) -> MetricDepthGrid:
        resized = self._cv2.resize(
            depth,
            (self.config.grid_width, self.config.grid_height),
            interpolation=self._cv2.INTER_AREA,
        )
        valid = (
            self._np.isfinite(resized)
            & (resized >= self.config.minimum_depth_m)
            & (resized <= self.config.maximum_depth_m)
        )
        encoded = self._np.zeros(resized.shape, dtype=self._np.uint8)
        if self.config.grid_encoding == "logarithmic":
            normalized = (
                self._np.log(resized[valid]) - math.log(self.config.minimum_depth_m)
            ) / math.log(self.config.maximum_depth_m / self.config.minimum_depth_m)
        else:
            normalized = (resized[valid] - self.config.minimum_depth_m) / (
                self.config.maximum_depth_m - self.config.minimum_depth_m
            )
        encoded[valid] = self._np.clip(
            self._np.rint(1.0 + normalized * 254.0),
            1,
            255,
        ).astype(self._np.uint8)
        return MetricDepthGrid(
            width=self.config.grid_width,
            height=self.config.grid_height,
            minimum_depth_m=self.config.minimum_depth_m,
            maximum_depth_m=self.config.maximum_depth_m,
            quantized_depth=encoded.tobytes(order="C"),
            encoding=self.config.grid_encoding,
        )

    def _load_session(self) -> Any:
        path = self.config.model_path
        if path.suffix.lower() in {".engine", ".plan"}:
            return TensorRtDepthSession(
                path,
                input_height=self.config.input_size,
                input_width=self.config.input_size,
            )
        if not path.is_file():
            raise MetricDepthContractError(f"metric-depth model does not exist: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != DEPTH_ANYTHING_V2_METRIC_INDOOR_SMALL_ONNX_SHA256:
            raise MetricDepthContractError("metric-depth ONNX artifact digest is not pinned")
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError("Install ONNX Runtime for metric-depth inference") from exc
        available = set(ort.get_available_providers())
        requested = self.config.providers or (
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        )
        providers = [provider for provider in requested if provider in available]
        if not providers:
            raise VisionDependencyError("No requested metric-depth provider is available")
        return ort.InferenceSession(str(path), providers=providers)

    @staticmethod
    def _dependencies() -> tuple[Any, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError("Install the vision dependencies for metric depth") from exc
        return np, cv2


class AsyncMetricDepthRunner:
    """Single-slot worker that keeps dense depth inference off the live frame loop."""

    def __init__(self, estimator: MetricDepthEstimator) -> None:
        self.estimator = estimator
        self.config = estimator.config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metric-depth")
        self._future: Future[MetricDepthResult] | None = None
        self._latest: MetricDepthResult | None = None
        self._last_submit_s = -math.inf
        self._closed = False
        self._lock = threading.Lock()
        self._history: dict[str, deque[MetricDepthResult]] = defaultdict(
            lambda: deque(maxlen=self.config.temporal_window_size)
        )
        self.inference_count = 0
        self.failure_count = 0
        self.last_error: str | None = None

    def submit(
        self,
        *,
        image_bgr: Any,
        target_id: str,
        bbox: BoundingBox,
        target_label: str = "manual",
        frame_id: str,
        captured_at_s: float,
        now_s: float,
    ) -> bool:
        with self._lock:
            self._harvest_locked()
            if self._closed or self._future is not None:
                return False
            if now_s - self._last_submit_s < self.config.minimum_interval_s:
                return False
            image = self.estimator._np.asarray(image_bgr).copy()
            self._future = self._executor.submit(
                self.estimator.estimate,
                image_bgr=image,
                target_id=target_id,
                bbox=bbox,
                target_label=target_label,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
            )
            self._last_submit_s = now_s
            return True

    def measurement_for(self, *, target_id: str, now_s: float) -> DirectRangeMeasurement | None:
        with self._lock:
            self._harvest_locked()
            result = self._latest
            if result is None or result.target_id != target_id:
                return None
            age_s = now_s - result.captured_at_s
            if age_s < 0.0 or age_s > self.config.maximum_result_age_s:
                return None
            return result.measurement()

    def latest_result(self) -> MetricDepthResult | None:
        with self._lock:
            self._harvest_locked()
            return self._latest

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.estimator.close()

    def _harvest_locked(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        try:
            result = future.result()
            history = self._history[result.target_id]
            history.append(result)
            distances = sorted(item.slant_range_m for item in history)
            median_distance = distances[len(distances) // 2]
            deviations = sorted(abs(value - median_distance) for value in distances)
            temporal_mad = deviations[len(deviations) // 2]
            self._latest = replace(
                result,
                slant_range_m=median_distance,
                sigma_m=max(
                    result.sigma_m,
                    temporal_mad * 1.4826 * 2.0,
                ),
            )
            self.inference_count += 1
            self.last_error = None
        # Inference backends can surface provider-specific Exception subclasses
        # (for example CUDA/TensorRT binding errors).  A failed asynchronous
        # depth sample is optional metadata and must stay isolated from the live
        # camera/operator loop.
        except Exception as exc:
            self.failure_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"


__all__ = [
    "AsyncMetricDepthRunner",
    "DEPTH_ANYTHING_V2_METRIC_INDOOR_SMALL_ONNX_SHA256",
    "MetricDepthConfig",
    "MetricDepthContractError",
    "MetricDepthEstimator",
    "MetricDepthGrid",
    "MetricDepthResult",
]

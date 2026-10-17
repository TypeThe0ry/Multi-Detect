"""Synchronous, opt-in near monocular depth gate for onboard position fusion."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .step_triangulation import (
    RangeMeasurement,
    fuse_position_estimates,
    position_from_range,
)

# Repository-relative so the same default resolves on the dev machine and the Jetson.
_DEFAULT_MODEL = (
    Path(__file__).resolve().parents[3]
    / "artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx"
)


@dataclass(frozen=True, slots=True)
class NearDepthConfig:
    enabled: bool = False
    model_path: Path = _DEFAULT_MODEL
    backend: str = "onnx-cpu"
    max_range_m: float = 10.0
    min_interval_s: float = 0.2
    max_age_s: float = 1.0
    sigma_scale: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.model_path, Path):
            object.__setattr__(self, "model_path", Path(self.model_path))
        if type(self.enabled) is not bool:
            raise ValueError("near_depth must be a boolean")
        if self.backend not in {"onnx-cpu", "tensorrt"}:
            raise ValueError("near_depth_backend must be onnx-cpu or tensorrt")
        for name in ("max_range_m", "min_interval_s", "max_age_s", "sigma_scale"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.enabled:
            if self.backend == "onnx-cpu" and self.model_path.suffix.lower() != ".onnx":
                raise ValueError("onnx-cpu near-depth model must be a .onnx file")
            if self.backend == "tensorrt" and self.model_path.suffix.lower() not in {
                ".engine",
                ".plan",
            }:
                raise ValueError("tensorrt near-depth model must be a .engine or .plan")
            if not self.model_path.is_file():
                raise ValueError(f"near-depth model does not exist: {self.model_path}")


def empirical_sigma(distance_m: float) -> float:
    """Offline P95 error buckets, deliberately conservative for the small sample."""
    if distance_m < 10.0:
        return 1.6
    if distance_m < 20.0:
        return 10.57
    if distance_m < 40.0:
        return 26.29
    return 50.71


@dataclass(slots=True)
class NearDepthStats:
    inference_count: int = 0
    failure_count: int = 0
    skip_counts: dict[str, int] = field(default_factory=dict)
    last_processing_time_ms: float | None = None
    last_memory_before_bytes: int | None = None
    last_memory_after_bytes: int | None = None

    def skip(self, reason: str) -> None:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "inference_count": self.inference_count,
            "failure_count": self.failure_count,
            "skip_counts": dict(self.skip_counts),
            "last_processing_time_ms": self.last_processing_time_ms,
            "last_memory_before_bytes": self.last_memory_before_bytes,
            "last_memory_after_bytes": self.last_memory_after_bytes,
        }


def _rss_bytes() -> int | None:
    try:
        import psutil  # optional; only imported when the feature is enabled

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


class NearDepthController:
    """One synchronous inference at most per frame and configured interval."""

    def __init__(
        self,
        config: NearDepthConfig,
        *,
        estimator: Any | None = None,
        estimator_factory: Callable[[NearDepthConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self.stats = NearDepthStats()
        self._last_inference_s = -math.inf
        self._estimator = estimator
        if config.enabled and estimator is None:
            if estimator_factory is not None:
                estimator = estimator_factory(config)
            else:
                # Keep all vision/backend imports behind the opt-in branch.
                from multidetect.metric_depth import MetricDepthConfig, MetricDepthEstimator

                providers = ("CPUExecutionProvider",) if config.backend == "onnx-cpu" else ()
                estimator = MetricDepthEstimator(
                    MetricDepthConfig(
                        model_path=config.model_path,
                        minimum_interval_s=config.min_interval_s,
                        maximum_result_age_s=config.max_age_s,
                        providers=providers,
                        intra_op_num_threads=2,
                        inter_op_num_threads=2,
                    )
                )
        self._estimator = estimator

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def close(self) -> None:
        close = getattr(self._estimator, "close", None)
        if callable(close):
            close()

    def run(
        self,
        *,
        track: Any,
        image_bgr: Any,
        frame_id: str,
        captured_at_s: float,
        now_s: float,
        camera_ray: Callable[[float, float], Any] | None,
        current_range_m: float | None,
        calibration: Any,
        image_size: tuple[int, int],
        stage_allowed: bool,
        focus: bool,
        step_token: int,
    ) -> bool:
        if not self.enabled:
            return False
        if not stage_allowed:
            self.stats.skip("stage")
            return False
        if not focus:
            self.stats.skip("not_focus")
            return False
        if (
            current_range_m is None
            or not math.isfinite(current_range_m)
            or current_range_m >= self.config.max_range_m
        ):
            self.stats.skip("range")
            return False
        if now_s - self._last_inference_s < self.config.min_interval_s:
            self.stats.skip("interval")
            return False
        if camera_ray is None or calibration is None:
            self.stats.skip("calibration")
            return False
        if image_bgr is None:
            self.stats.skip("image")
            return False
        if getattr(self, "_last_step_token", None) == step_token:
            self.stats.skip("step")
            return False
        self._last_step_token = step_token
        bbox = track.bbox
        # OnboardLoop boxes are pixels; metric_depth consumes normalized boxes.
        from multidetect.domain import BoundingBox

        width, height = image_size
        normalized = BoundingBox(
            bbox[0] / width, bbox[1] / height, bbox[2] / width, bbox[3] / height
        )
        started = time.perf_counter()
        before = _rss_bytes()
        self._last_inference_s = now_s
        try:
            result = self._estimator.estimate(
                image_bgr=image_bgr,
                target_id=track.track_id,
                bbox=normalized,
                target_label="manual",
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                calibration=calibration,
            )
            distance = float(result.slant_range_m)
            model_sigma = float(result.sigma_m)
            float(getattr(result, "produced_at_s", now_s))
            if (
                not math.isfinite(distance)
                or distance <= 0
                or not math.isfinite(model_sigma)
                or model_sigma <= 0
            ):
                self.stats.skip("invalid")
                return False
            # The estimator carries the frame timestamp through the result.
            # Compare timestamps on that same observation clock; inference
            # latency is accounted for by ``now_s`` rather than wall time.
            result_captured_at_s = float(getattr(result, "captured_at_s", captured_at_s))
            age_s = now_s - result_captured_at_s
            if age_s < 0.0 or age_s > self.config.max_age_s:
                self.stats.skip("expired")
                return False
            ray_data = camera_ray((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            if ray_data is None:
                self.stats.skip("calibration")
                return False
            origin, direction = ray_data
            measurement = RangeMeasurement(
                distance,
                max(model_sigma, empirical_sigma(distance)) * self.config.sigma_scale,
                captured_at_s,
                "near_depth",
            )
            estimate = position_from_range(
                measurement, camera_ned=tuple(origin), direction_ned=tuple(direction)
            )
            if estimate is None:
                self.stats.skip("invalid")
                return False
            track.position_estimate = fuse_position_estimates(track.position_estimate, estimate)
            self.stats.inference_count += 1
            return True
        except Exception:
            self.stats.failure_count += 1
            self.stats.skip("error")
            return False
        finally:
            self.stats.last_processing_time_ms = (time.perf_counter() - started) * 1000.0
            self.stats.last_memory_before_bytes = before
            self.stats.last_memory_after_bytes = _rss_bytes()


__all__ = ["NearDepthConfig", "NearDepthController", "NearDepthStats", "empirical_sigma"]

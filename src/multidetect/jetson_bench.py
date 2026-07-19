from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .compat import UTC
from .vision import CameraReadError

GPU_PROVIDERS = frozenset({"TensorrtExecutionProvider", "CUDAExecutionProvider"})
SUPPORTED_JETSON_MODELS = frozenset({"Jetson Orin Nano", "Jetson Orin NX"})


@dataclass(frozen=True, slots=True)
class JetsonVisionBenchConfig:
    minimum_frames: int = 54_000
    minimum_duration_seconds: float = 3600.0
    maximum_duration_seconds: float = 3900.0
    maximum_temperature_c: float = 95.0
    minimum_processing_fps: float = 15.0
    maximum_inference_latency_p95_ms: float = 66.7
    maximum_capture_queue_high_watermark: int = 1
    maximum_memory_growth_mb: float = 256.0
    memory_warmup_seconds: float = 60.0
    temperature_sample_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_frames, bool)
            or not isinstance(self.minimum_frames, int)
            or self.minimum_frames <= 0
        ):
            raise ValueError("Jetson bench minimum frames must be a positive integer")
        for name, value, allow_zero in (
            ("minimum duration", self.minimum_duration_seconds, True),
            ("maximum duration", self.maximum_duration_seconds, False),
            ("maximum temperature", self.maximum_temperature_c, False),
            ("minimum processing FPS", self.minimum_processing_fps, False),
            (
                "maximum inference P95 latency",
                self.maximum_inference_latency_p95_ms,
                False,
            ),
            ("maximum memory growth", self.maximum_memory_growth_mb, False),
            ("memory warmup", self.memory_warmup_seconds, True),
            ("temperature sample interval", self.temperature_sample_interval_seconds, False),
        ):
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                raise ValueError(f"Jetson bench {name} must be finite and positive")
        if self.maximum_duration_seconds <= self.minimum_duration_seconds:
            raise ValueError("Jetson bench maximum duration must exceed minimum duration")
        if (
            isinstance(self.maximum_capture_queue_high_watermark, bool)
            or not isinstance(self.maximum_capture_queue_high_watermark, int)
            or self.maximum_capture_queue_high_watermark < 0
        ):
            raise ValueError("Jetson bench maximum capture queue high watermark is invalid")


def run_jetson_vision_bench(
    source: Any,
    detector: Any,
    config: JetsonVisionBenchConfig,
    *,
    device_model_reader: Callable[[], str] = lambda: read_jetson_device_model(),
    temperature_reader: Callable[[], tuple[float, ...]] = lambda: read_temperatures_c(),
    process_rss_reader: Callable[[], float | None] = lambda: read_process_rss_mb(),
    clock: Callable[[], float] = time.perf_counter,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Run capture and inference together while collecting fail-closed Jetson evidence."""

    raw_model = device_model_reader().strip().replace("\x00", "")
    device_model = normalize_jetson_device_model(raw_model)
    providers = tuple(str(item) for item in getattr(detector, "provider_names", ()))
    active_provider = providers[0] if providers else None
    reasons: list[str] = []
    if device_model not in SUPPORTED_JETSON_MODELS:
        reasons.append("system model is not a supported Jetson Orin NX/Nano")
    if active_provider not in GPU_PROVIDERS:
        reasons.append("TensorRT or CUDA inference provider is not active")

    started_s = clock()
    processed_frames = 0
    detection_count = 0
    frames_with_raw_candidates = 0
    raw_candidates_by_class: Counter[str] = Counter()
    raw_candidate_confidences: defaultdict[str, list[float]] = defaultdict(list)
    raw_candidate_areas: defaultdict[str, list[float]] = defaultdict(list)
    failed_reads = 0
    inference_failures = 0
    inference_latencies_ms: list[float] = []
    expected_size: tuple[int, int] | None = None
    resolution_stable = True
    temperature_samples: list[float] = []
    memory_samples: list[tuple[float, float]] = []
    next_temperature_sample_s = started_s

    while not reasons:
        current_s = clock()
        elapsed_s = max(0.0, current_s - started_s)
        if current_s >= next_temperature_sample_s:
            temperature_samples.extend(_valid_temperatures(temperature_reader()))
            rss_mb = process_rss_reader()
            if rss_mb is not None and math.isfinite(rss_mb) and rss_mb >= 0.0:
                memory_samples.append((elapsed_s, float(rss_mb)))
            next_temperature_sample_s = current_s + config.temperature_sample_interval_seconds
        if (
            processed_frames >= config.minimum_frames
            and elapsed_s >= config.minimum_duration_seconds
        ):
            break
        if elapsed_s >= config.maximum_duration_seconds:
            if processed_frames < config.minimum_frames:
                reasons.append("minimum inference frame count was not reached before the deadline")
            break
        try:
            frame = source.read()
        except CameraReadError:
            failed_reads += 1
            reasons.append("camera read failed after configured reconnect attempts")
            break
        size = (int(frame.width), int(frame.height))
        if expected_size is None:
            expected_size = size
        elif size != expected_size:
            resolution_stable = False
            reasons.append("camera resolution changed during the Jetson bench")
            break
        inference_started_s = clock()
        try:
            detections = detector.detect(frame.image_bgr)
        except (RuntimeError, ValueError):
            inference_failures += 1
            reasons.append("ONNX inference failed during the Jetson bench")
            break
        inference_latencies_ms.append(max(0.0, (clock() - inference_started_s) * 1_000.0))
        processed_frames += 1
        detection_count += len(detections)
        if detections:
            frames_with_raw_candidates += 1
        for detection in detections:
            label = str(detection.label)
            raw_candidates_by_class[label] += 1
            raw_candidate_confidences[label].append(float(detection.confidence))
            raw_candidate_areas[label].append(float(detection.bbox.area))

    duration_s = max(0.0, clock() - started_s)
    processing_fps = processed_frames / duration_s if duration_s > 0.0 else 0.0
    inference_latency_p50_ms = _percentile(inference_latencies_ms, 0.50)
    inference_latency_p95_ms = _percentile(inference_latencies_ms, 0.95)
    capture_queue_high_watermark = int(getattr(source, "queue_high_watermark", 0))
    capture_queue_backpressure_count = int(getattr(source, "backpressure_count", 0))
    captured_frame_count = int(getattr(source, "captured_frame_count", processed_frames))
    memory_trend = _memory_trend(
        memory_samples,
        warmup_seconds=config.memory_warmup_seconds,
    )
    if processing_fps < config.minimum_processing_fps:
        reasons.append("processing FPS fell below the configured minimum")
    if (
        inference_latency_p95_ms is None
        or inference_latency_p95_ms > config.maximum_inference_latency_p95_ms
    ):
        reasons.append("inference P95 latency exceeded the configured limit")
    if capture_queue_high_watermark > config.maximum_capture_queue_high_watermark:
        reasons.append("capture queue high watermark exceeded the bounded-latency limit")
    memory_evidence_required = (
        config.minimum_duration_seconds >= 2.0 * config.temperature_sample_interval_seconds
    )
    if memory_evidence_required and memory_trend is None:
        reasons.append("process RSS trend could not be measured during the Jetson soak")
    elif memory_trend is not None and memory_trend["growth_mb"] > config.maximum_memory_growth_mb:
        reasons.append("process RSS sustained growth exceeded the configured limit")
    maximum_temperature = max(temperature_samples) if temperature_samples else None
    if not temperature_samples:
        reasons.append("Jetson thermal-zone temperature could not be read")
    elif maximum_temperature is not None and maximum_temperature > config.maximum_temperature_c:
        reasons.append("Jetson temperature exceeded the configured limit")
    if expected_size is None:
        resolution_stable = False
    passed = (
        not reasons
        and processed_frames >= config.minimum_frames
        and duration_s >= config.minimum_duration_seconds
        and resolution_stable
    )
    timestamp = observed_at()
    if timestamp.tzinfo is None:
        raise ValueError("Jetson bench observation time must include a timezone")
    width, height = expected_size if expected_size is not None else (None, None)
    return {
        "event": f"jetson_orin_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": timestamp.astimezone(UTC).isoformat(),
        "hardware_observed": device_model in SUPPORTED_JETSON_MODELS and processed_frames > 0,
        "simulation_only": False,
        "passed": passed,
        "reasons": reasons,
        "device_model": device_model,
        "device_model_raw": raw_model or None,
        "active_inference_provider": active_provider,
        "available_inference_providers": list(providers),
        "processed_frames": processed_frames,
        "detections_processed": detection_count,
        "frames_with_raw_candidates": frames_with_raw_candidates,
        "raw_candidate_frame_rate": (
            frames_with_raw_candidates / processed_frames if processed_frames else None
        ),
        "raw_candidates_by_class": dict(sorted(raw_candidates_by_class.items())),
        "raw_candidate_statistics_by_class": {
            label: {
                "count": raw_candidates_by_class[label],
                "confidence_p50": _percentile(raw_candidate_confidences[label], 0.50),
                "confidence_p95": _percentile(raw_candidate_confidences[label], 0.95),
                "confidence_max": max(raw_candidate_confidences[label]),
                "bbox_area_p50": _percentile(raw_candidate_areas[label], 0.50),
                "bbox_area_p95": _percentile(raw_candidate_areas[label], 0.95),
            }
            for label in sorted(raw_candidates_by_class)
        },
        "soak_duration_seconds": duration_s,
        "processing_fps": processing_fps,
        "minimum_processing_fps": config.minimum_processing_fps,
        "minimum_frames": config.minimum_frames,
        "minimum_duration_seconds": config.minimum_duration_seconds,
        "maximum_duration_seconds": config.maximum_duration_seconds,
        "width": width,
        "height": height,
        "resolution_stable": resolution_stable,
        "inference_latency_p50_ms": inference_latency_p50_ms,
        "inference_latency_p95_ms": inference_latency_p95_ms,
        "maximum_inference_latency_p95_ms": config.maximum_inference_latency_p95_ms,
        "capture_queue_high_watermark": capture_queue_high_watermark,
        "maximum_capture_queue_high_watermark": config.maximum_capture_queue_high_watermark,
        "capture_queue_backpressure_count": capture_queue_backpressure_count,
        "captured_frame_count": captured_frame_count,
        "capture_queue_bounded": (
            capture_queue_high_watermark <= config.maximum_capture_queue_high_watermark
        ),
        "memory_sample_count": len(memory_samples),
        "memory_warmup_seconds": config.memory_warmup_seconds,
        "process_rss_start_mb": memory_trend["start_mb"] if memory_trend else None,
        "process_rss_end_mb": memory_trend["end_mb"] if memory_trend else None,
        "process_rss_peak_mb": memory_trend["peak_mb"] if memory_trend else None,
        "process_rss_growth_mb": memory_trend["growth_mb"] if memory_trend else None,
        "process_rss_slope_mb_per_hour": (
            memory_trend["slope_mb_per_hour"] if memory_trend else None
        ),
        "maximum_memory_growth_mb": config.maximum_memory_growth_mb,
        "memory_growth_bounded": (
            memory_trend is not None
            and memory_trend["growth_mb"] <= config.maximum_memory_growth_mb
        ),
        "maximum_temperature_c": maximum_temperature,
        "temperature_limit_c": config.maximum_temperature_c,
        "temperature_sample_count": len(temperature_samples),
        "reconnect_count": int(getattr(source, "reconnect_count", 0)),
        "capture_read_failures": failed_reads,
        "inference_failures": inference_failures,
        "credentials_recorded": False,
        "images_saved": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "hardware_control_enabled": False,
    }


def read_jetson_device_model(path: Path = Path("/proc/device-tree/model")) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def normalize_jetson_device_model(raw_model: str) -> str:
    """Return the supported Orin module family without trusting arbitrary Jetson text."""

    normalized = raw_model.strip().replace("\x00", "").lower()
    if "jetson orin nx" in normalized:
        return "Jetson Orin NX"
    if "jetson orin nano" in normalized:
        return "Jetson Orin Nano"
    return "unknown"


def read_temperatures_c(root: Path = Path("/sys/class/thermal")) -> tuple[float, ...]:
    values: list[float] = []
    try:
        paths = tuple(root.glob("thermal_zone*/temp"))
    except OSError:
        return ()
    for path in paths:
        try:
            raw = float(path.read_text(encoding="utf-8").strip())
        # Some Jetson thermal zones transiently return EAGAIN. Python's
        # TextIOWrapper can surface that sysfs behavior as a TypeError from
        # the incremental decoder instead of the underlying BlockingIOError.
        # Skip only that sensor; the bench still fails closed if no readable
        # temperature remains.
        except (OSError, TypeError, ValueError):
            continue
        value = raw / 1_000.0 if abs(raw) >= 1_000 else raw
        if math.isfinite(value):
            values.append(value)
    return tuple(values)


def read_process_rss_mb(path: Path = Path("/proc/self/status")) -> float | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("VmRSS:"):
            continue
        fields = line.split()
        if len(fields) < 2:
            return None
        try:
            kib = float(fields[1])
        except ValueError:
            return None
        return kib / 1024.0 if math.isfinite(kib) and kib >= 0.0 else None
    return None


def _valid_temperatures(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(value) for value in values if math.isfinite(value))


def _memory_trend(
    samples: list[tuple[float, float]],
    *,
    warmup_seconds: float,
) -> dict[str, float] | None:
    stable = [(elapsed, rss) for elapsed, rss in samples if elapsed >= warmup_seconds]
    if len(stable) < 2:
        return None
    window = max(1, len(stable) // 10)
    start_mb = sum(rss for _elapsed, rss in stable[:window]) / window
    end_mb = sum(rss for _elapsed, rss in stable[-window:]) / window
    mean_time = sum(elapsed for elapsed, _rss in stable) / len(stable)
    mean_rss = sum(rss for _elapsed, rss in stable) / len(stable)
    denominator = sum((elapsed - mean_time) ** 2 for elapsed, _rss in stable)
    slope_mb_per_second = (
        sum((elapsed - mean_time) * (rss - mean_rss) for elapsed, rss in stable) / denominator
        if denominator > 0.0
        else 0.0
    )
    return {
        "start_mb": start_mb,
        "end_mb": end_mb,
        "peak_mb": max(rss for _elapsed, rss in stable),
        "growth_mb": max(0.0, end_mb - start_mb),
        "slope_mb_per_hour": slope_mb_per_second * 3600.0,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


__all__ = [
    "GPU_PROVIDERS",
    "SUPPORTED_JETSON_MODELS",
    "JetsonVisionBenchConfig",
    "normalize_jetson_device_model",
    "read_jetson_device_model",
    "read_process_rss_mb",
    "read_temperatures_c",
    "run_jetson_vision_bench",
]

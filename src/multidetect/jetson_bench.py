from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .vision import CameraReadError

GPU_PROVIDERS = frozenset({"TensorrtExecutionProvider", "CUDAExecutionProvider"})


@dataclass(frozen=True, slots=True)
class JetsonVisionBenchConfig:
    minimum_frames: int = 1000
    minimum_duration_seconds: float = 1800.0
    maximum_duration_seconds: float = 2100.0
    maximum_temperature_c: float = 95.0
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


def run_jetson_vision_bench(
    source: Any,
    detector: Any,
    config: JetsonVisionBenchConfig,
    *,
    device_model_reader: Callable[[], str] = lambda: read_jetson_device_model(),
    temperature_reader: Callable[[], tuple[float, ...]] = lambda: read_temperatures_c(),
    clock: Callable[[], float] = time.perf_counter,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Run capture and inference together while collecting fail-closed Jetson evidence."""

    raw_model = device_model_reader().strip().replace("\x00", "")
    device_model = "Jetson Orin Nano" if "jetson orin nano" in raw_model.lower() else "unknown"
    providers = tuple(str(item) for item in getattr(detector, "provider_names", ()))
    active_provider = providers[0] if providers else None
    reasons: list[str] = []
    if device_model != "Jetson Orin Nano":
        reasons.append("system model is not recognized as Jetson Orin Nano")
    if active_provider not in GPU_PROVIDERS:
        reasons.append("TensorRT or CUDA inference provider is not active")

    started_s = clock()
    processed_frames = 0
    detection_count = 0
    failed_reads = 0
    inference_failures = 0
    inference_latencies_ms: list[float] = []
    expected_size: tuple[int, int] | None = None
    resolution_stable = True
    temperature_samples: list[float] = []
    next_temperature_sample_s = started_s

    while not reasons:
        current_s = clock()
        elapsed_s = max(0.0, current_s - started_s)
        if current_s >= next_temperature_sample_s:
            temperature_samples.extend(_valid_temperatures(temperature_reader()))
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

    duration_s = max(0.0, clock() - started_s)
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
        "event": f"jetson_orin_nano_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": timestamp.astimezone(UTC).isoformat(),
        "hardware_observed": device_model == "Jetson Orin Nano" and processed_frames > 0,
        "simulation_only": False,
        "passed": passed,
        "reasons": reasons,
        "device_model": device_model,
        "device_model_raw": raw_model or None,
        "active_inference_provider": active_provider,
        "available_inference_providers": list(providers),
        "processed_frames": processed_frames,
        "detections_processed": detection_count,
        "soak_duration_seconds": duration_s,
        "minimum_frames": config.minimum_frames,
        "minimum_duration_seconds": config.minimum_duration_seconds,
        "maximum_duration_seconds": config.maximum_duration_seconds,
        "width": width,
        "height": height,
        "resolution_stable": resolution_stable,
        "inference_latency_p50_ms": _percentile(inference_latencies_ms, 0.50),
        "inference_latency_p95_ms": _percentile(inference_latencies_ms, 0.95),
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


def read_temperatures_c(root: Path = Path("/sys/class/thermal")) -> tuple[float, ...]:
    values: list[float] = []
    try:
        paths = tuple(root.glob("thermal_zone*/temp"))
    except OSError:
        return ()
    for path in paths:
        try:
            raw = float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        value = raw / 1_000.0 if abs(raw) >= 1_000 else raw
        if math.isfinite(value):
            values.append(value)
    return tuple(values)


def _valid_temperatures(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(value) for value in values if math.isfinite(value))


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


__all__ = [
    "GPU_PROVIDERS",
    "JetsonVisionBenchConfig",
    "read_jetson_device_model",
    "read_temperatures_c",
    "run_jetson_vision_bench",
]

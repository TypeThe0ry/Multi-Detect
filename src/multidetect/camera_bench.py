from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .vision import CameraReadError, CaptureConfig


@dataclass(frozen=True, slots=True)
class CameraBenchConfig:
    minimum_frames: int = 300
    minimum_duration_seconds: float = 60.0
    maximum_duration_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_frames, bool)
            or not isinstance(self.minimum_frames, int)
            or self.minimum_frames <= 0
        ):
            raise ValueError("camera bench minimum frames must be a positive integer")
        if (
            isinstance(self.minimum_duration_seconds, bool)
            or not math.isfinite(self.minimum_duration_seconds)
            or self.minimum_duration_seconds < 0
        ):
            raise ValueError("camera bench minimum duration must be finite and non-negative")
        if (
            isinstance(self.maximum_duration_seconds, bool)
            or not math.isfinite(self.maximum_duration_seconds)
            or self.maximum_duration_seconds <= self.minimum_duration_seconds
        ):
            raise ValueError(
                "camera bench maximum duration must be finite and greater than minimum duration"
            )


def run_camera_bench(
    source: Any,
    capture_config: CaptureConfig,
    bench_config: CameraBenchConfig,
    *,
    clock: Callable[[], float] = time.perf_counter,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Continuously read a real capture source and return redacted bench evidence."""

    started_s = clock()
    processed_frames = 0
    failed_reads = 0
    latencies_ms: list[float] = []
    expected_size: tuple[int, int] | None = None
    resolution_stable = True
    reasons: list[str] = []

    while True:
        elapsed_s = max(0.0, clock() - started_s)
        if (
            processed_frames >= bench_config.minimum_frames
            and elapsed_s >= bench_config.minimum_duration_seconds
        ):
            break
        if elapsed_s >= bench_config.maximum_duration_seconds:
            if processed_frames < bench_config.minimum_frames:
                reasons.append("minimum frame count was not reached before the deadline")
            if elapsed_s < bench_config.minimum_duration_seconds:
                reasons.append("minimum capture duration was not reached")
            break
        frame_started_s = clock()
        try:
            frame = source.read()
        except CameraReadError:
            failed_reads += 1
            reasons.append("camera read failed after configured reconnect attempts")
            break
        latencies_ms.append(max(0.0, (clock() - frame_started_s) * 1_000.0))
        processed_frames += 1
        size = (int(frame.width), int(frame.height))
        if expected_size is None:
            expected_size = size
        elif size != expected_size:
            resolution_stable = False
            reasons.append("camera resolution changed during the bench")
            break

    duration_s = max(0.0, clock() - started_s)
    if expected_size is None:
        resolution_stable = False
    passed = (
        not reasons
        and processed_frames >= bench_config.minimum_frames
        and duration_s >= bench_config.minimum_duration_seconds
        and resolution_stable
    )
    source_kind = "rtsp" if capture_config.is_rtsp else "local_device"
    event_prefix = "rtsp_camera" if capture_config.is_rtsp else "local_camera"
    timestamp = observed_at()
    if timestamp.tzinfo is None:
        raise ValueError("camera bench observation time must include a timezone")
    width, height = expected_size if expected_size is not None else (None, None)
    return {
        "event": f"{event_prefix}_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": timestamp.astimezone(UTC).isoformat(),
        "source_kind": source_kind,
        "hardware_observed": processed_frames > 0,
        "simulation_only": False,
        "passed": passed,
        "reasons": reasons,
        "processed_frames": processed_frames,
        "duration_seconds": duration_s,
        "minimum_frames": bench_config.minimum_frames,
        "minimum_duration_seconds": bench_config.minimum_duration_seconds,
        "maximum_duration_seconds": bench_config.maximum_duration_seconds,
        "width": width,
        "height": height,
        "resolution_stable": resolution_stable,
        "average_fps": processed_frames / duration_s if duration_s > 0 else None,
        "capture_latency_p50_ms": _percentile(latencies_ms, 0.50),
        "capture_latency_p95_ms": _percentile(latencies_ms, 0.95),
        "reconnect_count": int(getattr(source, "reconnect_count", 0)),
        "capture_read_failures": failed_reads,
        "rtsp_transport": capture_config.rtsp_transport if capture_config.is_rtsp else None,
        "credentials_recorded": False,
        "images_saved": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "hardware_control_enabled": False,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


__all__ = ["CameraBenchConfig", "run_camera_bench"]

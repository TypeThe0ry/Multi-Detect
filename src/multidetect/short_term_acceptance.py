from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .domain import BoundingBox
from .short_term_tracking import (
    OpenCVShortTermTargetTracker,
    ShortTermTrackingConfig,
    ShortTermTrackingStatus,
)
from .unified_tracking import (
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackState,
)
from .vision import VisionDependencyError


@dataclass(frozen=True, slots=True)
class ShortTermTrackingAcceptanceConfig:
    track_count: int = 10
    benchmark_frames: int = 300
    frame_rate_hz: float = 30.0
    analysis_width: int = 320
    frame_stride: int = 2
    maximum_processing_latency_p95_ms: float = 66.7
    minimum_end_to_end_rate_hz: float = 15.0
    maximum_recovery_s: float = 0.5

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("track_count", self.track_count, 10, 16),
            ("benchmark_frames", self.benchmark_frames, 60, 10_000),
            ("analysis_width", self.analysis_width, 160, 1920),
            ("frame_stride", self.frame_stride, 1, 10),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
        for name, value in (
            ("frame_rate_hz", self.frame_rate_hz),
            ("maximum_processing_latency_p95_ms", self.maximum_processing_latency_p95_ms),
            ("minimum_end_to_end_rate_hz", self.minimum_end_to_end_rate_hz),
            ("maximum_recovery_s", self.maximum_recovery_s),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


def run_short_term_tracking_acceptance(
    config: ShortTermTrackingAcceptanceConfig | None = None,
) -> dict[str, object]:
    """Benchmark image-level flow/template tracking without opening camera or hardware."""

    cfg = config or ShortTermTrackingAcceptanceConfig()
    _cv2, np = _require_dependencies()
    width, height, patch_size = 640, 360, 32
    reappearance_frame = 33
    occlusion_start_frame = 20
    if cfg.benchmark_frames < reappearance_frame + 10:
        raise ValueError("benchmark_frames must leave at least ten frames after reacquisition")
    centers = _target_centers(cfg.track_count)
    patches = tuple(
        np.random.default_rng(10_000 + index).integers(
            0,
            256,
            size=(patch_size, patch_size, 3),
            dtype=np.uint8,
        )
        for index in range(cfg.track_count)
    )
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            maximum_tracks=16,
            minimum_confirmed_hits=1,
            occluded_after_s=0.2,
            reacquisition_timeout_s=1.0,
            lost_retention_s=5.0,
            locked_lost_retention_s=5.0,
            maximum_center_distance=0.08,
        )
    )
    tracker = OpenCVShortTermTargetTracker(
        ShortTermTrackingConfig(
            analysis_width=cfg.analysis_width,
            maximum_tracks=16,
            minimum_box_size_px=8,
            frame_stride=cfg.frame_stride,
            search_expansion=2.5,
            occluded_search_multiplier=1.5,
            reacquiring_search_multiplier=2.0,
            maximum_search_expansion=6.0,
            maximum_retained_template_age_s=2.0,
        )
    )
    initial_frame = _render_scene(
        np,
        width,
        height,
        patch_size,
        centers,
        patches,
        hidden_target_zero=False,
        target_zero_offset_px=0,
    )
    initial_time_s = 1.0
    warmup = tracker.update_frame(initial_frame, captured_at_s=initial_time_s)
    initial = pool.update(
        frame_id="short-image-0",
        captured_at_s=initial_time_s,
        observations=_observations(width, height, patch_size, centers, hidden=False, offset_px=0),
    )
    tracker.synchronize_tracks(initial.tracks)
    original_track_id = initial.tracks[0].track_id
    if warmup.status is not ShortTermTrackingStatus.WARMUP:
        raise RuntimeError("short-term image benchmark did not enter warmup")

    processed_latencies_ms: list[float] = []
    end_to_end_latencies_ms: list[float] = []
    status_counts = {status.value: 0 for status in ShortTermTrackingStatus}
    maximum_retained_templates = tracker.retained_template_count
    retained_recovery_hint_observed = False
    recovered_same_track = False
    recovery_s: float | None = None
    occlusion_duration_s = (reappearance_frame - occlusion_start_frame) / cfg.frame_rate_hz
    benchmark_started_s = time.perf_counter()
    for frame_index in range(1, cfg.benchmark_frames + 1):
        captured_at_s = initial_time_s + frame_index / cfg.frame_rate_hz
        hidden = occlusion_start_frame <= frame_index < reappearance_frame
        offset_px = 60 if frame_index >= reappearance_frame else 0
        image = _render_scene(
            np,
            width,
            height,
            patch_size,
            centers,
            patches,
            hidden_target_zero=hidden,
            target_zero_offset_px=offset_px,
        )
        frame_started_s = time.perf_counter()
        short_result = tracker.update_frame(image, captured_at_s=captured_at_s)
        observations = _observations(
            width,
            height,
            patch_size,
            centers,
            hidden=hidden,
            offset_px=offset_px,
        )
        update = pool.update(
            frame_id=f"short-image-{frame_index}",
            captured_at_s=captured_at_s,
            observations=observations,
            motion_hints=short_result.hints,
        )
        tracker.synchronize_tracks(update.tracks)
        end_to_end_latencies_ms.append((time.perf_counter() - frame_started_s) * 1_000.0)
        status_counts[short_result.status.value] += 1
        maximum_retained_templates = max(
            maximum_retained_templates,
            tracker.retained_template_count,
        )
        if short_result.status is not ShortTermTrackingStatus.SKIPPED:
            processed_latencies_ms.append(short_result.processing_time_ms)
        if frame_index == reappearance_frame:
            retained_recovery_hint_observed = any(
                hint.track_id == original_track_id
                and hint.source == "retained_template_correlation"
                for hint in short_result.hints
            )
            recovered_same_track = update.recovered_track_ids == (original_track_id,)
            # Recovery latency starts when the target becomes visible again.  The
            # old calculation included the time during which the target was
            # intentionally hidden, which made the same immediate recovery pass
            # at 30 Hz and fail at 25 Hz even though no tracker behavior changed.
            recovery_s = 0.0 if recovered_same_track else None
            recovered = next(
                track for track in update.tracks if track.track_id == original_track_id
            )
            if recovered.state is not UnifiedTrackState.RECOVERED:
                recovered_same_track = False
                recovery_s = None
        elif frame_index > reappearance_frame and recovery_s is None:
            recovered = next(
                (track for track in update.tracks if track.track_id == original_track_id),
                None,
            )
            if recovered is not None and recovered.state is UnifiedTrackState.RECOVERED:
                recovered_same_track = True
                recovery_s = (frame_index - reappearance_frame) / cfg.frame_rate_hz
    benchmark_elapsed_s = time.perf_counter() - benchmark_started_s
    if not processed_latencies_ms or not end_to_end_latencies_ms:
        raise RuntimeError("short-term image benchmark produced no timing samples")
    processing_p95_ms = _percentile(processed_latencies_ms, 0.95)
    end_to_end_rate_hz = cfg.benchmark_frames / max(benchmark_elapsed_s, 1e-9)
    simulated_duration_s = cfg.benchmark_frames / cfg.frame_rate_hz
    processed_update_rate_hz = len(processed_latencies_ms) / simulated_duration_s
    failures: list[str] = []
    if processing_p95_ms > cfg.maximum_processing_latency_p95_ms:
        failures.append("short-term processing P95 exceeds its budget")
    if end_to_end_rate_hz < cfg.minimum_end_to_end_rate_hz:
        failures.append("image-level tracking loop is below the required rate")
    if processed_update_rate_hz + 1e-9 < cfg.frame_rate_hz / cfg.frame_stride:
        failures.append("short-term tracker did not maintain its configured update cadence")
    if not retained_recovery_hint_observed:
        failures.append("retained-template recovery hint was not observed")
    if not recovered_same_track:
        failures.append("occluded target did not recover the original track ID")
    if recovery_s is None or recovery_s > cfg.maximum_recovery_s:
        failures.append("image-level occlusion recovery exceeded its budget")
    if maximum_retained_templates > 16:
        failures.append("retained-template cache exceeded its configured bound")
    if status_counts[ShortTermTrackingStatus.INVALID.value] != 0:
        failures.append("short-term tracker emitted an invalid frame result")
    if failures:
        raise RuntimeError("; ".join(failures))
    return {
        "track_count": cfg.track_count,
        "benchmark_frame_count": cfg.benchmark_frames,
        "frame_rate_hz": cfg.frame_rate_hz,
        "analysis_width": cfg.analysis_width,
        "frame_stride": cfg.frame_stride,
        "status_counts": status_counts,
        "processing_latency_p50_ms": _percentile(processed_latencies_ms, 0.50),
        "processing_latency_p95_ms": processing_p95_ms,
        "processing_latency_p99_ms": _percentile(processed_latencies_ms, 0.99),
        "processing_latency_maximum_ms": max(processed_latencies_ms),
        "end_to_end_latency_p95_ms": _percentile(end_to_end_latencies_ms, 0.95),
        "benchmark_elapsed_s": benchmark_elapsed_s,
        "end_to_end_rate_hz": end_to_end_rate_hz,
        "processed_update_rate_hz": processed_update_rate_hz,
        "maximum_processing_latency_p95_ms": cfg.maximum_processing_latency_p95_ms,
        "minimum_end_to_end_rate_hz": cfg.minimum_end_to_end_rate_hz,
        "retained_template_recovery_hint_observed": retained_recovery_hint_observed,
        "recovered_same_track_id": recovered_same_track,
        "recovery_s": recovery_s,
        "occlusion_duration_s": occlusion_duration_s,
        "maximum_recovery_s": cfg.maximum_recovery_s,
        "maximum_retained_template_count": maximum_retained_templates,
        "retained_template_cache_bound": 16,
        "synthetic_image_input": True,
        "camera_opened": False,
        "model_inference_executed": False,
        "pixhawk_opened": False,
        "metadata_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _target_centers(count: int) -> tuple[tuple[int, int], ...]:
    x_values = (70, 195, 320, 445, 570)
    y_values = (95, 265, 180, 95)
    return tuple(
        (x_values[index % len(x_values)], y_values[index // len(x_values)])
        for index in range(count)
    )


def _render_scene(
    np: Any,
    width: int,
    height: int,
    patch_size: int,
    centers: tuple[tuple[int, int], ...],
    patches: tuple[Any, ...],
    *,
    hidden_target_zero: bool,
    target_zero_offset_px: int,
) -> Any:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    half = patch_size // 2
    for index, ((center_x, center_y), patch) in enumerate(zip(centers, patches, strict=True)):
        if index == 0 and hidden_target_zero:
            continue
        offset = target_zero_offset_px if index == 0 else 0
        x1, y1 = center_x + offset - half, center_y - half
        image[y1 : y1 + patch_size, x1 : x1 + patch_size] = patch
    return image


def _observations(
    width: int,
    height: int,
    patch_size: int,
    centers: tuple[tuple[int, int], ...],
    *,
    hidden: bool,
    offset_px: int,
) -> tuple[TargetObservation, ...]:
    half = patch_size / 2.0
    observations = []
    for index, (center_x, center_y) in enumerate(centers):
        if index == 0 and hidden:
            continue
        x = center_x + (offset_px if index == 0 else 0)
        observations.append(
            TargetObservation(
                label="vehicle",
                confidence=0.95,
                bbox=BoundingBox(
                    (x - half) / width,
                    (center_y - half) / height,
                    (x + half) / width,
                    (center_y + half) / height,
                ),
                appearance_reliable=False,
                source="synthetic_image_acceptance",
            )
        )
    return tuple(observations)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _require_dependencies() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific.
        raise VisionDependencyError(
            "OpenCV and NumPy are required for image tracking bench"
        ) from exc
    return cv2, np


__all__ = [
    "ShortTermTrackingAcceptanceConfig",
    "run_short_term_tracking_acceptance",
]

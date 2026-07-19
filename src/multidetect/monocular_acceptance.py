from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .monocular_avoidance import (
    CollisionRiskState,
    MonocularAvoidanceAssessment,
    MonocularAvoidanceConfig,
    OpenCVSparseFlowAvoidance,
    VisionZone,
)
from .vision import VisionDependencyError


@dataclass(frozen=True, slots=True)
class MonocularAvoidanceAcceptanceConfig:
    benchmark_frames: int = 300
    frame_rate_hz: float = 30.0
    analysis_width: int = 320
    maximum_processing_latency_p95_ms: float = 66.7
    minimum_end_to_end_rate_hz: float = 15.0

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("benchmark_frames", self.benchmark_frames, 60, 10_000),
            ("analysis_width", self.analysis_width, 160, 1920),
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
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


def run_monocular_avoidance_acceptance(
    config: MonocularAvoidanceAcceptanceConfig | None = None,
) -> dict[str, object]:
    """Exercise real OpenCV flow/RANSAC without opening camera or flight hardware."""

    cfg = config or MonocularAvoidanceAcceptanceConfig()
    cv2, np = _require_dependencies()
    width, height = 640, 360
    frame_interval_s = 1.0 / cfg.frame_rate_hz
    base, approaching = _acceptance_frames(cv2, np, width=width, height=height)
    frontend_config = MonocularAvoidanceConfig(
        minimum_feature_count=24,
        minimum_zone_feature_count=3,
        analysis_width=cfg.analysis_width,
    )

    static = _two_frame_scenario(
        frontend_config,
        base,
        base,
        frame_interval_s=frame_interval_s,
        name="static",
    )
    translated = cv2.warpAffine(
        base,
        np.asarray([[1.0, 0.0, 8.0], [0.0, 1.0, 0.0]], dtype=np.float32),
        (width, height),
    )
    camera_translation = _two_frame_scenario(
        frontend_config,
        base,
        translated,
        frame_interval_s=frame_interval_s,
        name="camera-translation",
    )
    approach = _two_frame_scenario(
        frontend_config,
        base,
        approaching,
        frame_interval_s=frame_interval_s,
        name="approach",
    )
    stale = _two_frame_scenario(
        frontend_config,
        base,
        base,
        frame_interval_s=frame_interval_s,
        name="stale",
        result_age_s=frontend_config.maximum_data_age_s + 0.05,
    )

    tracker = OpenCVSparseFlowAvoidance(frontend_config)
    initial_time_s = 10.0
    warmup = tracker.update(
        base,
        frame_id="avoidance-bench-0",
        captured_at_s=initial_time_s,
        produced_at_s=initial_time_s + 0.01,
    )
    if warmup.state is not CollisionRiskState.INVALID or warmup.reason != "WARMUP":
        raise RuntimeError("monocular avoidance benchmark did not enter warmup")

    processing_latencies_ms: list[float] = []
    end_to_end_latencies_ms: list[float] = []
    state_counts = {state.value: 0 for state in CollisionRiskState}
    all_advisory_only = warmup.advisory_only
    benchmark_started_s = time.perf_counter()
    for frame_index in range(1, cfg.benchmark_frames + 1):
        captured_at_s = initial_time_s + frame_index * frame_interval_s
        image = approaching if frame_index % 2 else base
        frame_started_s = time.perf_counter()
        assessment = tracker.update(
            image,
            frame_id=f"avoidance-bench-{frame_index}",
            captured_at_s=captured_at_s,
            produced_at_s=captured_at_s + 0.01,
        )
        end_to_end_latencies_ms.append((time.perf_counter() - frame_started_s) * 1_000.0)
        processing_latencies_ms.append(assessment.processing_time_ms)
        state_counts[assessment.state.value] += 1
        all_advisory_only = all_advisory_only and assessment.advisory_only
    benchmark_elapsed_s = time.perf_counter() - benchmark_started_s

    processing_p95_ms = _percentile(processing_latencies_ms, 0.95)
    end_to_end_rate_hz = cfg.benchmark_frames / max(benchmark_elapsed_s, 1e-9)
    center_approach_zone = next(zone for zone in approach.zones if zone.zone is VisionZone.CENTER)
    failures: list[str] = []
    if static.state is not CollisionRiskState.CLEAR:
        failures.append("static scene did not remain clear")
    if camera_translation.state is not CollisionRiskState.CLEAR:
        failures.append("compensated camera translation produced a collision warning")
    if not camera_translation.rotation_compensated:
        failures.append("camera translation was not motion-compensated")
    if camera_translation.camera_motion_confidence is None:
        failures.append("camera translation did not expose motion confidence")
    elif camera_translation.camera_motion_confidence < 0.5:
        failures.append("camera-motion confidence was below the association threshold")
    if camera_translation.camera_motion_dx is None or camera_translation.camera_motion_dx <= 0.0:
        failures.append("camera translation did not expose positive normalized motion")
    if approach.state is not CollisionRiskState.AVOID:
        failures.append("approaching obstacle did not enter AVOID")
    if center_approach_zone.state is not CollisionRiskState.AVOID:
        failures.append("approaching obstacle was not localized to the center risk zone")
    if stale.state is not CollisionRiskState.INVALID or stale.reason != "STALE_FRAME":
        failures.append("stale visual evidence did not fail closed")
    if state_counts[CollisionRiskState.AVOID.value] == 0:
        failures.append("image benchmark did not observe an AVOID frame")
    if state_counts[CollisionRiskState.CLEAR.value] == 0:
        failures.append("image benchmark did not observe a CLEAR frame")
    if state_counts[CollisionRiskState.INVALID.value] != 0:
        failures.append("valid benchmark frames unexpectedly became INVALID")
    if not all_advisory_only:
        failures.append("monocular avoidance claimed flight-control authority")
    if processing_p95_ms > cfg.maximum_processing_latency_p95_ms:
        failures.append("monocular avoidance processing P95 exceeds its budget")
    if end_to_end_rate_hz < cfg.minimum_end_to_end_rate_hz:
        failures.append("monocular avoidance loop is below the required rate")
    if failures:
        raise RuntimeError("; ".join(failures))

    return {
        "benchmark_frame_count": cfg.benchmark_frames,
        "frame_rate_hz": cfg.frame_rate_hz,
        "analysis_width": cfg.analysis_width,
        "state_counts": state_counts,
        "processing_latency_p50_ms": _percentile(processing_latencies_ms, 0.50),
        "processing_latency_p95_ms": processing_p95_ms,
        "processing_latency_p99_ms": _percentile(processing_latencies_ms, 0.99),
        "processing_latency_maximum_ms": max(processing_latencies_ms),
        "end_to_end_latency_p95_ms": _percentile(end_to_end_latencies_ms, 0.95),
        "benchmark_elapsed_s": benchmark_elapsed_s,
        "end_to_end_rate_hz": end_to_end_rate_hz,
        "maximum_processing_latency_p95_ms": cfg.maximum_processing_latency_p95_ms,
        "minimum_end_to_end_rate_hz": cfg.minimum_end_to_end_rate_hz,
        "static_scene_state": static.state.value,
        "camera_translation_state": camera_translation.state.value,
        "camera_motion_dx": camera_translation.camera_motion_dx,
        "camera_motion_dy": camera_translation.camera_motion_dy,
        "camera_motion_scale": camera_translation.camera_motion_scale,
        "camera_motion_confidence": camera_translation.camera_motion_confidence,
        "approaching_obstacle_state": approach.state.value,
        "approaching_center_zone_state": center_approach_zone.state.value,
        "approaching_center_zone_ttc_s": center_approach_zone.ttc_s,
        "stale_evidence_state": stale.state.value,
        "stale_evidence_reason": stale.reason,
        "all_outputs_advisory_only": all_advisory_only,
        "synthetic_image_input": True,
        "camera_opened": False,
        "model_inference_executed": False,
        "pixhawk_opened": False,
        "metric_depth_available": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _two_frame_scenario(
    config: MonocularAvoidanceConfig,
    previous: Any,
    current: Any,
    *,
    frame_interval_s: float,
    name: str,
    result_age_s: float = 0.01,
) -> MonocularAvoidanceAssessment:
    tracker = OpenCVSparseFlowAvoidance(config)
    tracker.update(
        previous,
        frame_id=f"{name}-1",
        captured_at_s=1.0,
        produced_at_s=1.01,
    )
    captured_at_s = 1.0 + frame_interval_s
    return tracker.update(
        current,
        frame_id=f"{name}-2",
        captured_at_s=captured_at_s,
        produced_at_s=captured_at_s + result_age_s,
    )


def _acceptance_frames(cv2: Any, np: Any, *, width: int, height: int) -> tuple[Any, Any]:
    rng = np.random.default_rng(12_345)
    base = np.zeros((height, width, 3), dtype=np.uint8)
    for _index in range(500):
        x = int(rng.integers(5, width - 5))
        y = int(rng.integers(5, height - 5))
        intensity = int(rng.integers(80, 256))
        cv2.circle(base, (x, y), 2, (intensity, intensity, intensity), -1)

    patch_size = 120
    patch = rng.integers(0, 256, size=(patch_size, patch_size, 3), dtype=np.uint8)
    half = patch_size // 2
    base[
        height // 2 - half : height // 2 + half,
        width // 2 - half : width // 2 + half,
    ] = patch
    approaching = base.copy()
    approaching_size = round(patch_size * 1.04)
    approaching_patch = cv2.resize(
        patch,
        (approaching_size, approaching_size),
        interpolation=cv2.INTER_LINEAR,
    )
    approaching_half = approaching_size // 2
    approaching[
        height // 2 - approaching_half : height // 2 - approaching_half + approaching_size,
        width // 2 - approaching_half : width // 2 - approaching_half + approaching_size,
    ] = approaching_patch
    return base, approaching


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
            "OpenCV and NumPy are required for monocular avoidance bench"
        ) from exc
    return cv2, np


__all__ = [
    "MonocularAvoidanceAcceptanceConfig",
    "run_monocular_avoidance_acceptance",
]

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .appearance_reid import (
    NVIDIA_TAO_REID_V1_2_SHA256,
    OnnxPersonReIdConfig,
    OnnxPersonReIdEncoder,
)
from .domain import BoundingBox, Detection
from .tensorrt_session import TensorRtEmbeddingSession
from .unified_tracking import TargetObservation
from .vehicle_reid import (
    OPENVINO_VEHICLE_REID_0001_SHA384,
    OnnxVehicleReIdConfig,
    OnnxVehicleReIdEncoder,
)
from .vision import VisionDependencyError


@dataclass(frozen=True, slots=True)
class ReIdModelAcceptanceConfig:
    person_model_path: Path
    vehicle_model_path: Path
    person_count: int = 4
    vehicle_count: int = 4
    iterations: int = 2
    realtime_frame_budget_ms: float = 66.7

    def __post_init__(self) -> None:
        for name, value in (
            ("person_count", self.person_count),
            ("vehicle_count", self.vehicle_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
                raise ValueError(f"{name} must be an integer in [1, 10]")
        if (
            isinstance(self.iterations, bool)
            or not isinstance(self.iterations, int)
            or not 1 <= self.iterations <= 100
        ):
            raise ValueError("iterations must be an integer in [1, 100]")
        if not math.isfinite(self.realtime_frame_budget_ms) or self.realtime_frame_budget_ms <= 0:
            raise ValueError("realtime_frame_budget_ms must be finite and positive")


@dataclass(frozen=True, slots=True)
class ReIdTensorRtAcceptanceConfig:
    person_model_path: Path
    vehicle_model_path: Path
    person_engine_path: Path
    vehicle_engine_path: Path
    person_count: int = 4
    vehicle_count: int = 4
    iterations: int = 20
    realtime_frame_budget_ms: float = 66.7

    def __post_init__(self) -> None:
        ReIdModelAcceptanceConfig(
            person_model_path=self.person_model_path,
            vehicle_model_path=self.vehicle_model_path,
            person_count=self.person_count,
            vehicle_count=self.vehicle_count,
            iterations=self.iterations,
            realtime_frame_budget_ms=self.realtime_frame_budget_ms,
        )
        if self.vehicle_count > 8:
            raise ValueError("vehicle_count must fit the deployed TensorRT profile maximum of 8")
        for name, path in (
            ("person TensorRT engine", self.person_engine_path),
            ("vehicle TensorRT engine", self.vehicle_engine_path),
        ):
            if not isinstance(path, Path):
                raise ValueError(f"{name} path must be a pathlib.Path")


def run_reid_model_acceptance(
    config: ReIdModelAcceptanceConfig,
    *,
    person_encoder: Any | None = None,
    vehicle_encoder: Any | None = None,
    image_bgr: Any | None = None,
) -> dict[str, object]:
    """Validate pinned person/vehicle ONNX domains on CPU without camera or hardware."""

    person_sha256 = _digest(config.person_model_path, "sha256")
    if person_sha256 != NVIDIA_TAO_REID_V1_2_SHA256:
        raise RuntimeError("person ReID artifact does not match its pinned SHA-256")
    vehicle_sha384 = _digest(config.vehicle_model_path, "sha384")
    if vehicle_sha384 != OPENVINO_VEHICLE_REID_0001_SHA384:
        raise RuntimeError("vehicle ReID artifact does not match its pinned SHA-384")

    if person_encoder is None:
        person_encoder = OnnxPersonReIdEncoder(
            OnnxPersonReIdConfig(
                model_path=config.person_model_path,
                maximum_batch_size=config.person_count,
                providers=("CPUExecutionProvider",),
            )
        )
    if vehicle_encoder is None:
        vehicle_encoder = OnnxVehicleReIdEncoder(
            OnnxVehicleReIdConfig(
                model_path=config.vehicle_model_path,
                maximum_batch_size=config.vehicle_count,
                providers=("CPUExecutionProvider",),
            )
        )
    return _run_reid_workload(
        config,
        person_encoder=person_encoder,
        vehicle_encoder=vehicle_encoder,
        image_bgr=image_bgr,
        expected_provider="CPUExecutionProvider",
        runtime_name="onnx_cpu",
        artifact_details={
            "person_model_sha256": person_sha256,
            "person_model_size_bytes": config.person_model_path.stat().st_size,
            "vehicle_model_sha384": vehicle_sha384,
            "vehicle_model_size_bytes": config.vehicle_model_path.stat().st_size,
        },
        target_tensorrt_runtime_validated=False,
    )


def run_reid_tensorrt_acceptance(
    config: ReIdTensorRtAcceptanceConfig,
    *,
    image_bgr: Any | None = None,
) -> dict[str, object]:
    """Exercise both pinned ReID models through target-built TensorRT engines."""

    person_sha256 = _digest(config.person_model_path, "sha256")
    if person_sha256 != NVIDIA_TAO_REID_V1_2_SHA256:
        raise RuntimeError("person ReID artifact does not match its pinned SHA-256")
    vehicle_sha384 = _digest(config.vehicle_model_path, "sha384")
    if vehicle_sha384 != OPENVINO_VEHICLE_REID_0001_SHA384:
        raise RuntimeError("vehicle ReID artifact does not match its pinned SHA-384")
    person_engine_sha256 = _digest(config.person_engine_path, "sha256")
    vehicle_engine_sha256 = _digest(config.vehicle_engine_path, "sha256")

    person_session: TensorRtEmbeddingSession | None = None
    vehicle_session: TensorRtEmbeddingSession | None = None
    try:
        person_session = TensorRtEmbeddingSession(
            config.person_engine_path,
            maximum_batch_size=config.person_count,
        )
        vehicle_session = TensorRtEmbeddingSession(
            config.vehicle_engine_path,
            maximum_batch_size=config.vehicle_count,
            input_height=208,
            input_width=208,
            feature_size=512,
        )
        person_encoder = OnnxPersonReIdEncoder(
            OnnxPersonReIdConfig(
                model_path=config.person_model_path,
                maximum_batch_size=config.person_count,
            ),
            session=person_session,
        )
        vehicle_encoder = OnnxVehicleReIdEncoder(
            OnnxVehicleReIdConfig(
                model_path=config.vehicle_model_path,
                maximum_batch_size=config.vehicle_count,
            ),
            session=vehicle_session,
        )
        return _run_reid_workload(
            config,
            person_encoder=person_encoder,
            vehicle_encoder=vehicle_encoder,
            image_bgr=image_bgr,
            expected_provider="TensorrtExecutionProvider",
            runtime_name="native_tensorrt",
            artifact_details={
                "person_model_sha256": person_sha256,
                "person_model_size_bytes": config.person_model_path.stat().st_size,
                "vehicle_model_sha384": vehicle_sha384,
                "vehicle_model_size_bytes": config.vehicle_model_path.stat().st_size,
                "person_engine_sha256": person_engine_sha256,
                "person_engine_size_bytes": config.person_engine_path.stat().st_size,
                "vehicle_engine_sha256": vehicle_engine_sha256,
                "vehicle_engine_size_bytes": config.vehicle_engine_path.stat().st_size,
            },
            target_tensorrt_runtime_validated=True,
        )
    finally:
        if vehicle_session is not None:
            vehicle_session.close()
        if person_session is not None:
            person_session.close()


def _run_reid_workload(
    config: ReIdModelAcceptanceConfig | ReIdTensorRtAcceptanceConfig,
    *,
    person_encoder: Any,
    vehicle_encoder: Any,
    image_bgr: Any | None,
    expected_provider: str,
    runtime_name: str,
    artifact_details: dict[str, object],
    target_tensorrt_runtime_validated: bool,
) -> dict[str, object]:
    if tuple(person_encoder.provider_names) != (expected_provider,):
        raise RuntimeError(f"person ReID benchmark selected an unexpected {runtime_name} provider")
    if tuple(vehicle_encoder.provider_names) != (expected_provider,):
        raise RuntimeError(f"vehicle ReID benchmark selected an unexpected {runtime_name} provider")

    np = _require_numpy()
    image = image_bgr if image_bgr is not None else _synthetic_image(np)
    detections = _mixed_detections(config.person_count, config.vehicle_count)
    person_encoder.warmup(batch_size=config.person_count)
    vehicle_encoder.warmup(batch_size=config.vehicle_count)

    person_latencies_ms: list[float] = []
    vehicle_latencies_ms: list[float] = []
    person_reference: tuple[TargetObservation, ...] | None = None
    vehicle_reference: tuple[TargetObservation, ...] | None = None
    maximum_person_repeat_distance = 0.0
    maximum_vehicle_repeat_distance = 0.0
    for _index in range(config.iterations):
        started_s = time.perf_counter()
        person_observations = tuple(person_encoder.encode_detections(image, detections))
        person_latencies_ms.append((time.perf_counter() - started_s) * 1_000.0)
        _validate_domain_observations(
            person_observations,
            detections,
            allowed_labels=frozenset({"person", "firefighter"}),
            expected_feature_size=256,
            domain="person",
        )

        started_s = time.perf_counter()
        vehicle_observations = tuple(vehicle_encoder.encode_detections(image, detections))
        vehicle_latencies_ms.append((time.perf_counter() - started_s) * 1_000.0)
        _validate_domain_observations(
            vehicle_observations,
            detections,
            allowed_labels=frozenset({"vehicle", "car", "bus", "truck"}),
            expected_feature_size=512,
            domain="vehicle",
        )

        if person_reference is not None:
            maximum_person_repeat_distance = max(
                maximum_person_repeat_distance,
                _maximum_repeat_distance(person_reference, person_observations),
            )
        if vehicle_reference is not None:
            maximum_vehicle_repeat_distance = max(
                maximum_vehicle_repeat_distance,
                _maximum_repeat_distance(vehicle_reference, vehicle_observations),
            )
        person_reference = person_observations
        vehicle_reference = vehicle_observations

    stability_tolerance = 1e-5
    if maximum_person_repeat_distance > stability_tolerance:
        raise RuntimeError("person ReID repeated inference exceeded the stability tolerance")
    if maximum_vehicle_repeat_distance > stability_tolerance:
        raise RuntimeError("vehicle ReID repeated inference exceeded the stability tolerance")

    person_p95_ms = _percentile(person_latencies_ms, 0.95)
    vehicle_p95_ms = _percentile(vehicle_latencies_ms, 0.95)
    stable_staggered_p95_ms = max(person_p95_ms, vehicle_p95_ms)
    combined_p95_ms = person_p95_ms + vehicle_p95_ms
    return {
        **artifact_details,
        "runtime": runtime_name,
        "providers": [expected_provider],
        "iterations": config.iterations,
        "person_count": config.person_count,
        "vehicle_count": config.vehicle_count,
        "mixed_detection_count": len(detections),
        "person_embedding_count": config.person_count,
        "vehicle_embedding_count": config.vehicle_count,
        "person_embedding_size": 256,
        "vehicle_embedding_size": 512,
        "person_latency_p50_ms": _percentile(person_latencies_ms, 0.50),
        "person_latency_p95_ms": person_p95_ms,
        "vehicle_latency_p50_ms": _percentile(vehicle_latencies_ms, 0.50),
        "vehicle_latency_p95_ms": vehicle_p95_ms,
        "stable_staggered_latency_p95_ms": stable_staggered_p95_ms,
        "combined_sequential_latency_p95_ms": combined_p95_ms,
        "realtime_frame_budget_ms": config.realtime_frame_budget_ms,
        "stable_staggered_budget_passed": (
            stable_staggered_p95_ms <= config.realtime_frame_budget_ms
        ),
        "recovery_combined_budget_passed": (combined_p95_ms <= config.realtime_frame_budget_ms),
        "realtime_budget_passed": combined_p95_ms <= config.realtime_frame_budget_ms,
        "maximum_person_repeat_cosine_distance": maximum_person_repeat_distance,
        "maximum_vehicle_repeat_cosine_distance": maximum_vehicle_repeat_distance,
        "repeat_stability_tolerance": stability_tolerance,
        "repeat_stability_validated": config.iterations >= 2,
        "identity_domains_disjoint": True,
        "synthetic_crop_input": True,
        "deployment_domain_accuracy_validated": False,
        "target_tensorrt_runtime_validated": target_tensorrt_runtime_validated,
        "camera_opened": False,
        "pixhawk_opened": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _mixed_detections(person_count: int, vehicle_count: int) -> tuple[Detection, ...]:
    detections: list[Detection] = []
    for index in range(person_count):
        x1 = 0.02 + index * 0.09
        detections.append(Detection("person", 0.95, BoundingBox(x1, 0.05, x1 + 0.07, 0.48)))
    vehicle_labels = ("car", "truck", "bus", "vehicle")
    for index in range(vehicle_count):
        x1 = 0.02 + index * 0.09
        detections.append(
            Detection(
                vehicle_labels[index % len(vehicle_labels)],
                0.93,
                BoundingBox(x1, 0.54, x1 + 0.08, 0.92),
            )
        )
    detections.extend(
        (
            Detection("flame", 0.90, BoundingBox(0.80, 0.05, 0.90, 0.25)),
            Detection("smoke", 0.88, BoundingBox(0.80, 0.30, 0.94, 0.52)),
        )
    )
    return tuple(detections)


def _synthetic_image(np: Any) -> Any:
    return np.random.default_rng(98_765).integers(
        0,
        256,
        size=(720, 1280, 3),
        dtype=np.uint8,
    )


def _validate_domain_observations(
    observations: tuple[TargetObservation, ...],
    detections: tuple[Detection, ...],
    *,
    allowed_labels: frozenset[str],
    expected_feature_size: int,
    domain: str,
) -> None:
    if len(observations) != len(detections):
        raise RuntimeError(f"{domain} ReID did not preserve detection ordering")
    for observation, detection in zip(observations, detections, strict=True):
        expected = detection.label in allowed_labels
        if observation.appearance_reliable is not expected:
            raise RuntimeError(f"{domain} ReID violated its label-domain boundary")
        if expected:
            if observation.appearance is None:
                raise RuntimeError(f"{domain} ReID omitted an eligible embedding")
            if len(observation.appearance.values) != expected_feature_size:
                raise RuntimeError(f"{domain} ReID embedding dimension is invalid")
            norm = math.sqrt(sum(value * value for value in observation.appearance.values))
            if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
                raise RuntimeError(f"{domain} ReID embedding is not L2 normalized")
        elif observation.appearance is not None:
            raise RuntimeError(f"{domain} ReID embedded a prohibited label")


def _maximum_repeat_distance(
    reference: tuple[TargetObservation, ...],
    current: tuple[TargetObservation, ...],
) -> float:
    distances = [
        left.appearance.cosine_distance(right.appearance)
        for left, right in zip(reference, current, strict=True)
        if left.appearance is not None and right.appearance is not None
    ]
    return max(distances, default=0.0)


def _digest(path: Path, algorithm: str) -> str:
    if not path.is_file():
        raise RuntimeError(f"ReID artifact does not exist: {path}")
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific.
        raise VisionDependencyError("NumPy is required for ReID model bench") from exc
    return np


__all__ = [
    "ReIdModelAcceptanceConfig",
    "ReIdTensorRtAcceptanceConfig",
    "run_reid_model_acceptance",
    "run_reid_tensorrt_acceptance",
]

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .domain import BoundingBox

CITYSEMSEGFORMER_LABELS = (
    "road",
    "sidewalk",
    "building",
    "wall",
    "fence",
    "pole",
    "traffic light",
    "traffic sign",
    "vegetation",
    "terrain",
    "sky",
    "person",
    "rider",
    "car",
    "truck",
    "bus",
    "train",
    "motorcycle",
    "bicycle",
)


@dataclass(frozen=True, slots=True)
class SemanticMaskConfig:
    class_names: tuple[str, ...] = CITYSEMSEGFORMER_LABELS
    selected_labels: frozenset[str] = frozenset({"building", "road"})
    void_class_ids: frozenset[int] = frozenset({19})
    minimum_component_pixels: int = 64
    minimum_component_area_fraction: float = 0.0005
    maximum_regions_per_label: int = 8

    def __post_init__(self) -> None:
        normalized = tuple(label.strip().lower() for label in self.class_names)
        if (
            not normalized
            or any(not label for label in normalized)
            or len(set(normalized)) != len(normalized)
        ):
            raise ValueError("semantic class names must be non-empty and unique")
        selected = frozenset(label.strip().lower() for label in self.selected_labels)
        if not selected or not selected.issubset(normalized):
            raise ValueError("selected semantic labels must exist in the class table")
        if any(class_id < len(normalized) or class_id < 0 for class_id in self.void_class_ids):
            raise ValueError("semantic void IDs must not overlap declared classes")
        if self.minimum_component_pixels <= 0 or self.maximum_regions_per_label <= 0:
            raise ValueError("semantic component limits must be positive")
        if not math.isfinite(self.minimum_component_area_fraction) or not (
            0.0 < self.minimum_component_area_fraction <= 1.0
        ):
            raise ValueError("semantic minimum area fraction must be in (0, 1]")
        object.__setattr__(self, "class_names", normalized)
        object.__setattr__(self, "selected_labels", selected)


@dataclass(frozen=True, slots=True)
class SemanticRegion:
    label: str
    class_id: int
    bbox: BoundingBox
    pixel_count: int
    frame_area_fraction: float
    bbox_fill_fraction: float
    categorical_mask_only: bool = True
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.label.strip() or self.class_id < 0 or self.pixel_count <= 0:
            raise ValueError("semantic region identity and size must be valid")
        for value in (self.frame_area_fraction, self.bbox_fill_fraction):
            if not math.isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError("semantic region fractions must be in (0, 1]")
        if not self.categorical_mask_only or not self.advisory_only:
            raise ValueError("semantic regions must remain categorical advisory metadata")
        if self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("semantic regions cannot enable control or release")


class CategoricalSemanticMaskAdapter:
    """Extract connected semantic regions without inventing detection confidence."""

    def __init__(self, config: SemanticMaskConfig | None = None) -> None:
        self.config = config or SemanticMaskConfig()

    def extract(self, output: Any) -> tuple[SemanticRegion, ...]:
        mask = self._as_mask(output)
        height, width = mask.shape
        frame_pixels = height * width
        minimum_pixels = max(
            self.config.minimum_component_pixels,
            math.ceil(frame_pixels * self.config.minimum_component_area_fraction),
        )
        regions: list[SemanticRegion] = []
        for class_id, label in enumerate(self.config.class_names):
            if label not in self.config.selected_labels:
                continue
            binary = np.asarray(mask == class_id, dtype=np.uint8)
            component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
                binary,
                connectivity=8,
            )
            components = sorted(
                (stats[index] for index in range(1, component_count)),
                key=lambda stat: int(stat[cv2.CC_STAT_AREA]),
                reverse=True,
            )[: self.config.maximum_regions_per_label]
            for component in components:
                x = int(component[cv2.CC_STAT_LEFT])
                y = int(component[cv2.CC_STAT_TOP])
                component_width = int(component[cv2.CC_STAT_WIDTH])
                component_height = int(component[cv2.CC_STAT_HEIGHT])
                area = int(component[cv2.CC_STAT_AREA])
                if area < minimum_pixels:
                    continue
                bbox_pixels = component_width * component_height
                regions.append(
                    SemanticRegion(
                        label=label,
                        class_id=class_id,
                        bbox=BoundingBox(
                            x / width,
                            y / height,
                            (x + component_width) / width,
                            (y + component_height) / height,
                        ),
                        pixel_count=area,
                        frame_area_fraction=area / frame_pixels,
                        bbox_fill_fraction=area / bbox_pixels,
                    )
                )
        return tuple(
            sorted(
                regions,
                key=lambda region: (
                    self.config.class_names.index(region.label),
                    -region.pixel_count,
                    region.bbox.x1,
                    region.bbox.y1,
                ),
            )
        )

    def _as_mask(self, output: Any) -> np.ndarray:
        array = np.asarray(output)
        if array.ndim == 4 and array.shape[0] == 1 and array.shape[-1] == 1:
            array = array[0, :, :, 0]
        elif array.ndim == 3 and array.shape[0] == 1:
            array = array[0]
        if array.ndim != 2 or array.size == 0:
            raise ValueError(
                "categorical semantic output must have shape [1,H,W,1], [1,H,W], or [H,W]"
            )
        if not np.all(np.isfinite(array)):
            raise ValueError("categorical semantic output contains non-finite values")
        rounded = np.rint(array)
        if not np.allclose(array, rounded, atol=1e-6):
            raise ValueError("categorical semantic output contains non-integer class IDs")
        mask = rounded.astype(np.int32)
        allowed_ids = set(range(len(self.config.class_names))) | set(self.config.void_class_ids)
        observed_ids = set(int(value) for value in np.unique(mask))
        unknown = observed_ids - allowed_ids
        if unknown:
            raise ValueError("categorical semantic output contains unknown class IDs")
        return mask


@dataclass(frozen=True, slots=True)
class OnnxSemanticContextConfig:
    model_path: Path
    input_width: int = 1820
    input_height: int = 1024
    rgb_offsets: tuple[float, float, float] = (123.675, 116.28, 103.53)
    network_scale_factor: float = 0.01735207357279195
    providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.input_width <= 0 or self.input_height <= 0:
            raise ValueError("semantic model input dimensions must be positive")
        if len(self.rgb_offsets) != 3 or not all(
            math.isfinite(value) for value in self.rgb_offsets
        ):
            raise ValueError("semantic RGB offsets must contain three finite values")
        if not math.isfinite(self.network_scale_factor) or self.network_scale_factor <= 0.0:
            raise ValueError("semantic network scale factor must be positive")


class OnnxCategoricalSemanticContext:
    """NVIDIA CitySemSegFormer-compatible ONNX wrapper for low-rate context only."""

    def __init__(
        self,
        config: OnnxSemanticContextConfig,
        *,
        adapter: CategoricalSemanticMaskAdapter | None = None,
        session: Any | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter or CategoricalSemanticMaskAdapter()
        if session is None:
            if not config.model_path.is_file():
                raise ValueError(f"semantic ONNX model does not exist: {config.model_path}")
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - optional runtime dependency.
                raise RuntimeError(
                    "onnxruntime is required for semantic context inference"
                ) from exc
            providers = list(config.providers) or ort.get_available_providers()
            session = ort.InferenceSession(str(config.model_path), providers=providers)
        inputs = tuple(session.get_inputs())
        outputs = tuple(session.get_outputs())
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("semantic ONNX model must expose exactly one input and one output")
        self._validate_input_shape(tuple(inputs[0].shape))
        self._validate_output_shape(tuple(outputs[0].shape))
        self._session = session
        self._input_name = str(inputs[0].name)
        self._output_name = str(outputs[0].name)

    @property
    def provider_names(self) -> tuple[str, ...]:
        getter = getattr(self._session, "get_providers", None)
        return tuple(getter()) if callable(getter) else ()

    def warmup(self) -> None:
        image = np.zeros((self.config.input_height, self.config.input_width, 3), dtype=np.uint8)
        self.infer(image)

    def infer(self, image_bgr: Any) -> tuple[SemanticRegion, ...]:
        if not hasattr(image_bgr, "shape") or len(image_bgr.shape) != 3:
            raise ValueError("semantic context requires an HxWx3 BGR image")
        if image_bgr.shape[2] != 3 or image_bgr.shape[0] <= 0 or image_bgr.shape[1] <= 0:
            raise ValueError("semantic context requires a non-empty three-channel image")
        resized = cv2.resize(
            image_bgr,
            (self.config.input_width, self.config.input_height),
            interpolation=cv2.INTER_LINEAR,
        )
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        offsets = np.asarray(self.config.rgb_offsets, dtype=np.float32).reshape(1, 1, 3)
        tensor = ((rgb - offsets) * self.config.network_scale_factor).transpose(2, 0, 1)[
            np.newaxis, ...
        ]
        outputs = self._session.run((self._output_name,), {self._input_name: tensor})
        if len(outputs) != 1:
            raise ValueError("semantic ONNX inference returned an unexpected output count")
        return self.adapter.extract(outputs[0])

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def _validate_input_shape(self, shape: tuple[Any, ...]) -> None:
        expected = (1, 3, self.config.input_height, self.config.input_width)
        if len(shape) != 4:
            raise ValueError("semantic ONNX input must be NCHW")
        for actual, required in zip(shape, expected, strict=True):
            if isinstance(actual, int) and actual > 0 and actual != required:
                raise ValueError(f"semantic ONNX input shape {shape} does not match {expected}")

    def _validate_output_shape(self, shape: tuple[Any, ...]) -> None:
        if len(shape) != 4:
            raise ValueError("semantic ONNX output must be [batch,H,W,1]")
        required = (1, self.config.input_height, self.config.input_width, 1)
        for actual, expected in zip(shape, required, strict=True):
            if isinstance(actual, int) and actual > 0 and actual != expected:
                raise ValueError(f"semantic ONNX output shape {shape} does not match {required}")


class SemanticContextState(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class SemanticContextSnapshot:
    frame_id: str
    captured_at_s: float
    produced_at_s: float
    state: SemanticContextState
    regions: tuple[SemanticRegion, ...]
    processing_time_ms: float
    error_type: str | None = None
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str) or not self.frame_id.strip():
            raise ValueError("semantic context frame ID must be a non-empty string")
        if not all(
            math.isfinite(value)
            for value in (self.captured_at_s, self.produced_at_s, self.processing_time_ms)
        ):
            raise ValueError("semantic context timestamps and latency must be finite")
        if self.produced_at_s < self.captured_at_s or self.processing_time_ms < 0.0:
            raise ValueError("semantic context timing is invalid")
        if self.state is SemanticContextState.VALID and self.error_type is not None:
            raise ValueError("valid semantic context cannot contain an error type")
        if self.state is SemanticContextState.INVALID and (
            self.regions or not isinstance(self.error_type, str) or not self.error_type
        ):
            raise ValueError("invalid semantic context must contain only a sanitized error type")
        if not self.advisory_only:
            raise ValueError("semantic context must remain advisory-only")
        if self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("semantic context cannot enable control or release")


@dataclass(frozen=True, slots=True)
class SemanticContextStatistics:
    submitted_frame_count: int
    interval_skipped_frame_count: int
    replaced_pending_frame_count: int
    completed_frame_count: int
    failed_frame_count: int
    latest_frame_id: str | None
    worker_alive: bool
    pending_frame_count: int
    queue_capacity: int = 1

    def __post_init__(self) -> None:
        counters = (
            self.submitted_frame_count,
            self.interval_skipped_frame_count,
            self.replaced_pending_frame_count,
            self.completed_frame_count,
            self.failed_frame_count,
            self.pending_frame_count,
        )
        if any(value < 0 for value in counters):
            raise ValueError("semantic context statistics cannot be negative")
        if self.pending_frame_count > self.queue_capacity or self.queue_capacity != 1:
            raise ValueError("semantic context queue must remain bounded to one latest frame")


class AsyncSemanticContextRunner:
    """Runs low-rate semantic context on a bounded latest-frame worker."""

    def __init__(
        self,
        model: OnnxCategoricalSemanticContext,
        *,
        minimum_interval_s: float = 0.5,
        shutdown_timeout_s: float = 2.0,
    ) -> None:
        if not math.isfinite(minimum_interval_s) or minimum_interval_s <= 0.0:
            raise ValueError("semantic context interval must be finite and positive")
        if not math.isfinite(shutdown_timeout_s) or shutdown_timeout_s <= 0.0:
            raise ValueError("semantic context shutdown timeout must be finite and positive")
        self.model = model
        self.minimum_interval_s = minimum_interval_s
        self.shutdown_timeout_s = shutdown_timeout_s
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._pending: tuple[str, float, Any] | None = None
        self._latest: SemanticContextSnapshot | None = None
        self._last_submitted_at_s: float | None = None
        self._submitted_frame_count = 0
        self._interval_skipped_frame_count = 0
        self._replaced_pending_frame_count = 0
        self._completed_frame_count = 0
        self._failed_frame_count = 0

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                raise RuntimeError("semantic context worker has already been started")
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._worker,
                name="semantic-context-latest-frame",
                daemon=True,
            )
            self._thread.start()

    def submit(
        self,
        image_bgr: Any,
        *,
        frame_id: str,
        captured_at_s: float,
        submitted_at_s: float | None = None,
    ) -> bool:
        if (
            not isinstance(frame_id, str)
            or not frame_id.strip()
            or not math.isfinite(captured_at_s)
        ):
            raise ValueError("semantic context frame identity and capture time must be valid")
        now_s = time.monotonic() if submitted_at_s is None else submitted_at_s
        if not math.isfinite(now_s) or now_s < captured_at_s:
            raise ValueError("semantic context submission time must follow capture time")
        if not hasattr(image_bgr, "shape") or tuple(image_bgr.shape[-1:]) != (3,):
            raise ValueError("semantic context submission requires an HxWx3 image")
        with self._condition:
            thread = self._thread
            if thread is None or self._stop_requested or not thread.is_alive():
                raise RuntimeError("semantic context worker is not running")
            if (
                self._last_submitted_at_s is not None
                and now_s - self._last_submitted_at_s < self.minimum_interval_s
            ):
                self._interval_skipped_frame_count += 1
                return False
            copied = np.ascontiguousarray(np.asarray(image_bgr).copy())
            if copied.ndim != 3 or copied.shape[2] != 3 or copied.size == 0:
                raise ValueError("semantic context submission requires a non-empty HxWx3 image")
            if self._pending is not None:
                self._replaced_pending_frame_count += 1
            self._pending = (frame_id, captured_at_s, copied)
            self._last_submitted_at_s = now_s
            self._submitted_frame_count += 1
            self._condition.notify_all()
            return True

    def latest_snapshot(self) -> SemanticContextSnapshot | None:
        with self._condition:
            return self._latest

    def statistics(self) -> SemanticContextStatistics:
        with self._condition:
            thread = self._thread
            return SemanticContextStatistics(
                submitted_frame_count=self._submitted_frame_count,
                interval_skipped_frame_count=self._interval_skipped_frame_count,
                replaced_pending_frame_count=self._replaced_pending_frame_count,
                completed_frame_count=self._completed_frame_count,
                failed_frame_count=self._failed_frame_count,
                latest_frame_id=self._latest.frame_id if self._latest is not None else None,
                worker_alive=bool(thread is not None and thread.is_alive()),
                pending_frame_count=int(self._pending is not None),
            )

    def wait_for_snapshot(
        self,
        *,
        frame_id: str | None = None,
        timeout_s: float = 2.0,
    ) -> SemanticContextSnapshot | None:
        if (frame_id is not None and (not isinstance(frame_id, str) or not frame_id.strip())) or (
            not math.isfinite(timeout_s) or timeout_s < 0.0
        ):
            raise ValueError("semantic context wait arguments are invalid")
        deadline_s = time.monotonic() + timeout_s
        with self._condition:
            while self._latest is None or (
                frame_id is not None and self._latest.frame_id != frame_id
            ):
                remaining_s = deadline_s - time.monotonic()
                if remaining_s <= 0.0:
                    return None
                self._condition.wait(remaining_s)
            return self._latest

    def close(self) -> bool:
        with self._condition:
            thread = self._thread
            if thread is None:
                self._close_model()
                return True
            self._stop_requested = True
            if self._pending is not None:
                self._pending = None
                self._replaced_pending_frame_count += 1
            self._condition.notify_all()
        thread.join(self.shutdown_timeout_s)
        clean = not thread.is_alive()
        if clean:
            self._close_model()
        return clean

    def _worker(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stop_requested:
                    self._condition.wait()
                if self._stop_requested:
                    return
                frame_id, captured_at_s, image_bgr = self._pending
                self._pending = None
            started_s = time.perf_counter()
            error_type = None
            regions: tuple[SemanticRegion, ...] = ()
            try:
                regions = self.model.infer(image_bgr)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                error_type = type(exc).__name__
            produced_at_s = max(captured_at_s, time.monotonic())
            snapshot = SemanticContextSnapshot(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                state=(
                    SemanticContextState.INVALID
                    if error_type is not None
                    else SemanticContextState.VALID
                ),
                regions=regions,
                processing_time_ms=(time.perf_counter() - started_s) * 1_000.0,
                error_type=error_type,
            )
            with self._condition:
                self._latest = snapshot
                self._completed_frame_count += 1
                self._failed_frame_count += int(error_type is not None)
                self._condition.notify_all()

    def _close_model(self) -> None:
        close = getattr(self.model, "close", None)
        if callable(close):
            close()


__all__ = [
    "CITYSEMSEGFORMER_LABELS",
    "AsyncSemanticContextRunner",
    "CategoricalSemanticMaskAdapter",
    "OnnxCategoricalSemanticContext",
    "OnnxSemanticContextConfig",
    "SemanticContextSnapshot",
    "SemanticContextState",
    "SemanticContextStatistics",
    "SemanticMaskConfig",
    "SemanticRegion",
]

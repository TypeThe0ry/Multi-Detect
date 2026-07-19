from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import BoundingBox, Detection
from .unified_tracking import AppearanceEmbedding, TargetObservation
from .vision import VisionDependencyError

NVIDIA_TAO_REID_V1_2_SHA256 = "0e21d09278508ec835955f422a9fdd3cd59b2a6ecdef98d705f388f33cebac2b"


class ReIdModelContractError(RuntimeError):
    """Raised when a ReID artifact or inference output violates the identity boundary."""


@dataclass(frozen=True, slots=True)
class OnnxPersonReIdConfig:
    model_path: Path
    expected_sha256: str = NVIDIA_TAO_REID_V1_2_SHA256
    input_width: int = 128
    input_height: int = 256
    feature_size: int = 256
    maximum_batch_size: int = 10
    allowed_labels: frozenset[str] = frozenset({"person", "firefighter"})
    pixel_mean: tuple[float, float, float] = (0.485, 0.456, 0.406)
    pixel_std: tuple[float, float, float] = (0.226, 0.226, 0.226)
    crop_padding_fraction: float = 0.04
    providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.input_width <= 0 or self.input_height <= 0 or self.feature_size <= 1:
            raise ValueError("ReID dimensions must be positive")
        if self.maximum_batch_size <= 0:
            raise ValueError("maximum_batch_size must be positive")
        if not self.allowed_labels:
            raise ValueError("person ReID needs at least one allowed label")
        if any(not label.strip() for label in self.allowed_labels):
            raise ValueError("person ReID labels cannot be empty")
        if len(self.pixel_mean) != 3 or len(self.pixel_std) != 3:
            raise ValueError("person ReID normalization must contain three channels")
        if any(not math.isfinite(value) for value in (*self.pixel_mean, *self.pixel_std)):
            raise ValueError("person ReID normalization values must be finite")
        if any(value <= 0.0 for value in self.pixel_std):
            raise ValueError("person ReID standard deviations must be positive")
        if not 0.0 <= self.crop_padding_fraction <= 0.25:
            raise ValueError("crop_padding_fraction must be in [0, 0.25]")
        normalized_hash = self.expected_sha256.strip().lower()
        if len(normalized_hash) != 64 or any(
            ch not in "0123456789abcdef" for ch in normalized_hash
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(
            self,
            "allowed_labels",
            frozenset(label.strip().lower() for label in self.allowed_labels),
        )
        object.__setattr__(self, "expected_sha256", normalized_hash)


class OnnxPersonReIdEncoder:
    """Batched person ReID encoder with hash, shape and L2-normalization gates."""

    def __init__(self, config: OnnxPersonReIdConfig, *, session: Any | None = None) -> None:
        self.config = config
        if session is None:
            self._verify_artifact()
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - dependency-specific.
                raise VisionDependencyError(
                    "Install ONNX Runtime: pip install -e '.[vision]'"
                ) from exc
            available = set(ort.get_available_providers())
            requested = config.providers or (
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            )
            providers = [provider for provider in requested if provider in available]
            if not providers:
                raise VisionDependencyError(
                    "No requested ReID ONNX Runtime provider is available; "
                    f"available={sorted(available)}"
                )
            session = ort.InferenceSession(str(config.model_path), providers=providers)
        self._session = session
        self._np, self._cv2 = self._require_dependencies()
        inputs = tuple(self._session.get_inputs())
        if len(inputs) != 1:
            raise ReIdModelContractError("person ReID model must expose exactly one input")
        self._input_name = inputs[0].name
        self._validate_input_shape(tuple(inputs[0].shape))
        outputs = (
            tuple(self._session.get_outputs()) if hasattr(self._session, "get_outputs") else ()
        )
        if outputs:
            if len(outputs) != 1:
                raise ReIdModelContractError("person ReID model must expose exactly one output")
            self._output_name = outputs[0].name
            self._validate_output_shape(tuple(outputs[0].shape))
        else:
            self._output_name = None

    @property
    def provider_names(self) -> tuple[str, ...]:
        if hasattr(self._session, "get_providers"):
            return tuple(self._session.get_providers())
        return ()

    def warmup(self, *, batch_size: int = 1) -> None:
        if not 1 <= batch_size <= self.config.maximum_batch_size:
            raise ValueError("ReID warmup batch size is outside the configured range")
        tensor = self._np.zeros(
            (batch_size, 3, self.config.input_height, self.config.input_width),
            dtype=self._np.float32,
        )
        self._infer_embeddings(tensor)

    def encode_detections(
        self,
        image_bgr: Any,
        detections: Sequence[Detection],
    ) -> tuple[TargetObservation, ...]:
        if not detections:
            return ()
        eligible_indices = [
            index
            for index, detection in enumerate(detections)
            if detection.label in self.config.allowed_labels
        ]
        embeddings: dict[int, AppearanceEmbedding] = {}
        for offset in range(0, len(eligible_indices), self.config.maximum_batch_size):
            indices = eligible_indices[offset : offset + self.config.maximum_batch_size]
            tensor = self._np.stack(
                [self._preprocess_crop(image_bgr, detections[index].bbox) for index in indices]
            )
            encoded = self._infer_embeddings(tensor)
            embeddings.update(zip(indices, encoded, strict=True))
        return tuple(
            TargetObservation.from_detection(
                detection,
                appearance=embeddings.get(index),
                appearance_reliable=index in embeddings,
            )
            for index, detection in enumerate(detections)
        )

    def _preprocess_crop(self, image_bgr: Any, bbox: BoundingBox) -> Any:
        if not hasattr(image_bgr, "shape") or len(image_bgr.shape) < 2:
            raise ValueError("person ReID requires a BGR image array")
        height, width = image_bgr.shape[:2]
        if width <= 0 or height <= 0:
            raise ValueError("person ReID image cannot be empty")
        padded = bbox.expanded(self.config.crop_padding_fraction)
        x1 = max(0, min(width - 1, round(padded.x1 * width)))
        y1 = max(0, min(height - 1, round(padded.y1 * height)))
        x2 = max(x1 + 1, min(width, round(padded.x2 * width)))
        y2 = max(y1 + 1, min(height, round(padded.y2 * height)))
        crop = image_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            raise ReIdModelContractError("person ReID crop is empty")
        resized = self._cv2.resize(
            crop,
            (self.config.input_width, self.config.input_height),
            interpolation=self._cv2.INTER_LINEAR,
        )
        rgb = self._cv2.cvtColor(resized, self._cv2.COLOR_BGR2RGB)
        tensor = rgb.astype(self._np.float32) / 255.0
        mean = self._np.asarray(self.config.pixel_mean, dtype=self._np.float32)
        std = self._np.asarray(self.config.pixel_std, dtype=self._np.float32)
        tensor = (tensor - mean) / std
        return self._np.ascontiguousarray(tensor.transpose(2, 0, 1))

    def _infer_embeddings(self, tensor: Any) -> tuple[AppearanceEmbedding, ...]:
        outputs = self._session.run(
            None if self._output_name is None else [self._output_name],
            {self._input_name: self._np.ascontiguousarray(tensor, dtype=self._np.float32)},
        )
        if len(outputs) != 1:
            raise ReIdModelContractError("person ReID inference must return exactly one output")
        array = self._np.asarray(outputs[0], dtype=self._np.float32)
        if array.ndim > 2:
            array = array.reshape(array.shape[0], -1)
        expected_shape = (tensor.shape[0], self.config.feature_size)
        if tuple(array.shape) != expected_shape:
            raise ReIdModelContractError(
                f"person ReID output shape {tuple(array.shape)} does not match {expected_shape}"
            )
        if not self._np.isfinite(array).all():
            raise ReIdModelContractError("person ReID output contains non-finite values")
        embeddings = []
        for row in array:
            try:
                embeddings.append(AppearanceEmbedding(tuple(float(value) for value in row)))
            except ValueError as exc:
                raise ReIdModelContractError("person ReID produced an invalid embedding") from exc
        return tuple(embeddings)

    def _verify_artifact(self) -> None:
        if not self.config.model_path.is_file():
            raise ReIdModelContractError(
                f"person ReID model does not exist: {self.config.model_path}"
            )
        digest = hashlib.sha256(self.config.model_path.read_bytes()).hexdigest()
        if digest != self.config.expected_sha256:
            raise ReIdModelContractError(
                "person ReID model SHA-256 does not match the pinned NVIDIA artifact"
            )

    def _validate_input_shape(self, shape: tuple[Any, ...]) -> None:
        if len(shape) != 4:
            raise ReIdModelContractError("person ReID input must be NCHW")
        expected = (3, self.config.input_height, self.config.input_width)
        actual = tuple(shape[1:])
        if actual != expected:
            raise ReIdModelContractError(
                f"person ReID input shape {shape} does not match batchx{expected}"
            )

    def _validate_output_shape(self, shape: tuple[Any, ...]) -> None:
        if len(shape) < 2:
            raise ReIdModelContractError("person ReID output must include batch and feature axes")
        static_feature_count = 1
        for dimension in shape[1:]:
            if not isinstance(dimension, int) or dimension <= 0:
                return
            static_feature_count *= dimension
        if static_feature_count != self.config.feature_size:
            raise ReIdModelContractError(
                "person ReID output feature size does not match the configured contract"
            )

    @staticmethod
    def _require_dependencies() -> tuple[Any, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError(
                "Install live vision dependencies: pip install -e '.[vision]'"
            ) from exc
        return np, cv2


__all__ = [
    "NVIDIA_TAO_REID_V1_2_SHA256",
    "OnnxPersonReIdConfig",
    "OnnxPersonReIdEncoder",
    "ReIdModelContractError",
]

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .appearance_reid import ReIdModelContractError
from .domain import BoundingBox, Detection
from .unified_tracking import AppearanceEmbedding, TargetObservation
from .vision import VisionDependencyError

OPENVINO_VEHICLE_REID_0001_SHA384 = (
    "0515ce72f653c39780d5b87dfed7255d396dd2b1e8b6e91fbaacdfad1da1891"
    "66343157273c02f3b0fede3050ef7abb7"
)


@dataclass(frozen=True, slots=True)
class OnnxVehicleReIdConfig:
    model_path: Path
    expected_sha384: str = OPENVINO_VEHICLE_REID_0001_SHA384
    input_width: int = 208
    input_height: int = 208
    feature_size: int = 512
    maximum_batch_size: int = 1
    # Open Model Zoo's vehicle ReID embedding is used only for road-vehicle
    # bodies.  A van shares that visual domain; two-wheelers, boats and trains
    # stay motion-only until a separately validated encoder is available.
    allowed_labels: frozenset[str] = frozenset({"vehicle", "car", "van", "bus", "truck"})
    crop_padding_fraction: float = 0.06
    providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.input_width <= 0 or self.input_height <= 0 or self.feature_size <= 1:
            raise ValueError("vehicle ReID dimensions must be positive")
        if self.maximum_batch_size <= 0:
            raise ValueError("vehicle ReID maximum_batch_size must be positive")
        if not self.allowed_labels or any(not label.strip() for label in self.allowed_labels):
            raise ValueError("vehicle ReID labels cannot be empty")
        if not math.isfinite(self.crop_padding_fraction) or not (
            0.0 <= self.crop_padding_fraction <= 0.25
        ):
            raise ValueError("vehicle ReID crop padding must be in [0, 0.25]")
        normalized_hash = self.expected_sha384.strip().lower()
        if len(normalized_hash) != 96 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("expected_sha384 must be a lowercase SHA-384 digest")
        object.__setattr__(
            self,
            "allowed_labels",
            frozenset(label.strip().lower() for label in self.allowed_labels),
        )
        object.__setattr__(self, "expected_sha384", normalized_hash)


class OnnxVehicleReIdEncoder:
    """Hash-pinned vehicle ReID encoder whose embeddings never cross label classes."""

    def __init__(self, config: OnnxVehicleReIdConfig, *, session: Any | None = None) -> None:
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
                    "No requested vehicle ReID provider is available; "
                    f"available={sorted(available)}"
                )
            session = ort.InferenceSession(str(config.model_path), providers=providers)
        self._session = session
        self._np, self._cv2 = self._require_dependencies()
        inputs = tuple(self._session.get_inputs())
        if len(inputs) != 1:
            raise ReIdModelContractError("vehicle ReID model must expose exactly one input")
        self._input_name = inputs[0].name
        self._validate_input_shape(tuple(inputs[0].shape))
        outputs = (
            tuple(self._session.get_outputs()) if hasattr(self._session, "get_outputs") else ()
        )
        if outputs:
            if len(outputs) != 1:
                raise ReIdModelContractError("vehicle ReID model must expose exactly one output")
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
            raise ValueError("vehicle ReID warmup batch size is outside the configured range")
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
            raise ValueError("vehicle ReID requires a BGR image array")
        height, width = image_bgr.shape[:2]
        if width <= 0 or height <= 0:
            raise ValueError("vehicle ReID image cannot be empty")
        padded = bbox.expanded(self.config.crop_padding_fraction)
        x1 = max(0, min(width - 1, round(padded.x1 * width)))
        y1 = max(0, min(height - 1, round(padded.y1 * height)))
        x2 = max(x1 + 1, min(width, round(padded.x2 * width)))
        y2 = max(y1 + 1, min(height, round(padded.y2 * height)))
        crop = image_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            raise ReIdModelContractError("vehicle ReID crop is empty")
        resized = self._cv2.resize(
            crop,
            (self.config.input_width, self.config.input_height),
            interpolation=self._cv2.INTER_LINEAR,
        )
        rgb = self._cv2.cvtColor(resized, self._cv2.COLOR_BGR2RGB)
        tensor = rgb.astype(self._np.float32)
        return self._np.ascontiguousarray(tensor.transpose(2, 0, 1))

    def _infer_embeddings(self, tensor: Any) -> tuple[AppearanceEmbedding, ...]:
        outputs = self._session.run(
            None if self._output_name is None else [self._output_name],
            {self._input_name: self._np.ascontiguousarray(tensor, dtype=self._np.float32)},
        )
        if len(outputs) != 1:
            raise ReIdModelContractError("vehicle ReID inference must return exactly one output")
        array = self._np.asarray(outputs[0], dtype=self._np.float32)
        if array.ndim > 2:
            array = array.reshape(array.shape[0], -1)
        expected_shape = (tensor.shape[0], self.config.feature_size)
        if tuple(array.shape) != expected_shape:
            raise ReIdModelContractError(
                f"vehicle ReID output shape {tuple(array.shape)} does not match {expected_shape}"
            )
        if not self._np.isfinite(array).all():
            raise ReIdModelContractError("vehicle ReID output contains non-finite values")
        embeddings = []
        for row in array:
            try:
                embeddings.append(AppearanceEmbedding(tuple(float(value) for value in row)))
            except ValueError as exc:
                raise ReIdModelContractError("vehicle ReID produced an invalid embedding") from exc
        return tuple(embeddings)

    def _verify_artifact(self) -> None:
        if not self.config.model_path.is_file():
            raise ReIdModelContractError(
                f"vehicle ReID model does not exist: {self.config.model_path}"
            )
        digest = hashlib.sha384()
        with self.config.model_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != self.config.expected_sha384:
            raise ReIdModelContractError(
                "vehicle ReID model SHA-384 does not match the pinned Open Model Zoo artifact"
            )

    def _validate_input_shape(self, shape: tuple[Any, ...]) -> None:
        if len(shape) != 4:
            raise ReIdModelContractError("vehicle ReID input must be NCHW")
        for actual, expected in zip(
            shape[1:],
            (3, self.config.input_height, self.config.input_width),
            strict=True,
        ):
            if isinstance(actual, int) and actual > 0 and actual != expected:
                raise ReIdModelContractError(
                    "vehicle ReID input shape does not match the configured contract"
                )

    def _validate_output_shape(self, shape: tuple[Any, ...]) -> None:
        if len(shape) < 2:
            raise ReIdModelContractError("vehicle ReID output must include batch and feature axes")
        static_feature_count = 1
        for dimension in shape[1:]:
            if not isinstance(dimension, int) or dimension <= 0:
                return
            static_feature_count *= dimension
        if static_feature_count != self.config.feature_size:
            raise ReIdModelContractError(
                "vehicle ReID output feature size does not match the configured contract"
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
    "OPENVINO_VEHICLE_REID_0001_SHA384",
    "OnnxVehicleReIdConfig",
    "OnnxVehicleReIdEncoder",
]

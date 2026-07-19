from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .domain import BoundingBox, Detection
from .unified_tracking import AppearanceEmbedding, TargetObservation
from .vision import VisionDependencyError

AIRCRAFT_APPEARANCE_LABELS = frozenset(
    {"aircraft", "airplane", "aeroplane", "plane", "helicopter", "drone", "uav"}
)


@dataclass(frozen=True, slots=True)
class AircraftAppearanceConfig:
    """Small deterministic descriptor for aircraft identity recovery.

    A generic COCO detector supplies the aircraft class, but the installed person
    and vehicle ReID networks intentionally do not emit embeddings for it.  This
    descriptor combines normalized low-frequency shape, local edge orientation
    and coarse colour.  It is a bounded recovery signal, not an independent
    detector: labels remain disjoint from person/vehicle embedding domains.
    """

    allowed_labels: frozenset[str] = AIRCRAFT_APPEARANCE_LABELS
    crop_padding_fraction: float = 0.10
    descriptor_width: int = 40
    descriptor_height: int = 40
    minimum_crop_side_px: int = 10
    minimum_grayscale_std: float = 2.0

    def __post_init__(self) -> None:
        if not self.allowed_labels or any(not label.strip() for label in self.allowed_labels):
            raise ValueError("aircraft appearance encoder needs non-empty allowed labels")
        if not math.isfinite(self.crop_padding_fraction) or not (
            0.0 <= self.crop_padding_fraction <= 0.25
        ):
            raise ValueError("aircraft appearance crop padding must be in [0, 0.25]")
        if self.descriptor_width < 16 or self.descriptor_height < 16:
            raise ValueError("aircraft appearance descriptor dimensions must be at least 16")
        if self.minimum_crop_side_px < 2:
            raise ValueError("aircraft appearance minimum crop side must be at least 2 pixels")
        if not math.isfinite(self.minimum_grayscale_std) or self.minimum_grayscale_std < 0.0:
            raise ValueError("aircraft appearance minimum grayscale standard deviation is invalid")
        object.__setattr__(
            self,
            "allowed_labels",
            frozenset(label.strip().lower() for label in self.allowed_labels),
        )


class HandcraftedAircraftAppearanceEncoder:
    """CPU-cheap aircraft-only appearance encoder for LOST/LCK recovery.

    It uses a deterministic image descriptor so live deployment does not need an
    additional large TensorRT engine.  The caller only merges its embeddings into
    aircraft observations; this preserves the existing strict no-cross-domain ReID
    rule in ``UnifiedTargetPool``.
    """

    def __init__(self, config: AircraftAppearanceConfig | None = None) -> None:
        self.config = config or AircraftAppearanceConfig()
        try:
            import cv2
            import numpy as np
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError(
                "Install live vision dependencies: pip install -e '.[vision]'"
            ) from exc
        self._cv2 = cv2
        self._np = np

    def encode_detections(
        self,
        image_bgr: Any,
        detections: Sequence[Detection],
    ) -> tuple[TargetObservation, ...]:
        """Emit appearance only for valid aircraft crops, preserving detector order."""

        if not detections:
            return ()
        embeddings: dict[int, AppearanceEmbedding] = {}
        for index, detection in enumerate(detections):
            if detection.label not in self.config.allowed_labels:
                continue
            embedding = self._encode_crop(image_bgr, detection.bbox)
            if embedding is not None:
                embeddings[index] = embedding
        return tuple(
            TargetObservation.from_detection(
                detection,
                appearance=embeddings.get(index),
                appearance_reliable=index in embeddings,
            )
            for index, detection in enumerate(detections)
        )

    def _encode_crop(self, image_bgr: Any, bbox: BoundingBox) -> AppearanceEmbedding | None:
        if not hasattr(image_bgr, "shape") or len(image_bgr.shape) < 2:
            raise ValueError("aircraft appearance encoder requires a BGR image array")
        image_height, image_width = image_bgr.shape[:2]
        if image_width <= 0 or image_height <= 0:
            raise ValueError("aircraft appearance encoder image cannot be empty")
        padded = bbox.expanded(self.config.crop_padding_fraction)
        x1 = max(0, min(image_width - 1, round(padded.x1 * image_width)))
        y1 = max(0, min(image_height - 1, round(padded.y1 * image_height)))
        x2 = max(x1 + 1, min(image_width, round(padded.x2 * image_width)))
        y2 = max(y1 + 1, min(image_height, round(padded.y2 * image_height)))
        crop = image_bgr[y1:y2, x1:x2]
        if (
            crop.size == 0
            or min(crop.shape[:2]) < self.config.minimum_crop_side_px
            or len(crop.shape) < 3
            or crop.shape[2] < 3
        ):
            return None
        resized = self._cv2.resize(
            crop,
            (self.config.descriptor_width, self.config.descriptor_height),
            interpolation=self._cv2.INTER_AREA,
        )
        gray = self._cv2.cvtColor(resized, self._cv2.COLOR_BGR2GRAY).astype(self._np.float32)
        grayscale_std = float(gray.std())
        if grayscale_std < self.config.minimum_grayscale_std:
            return None
        normalized_gray = (gray - float(gray.mean())) / max(grayscale_std, 1e-6)

        # Shape: normalized low-frequency DCT is stable across moderate scale and
        # brightness changes after the crop has been resized to a fixed canvas.
        dct = self._cv2.dct(normalized_gray)
        dct_features = dct[:8, :8].reshape(-1)[1:]

        # Local edge orientation: 4x4 HOG-style cells distinguish a winged
        # silhouette from generic rectangular and circular false matches.
        gradient_x = self._cv2.Sobel(normalized_gray, self._cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = self._cv2.Sobel(normalized_gray, self._cv2.CV_32F, 0, 1, ksize=3)
        magnitude, angle = self._cv2.cartToPolar(gradient_x, gradient_y, angleInDegrees=True)
        orientation = self._np.floor((angle % 180.0) / 22.5).astype(self._np.int32) % 8
        hog_parts: list[Any] = []
        for y0, y1_cell in _cells(self.config.descriptor_height, 4):
            for x0, x1_cell in _cells(self.config.descriptor_width, 4):
                hog_parts.append(
                    self._np.bincount(
                        orientation[y0:y1_cell, x0:x1_cell].reshape(-1),
                        weights=magnitude[y0:y1_cell, x0:x1_cell].reshape(-1),
                        minlength=8,
                    ).astype(self._np.float32)
                )
        hog_features = self._np.concatenate(hog_parts)

        # Coarse HSV protects recovery when two aircraft overlap in projection but
        # carry different paint/lighting; it has a deliberately lower block weight.
        hsv = self._cv2.cvtColor(resized, self._cv2.COLOR_BGR2HSV)
        hue_hist = self._np.histogram(hsv[:, :, 0], bins=8, range=(0, 180))[0].astype(
            self._np.float32
        )
        saturation_hist = self._np.histogram(hsv[:, :, 1], bins=4, range=(0, 256))[0].astype(
            self._np.float32
        )
        colour_features = self._np.concatenate((hue_hist, saturation_hist))

        aspect = self._np.asarray(
            [math.log(max(1.0, crop.shape[1]) / max(1.0, crop.shape[0]))],
            dtype=self._np.float32,
        )
        values = self._np.concatenate(
            (
                _normalize_block(dct_features, self._np) * 0.60,
                _normalize_block(hog_features, self._np) * 0.85,
                _normalize_block(colour_features, self._np) * 0.25,
                aspect * 0.10,
            )
        )
        try:
            return AppearanceEmbedding(tuple(float(value) for value in values))
        except ValueError:
            return None


def _cells(size: int, count: int) -> tuple[tuple[int, int], ...]:
    return tuple((index * size // count, (index + 1) * size // count) for index in range(count))


def _normalize_block(values: Any, np: Any) -> Any:
    norm = float(np.linalg.norm(values))
    return values / max(norm, 1e-6)


__all__ = [
    "AIRCRAFT_APPEARANCE_LABELS",
    "AircraftAppearanceConfig",
    "HandcraftedAircraftAppearanceEncoder",
]

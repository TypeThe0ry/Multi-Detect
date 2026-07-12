from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

from ..domain import BoundingBox, Detection, SensorKind

_LABEL_ALIASES = {"fire": "flame"}


def _normalize_label(label: Any) -> str:
    if isinstance(label, bytes):
        label = label.decode("utf-8")
    normalized = str(label).strip().lower()
    if not normalized:
        raise ValueError("legacy detection label cannot be empty")
    return _LABEL_ALIASES.get(normalized, normalized)


def _normalize_confidence(value: Any) -> float:
    if isinstance(value, bytes):
        value = value.decode("ascii")
    confidence = float(value)
    if not isfinite(confidence):
        raise ValueError("confidence must be finite")
    # Darknet bindings commonly expose either a 0..1 ratio or a 0..100 percent.
    if 1.0 < confidence <= 100.0:
        confidence /= 100.0
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be a ratio or percentage")
    return confidence


def _normalized_xyxy(
    x1: Any,
    y1: Any,
    x2: Any,
    y2: Any,
    *,
    image_width: int,
    image_height: int,
) -> BoundingBox:
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    values = tuple(float(value) for value in (x1, y1, x2, y2))
    if not all(isfinite(value) for value in values):
        raise ValueError("bounding box coordinates must be finite")
    left, top, right, bottom = values
    return BoundingBox(
        x1=max(0.0, min(1.0, left / image_width)),
        y1=max(0.0, min(1.0, top / image_height)),
        x2=max(0.0, min(1.0, right / image_width)),
        y2=max(0.0, min(1.0, bottom / image_height)),
    )


def adapt_darknet_detection(
    raw_detection: tuple[Any, Any, tuple[Any, Any, Any, Any]],
    *,
    image_width: int,
    image_height: int,
    sensor: SensorKind = SensorKind.RGB,
    model_version: str = "legacy-darknet",
) -> Detection:
    """Convert ``(label, confidence, (cx, cy, w, h))`` to a Detection."""

    try:
        raw_label, raw_confidence, raw_box = raw_detection
        center_x, center_y, width, height = (float(value) for value in raw_box)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Darknet detection tuple") from exc
    if not all(isfinite(value) for value in (center_x, center_y, width, height)):
        raise ValueError("Darknet bounding box coordinates must be finite")
    if width <= 0 or height <= 0:
        raise ValueError("Darknet bounding box width and height must be positive")

    bbox = _normalized_xyxy(
        center_x - width / 2.0,
        center_y - height / 2.0,
        center_x + width / 2.0,
        center_y + height / 2.0,
        image_width=image_width,
        image_height=image_height,
    )
    normalized_label = _normalize_label(raw_label)
    return Detection(
        label=normalized_label,
        confidence=_normalize_confidence(raw_confidence),
        bbox=bbox,
        sensor=sensor,
        model_version=model_version,
        metadata={"source_format": "darknet_cxcywh", "raw_label": str(raw_label)},
    )


def adapt_darknet_detections(
    raw_detections: Iterable[tuple[Any, Any, tuple[Any, Any, Any, Any]]],
    *,
    image_width: int,
    image_height: int,
    sensor: SensorKind = SensorKind.RGB,
    model_version: str = "legacy-darknet",
) -> tuple[Detection, ...]:
    return tuple(
        adapt_darknet_detection(
            detection,
            image_width=image_width,
            image_height=image_height,
            sensor=sensor,
            model_version=model_version,
        )
        for detection in raw_detections
    )


def _rows_as_python(raw_rows: Any) -> Iterable[Sequence[Any]]:
    rows = raw_rows
    if hasattr(rows, "detach"):
        rows = rows.detach()
    if hasattr(rows, "cpu"):
        rows = rows.cpu()
    if hasattr(rows, "tolist"):
        rows = rows.tolist()
    return rows


def adapt_yolov5_detections(
    raw_rows: Any,
    *,
    image_width: int,
    image_height: int,
    class_names: Sequence[str] | Mapping[int, str] = ("fire", "smoke"),
    sensor: SensorKind = SensorKind.RGB,
    model_version: str = "legacy-yolov5",
) -> tuple[Detection, ...]:
    """Convert YOLOv5 ``N x 6`` XYXY rows to normalized Detections."""

    detections: list[Detection] = []
    for row in _rows_as_python(raw_rows):
        if len(row) != 6:
            raise ValueError("each YOLOv5 row must contain exactly 6 values")
        x1, y1, x2, y2, raw_confidence, raw_class = row
        numeric_class = float(raw_class)
        class_index = int(numeric_class)
        if not isfinite(numeric_class) or numeric_class != class_index:
            raise ValueError("YOLOv5 class index must be a finite integer")
        try:
            raw_label = class_names[class_index]
        except (IndexError, KeyError) as exc:
            raise ValueError(f"unknown YOLOv5 class index: {class_index}") from exc

        detections.append(
            Detection(
                label=_normalize_label(raw_label),
                confidence=_normalize_confidence(raw_confidence),
                bbox=_normalized_xyxy(
                    x1,
                    y1,
                    x2,
                    y2,
                    image_width=image_width,
                    image_height=image_height,
                ),
                sensor=sensor,
                model_version=model_version,
                metadata={
                    "source_format": "yolov5_xyxy_nx6",
                    "class_index": class_index,
                    "raw_label": str(raw_label),
                },
            )
        )
    return tuple(detections)


@dataclass(frozen=True, slots=True)
class FireSmokeLegacyAdapter:
    """Configuration wrapper around the stateless legacy conversion helpers."""

    class_names: Sequence[str] | Mapping[int, str] = ("fire", "smoke")
    model_version: str = "legacy-fire-smoke"

    def from_darknet(
        self,
        raw_detections: Iterable[tuple[Any, Any, tuple[Any, Any, Any, Any]]],
        *,
        image_width: int,
        image_height: int,
        sensor: SensorKind = SensorKind.RGB,
    ) -> tuple[Detection, ...]:
        return adapt_darknet_detections(
            raw_detections,
            image_width=image_width,
            image_height=image_height,
            sensor=sensor,
            model_version=self.model_version,
        )

    def from_yolov5(
        self,
        raw_rows: Any,
        *,
        image_width: int,
        image_height: int,
        sensor: SensorKind = SensorKind.RGB,
    ) -> tuple[Detection, ...]:
        return adapt_yolov5_detections(
            raw_rows,
            image_width=image_width,
            image_height=image_height,
            class_names=self.class_names,
            sensor=sensor,
            model_version=self.model_version,
        )

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from .domain import BoundingBox, Detection


@dataclass(frozen=True, slots=True)
class LabeledBox:
    label: str
    bbox: BoundingBox
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class GroundTruthFrame:
    frame_id: str
    objects: tuple[LabeledBox, ...]


@dataclass(frozen=True, slots=True)
class PredictionFrame:
    frame_id: str
    detections: tuple[LabeledBox, ...]
    inference_latency_ms: float


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    label: str
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None


@dataclass(frozen=True, slots=True)
class DetectionEvaluationReport:
    frame_count: int
    iou_threshold: float
    confidence_threshold: float
    per_class: tuple[ClassMetrics, ...]
    overall: ClassMetrics
    false_alarm_frame_count: int
    missed_detection_frame_count: int
    inference_latency_p50_ms: float
    inference_latency_p95_ms: float


class JsonlPredictionWriter:
    """Streams normalized per-frame predictions without retaining video frames."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        self._lock = RLock()
        self._closed = False

    def append(
        self,
        *,
        frame_id: str,
        captured_at_s: float,
        detections: tuple[Detection, ...],
        inference_latency_ms: float,
    ) -> None:
        if not frame_id:
            raise ValueError("prediction frame_id cannot be empty")
        if not math.isfinite(captured_at_s) or captured_at_s < 0:
            raise ValueError("prediction captured_at_s must be finite and non-negative")
        if not math.isfinite(inference_latency_ms) or inference_latency_ms < 0:
            raise ValueError("inference_latency_ms must be finite and non-negative")
        document = {
            "frame_id": frame_id,
            "captured_at_s": captured_at_s,
            "inference_latency_ms": inference_latency_ms,
            "detections": [
                {
                    "label": detection.label,
                    "confidence": detection.confidence,
                    "bbox": detection.bbox.rounded(),
                    "sensor": detection.sensor.value,
                    "model_version": detection.model_version,
                    **(
                        {"diagnostics": diagnostics}
                        if (diagnostics := fire_rgb_diagnostics(detection))
                        else {}
                    ),
                }
                for detection in detections
            ],
        }
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("prediction writer is closed")
            self._handle.write(encoded)
            self._handle.write("\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()


_FIRE_RGB_DIAGNOSTIC_FIELDS = (
    "fire_rgb_bright_neutral_fraction",
    "fire_rgb_colorful_fraction",
    "fire_rgb_warm_fraction",
    "fire_rgb_bright_warm_fraction",
    "fire_rgb_bbox_aspect_ratio",
)


def fire_rgb_diagnostics(detection: Detection) -> dict[str, float]:
    """Return the bounded scalar fire-review evidence safe for prediction JSONL.

    Detection metadata can include arbitrary application objects.  Keep logs
    deterministic and pixel-free by exporting only the explicit finite RGB
    fields written by :class:`BrightNeutralLightVetoFilter`.
    """

    if detection.label.strip().lower() not in {"fire", "flame", "smoke"}:
        return {}
    result: dict[str, float] = {}
    for field in _FIRE_RGB_DIAGNOSTIC_FIELDS:
        value = detection.metadata.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        normalized = float(value)
        if math.isfinite(normalized):
            result[field] = normalized
    return result


def load_ground_truth_jsonl(path: str | Path) -> tuple[GroundTruthFrame, ...]:
    records = _load_jsonl(path)
    frames = tuple(
        GroundTruthFrame(
            frame_id=_required_text(record, "frame_id"),
            objects=_parse_labeled_boxes(record.get("objects"), require_confidence=False),
        )
        for record in records
    )
    _require_unique_frame_ids(frame.frame_id for frame in frames)
    return frames


def load_prediction_jsonl(path: str | Path) -> tuple[PredictionFrame, ...]:
    records = _load_jsonl(path)
    frames = tuple(
        PredictionFrame(
            frame_id=_required_text(record, "frame_id"),
            detections=_parse_labeled_boxes(record.get("detections"), require_confidence=True),
            inference_latency_ms=_finite_nonnegative(
                record.get("inference_latency_ms"),
                "inference_latency_ms",
            ),
        )
        for record in records
    )
    _require_unique_frame_ids(frame.frame_id for frame in frames)
    return frames


def evaluate_detections(
    ground_truth: tuple[GroundTruthFrame, ...],
    predictions: tuple[PredictionFrame, ...],
    *,
    iou_threshold: float = 0.5,
    confidence_threshold: float = 0.25,
) -> DetectionEvaluationReport:
    if not 0 < iou_threshold <= 1:
        raise ValueError("iou_threshold must be in (0, 1]")
    if not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be in [0, 1]")
    truth_by_id = {frame.frame_id: frame for frame in ground_truth}
    prediction_by_id = {frame.frame_id: frame for frame in predictions}
    if set(truth_by_id) != set(prediction_by_id):
        missing = sorted(set(truth_by_id) - set(prediction_by_id))
        extra = sorted(set(prediction_by_id) - set(truth_by_id))
        raise ValueError(
            f"ground-truth/prediction frame IDs differ; missing={missing}, extra={extra}"
        )

    counts: dict[str, list[int]] = {}
    false_alarm_frames = 0
    missed_detection_frames = 0
    latencies: list[float] = []
    for frame_id in sorted(truth_by_id):
        truth = truth_by_id[frame_id]
        prediction = prediction_by_id[frame_id]
        latencies.append(prediction.inference_latency_ms)
        labels = {item.label for item in truth.objects} | {
            item.label for item in prediction.detections if item.confidence >= confidence_threshold
        }
        frame_fp = 0
        frame_fn = 0
        for label in labels:
            truth_items = tuple(item for item in truth.objects if item.label == label)
            predicted_items = tuple(
                item
                for item in prediction.detections
                if item.label == label and item.confidence >= confidence_threshold
            )
            true_positive, false_positive, false_negative = _match_boxes(
                truth_items,
                predicted_items,
                iou_threshold=iou_threshold,
            )
            bucket = counts.setdefault(label, [0, 0, 0])
            bucket[0] += true_positive
            bucket[1] += false_positive
            bucket[2] += false_negative
            frame_fp += false_positive
            frame_fn += false_negative
        false_alarm_frames += frame_fp > 0
        missed_detection_frames += frame_fn > 0

    per_class = tuple(_metrics(label, *counts[label]) for label in sorted(counts))
    total_tp = sum(item.true_positives for item in per_class)
    total_fp = sum(item.false_positives for item in per_class)
    total_fn = sum(item.false_negatives for item in per_class)
    return DetectionEvaluationReport(
        frame_count=len(ground_truth),
        iou_threshold=iou_threshold,
        confidence_threshold=confidence_threshold,
        per_class=per_class,
        overall=_metrics("__overall__", total_tp, total_fp, total_fn),
        false_alarm_frame_count=false_alarm_frames,
        missed_detection_frame_count=missed_detection_frames,
        inference_latency_p50_ms=_percentile(latencies, 0.50),
        inference_latency_p95_ms=_percentile(latencies, 0.95),
    )


def evaluation_document(report: DetectionEvaluationReport) -> dict[str, Any]:
    return {
        "frame_count": report.frame_count,
        "iou_threshold": report.iou_threshold,
        "confidence_threshold": report.confidence_threshold,
        "per_class": [_metrics_document(item) for item in report.per_class],
        "overall": _metrics_document(report.overall),
        "false_alarm_frame_count": report.false_alarm_frame_count,
        "missed_detection_frame_count": report.missed_detection_frame_count,
        "inference_latency_p50_ms": report.inference_latency_p50_ms,
        "inference_latency_p95_ms": report.inference_latency_p95_ms,
    }


def _match_boxes(
    truth: tuple[LabeledBox, ...],
    predictions: tuple[LabeledBox, ...],
    *,
    iou_threshold: float,
) -> tuple[int, int, int]:
    candidates = sorted(
        (
            (truth_item.bbox.iou(prediction.bbox), truth_index, prediction_index)
            for truth_index, truth_item in enumerate(truth)
            for prediction_index, prediction in enumerate(predictions)
        ),
        reverse=True,
    )
    matched_truth: set[int] = set()
    matched_predictions: set[int] = set()
    for overlap, truth_index, prediction_index in candidates:
        if overlap < iou_threshold:
            break
        if truth_index in matched_truth or prediction_index in matched_predictions:
            continue
        matched_truth.add(truth_index)
        matched_predictions.add(prediction_index)
    true_positive = len(matched_truth)
    return (
        true_positive,
        len(predictions) - true_positive,
        len(truth) - true_positive,
    )


def _metrics(
    label: str, true_positive: int, false_positive: int, false_negative: int
) -> ClassMetrics:
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    return ClassMetrics(
        label=label,
        true_positives=true_positive,
        false_positives=false_positive,
        false_negatives=false_negative,
        precision=(true_positive / precision_denominator if precision_denominator else None),
        recall=(true_positive / recall_denominator if recall_denominator else None),
    )


def _metrics_document(metrics: ClassMetrics) -> dict[str, Any]:
    return {
        "label": metrics.label,
        "true_positives": metrics.true_positives,
        "false_positives": metrics.false_positives,
        "false_negatives": metrics.false_negatives,
        "precision": metrics.precision,
        "recall": metrics.recall,
    }


def _parse_labeled_boxes(value: object, *, require_confidence: bool) -> tuple[LabeledBox, ...]:
    if not isinstance(value, list):
        raise ValueError("labeled boxes must be an array")
    boxes: list[LabeledBox] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("labeled box entries must be objects")
        raw_bbox = item.get("bbox")
        if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
            raise ValueError("labeled box bbox must be a four-number array")
        confidence = (
            _finite_probability(item.get("confidence"), "confidence") if require_confidence else 1.0
        )
        boxes.append(
            LabeledBox(
                label=_canonical_label(_required_text(item, "label")),
                bbox=BoundingBox(*(float(component) for component in raw_bbox)),
                confidence=confidence,
            )
        )
    return tuple(boxes)


def _load_jsonl(path: str | Path) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            records.append(raw)
    if not records:
        raise ValueError("evaluation JSONL cannot be empty")
    return tuple(records)


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _finite_nonnegative(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _finite_probability(value: object, name: str) -> float:
    number = _finite_nonnegative(value, name)
    if number > 1:
        raise ValueError(f"{name} must be in [0, 1]")
    return number


def _canonical_label(label: str) -> str:
    normalized = label.strip().lower()
    return "flame" if normalized == "fire" else normalized


def _require_unique_frame_ids(frame_ids: Any) -> None:
    seen: set[str] = set()
    for frame_id in frame_ids:
        if frame_id in seen:
            raise ValueError(f"duplicate evaluation frame_id: {frame_id}")
        seen.add(frame_id)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


__all__ = [
    "ClassMetrics",
    "DetectionEvaluationReport",
    "GroundTruthFrame",
    "JsonlPredictionWriter",
    "LabeledBox",
    "PredictionFrame",
    "evaluate_detections",
    "evaluation_document",
    "load_ground_truth_jsonl",
    "load_prediction_jsonl",
]

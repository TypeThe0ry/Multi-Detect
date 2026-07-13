from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .evaluation import (
    DetectionEvaluationReport,
    GroundTruthFrame,
    PredictionFrame,
    evaluate_detections,
    evaluation_document,
)


@dataclass(frozen=True, slots=True)
class ThresholdRecommendation:
    label: str
    threshold: float
    precision: float
    recall: float
    f_beta: float
    recall_floor_satisfied: bool


def calibrate_class_thresholds(
    ground_truth: tuple[GroundTruthFrame, ...],
    predictions: tuple[PredictionFrame, ...],
    *,
    thresholds: Iterable[float],
    iou_threshold: float = 0.5,
    beta: float = 0.5,
    minimum_recall: float = 0.70,
) -> dict[str, Any]:
    """Sweep a shared grid, then recommend an independent threshold per class.

    ``beta < 1`` weighs precision more heavily, while ``minimum_recall`` prevents
    selecting an apparently clean threshold that suppresses too many true fires.
    """

    grid = _validated_threshold_grid(thresholds)
    if not math.isfinite(beta) or beta <= 0:
        raise ValueError("calibration beta must be finite and positive")
    if not math.isfinite(minimum_recall) or not 0 <= minimum_recall <= 1:
        raise ValueError("calibration minimum_recall must be in [0, 1]")
    reports = tuple(
        evaluate_detections(
            ground_truth,
            predictions,
            iou_threshold=iou_threshold,
            confidence_threshold=threshold,
        )
        for threshold in grid
    )
    labels = sorted({metric.label for report in reports for metric in report.per_class})
    recommendations: list[ThresholdRecommendation] = []
    sweeps: dict[str, list[dict[str, Any]]] = {label: [] for label in labels}
    for threshold, report in zip(grid, reports, strict=True):
        metrics_by_label = {metric.label: metric for metric in report.per_class}
        for label in labels:
            metrics = metrics_by_label.get(label)
            if metrics is None:
                continue
            score = _f_beta(metrics.precision, metrics.recall, beta=beta)
            sweeps[label].append(
                {
                    "threshold": threshold,
                    "true_positives": metrics.true_positives,
                    "false_positives": metrics.false_positives,
                    "false_negatives": metrics.false_negatives,
                    "precision": metrics.precision,
                    "recall": metrics.recall,
                    "f_beta": score,
                }
            )
    for label in labels:
        recommendation = _recommend_threshold(
            label,
            sweeps[label],
            minimum_recall=minimum_recall,
        )
        if recommendation is not None:
            recommendations.append(recommendation)

    recommended_thresholds = {item.label: item.threshold for item in recommendations}
    combined_report = evaluate_class_thresholds(
        ground_truth,
        predictions,
        thresholds=recommended_thresholds,
        iou_threshold=iou_threshold,
    )
    return {
        "frame_count": len(ground_truth),
        "iou_threshold": iou_threshold,
        "beta": beta,
        "minimum_recall": minimum_recall,
        "threshold_grid": list(grid),
        "recommendations": [
            {
                "label": item.label,
                "threshold": item.threshold,
                "precision": item.precision,
                "recall": item.recall,
                "f_beta": item.f_beta,
                "recall_floor_satisfied": item.recall_floor_satisfied,
            }
            for item in recommendations
        ],
        "combined_recommended_metrics": evaluation_document(combined_report),
        "per_class_sweeps": sweeps,
    }


def evaluate_class_thresholds(
    ground_truth: tuple[GroundTruthFrame, ...],
    predictions: tuple[PredictionFrame, ...],
    *,
    thresholds: dict[str, float],
    iou_threshold: float = 0.5,
) -> DetectionEvaluationReport:
    normalized: dict[str, float] = {}
    for label, threshold in thresholds.items():
        if not isinstance(label, str) or not label.strip():
            raise ValueError("class threshold label cannot be empty")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("class thresholds must be in [0, 1]")
        normalized[label.strip().lower()] = float(threshold)
    filtered = tuple(
        PredictionFrame(
            frame_id=frame.frame_id,
            detections=tuple(
                detection
                for detection in frame.detections
                if detection.label.lower() in normalized
                and detection.confidence >= normalized[detection.label.lower()]
            ),
            inference_latency_ms=frame.inference_latency_ms,
        )
        for frame in predictions
    )
    return evaluate_detections(
        ground_truth,
        filtered,
        iou_threshold=iou_threshold,
        confidence_threshold=0.0,
    )


def _validated_threshold_grid(thresholds: Iterable[float]) -> tuple[float, ...]:
    grid = tuple(sorted(set(float(value) for value in thresholds)))
    if not grid or any(not math.isfinite(value) or not 0 <= value <= 1 for value in grid):
        raise ValueError("calibration thresholds must be finite values in [0, 1]")
    return grid


def _f_beta(precision: float | None, recall: float | None, *, beta: float) -> float:
    if precision is None or recall is None or precision == 0 or recall == 0:
        return 0.0
    beta_squared = beta * beta
    return (1 + beta_squared) * precision * recall / (beta_squared * precision + recall)


def _recommend_threshold(
    label: str,
    points: list[dict[str, Any]],
    *,
    minimum_recall: float,
) -> ThresholdRecommendation | None:
    usable = [
        point for point in points if point["precision"] is not None and point["recall"] is not None
    ]
    if not usable:
        return None
    constrained = [point for point in usable if point["recall"] >= minimum_recall]
    candidates = constrained or usable
    selected = max(
        candidates,
        key=lambda point: (
            point["f_beta"],
            point["precision"],
            point["recall"],
            point["threshold"],
        ),
    )
    return ThresholdRecommendation(
        label=label,
        threshold=selected["threshold"],
        precision=selected["precision"],
        recall=selected["recall"],
        f_beta=selected["f_beta"],
        recall_floor_satisfied=bool(constrained),
    )


__all__ = [
    "ThresholdRecommendation",
    "calibrate_class_thresholds",
    "evaluate_class_thresholds",
]

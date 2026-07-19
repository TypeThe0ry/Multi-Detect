from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.domain import BoundingBox, Detection
from multidetect.evaluation import (
    JsonlPredictionWriter,
    evaluate_detections,
    load_ground_truth_jsonl,
    load_prediction_jsonl,
)

ROOT = Path(__file__).resolve().parents[1]


def test_evaluation_reports_precision_recall_false_alarms_and_misses() -> None:
    report = evaluate_detections(
        load_ground_truth_jsonl(ROOT / "examples/evaluation_ground_truth.demo.jsonl"),
        load_prediction_jsonl(ROOT / "examples/evaluation_predictions.demo.jsonl"),
    )

    assert report.frame_count == 3
    assert report.overall.true_positives == 1
    assert report.overall.false_positives == 1
    assert report.overall.false_negatives == 1
    assert report.overall.precision == pytest.approx(0.5)
    assert report.overall.recall == pytest.approx(0.5)
    assert report.false_alarm_frame_count == 1
    assert report.missed_detection_frame_count == 1
    assert report.inference_latency_p50_ms == pytest.approx(20.0)
    assert report.inference_latency_p95_ms == pytest.approx(30.0)


def test_evaluation_requires_exact_frame_alignment(tmp_path: Path) -> None:
    prediction = tmp_path / "predictions.jsonl"
    prediction.write_text(
        '{"frame_id":"wrong","inference_latency_ms":1,"detections":[]}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="frame IDs differ"):
        evaluate_detections(
            load_ground_truth_jsonl(ROOT / "examples/evaluation_ground_truth.demo.jsonl"),
            load_prediction_jsonl(prediction),
        )


def test_prediction_writer_streams_every_processed_frame(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = JsonlPredictionWriter(path)
    writer.append(
        frame_id="frame-1",
        captured_at_s=10.0,
        detections=(Detection("flame", 0.9, BoundingBox(0.1, 0.2, 0.3, 0.4)),),
        inference_latency_ms=12.5,
    )
    writer.close()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["frame_id"] == "frame-1"
    assert record["inference_latency_ms"] == pytest.approx(12.5)
    assert record["detections"][0]["label"] == "flame"
    assert record["detections"][0]["bbox"] == [0.1, 0.2, 0.3, 0.4]


def test_prediction_writer_exports_only_explicit_scalar_fire_rgb_diagnostics(
    tmp_path: Path,
) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = JsonlPredictionWriter(path)
    writer.append(
        frame_id="frame-1",
        captured_at_s=10.0,
        detections=(
            Detection(
                "flame",
                0.9,
                BoundingBox(0.1, 0.2, 0.3, 0.4),
                metadata={
                    "fire_rgb_warm_fraction": 0.7,
                    "fire_rgb_bbox_aspect_ratio": 1.25,
                    "unrelated": object(),
                    "fire_rgb_colorful_fraction": float("nan"),
                },
            ),
            Detection(
                "person",
                0.9,
                BoundingBox(0.4, 0.2, 0.6, 0.8),
                metadata={"fire_rgb_warm_fraction": 0.9},
            ),
        ),
        inference_latency_ms=12.5,
    )
    writer.close()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["detections"][0]["diagnostics"] == {
        "fire_rgb_warm_fraction": 0.7,
        "fire_rgb_bbox_aspect_ratio": 1.25,
    }
    assert "diagnostics" not in record["detections"][1]

#!/usr/bin/env python3
"""Evaluate the VisDrone priority detector on a recorded public video.

The harness mirrors the production priority-detector path: source-label thresholds,
VisDrone-to-runtime label remapping, temporal vehicle confirmation, and a scheduled
frame cadence.  It never opens a live camera or any vehicle interface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from multidetect.cli import (
    VEHICLE_TEMPORAL_LABEL_ALIASES,
    VISDRONE_PRIORITY_CLASS_NAMES,
    VISDRONE_PRIORITY_LABEL_MAP,
)
from multidetect.evaluation import JsonlPredictionWriter
from multidetect.vision import (
    ClassConfidenceFilter,
    FrameCadencedDetector,
    LabelRemapDetector,
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    TemporalDetectionFilter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the production-equivalent VisDrone priority detector over a recorded "
            "video and write reproducible class/cadence evidence."
        )
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--predictions-out",
        type=Path,
        help="normalized emitted per-frame detections; defaults beside --out",
    )
    parser.add_argument(
        "--source-url",
        default="",
        help="public source URL recorded in the evidence document",
    )
    parser.add_argument("--input-width", type=int, default=960)
    parser.add_argument("--input-height", type=int, default=960)
    parser.add_argument("--class-names", default=",".join(VISDRONE_PRIORITY_CLASS_NAMES))
    parser.add_argument("--label-map", default=VISDRONE_PRIORITY_LABEL_MAP)
    parser.add_argument("--candidate-confidence", type=float, default=0.10)
    parser.add_argument("--priority-confidence", type=float, default=0.30)
    parser.add_argument("--person-confidence", type=float, default=0.30)
    parser.add_argument("--vehicle-confidence", type=float, default=0.60)
    parser.add_argument(
        "--label-confidence-thresholds",
        default="truck=0.80",
        help="comma-separated source-label=confidence overrides before runtime label remap",
    )
    parser.add_argument("--vehicle-stability-frames", type=int, default=3)
    parser.add_argument("--model-iou-threshold", type=float, default=0.45)
    parser.add_argument("--frame-stride", type=int, default=4)
    parser.add_argument("--frame-phase", type=int, default=0)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--provider", action="append", default=[])
    return parser


def parse_class_names(raw: str) -> tuple[str, ...]:
    labels = tuple(label.strip().lower() for label in raw.split(",") if label.strip())
    if not labels:
        raise ValueError("class names must contain at least one comma-separated label")
    return labels


def parse_label_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("label map entries must use source=destination")
        source, destination = (value.strip().lower() for value in item.split("=", 1))
        if not source or not destination:
            raise ValueError("label map entries cannot be empty")
        mapping[source] = destination
    return mapping


def parse_label_confidence_thresholds(raw: str) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("label confidence entries must use class=confidence")
        label, raw_value = (value.strip() for value in item.split("=", 1))
        if not label:
            raise ValueError("label confidence labels cannot be empty")
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError("label confidence values must be numeric") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("label confidence values must be in [0, 1]")
        thresholds[label.lower()] = value
    return thresholds


def build_source_thresholds(
    *,
    person_confidence: float,
    vehicle_confidence: float,
    overrides: dict[str, float],
) -> dict[str, float]:
    thresholds = {
        "pedestrian": person_confidence,
        "people": person_confidence,
        "bicycle": vehicle_confidence,
        "car": vehicle_confidence,
        "van": vehicle_confidence,
        "truck": vehicle_confidence,
        "tricycle": vehicle_confidence,
        "awning-tricycle": vehicle_confidence,
        "bus": vehicle_confidence,
        "motor": vehicle_confidence,
    }
    thresholds.update(overrides)
    return thresholds


class _RecordingDetector:
    """Keep one raw detector result for source-label evidence on scheduled frames."""

    def __init__(self, detector: OnnxRawYoloDetector) -> None:
        self.detector = detector
        self.last_detections: tuple[Any, ...] = ()
        self.call_count = 0

    @property
    def class_names(self) -> tuple[str, ...]:
        return self.detector.class_names

    @property
    def provider_names(self) -> tuple[str, ...]:
        return self.detector.provider_names

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)

    def detect(self, image_bgr: Any) -> tuple[Any, ...]:
        self.call_count += 1
        self.last_detections = tuple(self.detector.detect(image_bgr))
        return self.last_detections


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for recorded-video evaluation") from exc

    class_names = parse_class_names(args.class_names)
    label_map = parse_label_map(args.label_map)
    overrides = parse_label_confidence_thresholds(args.label_confidence_thresholds)
    source_labels = set(class_names)
    unknown_map = set(label_map).difference(source_labels)
    unknown_overrides = set(overrides).difference(source_labels)
    if unknown_map:
        raise ValueError(
            "label map contains unknown source classes: " + ", ".join(sorted(unknown_map))
        )
    if unknown_overrides:
        raise ValueError(
            "label confidence overrides contain unknown source classes: "
            + ", ".join(sorted(unknown_overrides))
        )
    source_thresholds = build_source_thresholds(
        person_confidence=args.person_confidence,
        vehicle_confidence=args.vehicle_confidence,
        overrides=overrides,
    )
    candidate_confidence = min(
        args.candidate_confidence,
        args.priority_confidence,
        args.person_confidence,
        args.vehicle_confidence,
        *overrides.values(),
    )
    raw = OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=args.model,
            class_names=class_names,
            input_width=args.input_width,
            input_height=args.input_height,
            confidence_threshold=candidate_confidence,
            iou_threshold=args.model_iou_threshold,
            providers=tuple(args.provider),
        )
    )
    recording = _RecordingDetector(raw)
    detector = FrameCadencedDetector(
        TemporalDetectionFilter(
            LabelRemapDetector(
                ClassConfidenceFilter(
                    recording,
                    source_thresholds,
                    default_threshold=args.priority_confidence,
                ),
                label_map,
                fusion_iou_threshold=args.model_iou_threshold,
            ),
            labels=frozenset({"bicycle", "car", "motorcycle", "bus", "truck"}),
            minimum_consecutive_frames=args.vehicle_stability_frames,
            iou_threshold=0.25,
            maximum_missed_frames=1,
            label_aliases=VEHICLE_TEMPORAL_LABEL_ALIASES,
        ),
        frame_stride=args.frame_stride,
        frame_phase=args.frame_phase,
    )
    detector.warmup(iterations=1)

    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise ValueError(f"OpenCV cannot open video: {args.video}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS))
    if not math.isfinite(source_fps) or source_fps <= 0.0:
        source_fps = 0.0
    source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    expected_frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    predictions_path = args.predictions_out or args.out.with_suffix(".predictions.jsonl")
    raw_counts: Counter[str] = Counter()
    emitted_counts: Counter[str] = Counter()
    emitted_frame_counts: Counter[str] = Counter()
    highest_confidence: dict[str, float] = defaultdict(float)
    raw_truck_examples: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    decoded_frames = 0

    with _prediction_writer(predictions_path) as writer:
        while args.max_frames is None or decoded_frames < args.max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            before_calls = recording.call_count
            started = time.perf_counter()
            emitted = tuple(detector.detect(frame))
            latency_ms = (time.perf_counter() - started) * 1_000.0
            if recording.call_count != before_calls:
                latencies_ms.append(latency_ms)
                for detection in recording.last_detections:
                    raw_counts[detection.label] += 1
                    highest_confidence[detection.label] = max(
                        highest_confidence[detection.label], detection.confidence
                    )
                    if detection.label == "truck":
                        raw_truck_examples.append(
                            {
                                "frame_index": decoded_frames,
                                "confidence": round(detection.confidence, 6),
                                "bbox": detection.bbox.rounded(),
                            }
                        )
            labels_in_frame: set[str] = set()
            for detection in emitted:
                emitted_counts[detection.label] += 1
                labels_in_frame.add(detection.label)
            for label in labels_in_frame:
                emitted_frame_counts[label] += 1
            writer.append(
                frame_id=f"video-{decoded_frames:06d}",
                captured_at_s=(
                    decoded_frames / source_fps if source_fps else float(decoded_frames)
                ),
                detections=emitted,
                inference_latency_ms=latency_ms,
            )
            decoded_frames += 1
    capture.release()
    raw_truck_examples.sort(key=lambda item: (-item["confidence"], item["frame_index"]))
    document = {
        "event": "priority_public_video_evaluated",
        "video": {
            "path": str(args.video.resolve()),
            "source_url": args.source_url,
            "sha256": _sha256(args.video),
            "bytes": args.video.stat().st_size,
            "decoded_frames": decoded_frames,
            "reported_frame_count": expected_frame_count,
            "fps": source_fps,
            "width": source_width,
            "height": source_height,
        },
        "model": {
            "path": str(args.model.resolve()),
            "sha256": _sha256(args.model),
            "active_providers": list(raw.provider_names),
            "class_names": list(class_names),
        },
        "runtime_equivalent": {
            "candidate_confidence": candidate_confidence,
            "priority_confidence": args.priority_confidence,
            "person_confidence": args.person_confidence,
            "vehicle_confidence": args.vehicle_confidence,
            "source_thresholds": source_thresholds,
            "label_map": label_map,
            "vehicle_stability_frames": args.vehicle_stability_frames,
            "frame_stride": args.frame_stride,
            "frame_phase": args.frame_phase,
        },
        "cadence": {
            "model_inference_frames": detector.inference_count,
            "skipped_frames": detector.skipped_count,
        },
        "raw_detection_count_by_source_label": dict(sorted(raw_counts.items())),
        "emitted_detection_count_by_runtime_label": dict(sorted(emitted_counts.items())),
        "emitted_frame_count_by_runtime_label": dict(sorted(emitted_frame_counts.items())),
        "highest_raw_confidence_by_source_label": {
            label: round(value, 6) for label, value in sorted(highest_confidence.items())
        },
        "highest_raw_truck_candidates": raw_truck_examples[:12],
        "scheduled_inference_latency_ms": _latency_summary(latencies_ms),
        "predictions_path": str(predictions_path.resolve()),
        "flight_control_enabled": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


class _prediction_writer:
    """Small context adapter because JsonlPredictionWriter intentionally has no __enter__."""

    def __init__(self, path: Path) -> None:
        self.writer = JsonlPredictionWriter(path)

    def __enter__(self) -> JsonlPredictionWriter:
        return self.writer

    def __exit__(self, *_unused: object) -> None:
        self.writer.close()


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": round(ordered[(len(ordered) - 1) // 2], 6),
        "p95": round(ordered[round(0.95 * (len(ordered) - 1))], 6),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_args(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise FileNotFoundError(f"video does not exist: {args.video}")
    if not args.model.is_file():
        raise FileNotFoundError(f"model does not exist: {args.model}")
    for name in ("input_width", "input_height", "vehicle_stability_frames", "frame_stride"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if not 0 <= args.frame_phase < args.frame_stride:
        raise ValueError("frame_phase must be in [0, frame_stride)")
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("max_frames must be positive when set")
    for name in (
        "candidate_confidence",
        "priority_confidence",
        "person_confidence",
        "vehicle_confidence",
        "model_iou_threshold",
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1]")
    if args.model_iou_threshold <= 0.0:
        raise ValueError("model_iou_threshold must be positive")


if __name__ == "__main__":
    raise SystemExit(main())

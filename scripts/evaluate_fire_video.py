#!/usr/bin/env python3
"""Evaluate the fire-candidate pipeline on an annotated recorded video.

The tool is deliberately offline: it opens a file, runs the same fire-model
candidate filters used by the live process, and writes reproducible detection
and temporal-continuity evidence.  It never opens RTSP, MAVLink, or a control
interface.

FURG Fire Dataset annotations are OpenCV 2.x XML rectangles.  They mark flame
regions (not smoke), so the quantitative score below is intentionally flame
only; smoke candidate counts remain available in the JSON evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import xml.etree.ElementTree as element_tree
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multidetect.domain import BoundingBox, Detection
from multidetect.evaluation import (
    GroundTruthFrame,
    LabeledBox,
    PredictionFrame,
    evaluate_detections,
    evaluation_document,
    fire_rgb_diagnostics,
)
from multidetect.vision import (
    BrightNeutralLightVetoFilter,
    ClassConfidenceFilter,
    OnnxNx6Config,
    OnnxNx6Detector,
    TemporalDetectionFilter,
    TiledDetectionConfig,
    TiledDetectionFusion,
)

FIRE_LABELS = frozenset({"fire", "flame", "smoke"})


@dataclass(frozen=True, slots=True)
class FurgAnnotations:
    """Normalized flame annotations decoded from a FURG-style XML document."""

    frame_width: int
    frame_height: int
    frame_count: int
    boxes_by_frame: dict[int, tuple[BoundingBox, ...]]

    @property
    def annotated_frame_count(self) -> int:
        return sum(bool(boxes) for boxes in self.boxes_by_frame.values())

    @property
    def annotated_box_count(self) -> int:
        return sum(len(boxes) for boxes in self.boxes_by_frame.values())


class _RecordingDetector:
    """Record one detector stage without issuing a second model inference."""

    def __init__(self, detector: Any) -> None:
        self.detector = detector
        self.last_detections: tuple[Detection, ...] = ()
        self.call_count = 0

    @property
    def class_names(self) -> tuple[str, ...]:
        current: Any = self.detector
        while current is not None:
            names = getattr(current, "class_names", None)
            if names is not None:
                return tuple(names)
            current = getattr(current, "detector", None)
        return ()

    @property
    def provider_names(self) -> tuple[str, ...]:
        current: Any = self.detector
        while current is not None:
            names = getattr(current, "provider_names", None)
            if names is not None:
                return tuple(names)
            current = getattr(current, "detector", None)
        return ()

    def warmup(self, *, iterations: int = 1) -> None:
        current: Any = self.detector
        while current is not None:
            warmup = getattr(current, "warmup", None)
            if callable(warmup):
                warmup(iterations=iterations)
                return
            current = getattr(current, "detector", None)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        current: Any = self.detector
        while current is not None:
            covers = getattr(current, "covers_labels", None)
            if callable(covers):
                return bool(covers(required_labels))
            current = getattr(current, "detector", None)
        available = {label.strip().lower() for label in self.class_names}
        return {label.strip().lower() for label in required_labels}.issubset(available)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        self.call_count += 1
        self.last_detections = tuple(self.detector.detect(image_bgr))
        return self.last_detections


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the production-equivalent fire candidate filters over a recorded "
            "video and score FURG OpenCV XML flame rectangles."
        )
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--onnx-model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--predictions-out", type=Path)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--input-width", type=int, default=640)
    parser.add_argument("--input-height", type=int, default=640)
    parser.add_argument("--class-names", default="flame,smoke")
    parser.add_argument("--candidate-confidence", type=float, default=0.10)
    parser.add_argument("--flame-confidence", type=float, default=0.72)
    parser.add_argument("--smoke-confidence", type=float, default=0.60)
    parser.add_argument("--minimum-bright-warm-fraction", type=float, default=0.0)
    parser.add_argument("--candidate-stability-frames", type=int, default=6)
    parser.add_argument("--temporal-iou", type=float, default=0.25)
    parser.add_argument("--maximum-missed-frames", type=int, default=1)
    parser.add_argument("--maximum-center-distance", type=float, default=0.10)
    parser.add_argument("--minimum-area-ratio", type=float, default=0.12)
    parser.add_argument("--evaluation-iou", type=float, default=0.25)
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument(
        "--tile-columns",
        type=int,
        default=1,
        help="optional horizontal fire-model tile count; 1x1 keeps the full-frame baseline",
    )
    parser.add_argument(
        "--tile-rows",
        type=int,
        default=1,
        help="optional vertical fire-model tile count; 1x1 keeps the full-frame baseline",
    )
    parser.add_argument("--tile-overlap", type=float, default=0.15)
    parser.add_argument("--tile-scan-interval-frames", type=int, default=3)
    parser.add_argument("--tile-fusion-iou-threshold", type=float, default=0.30)
    parser.add_argument(
        "--tile-confidence-threshold",
        type=float,
        default=0.25,
        help="minimum raw confidence for a mapped tile candidate before class filtering",
    )
    parser.add_argument(
        "--tile-labels",
        default="flame,smoke",
        help="comma-separated labels eligible for tile inference; empty accepts every model label",
    )
    parser.add_argument(
        "--tile-maximum-box-area",
        type=float,
        default=1.0,
        help="discard mapped tile boxes larger than this normalized image area",
    )
    parser.add_argument(
        "--expected-label",
        help=(
            "optional known-positive label for unannotated video; records raw-to-stable "
            "frame coverage and continuity (fire is treated as flame/fire)"
        ),
    )
    parser.add_argument("--provider", action="append", default=[])
    return parser


def parse_class_names(raw: str) -> tuple[str, ...]:
    names = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    if not names:
        raise ValueError("class names must contain at least one comma-separated label")
    return names


def tiled_detection_config_from_args(args: argparse.Namespace) -> TiledDetectionConfig | None:
    """Build an optional tiled fire pass without changing the full-frame baseline.

    The live fire model is normally a single full-frame pass.  Keeping tile scan
    controls in the offline evaluator makes resolution/latency trade-offs
    measurable on public video before a production launcher is changed.
    """

    if args.tile_columns == 1 and args.tile_rows == 1:
        return None
    labels = frozenset(item.strip().lower() for item in args.tile_labels.split(",") if item.strip())
    return TiledDetectionConfig(
        columns=args.tile_columns,
        rows=args.tile_rows,
        overlap_fraction=args.tile_overlap,
        scan_interval_frames=args.tile_scan_interval_frames,
        fusion_iou_threshold=args.tile_fusion_iou_threshold,
        tile_confidence_threshold=args.tile_confidence_threshold,
        tile_labels=labels,
        maximum_tile_box_area=args.tile_maximum_box_area,
    )


def load_furg_annotations(path: Path) -> FurgAnnotations:
    """Read OpenCV 2.x ``Rect(x, y, width, height)`` flame annotations."""

    root = element_tree.parse(path).getroot()
    frame_width = _required_positive_int(root.findtext("frameWidth"), "frameWidth")
    frame_height = _required_positive_int(root.findtext("frameHeight"), "frameHeight")
    frames = root.find("frames")
    if frames is None:
        raise ValueError(f"FURG annotation has no frames node: {path}")

    boxes_by_frame: dict[int, tuple[BoundingBox, ...]] = {}
    seen_frame_numbers: set[int] = set()
    for frame in frames.findall("_"):
        frame_number = _required_nonnegative_int(frame.findtext("frameNumber"), "frameNumber")
        if frame_number in seen_frame_numbers:
            raise ValueError(f"duplicate FURG frame number {frame_number}: {path}")
        seen_frame_numbers.add(frame_number)
        annotations = frame.find("annotations")
        rectangles = () if annotations is None else annotations.findall("_")
        boxes_by_frame[frame_number] = tuple(
            _parse_furg_rectangle(
                rectangle.text or "",
                frame_width=frame_width,
                frame_height=frame_height,
                path=path,
                frame_number=frame_number,
            )
            for rectangle in rectangles
            if (rectangle.text or "").strip()
        )
    if not boxes_by_frame:
        raise ValueError(f"FURG annotation has no frame entries: {path}")
    return FurgAnnotations(
        frame_width=frame_width,
        frame_height=frame_height,
        frame_count=len(boxes_by_frame),
        boxes_by_frame=boxes_by_frame,
    )


def tracking_continuity_document(
    ground_truth: Sequence[GroundTruthFrame],
    predictions: Sequence[PredictionFrame],
    *,
    iou_threshold: float,
) -> dict[str, Any]:
    """Quantify stable target continuity for annotated flame intervals."""

    if not 0.0 < iou_threshold <= 1.0:
        raise ValueError("iou_threshold must be in (0, 1]")
    truth_by_id = {frame.frame_id: frame for frame in ground_truth}
    prediction_by_id = {frame.frame_id: frame for frame in predictions}
    if set(truth_by_id) != set(prediction_by_id):
        raise ValueError("ground truth and prediction frame IDs must match")

    ordered_ids = sorted(truth_by_id, key=_frame_order_key)
    positive_indices = [
        index for index, frame_id in enumerate(ordered_ids) if truth_by_id[frame_id].objects
    ]
    positive_segments = _contiguous_segments(positive_indices)
    matched_indices = [
        index
        for index, frame_id in enumerate(ordered_ids)
        if _has_flame_match(
            truth_by_id[frame_id].objects,
            prediction_by_id[frame_id].detections,
            iou_threshold=iou_threshold,
        )
    ]
    matched_set = set(matched_indices)
    false_alarm_indices = [
        index
        for index, frame_id in enumerate(ordered_ids)
        if not truth_by_id[frame_id].objects and prediction_by_id[frame_id].detections
    ]
    confirmation_delays: list[int | None] = []
    maximum_missed_gap = 0
    for segment in positive_segments:
        hits = [index for index in segment if index in matched_set]
        confirmation_delays.append((hits[0] - segment[0]) if hits else None)
        maximum_missed_gap = max(maximum_missed_gap, _maximum_unmatched_run(segment, matched_set))

    positive_frame_count = len(positive_indices)
    matched_positive_frame_count = sum(index in matched_set for index in positive_indices)
    resolved_delays = [delay for delay in confirmation_delays if delay is not None]
    return {
        "positive_segment_count": len(positive_segments),
        "positive_frame_count": positive_frame_count,
        "matched_positive_frame_count": matched_positive_frame_count,
        "matched_positive_frame_rate": (
            matched_positive_frame_count / positive_frame_count if positive_frame_count else None
        ),
        "false_alarm_frame_count": len(false_alarm_indices),
        "maximum_unmatched_positive_run_frames": maximum_missed_gap,
        "confirmation_delay_frames_by_positive_segment": confirmation_delays,
        "confirmation_delay_frames_p50": _percentile(resolved_delays, 0.50),
        "confirmation_delay_frames_p95": _percentile(resolved_delays, 0.95),
    }


def label_presence_document(
    present_by_frame: Sequence[bool],
    *,
    evaluated_frame_count: int,
) -> dict[str, Any]:
    """Summarize label visibility for a known-positive unannotated clip."""

    if evaluated_frame_count != len(present_by_frame):
        raise ValueError("evaluated_frame_count must match presence observations")
    hit_indices = [index for index, present in enumerate(present_by_frame) if present]
    return {
        "detected_frame_count": len(hit_indices),
        "detected_frame_rate": (
            len(hit_indices) / evaluated_frame_count if evaluated_frame_count else None
        ),
        "first_detected_evaluated_frame": hit_indices[0] if hit_indices else None,
        "longest_detected_run_frames": _longest_true_run(present_by_frame),
    }


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for recorded-video evaluation") from exc

    class_names = parse_class_names(args.class_names)
    annotations = load_furg_annotations(args.annotations) if args.annotations is not None else None
    raw_detector: Any = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=args.onnx_model,
            class_names=class_names,
            input_width=args.input_width,
            input_height=args.input_height,
            confidence_threshold=args.candidate_confidence,
            output_coordinates="letterbox_xyxy_px",
            providers=tuple(args.provider),
        )
    )
    tile_config = tiled_detection_config_from_args(args)
    if tile_config is not None:
        raw_detector = TiledDetectionFusion(raw_detector, tile_config)
    raw = _RecordingDetector(raw_detector)
    thresholded = _RecordingDetector(
        ClassConfidenceFilter(
            raw,
            {
                "fire": args.flame_confidence,
                "flame": args.flame_confidence,
                "smoke": args.smoke_confidence,
            },
            default_threshold=args.candidate_confidence,
        )
    )
    vetoed = _RecordingDetector(
        BrightNeutralLightVetoFilter(
            thresholded,
            minimum_bright_warm_fraction=args.minimum_bright_warm_fraction,
        )
    )
    stable = TemporalDetectionFilter(
        vetoed,
        labels=FIRE_LABELS,
        minimum_consecutive_frames=args.candidate_stability_frames,
        iou_threshold=args.temporal_iou,
        maximum_missed_frames=args.maximum_missed_frames,
        label_aliases={"fire": "flame"},
        maximum_center_distance=args.maximum_center_distance,
        minimum_area_ratio=args.minimum_area_ratio,
    )
    raw.warmup(iterations=1)

    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise ValueError(f"OpenCV cannot open video: {args.video}")
    video_fps = _finite_positive_or_zero(capture.get(cv2.CAP_PROP_FPS))
    video_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    expected_frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if video_width <= 0 or video_height <= 0:
        raise ValueError(f"video has invalid dimensions: {args.video}")

    predictions_path = args.predictions_out or args.out.with_suffix(".predictions.jsonl")
    raw_counts: Counter[str] = Counter()
    thresholded_counts: Counter[str] = Counter()
    vetoed_counts: Counter[str] = Counter()
    stable_counts: Counter[str] = Counter()
    latencies_ms: list[float] = []
    ground_truth: list[GroundTruthFrame] = []
    raw_predictions: list[PredictionFrame] = []
    thresholded_predictions: list[PredictionFrame] = []
    vetoed_predictions: list[PredictionFrame] = []
    stable_predictions: list[PredictionFrame] = []
    expected_label = _normalize_expected_label(args.expected_label)
    expected_presence: dict[str, list[bool]] | None = (
        {"raw": [], "thresholded": [], "bright_neutral_vetoed": [], "stable": []}
        if expected_label is not None
        else None
    )
    decoded_frames = 0
    evaluated_frames = 0
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with predictions_path.open("w", encoding="utf-8", newline="\n") as handle:
        while args.max_frames is None or decoded_frames < args.max_frames:
            ok, image = capture.read()
            if not ok:
                break
            frame_index = decoded_frames
            decoded_frames += 1
            if frame_index % args.frame_stride:
                continue
            frame_id = str(frame_index)
            started = time.perf_counter()
            stable_detections = tuple(stable.detect(image))
            latency_ms = (time.perf_counter() - started) * 1_000.0
            raw_detections = raw.last_detections
            thresholded_detections = thresholded.last_detections
            vetoed_detections = vetoed.last_detections
            evaluated_frames += 1
            latencies_ms.append(latency_ms)
            raw_counts.update(item.label for item in raw_detections)
            thresholded_counts.update(item.label for item in thresholded_detections)
            vetoed_counts.update(item.label for item in vetoed_detections)
            stable_counts.update(item.label for item in stable_detections)
            if expected_presence is not None:
                expected_presence["raw"].append(
                    _has_expected_label(raw_detections, expected_label)
                )
                expected_presence["thresholded"].append(
                    _has_expected_label(thresholded_detections, expected_label)
                )
                expected_presence["bright_neutral_vetoed"].append(
                    _has_expected_label(vetoed_detections, expected_label)
                )
                expected_presence["stable"].append(
                    _has_expected_label(stable_detections, expected_label)
                )
            truth = () if annotations is None else annotations.boxes_by_frame.get(frame_index, ())
            captured_at_s = frame_index / video_fps if video_fps else 0.0
            document = {
                "frame_id": frame_id,
                "frame_index": frame_index,
                "captured_at_s": captured_at_s,
                "inference_latency_ms": latency_ms,
                "ground_truth_flame": [_box_document(box) for box in truth],
                "raw": _detections_document(raw_detections),
                "thresholded": _detections_document(thresholded_detections),
                "vetoed": _detections_document(vetoed_detections),
                "stable": _detections_document(stable_detections),
            }
            handle.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
            if annotations is not None:
                ground_truth.append(
                    GroundTruthFrame(
                        frame_id=frame_id,
                        objects=tuple(LabeledBox("flame", box) for box in truth),
                    )
                )
                raw_predictions.append(_prediction_frame(frame_id, raw_detections, latency_ms))
                thresholded_predictions.append(
                    _prediction_frame(frame_id, thresholded_detections, latency_ms)
                )
                vetoed_predictions.append(
                    _prediction_frame(frame_id, vetoed_detections, latency_ms)
                )
                stable_predictions.append(
                    _prediction_frame(frame_id, stable_detections, latency_ms)
                )
    capture.release()

    metrics: dict[str, Any] | None = None
    continuity: dict[str, Any] | None = None
    if annotations is not None:
        truth_tuple = tuple(ground_truth)
        metrics = {
            "raw_flame": _flame_evaluation_document(
                truth_tuple,
                tuple(raw_predictions),
                args.evaluation_iou,
            ),
            "thresholded_flame": _flame_evaluation_document(
                truth_tuple, tuple(thresholded_predictions), args.evaluation_iou
            ),
            "bright_neutral_vetoed_flame": _flame_evaluation_document(
                truth_tuple, tuple(vetoed_predictions), args.evaluation_iou
            ),
            "stable_flame": _flame_evaluation_document(
                truth_tuple, tuple(stable_predictions), args.evaluation_iou
            ),
        }
        continuity = tracking_continuity_document(
            truth_tuple,
            _flame_only_predictions(tuple(stable_predictions)),
            iou_threshold=args.evaluation_iou,
        )

    output = {
        "schema_version": 1,
        "evaluation": "offline_recorded_fire_video",
        "source_url": args.source_url,
        "video": {
            "path": str(args.video.resolve()),
            "sha256": _sha256(args.video),
            "expected_frame_count": expected_frame_count,
            "decoded_frame_count": decoded_frames,
            "evaluated_frame_count": evaluated_frames,
            "evaluation_frame_stride": args.frame_stride,
            "fps": video_fps,
            "width": video_width,
            "height": video_height,
        },
        "annotations": (
            {
                "path": str(args.annotations.resolve()),
                "sha256": _sha256(args.annotations),
                "format": "FURG OpenCV 2.x XML Rect(x,y,width,height)",
                "semantic_labels": ["flame"],
                "source_frame_count": annotations.frame_count,
                "annotated_frame_count": annotations.annotated_frame_count,
                "annotated_box_count": annotations.annotated_box_count,
                "width": annotations.frame_width,
                "height": annotations.frame_height,
            }
            if annotations is not None
            else None
        ),
        "model": {
            "path": str(args.onnx_model.resolve()),
            "sha256": _sha256(args.onnx_model),
            "class_names": class_names,
            "providers": raw.provider_names,
        },
        "configuration": {
            "candidate_confidence": args.candidate_confidence,
            "flame_confidence": args.flame_confidence,
            "smoke_confidence": args.smoke_confidence,
            "minimum_bright_warm_fraction": args.minimum_bright_warm_fraction,
            "candidate_stability_frames": args.candidate_stability_frames,
            "temporal_iou": args.temporal_iou,
            "maximum_missed_frames": args.maximum_missed_frames,
            "maximum_center_distance": args.maximum_center_distance,
            "minimum_area_ratio": args.minimum_area_ratio,
            "evaluation_iou": args.evaluation_iou,
            "bright_neutral_light_veto": True,
            "tiled_detection": (
                {
                    "columns": tile_config.columns,
                    "rows": tile_config.rows,
                    "overlap_fraction": tile_config.overlap_fraction,
                    "scan_interval_frames": tile_config.scan_interval_frames,
                    "fusion_iou_threshold": tile_config.fusion_iou_threshold,
                    "tile_confidence_threshold": tile_config.tile_confidence_threshold,
                    "tile_labels": sorted(tile_config.tile_labels),
                    "maximum_tile_box_area": tile_config.maximum_tile_box_area,
                }
                if tile_config is not None
                else None
            ),
        },
        "stage_detection_counts": {
            "raw": dict(sorted(raw_counts.items())),
            "thresholded": dict(sorted(thresholded_counts.items())),
            "bright_neutral_vetoed": dict(sorted(vetoed_counts.items())),
            "stable": dict(sorted(stable_counts.items())),
        },
        "latency_ms": {
            "p50": _percentile(latencies_ms, 0.50),
            "p95": _percentile(latencies_ms, 0.95),
        },
        "flame_metrics": metrics,
        "stable_flame_tracking": continuity,
        "expected_label_presence": (
            {
                "expected_label": expected_label,
                "stages": {
                    stage: label_presence_document(
                        present_by_frame,
                        evaluated_frame_count=evaluated_frames,
                    )
                    for stage, present_by_frame in expected_presence.items()
                },
            }
            if expected_presence is not None
            else None
        ),
        "predictions_path": str(predictions_path.resolve()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


def _validate_args(args: argparse.Namespace) -> None:
    for path, name in ((args.video, "video"), (args.onnx_model, "onnx model")):
        if not path.is_file():
            raise ValueError(f"{name} does not exist: {path}")
    if args.annotations is not None and not args.annotations.is_file():
        raise ValueError(f"annotations do not exist: {args.annotations}")
    if args.input_width <= 0 or args.input_height <= 0:
        raise ValueError("input dimensions must be positive")
    for name in (
        "candidate_confidence",
        "flame_confidence",
        "smoke_confidence",
        "temporal_iou",
        "minimum_area_ratio",
        "evaluation_iou",
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be finite and in [0, 1]")
    if args.temporal_iou == 0.0 or args.evaluation_iou == 0.0:
        raise ValueError("IoU thresholds must be positive")
    if not math.isfinite(args.maximum_center_distance) or args.maximum_center_distance <= 0.0:
        raise ValueError("maximum_center_distance must be finite and positive")
    if args.candidate_stability_frames <= 0:
        raise ValueError("candidate_stability_frames must be positive")
    if args.maximum_missed_frames < 0:
        raise ValueError("maximum_missed_frames cannot be negative")
    if args.frame_stride <= 0:
        raise ValueError("frame_stride must be positive")
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("max_frames must be positive when set")
    # Materialize optional tile settings during argument validation so invalid
    # scan geometry fails before model/session initialization.
    tiled_detection_config_from_args(args)
    parse_class_names(args.class_names)
    _normalize_expected_label(args.expected_label)


def _normalize_expected_label(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("expected_label cannot be empty")
    if normalized not in FIRE_LABELS:
        raise ValueError(f"expected_label must be one of {sorted(FIRE_LABELS)}")
    return normalized


def _has_expected_label(detections: Sequence[Detection], expected_label: str) -> bool:
    accepted_labels = {"fire", "flame"} if expected_label in {"fire", "flame"} else {expected_label}
    return any(detection.label.strip().lower() in accepted_labels for detection in detections)


def _longest_true_run(values: Sequence[bool]) -> int:
    longest = 0
    current = 0
    for value in values:
        if value:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _required_positive_int(raw: str | None, name: str) -> int:
    value = _required_nonnegative_int(raw, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _required_nonnegative_int(raw: str | None, name: str) -> int:
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _parse_furg_rectangle(
    raw: str,
    *,
    frame_width: int,
    frame_height: int,
    path: Path,
    frame_number: int,
) -> BoundingBox:
    values = raw.split()
    if len(values) != 4:
        raise ValueError(f"invalid FURG rectangle at {path}:{frame_number}")
    try:
        x, y, width, height = (float(value) for value in values)
    except ValueError as exc:
        raise ValueError(f"invalid FURG rectangle at {path}:{frame_number}") from exc
    if (
        not all(math.isfinite(value) for value in (x, y, width, height))
        or width <= 0
        or height <= 0
    ):
        raise ValueError(f"invalid FURG rectangle at {path}:{frame_number}")
    x1 = min(1.0, max(0.0, x / frame_width))
    y1 = min(1.0, max(0.0, y / frame_height))
    x2 = min(1.0, max(0.0, (x + width) / frame_width))
    y2 = min(1.0, max(0.0, (y + height) / frame_height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"FURG rectangle is outside the frame at {path}:{frame_number}")
    return BoundingBox(x1, y1, x2, y2)


def _prediction_frame(
    frame_id: str,
    detections: Iterable[Detection],
    latency_ms: float,
) -> PredictionFrame:
    return PredictionFrame(
        frame_id=frame_id,
        detections=tuple(
            LabeledBox(item.label, item.bbox, item.confidence)
            for item in detections
            if item.label == "flame"
        ),
        inference_latency_ms=latency_ms,
    )


def _flame_only_predictions(predictions: Sequence[PredictionFrame]) -> tuple[PredictionFrame, ...]:
    return tuple(
        PredictionFrame(
            frame_id=frame.frame_id,
            detections=tuple(item for item in frame.detections if item.label == "flame"),
            inference_latency_ms=frame.inference_latency_ms,
        )
        for frame in predictions
    )


def _flame_evaluation_document(
    truth: tuple[GroundTruthFrame, ...],
    predictions: tuple[PredictionFrame, ...],
    iou_threshold: float,
) -> dict[str, Any]:
    report = evaluate_detections(
        truth,
        predictions,
        iou_threshold=iou_threshold,
        confidence_threshold=0.0,
    )
    return evaluation_document(report)


def _has_flame_match(
    truth: Sequence[LabeledBox],
    detections: Sequence[LabeledBox],
    *,
    iou_threshold: float,
) -> bool:
    return any(
        predicted.label == "flame"
        and ground.label == "flame"
        and predicted.bbox.iou(ground.bbox) >= iou_threshold
        for ground in truth
        for predicted in detections
    )


def _contiguous_segments(indices: Sequence[int]) -> list[list[int]]:
    segments: list[list[int]] = []
    for index in indices:
        if not segments or index != segments[-1][-1] + 1:
            segments.append([index])
        else:
            segments[-1].append(index)
    return segments


def _maximum_unmatched_run(segment: Sequence[int], matched_indices: set[int]) -> int:
    maximum = 0
    current = 0
    for index in segment:
        if index in matched_indices:
            current = 0
        else:
            current += 1
            maximum = max(maximum, current)
    return maximum


def _frame_order_key(frame_id: str) -> tuple[int, str]:
    try:
        return (0, f"{int(frame_id):012d}")
    except ValueError:
        return (1, frame_id)


def _detections_document(detections: Sequence[Detection]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for item in detections:
        document: dict[str, Any] = {
            "label": item.label,
            "confidence": item.confidence,
            "bbox": _box_document(item.bbox),
        }
        if diagnostics := fire_rgb_diagnostics(item):
            document["diagnostics"] = diagnostics
        documents.append(document)
    return documents


def _box_document(box: BoundingBox) -> list[float]:
    return list(box.rounded())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_positive_or_zero(value: float) -> float:
    value = float(value)
    return value if math.isfinite(value) and value > 0.0 else 0.0


def _percentile(values: Sequence[float | int], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())

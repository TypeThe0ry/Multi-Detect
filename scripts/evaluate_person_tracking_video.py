#!/usr/bin/env python3
"""Evaluate the project person detector/ReID/target-pool chain on a public MOT video.

This is an offline recorded-video harness.  It consumes a locally downloaded MP4
and MOT-style ``gt.txt`` annotations, runs the same COCO person detector and
optional person ReID encoder used by the live path, writes frame-aligned target
IDs, and scores identity continuity with the project's tracking evaluator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from multidetect.appearance_reid import OnnxPersonReIdConfig, OnnxPersonReIdEncoder
from multidetect.cli import COCO80_CLASS_NAMES
from multidetect.domain import BoundingBox
from multidetect.tensorrt_session import TensorRtEmbeddingSession
from multidetect.tracking_evaluation import (
    GroundTruthVisibility,
    IdentityGroundTruthFrame,
    IdentityGroundTruthObject,
    IdentityPredictionFrame,
    JsonlIdentityPredictionWriter,
    PredictedTrack,
    evaluate_identity_tracking,
    tracking_evaluation_document,
)
from multidetect.unified_tracking import (
    AppearanceEmbedding,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
)
from multidetect.vision import (
    ClassConfidenceFilter,
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    SameLabelDetectionFusion,
)

PUBLIC_PERSON_TRACKING_SESSION_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "multidetect/public-person-tracking")
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the production-equivalent person tracking chain over a public "
            "MOT-style recorded video."
        )
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--sequence-info", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--model", type=Path, help="COCO detector ONNX or TensorRT engine")
    source.add_argument(
        "--observations-in",
        type=Path,
        help="reuse a prior observations JSONL cache without rerunning learned models",
    )
    parser.add_argument(
        "--observations-out",
        type=Path,
        help="write normalized person detections/ReID embeddings for fast parameter sweeps",
    )
    parser.add_argument("--source-url", default="", help="public video/dataset source URL")
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument(
        "--model-format",
        choices=("post_nms_nx6", "ultralytics_raw"),
        default="post_nms_nx6",
        help=(
            "detector tensor contract; ultralytics_raw runs host-side class-aware NMS "
            "for a 1x(4+classes)xN export/engine"
        ),
    )
    parser.add_argument("--person-confidence", type=float, default=0.25)
    parser.add_argument("--model-confidence", type=float, default=0.10)
    parser.add_argument("--model-iou-threshold", type=float, default=0.45)
    parser.add_argument("--maximum-detections", type=int, default=300)
    parser.add_argument("--person-reid-model", type=Path)
    parser.add_argument("--person-reid-engine", type=Path)
    parser.add_argument("--person-reid-maximum-batch-size", type=int, default=10)
    parser.add_argument("--minimum-visibility", type=float, default=0.20)
    parser.add_argument("--iou-threshold", type=float, default=0.50)
    parser.add_argument("--confidence-threshold", type=float, default=0.10)
    parser.add_argument("--minimum-confirmed-hits", type=int, default=3)
    parser.add_argument("--maximum-center-distance", type=float, default=0.16)
    parser.add_argument("--maximum-appearance-distance", type=float, default=0.38)
    parser.add_argument("--strict-reid-distance", type=float, default=0.22)
    parser.add_argument(
        "--person-maximum-appearance-distance",
        type=float,
        help="person-only ReID association override, matching the live target-pool option",
    )
    parser.add_argument(
        "--person-strict-reid-distance",
        type=float,
        help="person-only strict ReID recovery override, matching the live target-pool option",
    )
    parser.add_argument("--occluded-after-seconds", type=float, default=0.35)
    parser.add_argument("--reacquisition-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--kalman-gate-sigma", type=float, default=4.0)
    parser.add_argument("--maximum-frames", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    video = _probe_video(args.video)
    sequence_info = load_sequence_info(args.sequence_info)
    _validate_sequence_matches_video(sequence_info, video)
    maximum_frames = min(video["frame_count"], args.maximum_frames or video["frame_count"])
    ground_truth, ground_truth_stats = load_mot_ground_truth(
        args.ground_truth,
        frame_count=maximum_frames,
        width=video["width"],
        height=video["height"],
        fps=video["fps"],
        minimum_visibility=args.minimum_visibility,
    )
    ground_truth_path = args.out.with_suffix(".ground_truth.jsonl")
    _write_ground_truth_jsonl(ground_truth_path, ground_truth)

    detector_latency_ms: list[float] = []
    reid_latency_ms: list[float] = []
    raw_person_detection_count = 0
    providers: dict[str, list[str]] = {}
    if args.observations_in is not None:
        observations_path = args.observations_in
        observations = load_observations_cache(args.observations_in)
        observations = observations[:maximum_frames]
        observation_source = "cache"
    else:
        observations_path = args.observations_out or args.out.with_suffix(".observations.jsonl")
        (
            observations,
            detector_latency_ms,
            reid_latency_ms,
            raw_person_detection_count,
            providers,
        ) = infer_observations(
            video=args.video,
            model=args.model,
            model_format=args.model_format,
            maximum_frames=maximum_frames,
            person_confidence=args.person_confidence,
            model_confidence=args.model_confidence,
            model_iou_threshold=args.model_iou_threshold,
            maximum_detections=args.maximum_detections,
            providers=tuple(args.provider),
            person_reid_model=args.person_reid_model,
            person_reid_engine=args.person_reid_engine,
            person_reid_maximum_batch_size=args.person_reid_maximum_batch_size,
        )
        write_observations_cache(observations_path, observations)
        observation_source = "fresh_inference"
    _validate_observation_alignment(ground_truth, observations)

    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            maximum_tracks=64,
            minimum_confirmed_hits=args.minimum_confirmed_hits,
            maximum_center_distance=args.maximum_center_distance,
            maximum_appearance_distance=args.maximum_appearance_distance,
            strict_reid_distance=args.strict_reid_distance,
            person_maximum_appearance_distance=args.person_maximum_appearance_distance,
            person_strict_reid_distance=args.person_strict_reid_distance,
            occluded_after_s=args.occluded_after_seconds,
            reacquisition_timeout_s=args.reacquisition_timeout_seconds,
            kalman_gate_sigma=args.kalman_gate_sigma,
        )
    )
    prediction_path = args.out.with_suffix(".predictions.jsonl")
    predictions = run_target_pool(
        pool=pool,
        observations=observations,
        prediction_path=prediction_path,
    )
    report = evaluate_identity_tracking(
        ground_truth,
        predictions,
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.confidence_threshold,
        maximum_timestamp_delta_s=0.01,
        maximum_occlusion_recovery_s=args.reacquisition_timeout_seconds,
    )
    document = {
        "event": "public_person_tracking_evaluated",
        "source_url": args.source_url,
        "video": {
            "path": str(args.video.resolve()),
            "sha256": _sha256(args.video),
            **video,
        },
        "ground_truth": {
            "path": str(args.ground_truth.resolve()),
            "sha256": _sha256(args.ground_truth),
            "sequence_info_path": str(args.sequence_info.resolve()),
            "sequence_info_sha256": _sha256(args.sequence_info),
            "sequence_info": sequence_info,
            **ground_truth_stats,
            "normalized_jsonl_path": str(ground_truth_path.resolve()),
            "normalized_jsonl_sha256": _sha256(ground_truth_path),
        },
        "runtime": {
            "observation_source": observation_source,
            "model_path": str(args.model.resolve()) if args.model is not None else None,
            "model_sha256": _sha256(args.model) if args.model is not None else None,
            "model_format": args.model_format,
            "person_confidence": args.person_confidence,
            "model_confidence": args.model_confidence,
            "model_iou_threshold": args.model_iou_threshold,
            "person_reid_model": (
                str(args.person_reid_model.resolve())
                if args.person_reid_model is not None
                else None
            ),
            "person_reid_engine": (
                str(args.person_reid_engine.resolve())
                if args.person_reid_engine is not None
                else None
            ),
            "target_pool": {
                "minimum_confirmed_hits": args.minimum_confirmed_hits,
                "maximum_center_distance": args.maximum_center_distance,
                "maximum_appearance_distance": args.maximum_appearance_distance,
                "strict_reid_distance": args.strict_reid_distance,
                "person_maximum_appearance_distance": args.person_maximum_appearance_distance,
                "person_strict_reid_distance": args.person_strict_reid_distance,
                "occluded_after_seconds": args.occluded_after_seconds,
                "reacquisition_timeout_seconds": args.reacquisition_timeout_seconds,
                "kalman_gate_sigma": args.kalman_gate_sigma,
            },
            "providers": providers,
        },
        "observations": {
            "path": str(observations_path.resolve()),
            "sha256": _sha256(observations_path),
            "frame_count": len(observations),
            "person_detection_count": raw_person_detection_count,
        },
        "predictions": {
            "path": str(prediction_path.resolve()),
            "sha256": _sha256(prediction_path),
        },
        "latency_ms": {
            "detector": _latency_summary(detector_latency_ms),
            "person_reid": _latency_summary(reid_latency_ms),
        },
        "metrics": tracking_evaluation_document(report),
        "control_interfaces_used": [],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


def load_mot_ground_truth(
    path: Path,
    *,
    frame_count: int,
    width: int,
    height: int,
    fps: float,
    minimum_visibility: float,
) -> tuple[tuple[IdentityGroundTruthFrame, ...], dict[str, int | float]]:
    """Normalize the public MOT rows into the project's explicit-identity format."""

    rows: dict[tuple[int, str], list[tuple[float, float, float, float, float]]] = defaultdict(list)
    raw_row_count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for line_number, row in enumerate(csv.reader(stream), start=1):
            if not row:
                continue
            if len(row) < 9:
                raise ValueError(f"ground-truth row {line_number} has fewer than nine columns")
            frame_number = _positive_int(row[0], f"ground-truth frame at row {line_number}")
            if frame_number > frame_count:
                continue
            identity = row[1].strip()
            if not identity:
                raise ValueError(f"ground-truth identity is empty at row {line_number}")
            x, y, box_width, box_height, visibility = (
                _finite_float(value, f"ground-truth row {line_number}")
                for value in (row[2], row[3], row[4], row[5], row[8])
            )
            if box_width <= 0.0 or box_height <= 0.0:
                continue
            rows[(frame_number, identity)].append((visibility, x, y, box_width, box_height))
            raw_row_count += 1

    per_frame: dict[int, dict[str, IdentityGroundTruthObject]] = defaultdict(dict)
    spans: dict[str, tuple[int, int]] = {}
    deduplicated_rows = 0
    visible_count = 0
    occluded_count = 0
    for (frame_number, identity), candidates in rows.items():
        deduplicated_rows += max(0, len(candidates) - 1)
        visibility, x, y, box_width, box_height = max(
            candidates,
            key=lambda item: (item[0], item[3] * item[4]),
        )
        identity_id = f"person-{identity}"
        if visibility >= minimum_visibility:
            object_value = IdentityGroundTruthObject(
                identity_id=identity_id,
                label="person",
                visibility=GroundTruthVisibility.VISIBLE,
                bbox=_normalized_box(x, y, box_width, box_height, width=width, height=height),
            )
            visible_count += 1
        else:
            object_value = IdentityGroundTruthObject(
                identity_id=identity_id,
                label="person",
                visibility=GroundTruthVisibility.OCCLUDED,
                bbox=None,
            )
            occluded_count += 1
        per_frame[frame_number][identity_id] = object_value
        start, end = spans.get(identity_id, (frame_number, frame_number))
        spans[identity_id] = min(start, frame_number), max(end, frame_number)

    out_of_frame_count = 0
    for identity_id, (start, end) in spans.items():
        for frame_number in range(start, end + 1):
            if identity_id not in per_frame[frame_number]:
                per_frame[frame_number][identity_id] = IdentityGroundTruthObject(
                    identity_id=identity_id,
                    label="person",
                    visibility=GroundTruthVisibility.OUT_OF_FRAME,
                    bbox=None,
                )
                out_of_frame_count += 1
    frames = tuple(
        IdentityGroundTruthFrame(
            frame_id=_frame_id(frame_number),
            captured_at_s=(frame_number - 1) / fps,
            objects=tuple(
                per_frame[frame_number][identity_id]
                for identity_id in sorted(per_frame[frame_number])
            ),
        )
        for frame_number in range(1, frame_count + 1)
    )
    return frames, {
        "raw_row_count": raw_row_count,
        "deduplicated_row_count": deduplicated_rows,
        "visible_object_count": visible_count,
        "occluded_object_count": occluded_count,
        "out_of_frame_object_count": out_of_frame_count,
        "identity_count": len(spans),
        "minimum_visibility": minimum_visibility,
    }


def infer_observations(
    *,
    video: Path,
    model: Path | None,
    model_format: str,
    maximum_frames: int,
    person_confidence: float,
    model_confidence: float,
    model_iou_threshold: float,
    maximum_detections: int,
    providers: tuple[str, ...],
    person_reid_model: Path | None,
    person_reid_engine: Path | None,
    person_reid_maximum_batch_size: int,
) -> tuple[
    tuple[tuple[str, float, tuple[TargetObservation, ...]], ...],
    list[float],
    list[float],
    int,
    dict[str, list[str]],
]:
    if model is None:
        raise ValueError("model is required for fresh inference")
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise RuntimeError("OpenCV is required for public video evaluation") from exc
    if model_format == "ultralytics_raw":
        base = OnnxRawYoloDetector(
            OnnxRawYoloConfig(
                model_path=model,
                class_names=COCO80_CLASS_NAMES,
                input_width=640,
                input_height=640,
                confidence_threshold=model_confidence,
                iou_threshold=model_iou_threshold,
                maximum_detections=maximum_detections,
                providers=providers,
            )
        )
        fused_detector = base
    elif model_format == "post_nms_nx6":
        base = OnnxNx6Detector(
            OnnxNx6Config(
                model_path=model,
                class_names=COCO80_CLASS_NAMES,
                input_width=640,
                input_height=640,
                confidence_threshold=model_confidence,
                output_coordinates="letterbox_xyxy_px",
                providers=providers,
            )
        )
        fused_detector = SameLabelDetectionFusion(
            base,
            iou_threshold=model_iou_threshold,
            maximum_detections=maximum_detections,
        )
    else:  # argparse constrains CLI input; keep the function safe for API callers.
        raise ValueError(f"unsupported person detector format: {model_format}")
    base.warmup(iterations=1)
    detector = ClassConfidenceFilter(
        fused_detector,
        {"person": person_confidence},
        default_threshold=1.0,
    )
    reid_session: TensorRtEmbeddingSession | None = None
    reid_encoder: OnnxPersonReIdEncoder | None = None
    if person_reid_model is not None:
        if person_reid_engine is not None:
            reid_session = TensorRtEmbeddingSession(
                person_reid_engine,
                maximum_batch_size=person_reid_maximum_batch_size,
            )
        reid_encoder = OnnxPersonReIdEncoder(
            OnnxPersonReIdConfig(
                model_path=person_reid_model,
                maximum_batch_size=person_reid_maximum_batch_size,
                providers=providers,
            ),
            session=reid_session,
        )
        reid_encoder.warmup(batch_size=1)

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        if reid_session is not None:
            reid_session.close()
        raise ValueError(f"OpenCV cannot open recorded video: {video}")
    detector_latencies: list[float] = []
    reid_latencies: list[float] = []
    observations: list[tuple[str, float, tuple[TargetObservation, ...]]] = []
    raw_person_detection_count = 0
    try:
        for frame_number in range(1, maximum_frames + 1):
            ok, image = capture.read()
            if not ok or image is None:
                raise RuntimeError("recorded video ended before its declared frame count")
            started = time.perf_counter()
            detections = tuple(
                item for item in detector.detect(image) if item.label.strip().lower() == "person"
            )
            detector_latencies.append((time.perf_counter() - started) * 1_000.0)
            raw_person_detection_count += len(detections)
            if reid_encoder is None:
                frame_observations = tuple(
                    TargetObservation.from_detection(item) for item in detections
                )
            else:
                started = time.perf_counter()
                frame_observations = reid_encoder.encode_detections(image, detections)
                reid_latencies.append((time.perf_counter() - started) * 1_000.0)
            captured_at_s = (frame_number - 1) / _video_fps(capture)
            observations.append((_frame_id(frame_number), captured_at_s, frame_observations))
    finally:
        capture.release()
        if reid_session is not None:
            reid_session.close()
    provider_document = {"detector": list(base.provider_names)}
    if reid_encoder is not None:
        provider_document["person_reid"] = list(reid_encoder.provider_names)
    return (
        tuple(observations),
        detector_latencies,
        reid_latencies,
        raw_person_detection_count,
        provider_document,
    )


def run_target_pool(
    *,
    pool: UnifiedTargetPool,
    observations: tuple[tuple[str, float, tuple[TargetObservation, ...]], ...],
    prediction_path: Path,
) -> tuple[IdentityPredictionFrame, ...]:
    frames: list[IdentityPredictionFrame] = []
    writer = JsonlIdentityPredictionWriter(
        prediction_path,
        session_id=PUBLIC_PERSON_TRACKING_SESSION_ID,
    )
    try:
        for frame_id, captured_at_s, frame_observations in observations:
            update = pool.update(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                observations=frame_observations,
            )
            writer.append(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                tracks=update.tracks,
            )
            frames.append(
                IdentityPredictionFrame(
                    frame_id=frame_id,
                    captured_at_s=captured_at_s,
                    session_id=PUBLIC_PERSON_TRACKING_SESSION_ID,
                    tracks=tuple(
                        PredictedTrack(
                            track_id=track.track_id,
                            label=track.label,
                            bbox=(
                                track.bbox
                                if track.state.value
                                in {"detected", "locked", "tracking", "recovered"}
                                else track.predicted_bbox
                            ),
                            state=track.state.value,
                            confidence=track.confidence,
                        )
                        for track in update.tracks
                    ),
                )
            )
    finally:
        writer.close()
    return tuple(frames)


def write_observations_cache(
    path: Path,
    observations: tuple[tuple[str, float, tuple[TargetObservation, ...]], ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for frame_id, captured_at_s, frame_observations in observations:
            document = {
                "frame_id": frame_id,
                "captured_at_s": captured_at_s,
                "observations": [
                    {
                        "label": observation.label,
                        "confidence": observation.confidence,
                        "bbox": observation.bbox.rounded(),
                        "appearance": (
                            list(observation.appearance.values)
                            if observation.appearance is not None
                            else None
                        ),
                        "appearance_reliable": observation.appearance_reliable,
                        "source": observation.source,
                    }
                    for observation in frame_observations
                ],
            }
            stream.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def load_observations_cache(
    path: Path,
) -> tuple[tuple[str, float, tuple[TargetObservation, ...]], ...]:
    frames: list[tuple[str, float, tuple[TargetObservation, ...]]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"observations cache line {line_number} must be an object")
        frame_id = _required_text(raw.get("frame_id"), "observations cache frame_id")
        captured_at_s = _finite_nonnegative(raw.get("captured_at_s"), "observations cache time")
        raw_observations = raw.get("observations")
        if not isinstance(raw_observations, list):
            raise ValueError("observations cache observations must be an array")
        decoded: list[TargetObservation] = []
        for raw_observation in raw_observations:
            if not isinstance(raw_observation, dict):
                raise ValueError("observations cache observation must be an object")
            appearance_raw = raw_observation.get("appearance")
            appearance = (
                AppearanceEmbedding(tuple(float(value) for value in appearance_raw))
                if isinstance(appearance_raw, list)
                else None
            )
            decoded.append(
                TargetObservation(
                    label=_required_text(raw_observation.get("label"), "observation label"),
                    confidence=_probability(
                        raw_observation.get("confidence"),
                        "observation confidence",
                    ),
                    bbox=_bbox_from_list(raw_observation.get("bbox")),
                    appearance=appearance,
                    appearance_reliable=bool(raw_observation.get("appearance_reliable", False)),
                    source=str(raw_observation.get("source", "detector")),
                )
            )
        frames.append((frame_id, captured_at_s, tuple(decoded)))
    if not frames:
        raise ValueError("observations cache is empty")
    return tuple(frames)


def _write_ground_truth_jsonl(path: Path, frames: Iterable[IdentityGroundTruthFrame]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for frame in frames:
            document = {
                "frame_id": frame.frame_id,
                "captured_at_s": frame.captured_at_s,
                "objects": [
                    {
                        "identity_id": item.identity_id,
                        "label": item.label,
                        "visibility": item.visibility.value,
                        "bbox": item.bbox.rounded() if item.bbox is not None else None,
                    }
                    for item in frame.objects
                ],
            }
            stream.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def _probe_video(path: Path) -> dict[str, int | float]:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise RuntimeError("OpenCV is required for public video evaluation") from exc
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"OpenCV cannot open recorded video: {path}")
    try:
        fps = _video_fps(capture)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    if frame_count <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("recorded video metadata is incomplete")
    return {"frame_count": frame_count, "fps": fps, "width": width, "height": height}


def load_sequence_info(path: Path) -> dict[str, int | float | str]:
    """Parse and validate the subset of MOT ``seqinfo.ini`` needed for scoring.

    The annotations are frame-numbered, so silently accepting a resized video or
    a file with a different cadence would turn an identity score into a misleading
    result.  Store the declared sequence contract in the report and verify it
    against OpenCV before any model work begins.
    """

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if "=" not in line:
            raise ValueError(f"sequence-info line {line_number} must use key=value syntax")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            raise ValueError(f"sequence-info line {line_number} has an empty key or value")
        if key in values:
            raise ValueError(f"sequence-info key is duplicated: {key}")
        values[key] = value

    required = ("seqLength", "frameRate", "imWidth", "imHeight")
    missing = [key for key in required if key not in values]
    if missing:
        raise ValueError(f"sequence-info is missing required keys: {', '.join(missing)}")
    sequence = {
        "seq_length": _positive_int(values["seqLength"], "sequence-info seqLength"),
        "fps": _finite_float(values["frameRate"], "sequence-info frameRate"),
        "width": _positive_int(values["imWidth"], "sequence-info imWidth"),
        "height": _positive_int(values["imHeight"], "sequence-info imHeight"),
    }
    if sequence["fps"] <= 0.0:
        raise ValueError("sequence-info frameRate must be positive")
    if "name" in values:
        sequence["name"] = values["name"]
    return sequence


def _validate_sequence_matches_video(
    sequence_info: dict[str, int | float | str],
    video: dict[str, int | float],
) -> None:
    """Fail fast when the video cannot be aligned to the supplied MOT labels."""

    expected = {
        "frame_count": int(sequence_info["seq_length"]),
        "width": int(sequence_info["width"]),
        "height": int(sequence_info["height"]),
    }
    actual = {
        "frame_count": int(video["frame_count"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
    }
    mismatches = [
        f"{name}: sequence={expected[name]}, video={actual[name]}"
        for name in expected
        if expected[name] != actual[name]
    ]
    expected_fps = float(sequence_info["fps"])
    actual_fps = float(video["fps"])
    if not math.isclose(actual_fps, expected_fps, rel_tol=0.001, abs_tol=0.01):
        mismatches.append(f"fps: sequence={expected_fps}, video={actual_fps}")
    if mismatches:
        raise ValueError("sequence-info does not match recorded video: " + "; ".join(mismatches))


def _video_fps(capture: Any) -> float:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise RuntimeError("OpenCV is required for public video evaluation") from exc
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    if not math.isfinite(fps) or fps <= 0.0:
        raise RuntimeError("recorded video is missing a positive FPS value")
    return fps


def _normalized_box(
    x: float,
    y: float,
    box_width: float,
    box_height: float,
    *,
    width: int,
    height: int,
) -> BoundingBox:
    x1 = min(1.0, max(0.0, x / width))
    y1 = min(1.0, max(0.0, y / height))
    x2 = min(1.0, max(0.0, (x + box_width) / width))
    y2 = min(1.0, max(0.0, (y + box_height) / height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("ground-truth bounding box is outside the recorded video")
    return BoundingBox(x1, y1, x2, y2)


def _validate_observation_alignment(
    ground_truth: tuple[IdentityGroundTruthFrame, ...],
    observations: tuple[tuple[str, float, tuple[TargetObservation, ...]], ...],
) -> None:
    if len(ground_truth) != len(observations):
        raise ValueError("observations cache frame count does not match ground truth")
    for ground_truth_frame, (frame_id, captured_at_s, _items) in zip(
        ground_truth,
        observations,
        strict=True,
    ):
        if frame_id != ground_truth_frame.frame_id or not math.isclose(
            captured_at_s,
            ground_truth_frame.captured_at_s,
            abs_tol=1e-6,
        ):
            raise ValueError("observations cache is not aligned with public ground truth")


def _latency_summary(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": round(statistics.median(ordered), 6),
        "p95": round(ordered[round(0.95 * (len(ordered) - 1))], 6),
    }


def _validate_args(args: argparse.Namespace) -> None:
    for path in (args.video, args.ground_truth, args.sequence_info):
        if not path.is_file():
            raise FileNotFoundError(f"required recorded-video input does not exist: {path}")
    if args.model is not None and not args.model.is_file():
        raise FileNotFoundError(f"detector model does not exist: {args.model}")
    if args.observations_in is not None and not args.observations_in.is_file():
        raise FileNotFoundError(f"observations cache does not exist: {args.observations_in}")
    for path in (args.person_reid_model, args.person_reid_engine):
        if path is not None and not path.is_file():
            raise FileNotFoundError(f"person ReID artifact does not exist: {path}")
    if args.person_reid_engine is not None and args.person_reid_model is None:
        raise ValueError("person-reid-engine requires person-reid-model")
    if args.observations_in is not None and (
        args.person_reid_model is not None or args.person_reid_engine is not None
    ):
        raise ValueError("observations-in already contains any requested person ReID embeddings")
    if args.observations_in is not None and args.observations_out is not None:
        raise ValueError("observations-out cannot be combined with observations-in")
    if args.maximum_frames is not None and args.maximum_frames <= 0:
        raise ValueError("maximum-frames must be positive")
    if not 1 <= args.maximum_detections <= 10_000:
        raise ValueError("maximum-detections must be between 1 and 10000")
    if not 1 <= args.person_reid_maximum_batch_size <= 10:
        raise ValueError("person-reid-maximum-batch-size must be between 1 and 10")
    if args.minimum_confirmed_hits <= 0:
        raise ValueError("minimum-confirmed-hits must be positive")
    for name in (
        "person_confidence",
        "model_confidence",
        "model_iou_threshold",
        "minimum_visibility",
        "iou_threshold",
        "confidence_threshold",
        "maximum_center_distance",
        "maximum_appearance_distance",
        "strict_reid_distance",
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1]")
    if args.model_iou_threshold <= 0.0 or args.iou_threshold <= 0.0:
        raise ValueError("model-iou-threshold and iou-threshold must be positive")
    if args.strict_reid_distance > args.maximum_appearance_distance:
        raise ValueError("strict-reid-distance cannot exceed maximum-appearance-distance")
    for name in (
        "person_maximum_appearance_distance",
        "person_strict_reid_distance",
    ):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or not 0.0 < value <= 1.0):
            raise ValueError(f"{name} must be in (0, 1] when provided")
    effective_person_appearance_gate = (
        args.person_maximum_appearance_distance
        if args.person_maximum_appearance_distance is not None
        else args.maximum_appearance_distance
    )
    effective_person_strict_reid = (
        args.person_strict_reid_distance
        if args.person_strict_reid_distance is not None
        else args.strict_reid_distance
    )
    if effective_person_strict_reid > effective_person_appearance_gate:
        raise ValueError("person strict-reid-distance cannot exceed person appearance distance")
    if not 0.0 < args.occluded_after_seconds < args.reacquisition_timeout_seconds:
        raise ValueError("person tracking timeouts must satisfy 0 < occluded < reacquisition")
    if args.kalman_gate_sigma <= 0.0:
        raise ValueError("kalman-gate-sigma must be positive")


def _frame_id(frame_number: int) -> str:
    return f"frame-{frame_number:06d}"


def _bbox_from_list(value: object) -> BoundingBox:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("observation bbox must be a four-number list")
    return BoundingBox(*(float(item) for item in value))


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _finite_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


def _probability(value: object, name: str) -> float:
    parsed = _finite_nonnegative(value, name)
    if parsed > 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return parsed


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

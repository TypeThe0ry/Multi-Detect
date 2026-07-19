from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from multidetect.cli import COCO80_CLASS_NAMES
from multidetect.domain import BoundingBox
from multidetect.evaluation import (
    GroundTruthFrame,
    LabeledBox,
    PredictionFrame,
    evaluate_detections,
    evaluation_document,
)
from multidetect.vision import (
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    TiledDetectionConfig,
    TiledDetectionFusion,
)

if TYPE_CHECKING:
    pass


IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
DEFAULT_PRIORITY_LABELS = (
    "person",
    "airplane",
    "bicycle",
    "car",
    "motorcycle",
    "bus",
    "truck",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a raw COCO YOLO ONNX full-frame pass with overlapping tiled "
            "small-object discovery on a YOLO-format labeled image directory."
        )
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--input-width", type=int, default=640)
    parser.add_argument("--input-height", type=int, default=640)
    parser.add_argument(
        "--model-class-names",
        default=",".join(COCO80_CLASS_NAMES),
        help="raw model output class-index names; repeated names may merge source classes",
    )
    parser.add_argument("--candidate-confidence", type=float, default=0.10)
    parser.add_argument("--evaluation-confidence", type=float, default=0.25)
    parser.add_argument("--iou-threshold", type=float, default=0.50)
    parser.add_argument("--nms-iou-threshold", type=float, default=0.45)
    parser.add_argument("--tile-columns", type=int, default=2)
    parser.add_argument("--tile-rows", type=int, default=1)
    parser.add_argument("--tile-overlap", type=float, default=0.15)
    parser.add_argument("--tile-fusion-iou-threshold", type=float, default=0.30)
    parser.add_argument("--tile-confidence-threshold", type=float, default=0.40)
    parser.add_argument(
        "--tile-label-confidence-thresholds",
        default="airplane=0.82",
        help="comma-separated class=confidence tiled-detection overrides",
    )
    parser.add_argument("--tile-maximum-box-area", type=float, default=0.04)
    parser.add_argument("--tile-labels", default=",".join(DEFAULT_PRIORITY_LABELS))
    parser.add_argument("--priority-labels", default=",".join(DEFAULT_PRIORITY_LABELS))
    parser.add_argument(
        "--ground-truth-class-names",
        default=",".join(COCO80_CLASS_NAMES),
        help="YOLO label-index names; repeated names may merge source classes",
    )
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--max-images", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.model.is_file():
        raise FileNotFoundError(f"model does not exist: {args.model}")
    if not args.images.is_dir() or not args.labels.is_dir():
        raise FileNotFoundError("images and labels must both be directories")
    image_paths = sorted(
        path for path in args.images.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    if args.max_images is not None:
        if args.max_images <= 0:
            raise ValueError("max-images must be positive")
        image_paths = image_paths[: args.max_images]
    if not image_paths:
        raise ValueError("image directory contains no supported images")

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for common-detector evaluation") from exc

    ground_truth_class_names = tuple(
        label.strip().lower()
        for label in args.ground_truth_class_names.split(",")
        if label.strip()
    )
    if not ground_truth_class_names:
        raise ValueError("ground-truth-class-names must contain at least one label")
    model_class_names = tuple(
        label.strip().lower()
        for label in args.model_class_names.split(",")
        if label.strip()
    )
    if not model_class_names:
        raise ValueError("model-class-names must contain at least one label")
    base = OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=args.model,
            class_names=model_class_names,
            input_width=args.input_width,
            input_height=args.input_height,
            confidence_threshold=args.candidate_confidence,
            iou_threshold=args.nms_iou_threshold,
            providers=tuple(args.provider),
        )
    )
    tile_labels = frozenset(
        label.strip().lower() for label in args.tile_labels.split(",") if label.strip()
    )
    tile_confidence_by_label = _parse_label_confidence_thresholds(
        args.tile_label_confidence_thresholds
    )
    tiled = TiledDetectionFusion(
        base,
        TiledDetectionConfig(
            columns=args.tile_columns,
            rows=args.tile_rows,
            overlap_fraction=args.tile_overlap,
            scan_interval_frames=1,
            fusion_iou_threshold=args.tile_fusion_iou_threshold,
            tile_confidence_threshold=args.tile_confidence_threshold,
            tile_confidence_by_label=tile_confidence_by_label,
            tile_labels=tile_labels,
            maximum_tile_box_area=args.tile_maximum_box_area,
        ),
    )
    base.warmup(iterations=2)
    truth: list[GroundTruthFrame] = []
    full_predictions: list[PredictionFrame] = []
    tiled_predictions: list[PredictionFrame] = []
    for image_path in image_paths:
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"OpenCV did not decode image: {image_path}")
        frame_id = image_path.name
        truth.append(
            _load_yolo_truth(
                frame_id,
                args.labels / f"{image_path.stem}.txt",
                ground_truth_class_names,
            )
        )
        full_predictions.append(_predict(base, image, frame_id))
        tiled_predictions.append(_predict(tiled, image, frame_id))

    priority_labels = frozenset(
        label.strip().lower() for label in args.priority_labels.split(",") if label.strip()
    )
    if not priority_labels:
        raise ValueError("priority-labels must contain at least one label")
    full_report = evaluate_detections(
        tuple(truth),
        tuple(full_predictions),
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.evaluation_confidence,
    )
    tiled_report = evaluate_detections(
        tuple(truth),
        tuple(tiled_predictions),
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.evaluation_confidence,
    )
    priority_truth = _filter_truth(truth, priority_labels)
    priority_full = _filter_predictions(full_predictions, priority_labels)
    priority_tiled = _filter_predictions(tiled_predictions, priority_labels)
    priority_full_report = evaluate_detections(
        priority_truth,
        priority_full,
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.evaluation_confidence,
    )
    priority_tiled_report = evaluate_detections(
        priority_truth,
        priority_tiled,
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.evaluation_confidence,
    )
    document = {
        "event": "common_detector_tiling_evaluated",
        "model": str(args.model.resolve()),
        "model_sha256": _sha256(args.model),
        "image_count": len(image_paths),
        "active_providers": list(base.provider_names),
        "candidate_confidence": args.candidate_confidence,
        "evaluation_confidence": args.evaluation_confidence,
        "iou_threshold": args.iou_threshold,
        "priority_labels": sorted(priority_labels),
        "ground_truth_class_names": list(ground_truth_class_names),
        "model_class_names": list(model_class_names),
        "tiled_config": {
            "columns": args.tile_columns,
            "rows": args.tile_rows,
            "overlap_fraction": args.tile_overlap,
            "fusion_iou_threshold": args.tile_fusion_iou_threshold,
            "tile_confidence_threshold": args.tile_confidence_threshold,
            "tile_confidence_by_label": tile_confidence_by_label,
            "tile_labels": sorted(tile_labels),
            "maximum_tile_box_area": args.tile_maximum_box_area,
        },
        "all_classes": {
            "full_frame": evaluation_document(full_report),
            "tiled": evaluation_document(tiled_report),
        },
        "priority_classes": {
            "full_frame": evaluation_document(priority_full_report),
            "tiled": evaluation_document(priority_tiled_report),
        },
        "flight_control_enabled": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


def _predict(detector: Any, image: Any, frame_id: str) -> PredictionFrame:
    started = time.perf_counter()
    detections = detector.detect(image)
    latency_ms = (time.perf_counter() - started) * 1_000.0
    return PredictionFrame(
        frame_id=frame_id,
        detections=tuple(
            LabeledBox(item.label, item.bbox, item.confidence) for item in detections
        ),
        inference_latency_ms=latency_ms,
    )


def _load_yolo_truth(
    frame_id: str, path: Path, class_names: tuple[str, ...]
) -> GroundTruthFrame:
    if not path.is_file():
        return GroundTruthFrame(frame_id, ())
    objects: list[LabeledBox] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        fields = raw.split()
        if not fields:
            continue
        if len(fields) != 5:
            raise ValueError(f"invalid YOLO label at {path}:{line_number}")
        class_id = int(fields[0])
        if not 0 <= class_id < len(class_names):
            raise ValueError(f"class id is out of range at {path}:{line_number}")
        center_x, center_y, width, height = (float(value) for value in fields[1:])
        objects.append(
            LabeledBox(
                class_names[class_id],
                BoundingBox(
                    max(0.0, center_x - width / 2.0),
                    max(0.0, center_y - height / 2.0),
                    min(1.0, center_x + width / 2.0),
                    min(1.0, center_y + height / 2.0),
                ),
            )
        )
    return GroundTruthFrame(frame_id, tuple(objects))


def _filter_truth(
    frames: list[GroundTruthFrame], labels: frozenset[str]
) -> tuple[GroundTruthFrame, ...]:
    return tuple(
        GroundTruthFrame(
            frame.frame_id,
            tuple(item for item in frame.objects if item.label in labels),
        )
        for frame in frames
    )


def _filter_predictions(
    frames: list[PredictionFrame], labels: frozenset[str]
) -> tuple[PredictionFrame, ...]:
    return tuple(
        PredictionFrame(
            frame.frame_id,
            tuple(item for item in frame.detections if item.label in labels),
            frame.inference_latency_ms,
        )
        for frame in frames
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_label_confidence_thresholds(raw: str) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("tile confidence entries must use class=confidence")
        raw_label, raw_value = (value.strip() for value in item.split("=", 1))
        label = raw_label.lower()
        if not label:
            raise ValueError("tile confidence labels cannot be empty")
        threshold = float(raw_value)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("tile confidence values must be in [0, 1]")
        thresholds[label] = threshold
    return thresholds


if __name__ == "__main__":
    raise SystemExit(main())

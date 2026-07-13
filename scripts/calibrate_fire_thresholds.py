from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from multidetect.calibration import calibrate_class_thresholds, evaluate_class_thresholds
from multidetect.domain import BoundingBox
from multidetect.evaluation import (
    GroundTruthFrame,
    LabeledBox,
    PredictionFrame,
    evaluation_document,
)
from multidetect.vision import OnnxNx6Config, OnnxNx6Detector

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate per-class fire/smoke thresholds on a labeled YOLO validation split."
    )
    parser.add_argument("--onnx-model", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--predictions-out", type=Path)
    parser.add_argument("--class-names", default="flame,smoke")
    parser.add_argument("--thresholds", default="0.10:0.90:0.02")
    parser.add_argument("--current-thresholds", default="flame=0.72,smoke=0.60")
    parser.add_argument("--minimum-recall", type=float, default=0.70)
    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--max-images", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    class_names = tuple(
        item.strip().lower() for item in args.class_names.split(",") if item.strip()
    )
    if not class_names:
        raise ValueError("class names cannot be empty")
    thresholds = _parse_threshold_grid(args.thresholds)
    current_thresholds = _parse_class_thresholds(args.current_thresholds, class_names)
    image_paths = sorted(
        path for path in args.images.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    if args.max_images is not None:
        if args.max_images <= 0:
            raise ValueError("max-images must be positive")
        image_paths = image_paths[: args.max_images]
    if not image_paths:
        raise ValueError("validation image directory is empty")

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for threshold calibration") from exc

    detector = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=args.onnx_model,
            class_names=class_names,
            confidence_threshold=min(thresholds),
            output_coordinates="letterbox_xyxy_px",
            providers=tuple(args.provider),
        )
    )
    ground_truth: list[GroundTruthFrame] = []
    predictions: list[PredictionFrame] = []
    strata: dict[str, list[str]] = {
        "background": [],
        "flame_only": [],
        "smoke_only": [],
        "flame_and_smoke": [],
        "dark": [],
        "normal_light": [],
        "bright": [],
    }
    for image_path in image_paths:
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(f"cannot read validation image: {image_path}")
        truth = _load_yolo_truth(args.labels / f"{image_path.stem}.txt", class_names)
        frame_id = image_path.name
        started_s = time.perf_counter()
        detections = detector.detect(image)
        latency_ms = (time.perf_counter() - started_s) * 1_000.0
        ground_truth.append(GroundTruthFrame(frame_id=frame_id, objects=truth))
        predictions.append(
            PredictionFrame(
                frame_id=frame_id,
                detections=tuple(
                    LabeledBox(item.label, item.bbox, item.confidence) for item in detections
                ),
                inference_latency_ms=latency_ms,
            )
        )
        _assign_strata(strata, frame_id=frame_id, truth=truth, image=image)

    truth_tuple = tuple(ground_truth)
    prediction_tuple = tuple(predictions)
    calibration = calibrate_class_thresholds(
        truth_tuple,
        prediction_tuple,
        thresholds=thresholds,
        iou_threshold=args.iou_threshold,
        beta=args.beta,
        minimum_recall=args.minimum_recall,
    )
    current_report = evaluate_class_thresholds(
        truth_tuple,
        prediction_tuple,
        thresholds=current_thresholds,
        iou_threshold=args.iou_threshold,
    )
    stratum_reports = _evaluate_strata(
        truth_tuple,
        prediction_tuple,
        strata=strata,
        thresholds=current_thresholds,
        iou_threshold=args.iou_threshold,
    )
    document = {
        "model": str(args.onnx_model.resolve()),
        "images": str(args.images.resolve()),
        "labels": str(args.labels.resolve()),
        "providers": detector.provider_names,
        "image_count": len(image_paths),
        "class_names": class_names,
        "current_thresholds": current_thresholds,
        "current_metrics": evaluation_document(current_report),
        "strata_at_current_thresholds": stratum_reports,
        "calibration": calibration,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.predictions_out is not None:
        _write_predictions(args.predictions_out, prediction_tuple)
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


def _parse_threshold_grid(raw: str) -> tuple[float, ...]:
    if ":" not in raw:
        return tuple(float(value) for value in raw.split(",") if value.strip())
    parts = raw.split(":")
    if len(parts) != 3:
        raise ValueError("threshold range must be start:stop:step")
    start, stop, step = (float(value) for value in parts)
    if step <= 0 or stop < start:
        raise ValueError("threshold range requires stop >= start and a positive step")
    count = int(round((stop - start) / step))
    return tuple(round(start + index * step, 10) for index in range(count + 1))


def _parse_class_thresholds(raw: str, class_names: tuple[str, ...]) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in raw.split(","):
        label, separator, threshold = item.partition("=")
        if not separator:
            raise ValueError("current thresholds must use label=value")
        values[label.strip().lower()] = float(threshold)
    if set(values) != set(class_names):
        raise ValueError("current thresholds must define every class exactly once")
    return values


def _load_yolo_truth(path: Path, class_names: tuple[str, ...]) -> tuple[LabeledBox, ...]:
    if not path.is_file():
        raise ValueError(f"missing YOLO label: {path}")
    boxes: list[LabeledBox] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError(f"invalid YOLO label at {path}:{line_number}")
        class_id = int(parts[0])
        if not 0 <= class_id < len(class_names):
            raise ValueError(f"YOLO class ID out of range at {path}:{line_number}")
        center_x, center_y, width, height = (float(value) for value in parts[1:])
        boxes.append(
            LabeledBox(
                class_names[class_id],
                BoundingBox(
                    center_x - width / 2,
                    center_y - height / 2,
                    center_x + width / 2,
                    center_y + height / 2,
                ),
            )
        )
    return tuple(boxes)


def _assign_strata(
    strata: dict[str, list[str]],
    *,
    frame_id: str,
    truth: tuple[LabeledBox, ...],
    image: object,
) -> None:
    labels = {item.label for item in truth}
    if not labels:
        strata["background"].append(frame_id)
    elif labels == {"flame"}:
        strata["flame_only"].append(frame_id)
    elif labels == {"smoke"}:
        strata["smoke_only"].append(frame_id)
    else:
        strata["flame_and_smoke"].append(frame_id)
    mean_brightness = float(image.mean())
    if mean_brightness < 70:
        strata["dark"].append(frame_id)
    elif mean_brightness > 180:
        strata["bright"].append(frame_id)
    else:
        strata["normal_light"].append(frame_id)


def _evaluate_strata(
    ground_truth: tuple[GroundTruthFrame, ...],
    predictions: tuple[PredictionFrame, ...],
    *,
    strata: dict[str, list[str]],
    thresholds: dict[str, float],
    iou_threshold: float,
) -> dict[str, object]:
    truth_by_id = {frame.frame_id: frame for frame in ground_truth}
    predictions_by_id = {frame.frame_id: frame for frame in predictions}
    reports: dict[str, object] = {}
    for name, frame_ids in strata.items():
        if not frame_ids:
            reports[name] = {"frame_count": 0, "metrics": None}
            continue
        report = evaluate_class_thresholds(
            tuple(truth_by_id[frame_id] for frame_id in frame_ids),
            tuple(predictions_by_id[frame_id] for frame_id in frame_ids),
            thresholds=thresholds,
            iou_threshold=iou_threshold,
        )
        reports[name] = {
            "frame_count": len(frame_ids),
            "metrics": evaluation_document(report),
        }
    return reports


def _write_predictions(path: Path, predictions: tuple[PredictionFrame, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for frame in predictions:
            handle.write(
                json.dumps(
                    {
                        "frame_id": frame.frame_id,
                        "inference_latency_ms": frame.inference_latency_ms,
                        "detections": [
                            {
                                "label": item.label,
                                "confidence": item.confidence,
                                "bbox": item.bbox.rounded(),
                            }
                            for item in frame.detections
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )


if __name__ == "__main__":
    raise SystemExit(main())

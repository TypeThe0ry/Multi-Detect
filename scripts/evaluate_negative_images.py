from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure false positives on known-negative images."
    )
    parser.add_argument("--onnx-model", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument(
        "--labels",
        type=Path,
        help="optional YOLO label directory; when supplied, evaluate only empty-label images",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--thresholds", default="0.10,0.25,0.50,0.65")
    parser.add_argument("--provider", action="append", default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    thresholds = tuple(float(value) for value in args.thresholds.split(","))
    if not thresholds or any(not 0.0 <= value <= 1.0 for value in thresholds):
        raise ValueError("thresholds must be comma-separated values in [0, 1]")
    images = select_negative_images(args.images, args.labels)
    if not images:
        raise ValueError("negative image directory is empty")

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for negative-image evaluation") from exc
    from multidetect.vision import OnnxNx6Config, OnnxNx6Detector

    detector = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=args.onnx_model,
            class_names=("flame", "smoke"),
            confidence_threshold=min(thresholds),
            output_coordinates="letterbox_xyxy_px",
            providers=tuple(args.provider),
        )
    )
    detections_by_image: list[tuple[Path, tuple[object, ...]]] = []
    for path in images:
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"cannot read negative image: {path}")
        detections_by_image.append((path, tuple(detector.detect(image))))

    metrics = {}
    for threshold in thresholds:
        false_positive_images = sum(
            any(detection.confidence >= threshold for detection in detections)
            for _path, detections in detections_by_image
        )
        detection_count = sum(
            sum(detection.confidence >= threshold for detection in detections)
            for _path, detections in detections_by_image
        )
        metrics[f"{threshold:.2f}"] = {
            "false_positive_images": false_positive_images,
            "false_positive_image_rate": false_positive_images / len(images),
            "detection_count": detection_count,
        }
    document = {
        "model": str(args.onnx_model.resolve()),
        "image_directory": str(args.images.resolve()),
        "image_count": len(images),
        "selection": "empty_yolo_labels" if args.labels is not None else "all_images",
        "providers": detector.provider_names,
        "metrics": metrics,
        "top_false_positives": _top_false_positives(
            detections_by_image,
            minimum_confidence=min(thresholds),
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


def select_negative_images(images_dir: Path, labels_dir: Path | None) -> list[Path]:
    images = sorted(
        path for path in images_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if labels_dir is None:
        return images
    selected: list[Path] = []
    for image in images:
        label = labels_dir / f"{image.stem}.txt"
        if not label.is_file():
            raise ValueError(f"missing YOLO label for negative-image selection: {label}")
        if not label.read_text(encoding="utf-8").strip():
            selected.append(image)
    return selected


def _top_false_positives(
    detections_by_image: list[tuple[Path, tuple[object, ...]]],
    *,
    minimum_confidence: float,
    limit: int = 20,
) -> list[dict[str, object]]:
    ranked = sorted(
        (
            (max(detection.confidence for detection in detections), path, detections)
            for path, detections in detections_by_image
            if detections
        ),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    return [
        {
            "image": str(path.resolve()),
            "maximum_confidence": maximum_confidence,
            "detections": [
                {
                    "label": detection.label,
                    "confidence": detection.confidence,
                    "bbox": detection.bbox.rounded(),
                }
                for detection in detections
                if detection.confidence >= minimum_confidence
            ],
        }
        for maximum_confidence, path, detections in ranked[:limit]
    ]


if __name__ == "__main__":
    raise SystemExit(main())

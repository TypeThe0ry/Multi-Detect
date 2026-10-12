#!/usr/bin/env python3
"""Class-aware open-vocabulary box evaluation on the original dataset labels."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from open_vocab_detector import (
    DEFAULT_HOLDOUT,
    DEFAULT_WEIGHTS,
    ROOT,
    configure_offline,
    configure_ultralytics,
    holdout_list,
    load_dataset,
    prompt_for,
    read_labels,
    score_predictions,
    sha256,
    write_json,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--weights-dir", type=Path, default=ROOT / "weights")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "val", "test"), default="test")
    parser.add_argument("--holdout-classes", default=DEFAULT_HOLDOUT)
    parser.add_argument("--out", type=Path, required=True, help="Output JSON file")
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--device", default="0")
    parser.add_argument("--confidence", type=float, default=0.25, help="Precision/recall threshold")
    parser.add_argument("--predict-conf", type=float, default=0.001, help="AP prediction floor")
    parser.add_argument("--nms-iou", type=float, default=0.7)
    parser.add_argument("--max-det", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.predict_conf <= args.confidence <= 1:
        parser.error("Require 0 <= predict-conf <= confidence <= 1")
    names, splits = load_dataset(args.data)
    images = splits[args.split]
    if not images:
        parser.error("Requested split has no images")
    labels = [read_labels(image, names) for image in images]
    counts = Counter(names[cls] for rows in labels for cls, _ in rows)
    classes = [name for name in names if counts[name]]
    if not classes:
        parser.error("Requested split has no annotated classes to prompt")
    holdout = holdout_list(args.holdout_classes)
    if set(holdout) - set(names):
        parser.error("Unknown holdout class names")
    phrases = [prompt_for(name) for name in classes]
    report = {
        "status": "dry_run" if args.dry_run else "measured",
        "weights": str(args.weights.resolve()),
        "data": str(args.data.resolve()),
        "data_sha256": sha256(args.data),
        "split": args.split,
        "images": len(images),
        "counts": {name: counts[name] for name in names},
        "classes": classes,
        "prompts": phrases,
        "holdout_classes": holdout,
        "imgsz": args.imgsz,
        "predict_conf": args.predict_conf,
        "pr_confidence": args.confidence,
        "nms_iou": args.nms_iou,
        "agnostic_nms": False,
        "max_det": args.max_det,
        "half": False,
        "latency_p50_ms": None,
        "latency_definition": (
            "Synchronized predict wall time; excludes image decode, "
            "prompt encoding and first warmup"
        ),
    }
    if args.dry_run:
        write_json(args.out, report)
        print(json.dumps(report, indent=2))
        return  # No model imports or device access.
    configure_offline(args.weights, args.weights_dir)
    import cv2
    import torch
    from ultralytics import YOLOE

    configure_ultralytics(args.weights_dir)
    model = YOLOE(str(args.weights.resolve()))
    model.to("cpu" if args.device == "cpu" else f"cuda:{args.device}")
    model.model.float().eval()
    # Encode once in fp32 before any prediction/fusion. Bypass wrapper's space restriction.
    embeddings = model.get_text_pe(phrases)
    model.model.set_classes(classes, embeddings)
    options = {
        "imgsz": args.imgsz,
        "conf": args.predict_conf,
        "iou": args.nms_iou,
        "max_det": args.max_det,
        "device": args.device,
        "half": False,
        "verbose": False,
        "save": False,
        "agnostic_nms": False,
    }
    first = cv2.imread(str(images[0]))
    if first is None:
        raise OSError(f"Cannot decode {images[0]}")
    model.predict(first, **options)  # Warmup excluded from latency.
    cuda = next(model.model.parameters()).is_cuda
    predictions, truth, latencies = [], [], []
    for image_path, rows in zip(images, labels, strict=True):
        image = cv2.imread(str(image_path))
        if image is None:
            raise OSError(f"Cannot decode {image_path}")
        height, width = image.shape[:2]
        truth.append(
            [
                {
                    "class": names[cls],
                    "box": [
                        (cx - w / 2) * width,
                        (cy - h / 2) * height,
                        (cx + w / 2) * width,
                        (cy + h / 2) * height,
                    ],
                }
                for cls, (cx, cy, w, h) in rows
            ]
        )
        if cuda:
            torch.cuda.synchronize()
        start = time.perf_counter()
        result = model.predict(image, **options)[0]
        if cuda:
            torch.cuda.synchronize()
        latencies.append((time.perf_counter() - start) * 1000)
        boxes = result.boxes
        predictions.append(
            []
            if boxes is None
            else [
                {"class": classes[int(cls)], "box": box, "confidence": score}
                for box, cls, score in zip(
                    boxes.xyxy.cpu().tolist(),
                    boxes.cls.cpu().tolist(),
                    boxes.conf.cpu().tolist(),
                    strict=True,
                )
            ]
        )
    report["weights_sha256"] = sha256(args.weights)
    report["latency_p50_ms"] = statistics.median(latencies)
    report.update(
        score_predictions(truth, predictions, classes, holdout, confidence=args.confidence)
    )
    write_json(args.out, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a reproducible small-object YOLO dataset from saved Gazebo frames.

This is a training-only utility.  It may use the scenario's known target crop
to create labels, but the crop/labels are never available to the runtime
detector or the OnboardAI decision contract.  The generated dataset is a
candidate bootstrap set, not a production accuracy claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

import cv2
import numpy as np

COCO_NAMES = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-frame-dir",
        type=Path,
        default=Path("artifacts/evaluation/external-red-car-actual-rgb-20261005-r3"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("artifacts/training/red-car-detector-v2"),
    )
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--train-count", type=int, default=600)
    parser.add_argument("--validation-count", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument(
        "--original-placement-ratio",
        type=float,
        default=0.20,
        help="fraction of positive samples that retain the source target placement",
    )
    parser.add_argument(
        "--source-bbox",
        type=float,
        nargs=4,
        default=(0.3815, 0.2095, 0.6118, 0.3622),
        metavar=("X1", "Y1", "X2", "Y2"),
        help="normalized car crop from the saved close-range Gazebo frame",
    )
    return parser


def _read_sources(directory: Path) -> list[np.ndarray]:
    manifest = directory / "gazebo-bgr24-frame-set.manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"missing frame manifest: {manifest}")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    frame = document["frames"][0]
    provenance = frame["provenance"]
    width = int(provenance["width"])
    height = int(provenance["height"])
    frames: list[np.ndarray] = []
    for path in sorted(directory.glob("*.bgr24")):
        payload = np.fromfile(path, dtype=np.uint8)
        expected = width * height * 3
        if payload.size != expected:
            raise ValueError(f"unexpected BGR24 size for {path}: {payload.size} != {expected}")
        frames.append(payload.reshape((height, width, 3)))
    if not frames:
        raise FileNotFoundError(f"no BGR24 frames in {directory}")
    return frames


def _background(
    frame: np.ndarray,
    width: int,
    height: int,
    rng: random.Random,
    *,
    source_bbox: tuple[float, float, float, float],
    mask_source_target: bool = True,
) -> np.ndarray:
    background = frame
    if mask_source_target:
        source_height, source_width = frame.shape[:2]
        sx1, sy1, sx2, sy2 = source_bbox
        mask = np.zeros((source_height, source_width), dtype=np.uint8)
        # Include a small border so the original object cannot remain as an
        # unlabeled positive after resizing. The inpainted image is training
        # data only; it is never a runtime truth channel.
        margin_x = max(2, round((sx2 - sx1) * source_width * 0.02))
        margin_y = max(2, round((sy2 - sy1) * source_height * 0.02))
        x1 = max(0, int(sx1 * source_width) - margin_x)
        y1 = max(0, int(sy1 * source_height) - margin_y)
        x2 = min(source_width, int(sx2 * source_width) + margin_x)
        y2 = min(source_height, int(sy2 * source_height) + margin_y)
        mask[y1:y2, x1:x2] = 255
        background = cv2.inpaint(frame, mask, 7.0, cv2.INPAINT_TELEA)
    resized = cv2.resize(background, (width, height), interpolation=cv2.INTER_AREA)
    # Use the real Gazebo frame as a background but perturb exposure and noise
    # so the detector does not merely memorize one pixel arrangement.
    gain = rng.uniform(0.72, 1.25)
    bias = rng.uniform(-18.0, 18.0)
    output = np.clip(resized.astype(np.float32) * gain + bias, 0.0, 255.0).astype(np.uint8)
    if rng.random() < 0.75:
        noise = np.random.default_rng(rng.randrange(2**32)).normal(0.0, 3.5, output.shape)
        output = np.clip(output.astype(np.float32) + noise, 0.0, 255.0).astype(np.uint8)
    return output


def _paste_target(
    background: np.ndarray,
    source: np.ndarray,
    source_bbox: tuple[float, float, float, float],
    rng: random.Random,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    source_height, source_width = source.shape[:2]
    sx1, sy1, sx2, sy2 = source_bbox
    crop = source[
        max(0, int(sy1 * source_height)) : min(source_height, int(sy2 * source_height)),
        max(0, int(sx1 * source_width)) : min(source_width, int(sx2 * source_width)),
    ]
    if crop.size == 0:
        raise ValueError("source bbox produced an empty crop")
    target_width = rng.randint(24, 160)
    target_height = max(12, int(target_width * crop.shape[0] / crop.shape[1]))
    target_height = min(target_height, background.shape[0] // 3)
    target_width = min(target_width, background.shape[1] // 3)
    target = cv2.resize(crop, (target_width, target_height), interpolation=cv2.INTER_AREA)
    if rng.random() < 0.5:
        target = cv2.convertScaleAbs(
            target,
            alpha=rng.uniform(0.75, 1.25),
            beta=rng.uniform(-15, 15),
        )
    x1 = rng.randint(0, background.shape[1] - target_width)
    y1 = rng.randint(0, background.shape[0] - target_height)
    background[y1 : y1 + target_height, x1 : x1 + target_width] = target
    return background, (
        (x1 + target_width / 2) / background.shape[1],
        (y1 + target_height / 2) / background.shape[0],
        target_width / background.shape[1],
        target_height / background.shape[0],
    )


def _write_yaml(path: Path, root: Path) -> None:
    lines = [
        f"path: {root.as_posix()}",
        "train: images/train",
        "val: images/val",
        "names:",
    ]
    lines.extend(f"  {index}: {name!r}" for index, name in enumerate(COCO_NAMES))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = _parser().parse_args()
    if args.width <= 0 or args.height <= 0 or args.train_count <= 0 or args.validation_count <= 0:
        raise ValueError("width/height and sample counts must be positive")
    if not 0.0 <= args.original_placement_ratio <= 1.0:
        raise ValueError("original-placement-ratio must be in [0, 1]")
    source_frames = _read_sources(args.source_frame_dir.resolve())
    output = args.output_directory.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty dataset directory: {output}")
    for split in ("train", "val"):
        (output / "images" / split).mkdir(parents=True, exist_ok=True)
        (output / "labels" / split).mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    source_bbox = tuple(float(value) for value in args.source_bbox)
    counts = {"train": args.train_count, "val": args.validation_count}
    records: list[dict[str, object]] = []
    for split, count in counts.items():
        for index in range(count):
            source = source_frames[index % len(source_frames)]
            keep_original_target = (
                index % 10 != 0 and rng.random() < args.original_placement_ratio
            )
            image = _background(
                source,
                args.width,
                args.height,
                rng,
                source_bbox=source_bbox,
                mask_source_target=not keep_original_target,
            )
            # Keep a small negative slice so a detector cannot turn every red
            # pixel into a car candidate; positives dominate for fine-tuning.
            if index % 10 == 0:
                box = None
            elif keep_original_target:
                box = (
                    (source_bbox[0] + source_bbox[2]) / 2.0,
                    (source_bbox[1] + source_bbox[3]) / 2.0,
                    source_bbox[2] - source_bbox[0],
                    source_bbox[3] - source_bbox[1],
                )
            else:
                image, box = _paste_target(image, source, source_bbox, rng)
            name = f"red-car-{split}-{index:05d}"
            image_path = output / "images" / split / f"{name}.jpg"
            label_path = output / "labels" / split / f"{name}.txt"
            if not cv2.imwrite(str(image_path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
                raise OSError(f"failed to write {image_path}")
            label_path.write_text(
                "" if box is None else "2 " + " ".join(f"{value:.8f}" for value in box) + "\n",
                encoding="utf-8",
            )
            records.append(
                {
                    "split": split,
                    "image": image_path.relative_to(output).as_posix(),
                    "has_car": box is not None,
                }
            )
    dataset_id = output.name
    yaml_path = output / f"{dataset_id}.yaml"
    _write_yaml(yaml_path, output)
    manifest = {
        "schema": "multidetect.red-car-detector-dataset.v2",
        "dataset_id": dataset_id,
        "seed": args.seed,
        "image_size": [args.width, args.height],
        "source_frame_directory": args.source_frame_dir.as_posix(),
        "source_frame_sha256": [
            _sha256(path) for path in sorted(args.source_frame_dir.resolve().glob("*.bgr24"))
        ],
        "source_bbox": list(source_bbox),
        "class_id": 2,
        "class_name": "car",
        "train_count": args.train_count,
        "validation_count": args.validation_count,
        "negative_ratio": 0.1,
        "original_placement_ratio": args.original_placement_ratio,
        "training_only": True,
        "truth_free_runtime": True,
        "production_approved": False,
        "records": records,
    }
    (output / "dataset-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"dataset": str(output), "yaml": str(yaml_path), "records": len(records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

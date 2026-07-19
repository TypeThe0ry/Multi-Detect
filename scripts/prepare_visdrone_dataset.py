#!/usr/bin/env python3
"""Convert downloaded VisDrone train/val annotations to a local YOLO dataset."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

VISDRONE_NAMES = (
    "pedestrian",
    "people",
    "bicycle",
    "car",
    "van",
    "truck",
    "tricycle",
    "awning-tricycle",
    "bus",
    "motor",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_root = args.source_root.resolve()
    output_root = args.out.resolve()
    counts = {
        split: _convert_split(
            source_root / f"VisDrone2019-DET-{source_name}", output_root, split
        )
        for split, source_name in (("train", "train"), ("val", "val"))
    }
    yaml_path = output_root / "visdrone-local.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                f"path: {output_root.as_posix()}",
                "train: images/train",
                "val: images/val",
                "names:",
                *(f"  {index}: {name}" for index, name in enumerate(VISDRONE_NAMES)),
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "event": "visdrone_local_dataset_prepared",
        "source_root": str(source_root),
        "output_root": str(output_root),
        "yaml": str(yaml_path),
        "counts": counts,
        "class_names": list(VISDRONE_NAMES),
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, separators=(",", ":")))
    return 0


def _convert_split(source: Path, output_root: Path, split: str) -> dict[str, int]:
    source_images = source / "images"
    source_annotations = source / "annotations"
    if not source_images.is_dir() or not source_annotations.is_dir():
        raise FileNotFoundError(f"VisDrone source split is incomplete: {source}")
    output_images = output_root / "images" / split
    output_labels = output_root / "labels" / split
    output_images.mkdir(parents=True, exist_ok=True)
    output_labels.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to convert VisDrone labels") from exc

    image_count = 0
    object_count = 0
    for image_path in sorted(source_images.glob("*.jpg")):
        destination = output_images / image_path.name
        if not destination.exists():
            try:
                os.link(image_path, destination)
            except OSError:
                shutil.copy2(image_path, destination)
        width, height = Image.open(image_path).size
        annotation = source_annotations / f"{image_path.stem}.txt"
        lines: list[str] = []
        if annotation.is_file():
            for raw in annotation.read_text(encoding="utf-8").splitlines():
                row = raw.split(",")
                if len(row) < 6 or row[4] == "0":
                    continue
                x, y, box_width, box_height = (int(value) for value in row[:4])
                class_id = int(row[5]) - 1
                if not 0 <= class_id < len(VISDRONE_NAMES):
                    continue
                center_x = (x + box_width / 2.0) / width
                center_y = (y + box_height / 2.0) / height
                lines.append(
                    f"{class_id} {center_x:.6f} {center_y:.6f} "
                    f"{box_width / width:.6f} {box_height / height:.6f}\n"
                )
        (output_labels / f"{image_path.stem}.txt").write_text(
            "".join(lines), encoding="utf-8"
        )
        image_count += 1
        object_count += len(lines)
    return {"images": image_count, "objects": object_count}


if __name__ == "__main__":
    raise SystemExit(main())

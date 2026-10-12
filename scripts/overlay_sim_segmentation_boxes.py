#!/usr/bin/env python3
"""Overlay visible segmentation object bounds on one recorded RGB frame."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rgb", type=Path, required=True)
    parser.add_argument("--seg", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--min-area-px", type=int, default=4)
    parser.add_argument("--min-box-px", type=float, default=4)
    args = parser.parse_args()
    labels = load_labels_map(args.seg)
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    with Image.open(args.rgb) as source:
        image = source.convert("RGB")
    if image.size != (labels.shape[1], labels.shape[0]):
        raise ValueError("RGB and segmentation geometry differ")
    boxes = boxes_from_segmentation(
        labels, truth, min_area_px=args.min_area_px, min_box_px=args.min_box_px
    )
    draw = ImageDraw.Draw(image)
    for target_id, record in boxes.items():
        x1, y1, x2, y2 = record["box"]
        draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="yellow", width=2)
        draw.text((x1, max(0, y1 - 12)), target_id, fill="yellow")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)
    print(json.dumps({"output": str(args.out), "objects": boxes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

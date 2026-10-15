#!/usr/bin/env python3
"""Generate analyze_small_object_proposals.py-compatible proposal cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cache-dir", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, required=True)
    parser.add_argument("--confidence", type=float, required=True)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    from onboard_policy import YoloeProposer

    proposer = YoloeProposer(
        args.weights, confidence=args.confidence, imgsz=args.imgsz, device=args.device
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with args.output.open("w", encoding="utf-8", newline="\n") as output:
        for source in sorted(args.source_cache_dir.glob("*.jsonl")):
            for line in source.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                key = str(row["image"]).replace("\\", "/")
                if key in seen:
                    continue
                seen.add(key)
                image = cv2.imread(str(args.artifact_root / key), cv2.IMREAD_COLOR)
                if image is None:
                    raise FileNotFoundError(args.artifact_root / key)
                boxes = []
                for detection in proposer(image):
                    boxes.append(
                        [*map(float, detection.bbox), float(getattr(detection, "confidence", 0.0))]
                    )
                output.write(json.dumps({"image": key, "boxes": boxes}) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "images": len(seen),
                "imgsz": args.imgsz,
                "confidence": args.confidence,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

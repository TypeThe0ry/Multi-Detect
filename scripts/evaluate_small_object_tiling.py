"""CPU-only paired proposer benchmark on the small-object audit manifest.

Uses local detector and MobileCLIP weights; never downloads or starts a simulator.
Per-frame results are flushed so interrupted measurements can be resumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.analyze_small_object_proposals import evaluate_frames, read_jsonl  # noqa: E402
from scripts.onboard_policy import YoloeProposer  # noqa: E402


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def filter_boxes(boxes: list, width: int, height: int, *, min_side: float = 6) -> list:
    """Mirror OnboardLoop's area/score/top-eight filter with an editable side limit."""
    kept = [b for b in boxes if min(b[2] - b[0], b[3] - b[1]) >= min_side
            and (b[2] - b[0]) * (b[3] - b[1]) <= 0.25 * width * height]
    return sorted(kept, key=lambda b: b[4], reverse=True)[:8]


def summarize(frames: list[dict], rows: list[dict]) -> dict:
    if not frames or len(frames) != len(rows):
        raise ValueError("summary requires one prediction row per nonempty sample frame")
    if [r["image"] for r in rows] != [f["image"] for f in frames]:
        raise ValueError("prediction order differs from sample")
    result = {}
    for mode in ("single", "tiles", "hybrid"):
        for suffix, min_side in (("raw", None), ("onboard", 6), ("onboard_min2", 2)):
            predictions = {}
            for frame, row in zip(frames, rows, strict=True):
                boxes = row[mode]
                predictions[frame["image"]] = (
                    filter_boxes(boxes, frame["width"], frame["height"], min_side=min_side)
                    if min_side is not None else boxes
                )
            result[f"{mode}_{suffix}"] = evaluate_frames(
                frames, predictions
            )
    result["timing_seconds"] = {}
    for mode in ("single", "tiles", "hybrid"):
        values = [r[f"{mode}_seconds"] for r in rows]
        result["timing_seconds"][mode] = {
            "mean": statistics.mean(values), "median": statistics.median(values),
            "p95": sorted(values)[min(len(values) - 1, int(0.95 * len(values)))],
        }
    result["cached_onboard"] = evaluate_frames(frames)
    result["definitions"] = {
        "raw": "model proposals before OnboardLoop size/area/top-eight filtering",
        "onboard": "minimum side 6 px, maximum image area fraction .25, top eight by score",
        "onboard_min2": "same onboard filter with proposal_min_side_px=2",
        "timing": "CPU inference plus merge; excludes image loading, model init, and warmup",
        "hybrid_timing": "single plus four tile inference durations plus hybrid NMS duration",
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--text-weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("--threads must be positive")
    frames = read_jsonl(args.sample)
    if not frames:
        parser.error("--sample must contain at least one frame")
    if len({f["image"] for f in frames}) != len(frames):
        parser.error("--sample must not repeat image paths")
    args.output = args.output.resolve()
    args.output.relative_to(ROOT)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "ultralytics").mkdir(exist_ok=True)
    os.environ.update({
        "CUDA_VISIBLE_DEVICES": "-1", "YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false",
        "YOLO_CONFIG_DIR": str(args.output / "ultralytics"), "HF_HUB_OFFLINE": "1",
        "MPLCONFIGDIR": str(args.output / "matplotlib"),
    })
    import cv2
    import torch
    import ultralytics
    import ultralytics.nn.text_model as text_model

    torch.set_num_threads(args.threads)
    cv2.setNumThreads(1)
    # Supply the already installed local TorchScript encoder without creating a
    # relative weights symlink or asking Ultralytics to resolve/download a name.
    if not args.weights.is_file() or not args.text_weights.is_file():
        raise FileNotFoundError("both local weight files are required")
    original_builder = text_model.build_text_model

    def local_text_model(variant, device=None):
        if variant != "mobileclip:blt":
            raise ValueError(f"unexpected text model {variant!r}")
        return text_model.MobileCLIPTS(device, weight=str(args.text_weights.resolve()))

    text_model.build_text_model = local_text_model
    try:
        proposer = YoloeProposer(args.weights, confidence=0.05, imgsz=1280, device="cpu")
    finally:
        text_model.build_text_model = original_builder
    receipt = {
        "sample_sha256": sha256(args.sample), "weights_sha256": sha256(args.weights),
        "text_weights_sha256": sha256(args.text_weights),
        "torch": torch.__version__, "ultralytics": ultralytics.__version__,
        "device": "cpu", "threads": args.threads, "frames": len(frames),
        "imgsz": 1280, "confidence": 0.05, "tile_overlap": 0.2, "merge_iou": 0.5,
        "warmup": "one full frame plus its four tiles, excluded from timing",
        "modes": {"single": "full frame", "tiles": "four crops + class-agnostic NMS",
                  "hybrid": "full frame + four crops + class-agnostic NMS"},
    }
    metadata_path = args.output / "receipt.json"
    if metadata_path.is_file() and json.loads(metadata_path.read_text()) != receipt:
        raise ValueError("refusing to resume a different benchmark")
    metadata_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    predictions_path = args.output / "predictions.jsonl"
    rows = read_jsonl(predictions_path) if predictions_path.is_file() else []
    if [r["image"] for r in rows] != [f["image"] for f in frames[:len(rows)]]:
        raise ValueError("prediction prefix differs from sample")
    image = cv2.imread(frames[0]["image"])
    proposer(image)
    proposer.tile_proposals(image)

    def serialize(detections):
        return [[*d.bbox, d.score] for d in detections]

    with predictions_path.open("a", encoding="utf-8") as handle:
        for index, frame in enumerate(frames[len(rows):], start=len(rows)):
            bgr = cv2.imread(frame["image"])
            if bgr is None:
                raise ValueError(f"cannot read {frame['image']}")
            start = time.perf_counter()
            single = proposer(bgr)
            single_seconds = time.perf_counter() - start
            start = time.perf_counter()
            tile_raw = proposer.tile_proposals(bgr)
            tile_inference_seconds = time.perf_counter() - start
            start = time.perf_counter()
            tiles = proposer.merge(tile_raw)
            tiles_seconds = tile_inference_seconds + time.perf_counter() - start
            start = time.perf_counter()
            hybrid = proposer.merge([*single, *tile_raw])
            hybrid_seconds = (single_seconds + tile_inference_seconds
                              + time.perf_counter() - start)
            row = {
                "image": frame["image"], "single": serialize(single),
                "tiles": serialize(tiles), "hybrid": serialize(hybrid),
                "single_seconds": single_seconds, "tiles_seconds": tiles_seconds,
                "hybrid_seconds": hybrid_seconds,
            }
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            rows.append(row)
            if (index + 1) % 10 == 0:
                print(json.dumps({"frames": index + 1,
                                  "last_hybrid_seconds": hybrid_seconds}), flush=True)
    report = summarize(frames, rows)
    (args.output / "metrics.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["timing_seconds"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

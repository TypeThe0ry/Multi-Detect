#!/usr/bin/env python3
"""Compare old/new YOLOE class-agnostic proposal recall on onboard-loop frames.

CPU is the default execution device. ``--dry-run`` only joins RGB and
segmentation truth and writes the evaluation plan; it never imports a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from build_sim_detector_dataset import _find_truth, _split_map  # noqa: E402

from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)

BUCKETS = ("<8", "8-16", "16-32", "32-64", ">=64")


def cross_task_split_check(splits: dict[str, str]) -> dict[str, Any]:
    """Check optional slow-system SFT/flight test manifests for overlap."""
    candidates = [
        ROOT / "artifacts/datasets/slow-system-sft-v2-r13-bal/test.jsonl",
        ROOT / "artifacts/benchmarks/flight-identity-eval-v1-r12fast.json",
    ]
    found = next((path for path in candidates if path.is_file()), None)
    if found is None:
        return {
            "status": "skipped",
            "note": "optional slow-system SFT/flight split artifact absent",
        }
    worlds: set[str] = set()
    for line in found.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = row.get("world_id") if isinstance(row, dict) else None
        if value:
            worlds.add(str(value))
    overlap = sorted(world for world in worlds if splits.get(world) == "train")
    if overlap:
        raise ValueError(f"detector train worlds overlap slow-system test worlds: {overlap}")
    return {"status": "checked", "source": str(found), "worlds": len(worlds), "overlap": []}


def bucket(value: float) -> str:
    return (
        "<8"
        if value < 8
        else "8-16"
        if value < 16
        else "16-32"
        if value < 32
        else "32-64"
        if value < 64
        else ">=64"
    )


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )
    aa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    ab = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (aa + ab - inter) if aa + ab - inter else 0.0


def collect_frames(
    roots: list[Path], index_paths: list[Path], *, max_frames: int | None = None
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    seen_sha: set[str] = set()
    splits = _split_map(index_paths)
    for root in roots:
        for episode in sorted(p.parent for p in root.rglob("steps.jsonl")):
            world = next((p for p in episode.parts if p.startswith("gen_")), episode.parent.name)
            if splits.get(world) != "test":
                raise ValueError(
                    f"evaluator requires test world, got {world!r} split={splits.get(world)!r}"
                )
            truth_path = _find_truth(world, [ROOT, root])
            if truth_path is None:
                continue
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            targets = {
                str(t.get("target_id")): t for t in truth.get("targets", []) if isinstance(t, dict)
            }
            if not targets or "segmentation_labels" not in truth:
                continue
            for row in episode.joinpath("steps.jsonl").read_text(encoding="utf-8").splitlines():
                step = json.loads(row)
                for side in ("before", "after"):
                    obs = step.get("obs_" + side) or {}
                    if not isinstance(obs.get("rgb_path"), str) or not isinstance(
                        obs.get("segmentation_path"), str
                    ):
                        continue
                    image_path = episode / obs["rgb_path"].replace("\\", "/")
                    seg_path = episode / obs["segmentation_path"].replace("\\", "/")
                    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
                    if image is None or not seg_path.is_file():
                        continue
                    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                    if digest in seen_sha:
                        continue
                    seen_sha.add(digest)
                    visible = boxes_from_segmentation(
                        load_labels_map(seg_path), truth, min_area_px=1, min_box_px=1
                    )
                    objects = []
                    for target_id, item in visible.items():
                        target = targets.get(str(target_id))
                        mapping = truth["segmentation_labels"].get(str(target_id), {})
                        if target is None or mapping.get("role") not in (
                            "target",
                            "distractor",
                            "confuser",
                            None,
                        ):
                            continue
                        box = tuple(float(v) for v in item["box"])
                        objects.append(
                            {
                                "category": str(
                                    target.get("class") or mapping.get("asset_slug") or "unknown"
                                ).replace(" ", "_"),
                                "box": box,
                                "size_bucket": bucket(min(box[2] - box[0], box[3] - box[1])),
                            }
                        )
                    if objects:
                        frames.append({"image": image_path, "objects": objects})
                        if max_frames is not None and len(frames) >= max_frames:
                            return frames
    return frames


def _box(item: tuple[Any, ...]) -> tuple[float, ...]:
    return tuple(
        float(v) for v in (item[0] if item and isinstance(item[0], (tuple, list)) else item)
    )


def _confidence(item: tuple[Any, ...]) -> float:
    return float(item[1]) if item and isinstance(item[0], (tuple, list)) and len(item) > 1 else 0.0


def _tables(frames, predictions, *, one_to_one: bool) -> dict[str, Any]:
    """Build recall tables for either legacy or one-to-one matching."""
    counters = defaultdict(lambda: {"objects": 0, "hit03": 0, "hit05": 0})
    for frame in frames:
        boxes = predictions.get(str(frame["image"]), [])
        pairs = [
            (iou(obj["box"], _box(box)), oi, pi)
            for oi, obj in enumerate(frame["objects"])
            for pi, box in enumerate(boxes)
        ]
        pairs.sort(
            key=lambda row: (
                -row[0],
                -_confidence(boxes[row[2]]),
                tuple(frame["objects"][row[1]]["box"]),
                _box(boxes[row[2]]),
            )
        )
        overlaps = {}
        used_objects: set[int] = set()
        used_predictions: set[int] = set()
        for overlap, oi, pi in pairs:
            if oi not in used_objects and pi not in used_predictions:
                overlaps[oi] = overlap
                used_objects.add(oi)
                used_predictions.add(pi)
        for oi, obj in enumerate(frame["objects"]):
            overlap = (
                overlaps.get(oi, 0.0)
                if one_to_one
                else max((iou(obj["box"], _box(box)) for box in boxes), default=0.0)
            )
            for dim, value in (
                ("category", obj["category"]),
                ("size", obj["size_bucket"]),
                ("category_size", obj["category"] + "|" + obj["size_bucket"]),
                ("overall", "all"),
            ):
                row = counters[dim, value]
                row["objects"] += 1
                row["hit03"] += overlap >= 0.3
                row["hit05"] += overlap >= 0.5
    tables = defaultdict(list)
    for (dim, value), row in sorted(counters.items()):
        tables[dim].append(
            {
                "group": value,
                **row,
                "recall_iou_0.3": row["hit03"] / row["objects"],
                "recall_iou_0.5": row["hit05"] / row["objects"],
            }
        )
    return {"frames": len(frames), "tables": dict(tables)}


def score(frames, predictions):
    return {
        "legacy_r3_recall": _tables(frames, predictions, one_to_one=False),
        "one_to_one_recall": _tables(frames, predictions, one_to_one=True),
        "metric_versions": {
            "legacy_r3_recall": (
                "independent per-GT max IoU, thresholds 0.3/0.5, "
                "matching analyze_small_object_proposals.py"
            ),
            "one_to_one_recall": (
                "greedy one-to-one; sort (-iou, -confidence, gt box, prediction box)"
            ),
        },
    }


def infer(
    weights: Path, frames: list[dict[str, Any]], *, imgsz: int, confidence: float, device: str
) -> dict[str, list[tuple[float, ...]]]:
    from onboard_policy import YoloeProposer

    proposer = YoloeProposer(weights, confidence=confidence, imgsz=imgsz, device=device)
    out = {}
    for frame in frames:
        image = cv2.imread(str(frame["image"]), cv2.IMREAD_COLOR)
        out[str(frame["image"])] = [
            (tuple(d.bbox), float(getattr(d, "confidence", 0.0))) for d in proposer(image)
        ]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", action="append", type=Path, required=True)
    parser.add_argument("--old-weights", type=Path, required=True)
    parser.add_argument("--index", type=Path, action="append", required=True)
    parser.add_argument("--new-weights", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, action="append", default=[1280, 960])
    parser.add_argument("--confidence", type=float, default=0.05)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    frames = collect_frames(args.campaign_root, args.index, max_frames=args.max_frames)
    report: dict[str, Any] = {
        "status": "dry_run" if args.dry_run else "measured",
        "evaluation_scope": "class-agnostic proposal recall on index worlds with split=test",
        "matching": (
            "legacy independent max headline plus documented greedy one-to-one secondary metric"
        ),
        "cross_task_split_contract": (
            "detector train worlds must not occur in slow-system SFT test worlds"
        ),
        "cross_task_split_check": cross_task_split_check(_split_map(args.index)),
        "frames": len(frames),
        "imgsz": sorted(set(args.imgsz)),
        "confidence": args.confidence,
        "device": args.device,
        "weights": {"old": str(args.old_weights.resolve()), "new": str(args.new_weights.resolve())},
        "metrics": {},
    }
    if not args.dry_run:
        for size in sorted(set(args.imgsz)):
            report["metrics"][str(size)] = {}
            for name, weights in (("old", args.old_weights), ("new", args.new_weights)):
                report["metrics"][str(size)][name] = score(
                    frames,
                    infer(
                        weights, frames, imgsz=size, confidence=args.confidence, device=args.device
                    ),
                )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

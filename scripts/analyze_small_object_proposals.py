"""Audit cached class-agnostic proposals against unfiltered panoptic truth.

All visible semantic components, including one-pixel objects, are retained.
This is an offline read-only audit: no models or simulator are started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)

SMALL_CATEGORIES = ("stop_sign", "traffic_cone", "fire_hydrant", "backpack")
SIZE_BUCKETS = ("<8", "8-16", "16-32", "32-64", ">=64")


def size_bucket(short_side: float) -> str:
    """Buckets are half-open, with 64 pixels in the final bucket."""
    return SIZE_BUCKETS[next((i for i, edge in enumerate((8, 16, 32, 64)) if short_side < edge), 4)]


def altitude_bucket(altitude: float | None) -> str:
    if altitude is None:
        return "unknown"
    for edge in (8, 12, 16, 20, 24, 28, 32):
        if altitude < edge:
            return f"{max(0, edge - 4)}-{edge}" if edge > 8 else "<8"
    return ">=32"


def iou(a, b) -> float:
    intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def load_caches(cache_dir: Path) -> tuple[dict[str, list], dict]:
    """Identical repeated keys count once; conflicts use first sorted cache file."""
    cached = {}
    sources = {}
    stats = {"rows": 0, "identical_duplicate_rows": 0, "conflicts": []}
    for path in sorted(cache_dir.glob("*.jsonl")):
        for row in read_jsonl(path):
            key = row["image"].replace("\\", "/")
            boxes = row["boxes"]
            stats["rows"] += 1
            if key in cached:
                if cached[key] == boxes:
                    stats["identical_duplicate_rows"] += 1
                else:
                    stats["conflicts"].append(
                        {"image": key, "first_source": sources[key], "other_source": path.name}
                    )
                continue
            cached[key] = boxes
            sources[key] = path.name
    stats["unique_images"] = len(cached)
    stats["conflict_policy"] = "first cache file in lexicographic order"
    return cached, stats


def segmentation_objects(labels, truth: dict) -> tuple[list[dict], list[dict]]:
    visible = boxes_from_segmentation(labels, truth, min_area_px=1, min_box_px=1)
    records = {str(t["target_id"]): t for t in truth.get("targets", [])}
    mappings = {str(t["target_id"]): t for t in truth["segmentation_labels"].values()}
    objects, scene = [], []
    for target_id, item in visible.items():
        record = records.get(target_id, {})
        entry = mappings[target_id]
        category = str(record.get("class") or entry.get("asset_slug") or "unknown").replace(
            " ", "_"
        )
        short_side = min(item["box"][2] - item["box"][0], item["box"][3] - item["box"][1])
        obj = {
            "target_id": target_id,
            "category": category,
            "role": entry.get("role"),
            **item,
            "short_side_px": short_side,
            "size_bucket": size_bucket(short_side),
        }
        scene.append(obj)
        if target_id in records and entry.get("role") in ("target", "distractor", None):
            objects.append(obj)
    return objects, scene


def load_episode(job: tuple[str, list[tuple[str, list]], str]) -> tuple[list[dict], dict]:
    episode_name, entries, source_root_name = job
    episode = Path(episode_name)
    source_root = Path(source_root_name)
    keys = dict(entries)
    observations = {}
    stats = Counter()
    steps = episode / "steps.jsonl"
    if not steps.is_file():
        return [], {"missing_steps": len(keys)}
    for row in read_jsonl(steps):
        for side in ("before", "after"):
            obs = row.get(f"obs_{side}") or {}
            if not isinstance(obs.get("rgb_path"), str):
                continue
            path = episode / obs["rgb_path"].replace("\\", "/")
            key = path.relative_to(source_root).as_posix()
            if key in keys:
                observations.setdefault(key, obs)
    world = next((p for p in episode.parts if p.startswith("gen_")), None)
    candidates = [
        ROOT / "configs/scenarios" / folder / f"{world}.truth.json"
        for folder in ("generated", "generated-v2", "generated-confusers")
    ]
    truth_path = next((p for p in candidates if p.is_file()), None)
    if truth_path is None:
        return [], {"missing_truth": len(keys)}
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if not truth.get("segmentation_labels"):
        return [], {"missing_segmentation_mapping": len(keys)}
    frames = []
    for key, boxes in keys.items():
        obs = observations.get(key)
        if obs is None:
            stats["missing_observation"] += 1
            continue
        seg = obs.get("segmentation_path")
        if not isinstance(seg, str) or not (episode / seg.replace("\\", "/")).is_file():
            stats["missing_segmentation"] += 1
            continue
        image = source_root / key
        if not image.is_file():
            stats["missing_rgb"] += 1
            continue
        labels = load_labels_map(episode / seg.replace("\\", "/"))
        objects, scene = segmentation_objects(labels, truth)
        pose = obs.get("pose") or {}
        ned = pose.get("local_ned_m")
        altitude = -float(ned[2]) if ned is not None else pose.get("alt_m")
        frames.append(
            {
                "image": str(image),
                "image_key": key,
                "world": world,
                "objects": objects,
                "scene": scene,
                "camera_info": obs.get("camera_info") or {},
                "pose": pose,
                "attitude": obs.get("attitude") or {},
                "altitude_m": altitude,
                "altitude_bucket": altitude_bucket(altitude),
                "width": labels.shape[1],
                "height": labels.shape[0],
                "cached_boxes": boxes,
                "rgb_record_sha256": (obs.get("segmentation_info") or {}).get("rgb_record_sha256"),
                "sim_skew_ms": obs.get("sim_skew_ms"),
                "segmentation_path": str(episode / seg.replace("\\", "/")),
                "truth_path": str(truth_path),
            }
        )
        stats["included_frames"] += 1
        if abs(float(obs.get("sim_skew_ms") or 0)) > 250:
            stats["included_frames_pose_skew_over_250ms"] += 1
    return frames, dict(stats)


def evaluate_frames(frames: list[dict], predictions: dict[str, list] | None = None) -> dict:
    """Evaluate any proposal list keyed by absolute image or image_key.

    Independent object recall allows one proposal to recall overlapping truths.
    A false proposal has no object with IoU >= .3, not a one-to-one AP match.
    """
    counters = defaultdict(Counter)
    total_proposals = false_catalogued = false_scene = 0
    for frame in frames:
        boxes = (
            frame["cached_boxes"]
            if predictions is None
            else predictions.get(frame["image"], predictions.get(frame["image_key"], []))
        )
        total_proposals += len(boxes)
        for name, truths in (("catalogued", frame["objects"]), ("scene", frame["scene"])):
            false = sum(
                max((iou(box, obj["box"]) for obj in truths), default=0) < 0.3 for box in boxes
            )
            if name == "catalogued":
                false_catalogued += false
            else:
                false_scene += false
        for obj in frame["objects"]:
            overlap = max((iou(obj["box"], box) for box in boxes), default=0)
            category, size, alt = obj["category"], obj["size_bucket"], frame["altitude_bucket"]
            groups = [
                ("overall", "all"),
                ("category", category),
                ("size", size),
                ("altitude", alt),
                ("category_size", f"{category}|{size}"),
                ("category_altitude", f"{category}|{alt}"),
            ]
            if category in SMALL_CATEGORIES:
                short_side = obj.get(
                    "short_side_px",
                    min(obj["box"][2] - obj["box"][0], obj["box"][3] - obj["box"][1]),
                )
                groups.extend(
                    (
                        ("small_size", size),
                        ("small_altitude", alt),
                        ("small_overall", "all"),
                        ("small_integer_size", str(int(short_side))),
                    )
                )
            for dimension, value in groups:
                counter = counters[dimension, value]
                counter["objects"] += 1
                counter["recalled_iou_0.3"] += overlap >= 0.3
                counter["recalled_iou_0.5"] += overlap >= 0.5
    result = defaultdict(list)
    for (dimension, value), counter in sorted(counters.items()):
        result[dimension].append(
            {
                "group": value,
                **counter,
                "recall_iou_0.3": counter["recalled_iou_0.3"] / counter["objects"],
                "recall_iou_0.5": counter["recalled_iou_0.5"] / counter["objects"],
            }
        )
    n = len(frames)
    return {
        "frames": n,
        "proposals": total_proposals,
        "proposals_per_frame": total_proposals / n if n else 0,
        "false_catalogued": false_catalogued,
        "false_catalogued_per_frame": false_catalogued / n if n else 0,
        "false_scene": false_scene,
        "false_scene_per_frame": false_scene / n if n else 0,
        "tables": dict(result),
    }


def sample_frames(frames: list[dict], count: int, seed: int) -> list[dict]:
    """Round-robin category/size strata, greedily favor less sampled worlds.

    Sampling is deliberately enriched, not an unbiased prevalence estimate.
    Exact recorded RGB hashes (file bytes when absent) deduplicate the sample.
    """
    rng = random.Random(seed)
    candidates = frames.copy()
    rng.shuffle(candidates)
    pools = defaultdict(list)
    for frame in candidates:
        strata = {
            (obj["category"], obj["size_bucket"])
            for obj in frame["objects"]
            if obj["category"] in SMALL_CATEGORIES and obj["short_side_px"] < 64
        }
        for stratum in sorted(strata):
            pools[stratum].append(frame)
    selected, used_images, used_hashes = [], set(), set()
    worlds = Counter()
    strata = sorted(pools, key=lambda key: (SIZE_BUCKETS.index(key[1]), key[0]))
    while len(selected) < count:
        progress = False
        for stratum in strata:
            pool = pools[stratum]
            pool.sort(key=lambda frame: worlds[frame["world"]])
            while pool:
                frame = pool.pop(0)
                if frame["image"] in used_images:
                    continue
                digest = (
                    frame.get("rgb_record_sha256")
                    or hashlib.sha256(Path(frame["image"]).read_bytes()).hexdigest()
                )
                if digest in used_hashes:
                    continue
                used_hashes.add(digest)
                used_images.add(frame["image"])
                selected.append(frame)
                worlds[frame["world"]] += 1
                progress = True
                break
            if len(selected) == count:
                break
        if not progress:
            break
    return selected


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/small-object-proposals")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    cached, stats = load_caches(args.source_root / "artifacts/datasets/onboard-proposals")
    grouped = defaultdict(list)
    for key, boxes in cached.items():
        grouped[str((args.source_root / key).parent.parent)].append((key, boxes))
    jobs = [
        (episode, entries, str(args.source_root)) for episode, entries in sorted(grouped.items())
    ]
    frames = []
    joins = Counter()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for index, (episode_frames, episode_stats) in enumerate(
            executor.map(load_episode, jobs), 1
        ):
            frames.extend(episode_frames)
            joins.update(episode_stats)
            if index % 20 == 0:
                print(
                    json.dumps(
                        {"episodes": index, "total_episodes": len(jobs), "frames": len(frames)}
                    ),
                    flush=True,
                )
    frames.sort(key=lambda frame: frame["image_key"])
    write_jsonl(args.output / "frames.jsonl", frames)
    sample = sample_frames(frames, args.sample_size, args.seed)
    write_jsonl(args.output / "sample.jsonl", sample)
    report = {
        "cache": stats,
        "joins": dict(joins),
        "definitions": {
            "truth": (
                "visible target/distractor records from panoptic maps, min_area_px=1, min_box_px=1"
            ),
            "size": (
                "visible half-open box short side; [0,8), [8,16), [16,32), [32,64), [64,infinity)"
            ),
            "recall": (
                "class-agnostic independent per-object max IoU >= threshold "
                "(.3 primary; .5 sensitivity)"
            ),
            "false_catalogued": "no IoU >= .3 to any catalogued visible target/distractor",
            "false_scene": (
                "no IoU >= .3 to any labelled scene object, including scenery; "
                "unlabelled objects remain unknown"
            ),
            "altitude": (
                "-pose.local_ned_m[2] (local-origin height, not terrain or object-relative AGL)"
            ),
            "sample": (
                "deterministic category/size round robin, world-balanced, RGB-hash deduplicated; "
                "enriched sample, not prevalence estimate"
            ),
            "seed": args.seed,
        },
        "cached": evaluate_frames(frames),
        "sample_cached": evaluate_frames(sample),
        "sample_worlds": dict(Counter(frame["world"] for frame in sample)),
    }
    (args.output / "cached-audit.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "frames": len(frames),
                "sample": len(sample),
                "joins": dict(joins),
                "output": str(args.output),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

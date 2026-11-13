#!/usr/bin/env python3
"""Build held-out identity evaluation sets from cached YOLOE proposals.

The builder joins every cached proposal to the lossless panoptic segmentation of
the same recorded frame.  It deliberately uses the scenario index's canonical
English task text and the same identity prompt row shape as
``build_flight_identity_eval.py``.  Only worlds whose index split is ``test``
are admitted.

Association contract (all boxes are half-open pixel ``xyxy``): a proposal is
associated with the visible object having the largest IoU when IoU >= 0.30.
When no such object exists, IoU >= 0.10 or truth-object pixel coverage >= 0.10 is counted
as ``ambiguous`` and omitted from the main set.  A proposal with both values
below 0.10 is a background/unmatched false proposal.  Coverage is intersection
area divided by truth-object area.  Main rows are limited to one highest-score
proposal per ``(episode, truth object, identity size bucket)``; this prevents a
single object and bucket from dominating through adjacent frames/candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)
from multidetect.host_paths import host_path  # noqa: E402
from scripts.build_flight_identity_eval import (  # noqa: E402
    MEMORY,
    _campaign_name,
    _task_query,
    _truth_match,
    _world_split,
)
from scripts.score_onboard_loop import episode_world_id, truth_for_episode  # noqa: E402

SIZE_BUCKETS = ("<12", "12-16", "16-20", "20-32", ">=32")
DEFAULT_SOURCE = ROOT
DEFAULT_V3_CACHE = host_path("MULTIDETECT_PROPOSAL_V3_CACHE", r"D:\Projects\mdwt\yoloe-retrain\tmp\yoloe-v3-round-eval\r3\new-cache.jsonl", ROOT / ".tmp/yoloe-v3-round-eval/r3/new-cache.jsonl")
DEFAULT_OLD_CACHE = (
    DEFAULT_SOURCE / "artifacts/datasets/onboard-proposals/v2r3seg-dagger-r8-cfs.jsonl"
)
DEFAULT_V3_OUT = DEFAULT_SOURCE / "artifacts/datasets/flight-identity-eval-v2-v3prop"
DEFAULT_V2_OUT = DEFAULT_SOURCE / "artifacts/datasets/flight-identity-eval-v2-v2prop"


def iou(a: Iterable[float], b: Iterable[float]) -> float:
    a, b = list(map(float, a)), list(map(float, b))
    inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )
    aa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    ab = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (aa + ab - inter) if aa + ab - inter > 0 else 0.0


def proposal_coverage(proposal: Iterable[float], truth_box: Iterable[float]) -> float:
    p, t = list(map(float, proposal)), list(map(float, truth_box))
    inter = max(0.0, min(p[2], t[2]) - max(p[0], t[0])) * max(
        0.0, min(p[3], t[3]) - max(p[1], t[1])
    )
    area = max(0.0, t[2] - t[0]) * max(0.0, t[3] - t[1])
    return inter / area if area > 0 else 0.0


def size_bucket(short_side_px: float) -> str:
    for edge, bucket in zip((12, 16, 20, 32), SIZE_BUCKETS, strict=False):
        if short_side_px < edge:
            return bucket
    return SIZE_BUCKETS[-1]


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_to(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def load_cache(path: Path) -> tuple[dict[str, list], dict[str, Any]]:
    cache: dict[str, list] = {}
    conflicts = 0
    rows = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = str(row["image"]).replace("\\", "/")
        rows += 1
        boxes = row.get("boxes") or []
        if key in cache:
            if cache[key] != boxes:
                conflicts += 1
            continue
        cache[key] = boxes
    return cache, {"rows": rows, "unique_images": len(cache), "conflicts": conflicts}


def _index_metadata(indexes: list[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in indexes:
        data = _json(path)
        for item in data.get("worlds", []):
            world = item.get("world_id") or Path(str(item.get("world_file", ""))).stem
            if not world:
                continue
            old = result.get(str(world))
            if old and old.get("split") != item.get("split"):
                raise ValueError(f"conflicting metadata split for {world}")
            result[str(world)] = {**(old or {}), **item}
    return result


def _episode_for_image(source_root: Path, key: str) -> Path:
    image = (source_root / key).resolve()
    # .../episodes/<episode>/rgb/frame.png
    if image.parent.name != "rgb" or image.parent.parent.parent.name != "episodes":
        raise ValueError(f"unexpected image path: {key}")
    return image.parent.parent


def _step_observations(episode: Path) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for side in ("before", "after"):
            obs = row.get(f"obs_{side}") or {}
            name = obs.get("rgb_path")
            if isinstance(name, str):
                out[name.replace("\\", "/")] = (row, obs)
    return out


def _truth_objects(labels, truth: dict[str, Any]) -> list[dict[str, Any]]:
    visible = boxes_from_segmentation(labels, truth, min_area_px=1, min_box_px=1)
    records = {str(x.get("target_id")): x for x in truth.get("targets", []) if x.get("target_id")}
    result = []
    for target_id, geometry in visible.items():
        record = dict(records.get(target_id, {}))
        result.append({**record, **geometry, "target_id": target_id})
    return result


def _normalised_candidates(boxes: list, width: int, height: int) -> list[dict[str, Any]]:
    result = []
    for idx, raw in enumerate(boxes):
        if not isinstance(raw, (list, tuple)) or len(raw) < 4:
            continue
        try:
            x1, y1, x2, y2 = (float(raw[i]) for i in range(4))
            score = float(raw[4]) if len(raw) > 4 else None
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
            continue
        x1, x2 = max(0.0, min(width, x1)), max(0.0, min(width, x2))
        y1, y2 = max(0.0, min(height, y1)), max(0.0, min(height, y2))
        if x2 <= x1 or y2 <= y1:
            continue
        item = {
            "track_id": f"track-{idx + 1}",
            "box_xyxy_normalized": [
                round(x1 / width, 4),
                round(y1 / height, 4),
                round(x2 / width, 4),
                round(y2 / height, 4),
            ],
            "box_xyxy": [x1, y1, x2, y2],
        }
        if score is not None and math.isfinite(score):
            item["score"] = round(score, 6)
        item["short_side_px"] = min(x2 - x1, y2 - y1)
        item["size_bucket"] = size_bucket(item["short_side_px"])
        result.append(item)
    return result


def associate(
    proposal: Iterable[float],
    objects: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.30,
    ambiguous_threshold: float = 0.10,
) -> tuple[dict[str, Any] | None, str, float, float]:
    scored = [
        (iou(proposal, obj["box"]), proposal_coverage(proposal, obj["box"]), obj) for obj in objects
    ]
    if not scored:
        return None, "background", 0.0, 0.0
    best_iou, best_cov, best_obj = max(
        scored, key=lambda item: (item[0], item[1], str(item[2].get("target_id")))
    )
    if best_iou >= iou_threshold:
        return best_obj, "associated", best_iou, best_cov
    if max(best_iou, best_cov) >= ambiguous_threshold:
        return None, "ambiguous", best_iou, best_cov
    return None, "background", best_iou, best_cov


def _reason(label_type: str, obj: dict[str, Any] | None, query: str) -> str:
    if label_type == "truth_match":
        return "Candidate class and color match the task."
    if obj is not None:
        return (
            f"Candidate is {obj.get('label') or obj.get('class') or obj.get('category')}; task "
            f"asks for {query}."
        )
    return "Candidate has no reliable nearby scenario truth object."


def _row(
    episode: Path,
    row: dict[str, Any],
    obs: dict[str, Any],
    world: str,
    split: str,
    task_text: str,
    task_source: str,
    truth: dict[str, Any],
    candidates: list[dict[str, Any]],
    focus_index: int,
    obj: dict[str, Any] | None,
    label_type: str,
    max_iou: float,
    coverage: float,
    source_name: str,
    source_root: Path,
) -> dict[str, Any]:
    focus = candidates[focus_index]
    query = _task_query(task_text)
    is_match = obj is not None and _truth_match(obj, truth, query)
    final_type = (
        "truth_match" if is_match else ("truth_distractor" if obj is not None else "unmatched")
    )
    target = {
        "decision": "confirm_target" if is_match else "reject_track",
        "args": {"track_id": focus["track_id"], "region": None, "duration_s": None},
        "reason": _reason(final_type, obj, query),
    }
    rgb = (episode / str(obs["rgb_path"]).replace("\\", "/")).resolve()
    key = json.dumps(
        [world, _relative_to(rgb, source_root), task_text, candidates, target],
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "sample_id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
        "world": world,
        "split": "test",
        "source_split": split,
        "image_path": _relative_to(rgb, source_root),
        "image_size": {
            "width": int((obs.get("camera_info") or {}).get("width", 1280)),
            "height": int((obs.get("camera_info") or {}).get("height", 960)),
        },
        "task_text": task_text,
        "recorded_task_text": str(row.get("task_text") or ""),
        "task_query": query,
        "task_text_source": task_source,
        "task_text_exact": True,
        "candidates": [
            {k: v for k, v in c.items() if k not in {"box_xyxy", "short_side_px", "size_bucket"}}
            for c in candidates
        ],
        "focus_track_id": focus["track_id"],
        "memory": MEMORY,
        "assistant_target": target,
        "campaign": _campaign_name(episode),
        "label_type": final_type,
        "truth_category": obj.get("class") or obj.get("category") or obj.get("label")
        if obj
        else None,
        "recorded_decision": None,
        "recorded_identity": None,
        "recorded_reason": None,
        "recorded_error": None,
        "truth_distance_m": None,
        "model": None,
        "model_source": "not_recorded_hypothetical_query",
        "truth_target_id": obj.get("target_id") if obj else None,
        "truth_method": "panoptic_segmentation_iou",
        "association_iou": round(max_iou, 6),
        "proposal_coverage": round(coverage, 6),
        "candidate_size_bucket": focus["size_bucket"],
        "candidate_short_side_px": round(focus["short_side_px"], 4),
        "candidate_boxes_source": source_name,
        "episode": _relative_to(episode, source_root),
        "step_index": row.get("step_index"),
        "recorded_track_id": None,
        "ground_ne": None,
    }


def build_dataset(
    cache_path: Path,
    out: Path,
    *,
    source_root: Path = DEFAULT_SOURCE,
    indexes: list[Path],
    include_unmatched: bool = False,
    max_per_object_bucket: int = 3,
) -> dict[str, Any]:
    cache, cache_stats = load_cache(cache_path)
    metadata = _index_metadata(indexes)
    rows: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    stats = Counter(cache_stats)
    skipped = Counter()
    pending: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    episode_cache: dict[
        Path,
        tuple[
            dict[str, tuple[dict[str, Any], dict[str, Any]]], dict[str, Any], dict[str, Any], str
        ],
    ] = {}
    for key, raw_boxes in sorted(cache.items()):
        try:
            episode = _episode_for_image(source_root, key)
            world = episode_world_id(episode)
            split = _world_split(world, indexes)
            if split != "test":
                skipped[f"world_split_{split or 'unknown'}"] += 1
                continue
            item = metadata.get(world) or {}
            task_text = str(item.get("task_text_en") or item.get("find_task_text_en") or "")
            if not task_text:
                skipped["missing_task_text_en"] += 1
                continue
            if episode not in episode_cache:
                scenario_dir = next(
                    (
                        index.parent
                        for index in indexes
                        if (index.parent / f"{world}.truth.json").is_file()
                    ),
                    None,
                )
                _, truth = truth_for_episode(episode, scenario_dir)
                if not truth:
                    skipped["missing_truth"] += 1
                    continue
                episode_cache[episode] = (_step_observations(episode), truth, item, split)
            observations, truth, item, split = episode_cache[episode]
            image_rel = key.split("/rgb/", 1)[-1]
            obs_key = "rgb/" + image_rel
            found = observations.get(obs_key)
            if found is None:
                skipped["missing_observation"] += 1
                continue
            step, obs = found
            seg_name = obs.get("segmentation_path")
            if not isinstance(seg_name, str):
                skipped["missing_segmentation"] += 1
                continue
            seg_path = episode / seg_name.replace("\\", "/")
            rgb_path = episode / str(obs.get("rgb_path", "")).replace("\\", "/")
            if not seg_path.is_file() or not rgb_path.is_file():
                skipped["missing_frame_files"] += 1
                continue
            labels = load_labels_map(seg_path)
            objects = _truth_objects(labels, truth)
            height, width = labels.shape[:2]
            candidates = _normalised_candidates(raw_boxes, width, height)
            if not candidates:
                skipped["invalid_boxes"] += 1
                continue
            for focus_index, candidate in enumerate(candidates):
                obj, status, best_iou, cov = associate(candidate["box_xyxy"], objects)
                if status == "ambiguous":
                    stats["ambiguous_candidates"] += 1
                    continue
                if status == "background":
                    stats["background_candidates"] += 1
                row_out = _row(
                    episode,
                    step,
                    obs,
                    world,
                    split,
                    task_text,
                    "scenario_index_task_text_en",
                    truth,
                    candidates,
                    focus_index,
                    obj,
                    "",
                    best_iou,
                    cov,
                    cache_path.name,
                    source_root,
                )
                if row_out["label_type"] == "unmatched":
                    unmatched.append(row_out)
                    if not include_unmatched:
                        continue
                stats[row_out["label_type"]] += 1
                if obj is None:
                    dedupe_key = (str(episode), "unmatched", row_out["candidate_size_bucket"])
                else:
                    dedupe_key = (
                        str(episode),
                        str(obj["target_id"]),
                        row_out["candidate_size_bucket"],
                    )
                pending[dedupe_key].append(row_out)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            skipped[f"error_{type(exc).__name__}"] += 1
    if max_per_object_bucket < 1:
        raise ValueError("max_per_object_bucket must be positive")
    rows = []
    for group in pending.values():
        group.sort(
            key=lambda r: (
                -float(
                    next(
                        (
                            c.get("score", -1)
                            for c in r["candidates"]
                            if c.get("track_id") == r["focus_track_id"]
                        ),
                        -1,
                    )
                ),
                r["image_path"],
                r["focus_track_id"],
            )
        )
        rows.extend(group[:max_per_object_bucket])
    rows.sort(key=lambda r: (r["image_path"], r["focus_track_id"]))
    out.mkdir(parents=True, exist_ok=True)
    for path, values in ((out / "test.jsonl", rows), (out / "unmatched.jsonl", unmatched)):
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for value in values:
                handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    counts = {
        "cache_rows": cache_stats["rows"],
        "cache_unique_images": cache_stats["unique_images"],
        **dict(stats),
        "rows_written": len(rows),
        "unmatched_rows": len(unmatched),
        "truth_match_rows": sum(x["label_type"] == "truth_match" for x in rows),
        "truth_distractor_rows": sum(x["label_type"] == "truth_distractor" for x in rows),
        "episodes_seen": len(episode_cache),
        "background_candidates": int(stats.get("background_candidates", 0)),
        "ambiguous_candidates": int(stats.get("ambiguous_candidates", 0)),
    }
    manifest = {
        "schema_version": 1,
        "dataset": "flight-identity-eval-v2",
        "split": "test",
        "proposal_source": cache_path.name,
        "crop_policy": "candidate_zoom_v2",
        "association": {
            "primary_iou": 0.30,
            "ambiguous_iou_or_truth_coverage": 0.10,
            "coverage_definition": "intersection_area / truth_object_area",
            "segmentation": "boxes_from_segmentation(min_area_px=1,min_box_px=1)",
        },
        "dedupe": "highest-score candidates per (episode, truth_target_id or unmatched, candidate "
        "size bucket); capped by max_per_object_bucket",
        "size_buckets": list(SIZE_BUCKETS),
        "include_unmatched": include_unmatched,
        "max_per_object_bucket": max_per_object_bucket,
        "counts": counts,
        "size_distribution": dict(Counter(x["candidate_size_bucket"] for x in rows)),
        "label_distribution": dict(Counter(x["label_type"] for x in rows)),
        "task_text_sources": dict(Counter(x["task_text_source"] for x in rows)),
        "skipped": dict(skipped),
        "cache": cache_stats,
        "inputs": {
            "cache": str(cache_path),
            "source_root": str(source_root),
            "indexes": [str(x) for x in indexes],
        },
        "notes": "Canonical task_text_en is taken from the same scenario index used for test split "
        "admission; this set asks the hypothetical candidate on every cached test frame, unlike "
        "v1's recorded slow calls.",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--v3-cache", type=Path, default=DEFAULT_V3_CACHE)
    parser.add_argument("--old-cache", type=Path, default=DEFAULT_OLD_CACHE)
    parser.add_argument("--out-v3", type=Path, default=DEFAULT_V3_OUT)
    parser.add_argument("--out-v2", type=Path, default=DEFAULT_V2_OUT)
    parser.add_argument("--index", action="append", type=Path, dest="indexes")
    parser.add_argument("--include-unmatched", action="store_true")
    parser.add_argument("--max-per-object-bucket", type=int, default=3)
    args = parser.parse_args(argv)
    indexes = args.indexes or sorted((args.source_root / "configs/scenarios").glob("*/index.json"))
    for name, cache, out in (
        ("v3prop", args.v3_cache, args.out_v3),
        ("v2prop", args.old_cache, args.out_v2),
    ):
        if not cache.is_file():
            raise FileNotFoundError(cache)
        print(
            json.dumps(
                {
                    "proposal": name,
                    **build_dataset(
                        cache,
                        out,
                        source_root=args.source_root,
                        indexes=indexes,
                        include_unmatched=args.include_unmatched,
                        max_per_object_bucket=args.max_per_object_bucket,
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

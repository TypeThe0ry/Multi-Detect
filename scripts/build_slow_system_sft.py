#!/usr/bin/env python3
"""Build truth-labelled, world-disjoint slow-system SFT rows from SIM teacher RGB.

Only task text, an RGB path, detector-like boxes, and short memory enter model
inputs. Scenario truth and teacher labels are used solely in this offline builder.
The original images are referenced in place; no image copies are written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_decision import parse_onboard_decision  # noqa: E402
from multidetect.simulator.camera_projection import project_target_bbox  # noqa: E402
from scripts.build_sim_detector_dataset import (  # noqa: E402
    CLASS_ALIASES,
    DEFAULT_SIZES,
    _sdf_sizes,
    _urban_target,
)

CAMPAIGNS = ("campaign-20261011-r2", "campaign-20261011-r3", "campaign-20261011-r4")
SPLITS = ("train", "val", "test")
COLORS = ("red", "blue", "green", "yellow", "orange", "black", "white")
SLUG = re.compile(r"(?:red|blue|green|yellow|orange|black|white)_(?:vehicle|rover|cargo_cart|inspection_marker)", re.I)
TASK_TEMPLATES = (
    "Find and record the {color} {class_name}.",
    "Locate the {color} {class_name}; verify the candidate first.",
    "Please identify the {color} {class_name} in the current view.",
    "\u8bf7\u5bfb\u627e{color_zh}{class_zh}\u5e76\u786e\u8ba4\u5019\u9009\u76ee\u6807\u3002",
    "\u627e\u5230{color_zh}{class_zh}\u540e\u62a5\u544a\u4f4d\u7f6e\u3002",
)
COLOR_ZH = {"red": "\u7ea2\u8272", "blue": "\u84dd\u8272", "green": "\u7eff\u8272", "yellow": "\u9ec4\u8272", "orange": "\u6a59\u8272", "black": "\u9ed1\u8272", "white": "\u767d\u8272"}
CLASS_ZH = {"vehicle": "\u8f66\u8f86", "rover": "\u63a2\u6d4b\u8f66", "cargo_cart": "\u8d27\u8fd0\u8f66", "inspection_marker": "\u68c0\u67e5\u6807\u8bb0"}
CLASS_NAMES = {"vehicle": "vehicle", "rover": "rover", "cargo_cart": "cargo cart", "inspection_marker": "inspection marker"}
SEARCH_REGIONS = ("center", "left", "right")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    return path.resolve(strict=True).relative_to(ROOT.resolve()).as_posix()


def _task_slug(text: str) -> tuple[str, str] | None:
    match = SLUG.search(text)
    if match is None:
        return None
    color, cls = match.group(0).lower().split("_", 1)
    return color, cls


def _target_color(target: dict) -> str | None:
    label = str(target.get("label", "")).lower()
    return next((color for color in COLORS if label.startswith(color + "_")), None)


def _target_class(target: dict) -> str:
    cls = str(target.get("class") or target.get("label", "").split("_", 1)[-1])
    if cls == "" and str(target.get("target_id", "")).startswith("multidetect_search_target"):
        return "inspection_marker"
    if cls.endswith("_inspection_marker"):
        return "inspection_marker"
    return CLASS_ALIASES.get(cls, cls)


def _task_text(slug: tuple[str, str], variant: int) -> str:
    color, cls = slug
    template = TASK_TEMPLATES[variant % len(TASK_TEMPLATES)]
    return template.format(
        color=color,
        class_name=CLASS_NAMES.get(cls, cls.replace("_", " ")),
        color_zh=COLOR_ZH[color],
        class_zh=CLASS_ZH.get(cls, cls),
    )


def _decision(kind: str, *, track: str | None = None, reason: str, region: str | None = None) -> dict:
    value = {
        "decision": kind,
        "args": {"track_id": track, "region": region, "duration_s": None},
        "reason": reason,
    }
    parse_onboard_decision(value)
    return value


def _dhash(image: Image.Image) -> int:
    gray = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = list(gray.getdata())
    result = 0
    for y in range(8):
        for x in range(8):
            result = (result << 1) | (pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return result


def _near_duplicate(signature: int, prior: list[int]) -> bool:
    return any((signature ^ old).bit_count() <= 3 for old in prior)


def _normalized(box: tuple[float, float, float, float], width: int, height: int) -> list[float]:
    return [round(value / (width if index % 2 == 0 else height), 4) for index, value in enumerate(box)]


def _project(obs: dict, targets: list[dict], sizes: dict[str, tuple[float, float, float]], width: int, height: int) -> list[dict]:
    projected = []
    pose = {**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}}
    for target in targets:
        metric = target.get("pose") or {}
        cls = _target_class(target)
        try:
            box = project_target_bbox(
                target_ned=(float(metric["y_north_m"]), float(metric["x_east_m"]), float(metric["z_up_m"])),
                pose=pose,
                camera_info=obs.get("camera_info") or {},
                image_size=(width, height),
                target_size_m=tuple(target.get("size_m") or sizes.get(str(target.get("target_id"))) or DEFAULT_SIZES.get(cls, (3.0, 1.8, 1.5))),
                target_yaw_rad=float(metric.get("yaw_rad", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if box is None or min(box[2] - box[0], box[3] - box[1]) < 8:
            continue
        pos = (obs.get("pose") or {}).get("local_ned_m") or (0, 0, 0)
        distance = math.hypot(float(metric["y_north_m"]) - float(pos[0]), float(metric["x_east_m"]) - float(pos[1]))
        color = _target_color(target)
        if color is None and cls == "inspection_marker":
            color = "orange"
        projected.append({"target": target, "box": box, "class": cls, "color": color, "range_m": distance})
    return projected


def _background_box(rng: random.Random, projected: list[dict], width: int, height: int) -> tuple[float, float, float, float] | None:
    for _ in range(24):
        box_w, box_h = rng.randint(24, 65), rng.randint(24, 65)
        x1, y1 = rng.randint(0, width - box_w), rng.randint(0, height - box_h)
        x2, y2 = x1 + box_w, y1 + box_h
        if all(x2 < p["box"][0] - 10 or x1 > p["box"][2] + 10 or y2 < p["box"][1] - 10 or y1 > p["box"][3] + 10 for p in projected):
            return float(x1), float(y1), float(x2), float(y2)
    return None


def _row(
    *, world: str, split: str, frame: Path, task_text: str, box: tuple[float, float, float, float] | None,
    size: tuple[int, int], target: dict, kind: str, campaign: str, label_type: str,
    candidate_boxes: list[tuple[float, float, float, float]] | None = None,
    focus_index: int | None = None,
) -> dict:
    boxes = candidate_boxes if candidate_boxes is not None else ([] if box is None else [box])
    candidates = [
        {"track_id": f"track-{index + 1}", "box_xyxy_normalized": _normalized(candidate, *size)}
        for index, candidate in enumerate(boxes)
    ]
    if focus_index is not None:
        target = {**target, "args": {**target["args"], "track_id": f"track-{focus_index + 1}"}}
        parse_onboard_decision(target)
    sample_key = json.dumps([world, _relative(frame), task_text, candidates, target], ensure_ascii=False, sort_keys=True)
    return {
        "sample_id": hashlib.sha256(sample_key.encode("utf-8")).hexdigest()[:24],
        "world": world,
        "split": split,
        "image_path": _relative(frame),
        "task_text": task_text,
        "candidates": candidates,
        "memory": "No prior confirmed target; search in progress.",
        "assistant_target": target,
        "campaign": campaign,
        "label_type": label_type,
    }


def _eligible_frames(campaign_roots: list[Path], world_splits: dict[str, str], source_hashes: dict[str, str], *, seed: int):
    seen: dict[str, list[int]] = defaultdict(list)
    stats = Counter()
    for campaign in campaign_roots:
        for metadata in sorted(campaign.rglob("episode.json")):
            ep = metadata.parent
            if _json(metadata).get("status") != "complete":
                continue
            world = next((part for part in ep.parts if part.startswith("gen_w")), None)
            urban = any(part.startswith("urban_inspection_multirotor_001") for part in ep.parts)
            if urban and world is None:
                world = "urban_inspection_multirotor_001"
            if world is None or (not urban and world not in world_splits):
                continue
            split = "train" if urban else world_splits[world]
            truth_path = ROOT / "configs/scenarios/generated" / f"{world}.truth.json"
            sdf_path = (ROOT / "simulation/gazebo/worlds" / f"{world}.sdf") if urban else (ROOT / "configs/scenarios/generated" / f"{world}.sdf")
            if not urban and not truth_path.is_file():
                continue
            targets = _urban_target(world, sdf_path) if urban else _json(truth_path).get("targets", [])
            if not targets:
                continue
            sizes = _sdf_sizes(sdf_path)
            steps = ep / "steps.jsonl"
            source_hashes[_relative(steps)] = _sha(steps)
            source_hashes[_relative(metadata)] = _sha(metadata)
            if truth_path.is_file():
                source_hashes[_relative(truth_path)] = _sha(truth_path)
            if sdf_path.is_file():
                source_hashes[_relative(sdf_path)] = _sha(sdf_path)
            episode_task = str(_json(metadata).get("task_text") or "")
            for line in steps.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                step = json.loads(line)
                slug = _task_slug(str(step.get("task_text") or episode_task))
                if slug is None:
                    stats["missing_task_slug"] += 1
                    continue
                for side in ("before", "after"):
                    obs = step.get("obs_" + side)
                    if not isinstance(obs, dict) or abs(float(obs.get("sim_skew_ms", 0))) > 250:
                        continue
                    rgb = obs.get("rgb_path")
                    if not isinstance(rgb, str):
                        continue
                    frame = ep / Path(rgb.replace("\\", "/"))
                    if not frame.is_file():
                        continue
                    with Image.open(frame) as opened:
                        if opened.mode != "RGB":
                            continue
                        width, height = opened.size
                        signature = _dhash(opened)
                    if _near_duplicate(signature, seen[world]):
                        stats["near_duplicate_frames"] += 1
                        continue
                    seen[world].append(signature)
                    source_hashes[_relative(frame)] = _sha(frame)
                    projected = _project(obs, targets, sizes, width, height)
                    stats["eligible_frames"] += 1
                    yield campaign.name, world, split, frame, (width, height), slug, projected, step, side, stats


def build_dataset(
    campaigns: list[Path], output: Path, *, index: Path, seed: int = 20261011,
    negative_ratio: float = 1.5,
) -> dict:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    if negative_ratio <= 0 or not math.isfinite(negative_ratio):
        raise ValueError("negative_ratio must be finite and positive")
    index_document = _json(index)
    world_splits = {str(w["world_id"]): str(w["split"]) for w in index_document["worlds"]}
    if set(world_splits.values()) != set(SPLITS):
        raise ValueError("World index must contain train, val and test")
    rng = random.Random(seed)
    source_hashes = {_relative(index): _sha(index), _relative(Path(__file__)): _sha(Path(__file__))}
    buckets: dict[str, dict[str, list[dict]]] = {split: defaultdict(list) for split in SPLITS}
    stats = Counter()
    for campaign, world, split, frame, size, slug, projected, step, side, frame_stats in _eligible_frames(campaigns, world_splits, source_hashes, seed=seed):
        stats.update(frame_stats)
        frame_stats.clear()
        variant = int(hashlib.sha256(f"{world}:{frame.name}:{seed}".encode()).hexdigest()[:8], 16)
        task = _task_text(slug, variant)
        labels = step.get("labels") if isinstance(step, dict) else {}
        labels = labels if isinstance(labels, dict) else {}
        expert_phase = str(labels.get("phase") or "").casefold() if side == "after" else ""
        expert_target_in_fov = labels.get("target_in_fov") if side == "after" else None
        try:
            expert_range = float(labels.get("target_range_m")) if side == "after" else math.inf
        except (TypeError, ValueError):
            expert_range = math.inf
        true_id = next((p["target"]["target_id"] for p in projected if str(p["target"].get("target_id", "")).startswith("target_true")), None)
        if world.startswith("urban_"):
            true_id = "multidetect_search_target_urban_001"
        if not projected:
            region = SEARCH_REGIONS[variant % len(SEARCH_REGIONS)]
            reason = "No visible candidate; continue searching."
            if expert_phase == "scan" and expert_target_in_fov is False:
                reason = "Expert scan reports no visible target; continue searching."
            target = _decision("search_region", region=region, reason=reason)
            buckets[split]["search_region"].append(_row(world=world, split=split, frame=frame, task_text=task, box=None, size=size, target=target, kind="search_region", campaign=campaign, label_type="no_candidate"))
            continue
        projected_boxes = [item["box"] for item in projected]
        for p_index, p in enumerate(projected):
            color_match = p["color"] == slug[0]
            class_match = p["class"] == slug[1]
            is_true = p["target"].get("target_id") == true_id
            if is_true and color_match and class_match:
                remote_by_teacher = expert_phase == "approach" and expert_range > 45
                if remote_by_teacher or p["range_m"] > 45 or min(p["box"][2] - p["box"][0], p["box"][3] - p["box"][1]) < 12:
                    target = _decision("approach_track", track=f"track-{p_index + 1}", reason="Matching candidate is distant; approach for a closer view.")
                    kind = "approach_track"
                else:
                    target = _decision("confirm_target", track=f"track-{p_index + 1}", reason="Candidate class and color match the task.")
                    kind = "confirm_target"
            elif not class_match:
                target = _decision("reject_track", track=f"track-{p_index + 1}", reason=f"Wrong class: requested {slug[1]}, candidate is {p['class']}.")
                kind = "reject_track"
            elif not color_match:
                target = _decision("reject_track", track=f"track-{p_index + 1}", reason=f"Wrong color: requested {slug[0]}, candidate is {p['color'] or 'unknown'}.")
                kind = "reject_track"
            else:
                continue
            buckets[split][kind].append(_row(world=world, split=split, frame=frame, task_text=task, box=p["box"], size=size, target=target, kind=kind, campaign=campaign, label_type="true_target" if is_true else "distractor", candidate_boxes=projected_boxes, focus_index=p_index))
        if variant % 12 == 0:
            background = _background_box(rng, projected, *size)
            if background is not None:
                target = _decision("reject_track", track="track-1", reason="No object is visible inside this candidate box.")
                buckets[split]["reject_track"].append(_row(world=world, split=split, frame=frame, task_text=task, box=background, size=size, target=target, kind="reject_track", campaign=campaign, label_type="background"))
        if variant % 35 == 0:
            target = _decision("ask_clarify", reason="Task lacks the class and color needed to identify a target.")
            buckets[split]["ask_clarify"].append(_row(world=world, split=split, frame=frame, task_text="Find the target; class and color unspecified.", box=projected[0]["box"], size=size, target=target, kind="ask_clarify", campaign=campaign, label_type="ambiguous_task"))
    selected: dict[str, list[dict]] = {}
    for split in SPLITS:
        groups = buckets[split]
        positive = groups["confirm_target"] + groups["approach_track"]
        negative = groups["reject_track"]
        rng.shuffle(positive)
        rng.shuffle(negative)
        # Keep negatives at roughly 1.5 per visual positive while preserving all
        # positive examples and a bounded set of search/clarification cases.
        negative = negative[: round(len(positive) * negative_ratio)]
        # Preserve at least one no-candidate/clarification example per world;
        # otherwise the bounded prefix can silently discard the urban train
        # world because generated worlds are visited first.
        search_limit = max(1, len(positive) // 5)
        search_by_world: dict[str, list[dict]] = defaultdict(list)
        for row in groups["search_region"]:
            search_by_world[row["world"]].append(row)
        search_representatives = [rows[0] for rows in search_by_world.values()]
        remaining_search = [row for row in groups["search_region"] if row not in search_representatives]
        extras_search = (search_representatives + remaining_search)[:max(search_limit, len(search_representatives))]
        clarify_limit = max(1, len(positive) // 12)
        clarify_by_world: dict[str, list[dict]] = defaultdict(list)
        for row in groups["ask_clarify"]:
            clarify_by_world[row["world"]].append(row)
        clarify_representatives = [rows[0] for rows in clarify_by_world.values()]
        remaining_clarify = [row for row in groups["ask_clarify"] if row not in clarify_representatives]
        extras_clarify = (clarify_representatives + remaining_clarify)[:max(clarify_limit, len(clarify_representatives))]
        extras = extras_search + extras_clarify
        rows = positive + negative + extras
        rows.sort(key=lambda r: r["sample_id"])
        selected[split] = rows
        if not rows:
            raise ValueError(f"No samples for {split}")
    seen = set()
    for split, rows in selected.items():
        for row in rows:
            if row["world"] in world_splits and world_splits[row["world"]] != split:
                raise ValueError("World crossed split boundary")
            if row["sample_id"] in seen:
                raise ValueError("Duplicate sample ID")
            seen.add(row["sample_id"])
            parse_onboard_decision(row["assistant_target"])
    output.mkdir(parents=True)
    output_hashes = {}
    for split, rows in selected.items():
        path = output / f"{split}.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8", newline="\n")
        output_hashes[path.name] = _sha(path)
    report = {
        "schema_version": 1,
        "seed": seed,
        "negative_ratio_requested": negative_ratio,
        "image_policy": "reference_existing_rgb_png_no_copy; resize_to_448_max_side_at_model_input",
        "dedupe": "per_world_64_bit_dhash_hamming_distance_at_most_3; one_source_frame_can_have_multiple_candidate_labels",
        "world_splits": {"urban_inspection_multirotor_001": "train", **world_splits},
        "counts": {split: {"total": len(rows), "worlds": len({r["world"] for r in rows}), "decisions": dict(Counter(r["assistant_target"]["decision"] for r in rows)), "label_types": dict(Counter(r["label_type"] for r in rows))} for split, rows in selected.items()},
        "source_counts": dict(stats),
        "source_sha256": dict(sorted(source_hashes.items())),
        "output_sha256": output_hashes,
    }
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", action="append", type=Path, default=[])
    parser.add_argument("--index", type=Path, default=ROOT / "configs/scenarios/generated/index.json")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/datasets/slow-system-sft-r1")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--negative-ratio", type=float, default=1.5)
    args = parser.parse_args(argv)
    campaigns = args.campaign or [ROOT / "artifacts/datasets/sim-teacher" / name for name in CAMPAIGNS]
    report = build_dataset(campaigns, args.output, index=args.index, seed=args.seed, negative_ratio=args.negative_ratio)
    print(json.dumps({"output": str(args.output), "counts": report["counts"], "source_counts": report["source_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

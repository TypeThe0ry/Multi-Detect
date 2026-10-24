#!/usr/bin/env python3
"""Mine difficult slow-model identity decisions from immutable SITL episodes."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.score_onboard_loop import _object_matches_query  # noqa: E402
from multidetect.simulator.task_semantics import task_equivalent  # noqa: E402

ACCEPT = {"match", "approach", "approach_track", "confirm", "confirm_target", "yes", "target"}
REJECT = {"no_match", "reject", "reject_track", "false", "no"}
DEFAULT_CROPS = Path("D:/Projects/mdwt/_scratch/cx-hardcase-mining/crops")


def accepted(call: dict) -> bool | None:
    value = str(call.get("identity") or call.get("verdict") or call.get("decision") or "").casefold()
    if value in ACCEPT or value.startswith(("approach_", "confirm_")):
        return True
    if value in REJECT:
        return False
    return None


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.casefold()))


def _target_obj(truth: dict) -> dict | None:
    return next((obj for obj in truth.get("targets", []) if obj.get("true_target") or obj.get("role") == "target"), None)


def _roles(truth: dict) -> dict[str, str]:
    result = {}
    for entry in (truth.get("segmentation_labels") or {}).values():
        if isinstance(entry, dict) and entry.get("target_id") and entry.get("role"):
            result[str(entry["target_id"])] = str(entry["role"])
    return result


def _truth_role(obj: dict, role_map: dict[str, str]) -> str:
    if obj.get("true_target"):
        return "true_target"
    if obj.get("count_member"):
        return "count_member"
    role = role_map.get(str(obj.get("target_id"))) or obj.get("role")
    if role in {"target", "count_member", "distractor", "confuser"}:
        return "distractor" if role == "confuser" else str(role)
    if obj.get("distractor") or obj.get("confuser"):
        return "distractor"
    return "other"


def _query_for(onboard: dict, truth: dict, task: str) -> str:
    query = onboard.get("query")
    mission = onboard.get("mission") or {}
    plan = mission.get("plan") or {}
    step_id = onboard.get("step")
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or (step_id and step.get("id") != step_id):
            continue
        query = (step.get("params") or {}).get("query") or query
    if query:
        return str(query)
    target = _target_obj(truth) or {}
    words = target.get("category_words") or target.get("semantic_words") or []
    return str(words[0]) if words else task


def expected_positive(task_kind: str | None, task: str, obj: dict, truth: dict, role: str, query: str | None = None) -> bool | None:
    """Mirror the scorer's task semantics; unknown roles never become negatives by default."""
    if role == "distractor":
        return False
    skill = (task_kind or "").casefold()
    if skill == "count" or (not skill and re.search(r"count|counting|数一数|计数|数量", task, re.I)):
        if role in {"true_target", "count_member"}:
            return True
        return False if role == "other" and obj.get("count_member") is False else None
    if skill in {"find", "inspect"} or (not skill and re.search(r"find|inspect|找到|检查|确认", task, re.I)):
        target = _target_obj(truth)
        if target is None:
            return None
        return bool(task_equivalent(target, obj))
    if skill in {"monitor", "patrol"} or (not skill and re.search(r"monitor|patrol|监视|巡逻", task, re.I)):
        return bool(_object_matches_query(obj, query or task))
    return True if role == "true_target" else None


def _world_id(episode: Path, metadata: dict) -> str:
    sdf = metadata.get("world_sdf")
    return Path(sdf).stem if sdf else next((p for p in reversed(episode.parts) if p.startswith("gen_")), "")


def load_truth(episode: Path, metadata: dict, scenario_roots: list[Path]) -> tuple[str, dict | None]:
    world = _world_id(episode, metadata)
    roots = list(scenario_roots)
    roots.extend(sorted((Path(__file__).resolve().parents[1] / "configs/scenarios").glob("*/")))
    for root in dict.fromkeys(roots):
        path = root / f"{world}.truth.json"
        if path.is_file():
            try:
                return world, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return world, None
    return world, None


def _truth_nearest(truth: dict, ground: list, time_s: float | None) -> tuple[dict | None, float | None]:
    best, best_d = None, float("inf")
    for obj in truth.get("targets", []):
        pose = obj.get("pose") or {}
        trajectory = obj.get("trajectory")
        if trajectory and time_s is not None:
            # Static world truth is common; for moving targets retain the recorded base pose.
            pass
        try:
            d = math.hypot(float(ground[0]) - float(pose["y_north_m"]), float(ground[1]) - float(pose["x_east_m"]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if d < best_d:
            best, best_d = obj, d
    return best, best_d if best else None


def _relative(episode: Path, name: str | None) -> str | None:
    if not name:
        return None
    path = (episode / name.replace("\\", "/")).resolve()
    try:
        return str(path.relative_to(episode.resolve()))
    except ValueError:
        return None


def mine_episode(episode: Path, truth: dict, campaign: str, low_confidence: float = 0.70, match_radius_m: float = 10.0) -> tuple[list[dict], int]:
    metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    task = str(metadata.get("task_text") or "")
    rows: list[dict] = []
    eligible = 0
    true_target = _target_obj(truth)
    role_map = _roles(truth)
    with (episode / "steps.jsonl").open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            step = json.loads(line)
            onboard = (step.get("labels") or {}).get("onboard") or {}
            tracks = {str(t.get("id")): t for t in onboard.get("tracks", []) if isinstance(t, dict)}
            obs = step.get("obs_before") or step.get("obs_after") or {}
            stamp = obs.get("pose_sim_stamp_ns") or obs.get("frame_sensor_stamp_ns")
            time_s = float(stamp) / 1e9 if stamp else None
            frame = _relative(episode, obs.get("rgb_path"))
            for call_index, call in enumerate(onboard.get("slow_calls") or []):
                if not isinstance(call, dict):
                    continue
                track_id = str(call.get("track_id") or call.get("track") or "")
                track = tracks.get(track_id, {})
                box, ground = track.get("bbox") or call.get("bbox"), track.get("ground_ne") or call.get("ground_ne")
                if not isinstance(box, list) or len(box) != 4 or not isinstance(ground, list) or len(ground) < 2:
                    continue
                obj, distance = _truth_nearest(truth, ground, time_s)
                pred = accepted(call)
                if obj is None or distance is None or pred is None:
                    continue
                task_kind = str(onboard.get("skill") or onboard.get("state") or "")
                role = _truth_role(obj, role_map)
                query = _query_for(onboard, truth, task)
                expected = None if distance > match_radius_m else expected_positive(task_kind, task, obj, truth, role, query)
                if expected is not None:
                    eligible += 1
                category = str(obj.get("class") or obj.get("category") or obj.get("asset_slug") or obj.get("label") or "unknown")
                if expected is True and not pred:
                    kind = "false_reject"
                elif expected is False and pred:
                    kind = "false_accept"
                elif expected is True and pred and float(call.get("confidence", 1.0)) <= low_confidence:
                    kind = "low_conf_correct"
                else:
                    kind = None
                row = {
                    "kind": kind, "campaign": campaign, "episode_id": episode.name,
                    "episode": str(episode), "task": task, "step": step.get("step_index", line_no - 1),
                    "track": track_id, "bbox": [round(float(v), 2) for v in box],
                    "decision": call.get("identity") or call.get("verdict") or call.get("decision"),
                    "confidence": call.get("confidence"), "truth_class": category,
                    "truth_role": role, "expected_positive": expected,
                    "nearest_truth_id": obj.get("target_id"),
                    "truth_distance_m": round(distance, 3), "ground_ne": [round(float(ground[0]), 3), round(float(ground[1]), 3)],
                    "frame": str((episode / frame).resolve()) if frame else None,
                    "frame_relative": frame, "line": line_no, "call_index": call_index,
                }
                row["hardcase_types"] = [kind] if kind else []
                if expected is None:
                    row["kind"] = "unknown"
                    rows.append(row)
                elif kind:
                    rows.append(row)
                # Keep correct decisions in the stream for flip detection, but remove them below.
                else:
                    row["kind"] = "_decision"
                    rows.append(row)
    _mark_flips(rows)
    return [row for row in rows if row["kind"] != "_decision"], eligible


def _mark_flips(rows: list[dict], radius_m: float = 2.0) -> None:
    by_episode: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("expected_positive") is not None and row.get("kind") in {"_decision", "false_reject", "false_accept", "low_conf_correct"}:
            by_episode[row["episode"]].append(row)
    for episode_rows in by_episode.values():
        clusters: list[list[dict]] = []
        for row in episode_rows:
            n, e = row["ground_ne"]
            cluster = next((group for group in clusters if any(
                math.hypot(n - other["ground_ne"][0], e - other["ground_ne"][1]) <= radius_m
                for other in group
            )), None)
            if cluster is None:
                clusters.append([row])
            else:
                cluster.append(row)
        for group in clusters:
            decisions = {accepted({"decision": row["decision"]}) for row in group}
            if len(group) > 1 and True in decisions and False in decisions:
                for row in group:
                    row.setdefault("hardcase_types", []).append("decision_flip")
                    row["kind"] = "decision_flip"


def _render(rows: list[dict], crop_dir: Path, sheet_path: Path) -> None:
    from PIL import Image, ImageDraw
    crop_dir.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for index, row in enumerate(rows):
        frame = Path(row["frame"])
        if not frame.is_file():
            continue
        with Image.open(frame) as src:
            image = src.convert("RGB")
            x1, y1, x2, y2 = (int(round(float(v))) for v in row["bbox"])
            pad = max(8, int(max(x2 - x1, y2 - y1) * 0.25))
            box = (max(0, x1-pad), max(0, y1-pad), min(image.width, x2+pad), min(image.height, y2+pad))
            crop = image.crop(box)
            ImageDraw.Draw(crop).rectangle((x1-box[0], y1-box[1], x2-box[0], y2-box[1]), outline="red", width=2)
            dest = crop_dir / f"{index:04d}_{row['campaign']}_{row['episode_id']}_s{row['step']}_{row['track']}.jpg"
            crop.save(dest, quality=90)
            row["crop"] = str(dest)
            if row["kind"] == "false_reject" and row.get("truth_class") == "fire_hydrant":
                tile = crop.copy()
                tile.thumbnail((220, 150))
                thumbs.append((tile, f"{row['campaign']} {row['episode_id']} s{row['step']} {row['truth_distance_m']}m"))
    if thumbs:
        cols, tile_w, tile_h, label_h = 4, 240, 180, 28
        sheet = Image.new("RGB", (cols * tile_w, math.ceil(len(thumbs) / cols) * (tile_h + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (im, label) in enumerate(thumbs):
            x, y = (i % cols) * tile_w, (i // cols) * (tile_h + label_h)
            sheet.paste(im, (x + (tile_w-im.width)//2, y + (tile_h-im.height)//2))
            draw.text((x+4, y+tile_h), label[:42], fill="black")
        sheet_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(sheet_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmarks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSONL output path")
    parser.add_argument("--since", default="2026-10-23")
    parser.add_argument("--scenario-root", type=Path, action="append", default=[])
    parser.add_argument("--crops", action="store_true")
    parser.add_argument("--crop-dir", type=Path, default=DEFAULT_CROPS)
    parser.add_argument("--contact-sheet", type=Path)
    parser.add_argument("--low-confidence", type=float, default=0.70)
    parser.add_argument("--match-radius-m", type=float, default=10.0, help="truth association radius; matches scorer default")
    args = parser.parse_args()
    roots = sorted(path for path in args.benchmarks.iterdir() if path.is_dir() and re.search(r"2026102[3-9]|202610[3-9]\d", path.name))
    all_rows, eligible_by_task = [], Counter()
    missing_truth = 0
    for root in roots:
        for steps in sorted(root.rglob("steps.jsonl")):
            episode = steps.parent
            try:
                metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
                world, truth = load_truth(episode, metadata, args.scenario_root)
                if truth is None:
                    missing_truth += 1
                    continue
                rows, eligible = mine_episode(episode, truth, root.name, args.low_confidence, args.match_radius_m)
                eligible_by_task[str(metadata.get("task_text") or "unknown")] += eligible
                all_rows.extend(rows)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                print(f"skip {episode}: {exc}")
    if args.crops:
        sheet = args.contact_sheet or args.crop_dir.parent / "false-reject-contact-sheet.jpg"
        _render(all_rows, args.crop_dir, sheet)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as out:
        for row in all_rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    counts = Counter(row["kind"] for row in all_rows if row["kind"] != "unknown")
    unknown_count = sum(1 for row in all_rows if row["kind"] == "unknown")
    by_task = defaultdict(Counter)
    by_class = defaultdict(Counter)
    for row in all_rows:
        if row["kind"] == "unknown":
            continue
        by_task[row["task"]][row["kind"]] += 1
        by_class[row["truth_class"]][row["kind"]] += 1
    task_summary = {}
    for task, values in by_task.items():
        total = eligible_by_task[task]
        task_summary[task] = {"eligible_calls": total, "hardcase_calls": sum(values.values()), "hardcase_rate": round(sum(values.values()) / total, 4) if total else 0.0, **dict(values)}
    summary = {"campaigns": [p.name for p in roots], "eligible_calls": sum(eligible_by_task.values()), "eligible_by_task": dict(eligible_by_task), "unknown_calls": unknown_count, "hardcase_counts": dict(counts), "hardcase_rate": round(sum(counts.values()) / sum(eligible_by_task.values()), 4) if eligible_by_task else 0.0, "by_task": task_summary, "by_class": {k: dict(v) for k, v in by_class.items()}, "missing_truth_episodes": missing_truth, "jsonl": str(args.output)}
    args.output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

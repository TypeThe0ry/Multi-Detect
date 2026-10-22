#!/usr/bin/env python3
"""Mine recorded slow-model calls from mission-suite campaigns.

The input campaigns are treated as immutable.  The output is a JSONL manifest
with the original frame/crop references plus a compact confusion breakdown.
Truth is resolved through the same ``truth_for_episode`` lookup used by the
onboard scorer; calls without a truth match are reported and excluded from
confusion counts instead of being guessed.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.score_onboard_loop import target_pose_at_time, truth_for_episode


def size_bin(value: float) -> str:
    if value < 12:
        return "<12px"
    if value < 16:
        return "12-16px"
    if value < 24:
        return "16-24px"
    return ">=24px"


def range_bin(value: float) -> str:
    if value <= 8:
        return "<=8m"
    if value <= 12:
        return "8-12m"
    if value <= 17:
        return "12-17m"
    if value <= 25:
        return "17-25m"
    return ">25m"


def nearest_truth(truth: dict, ground_ne: list[float], time_s: float | None) -> tuple[str | None, float | None, bool]:
    best = None
    best_d = float("inf")
    for obj in truth.get("targets", []):
        pose = target_pose_at_time(obj, time_s)
        try:
            d = math.hypot(float(ground_ne[0]) - float(pose["y_north_m"]), float(ground_ne[1]) - float(pose["x_east_m"]))
        except (KeyError, TypeError, ValueError):
            continue
        if d < best_d:
            best, best_d = obj, d
    if not best:
        return None, None, False
    cls = str(best.get("asset_slug") or best.get("class") or best.get("category") or best.get("label") or "unknown")
    cls = {"traffic_cone": "construction_cone", "car": "hatchback", "person": "casual_female"}.get(cls, cls)
    role = str(best.get("role") or "")
    return cls, best_d, role == "count_member"


def pred_positive(call: dict) -> bool:
    value = str(call.get("identity") or call.get("decision") or "").lower()
    return value in {"confirm_target", "approach_track", "match", "yes", "confirm", "target"}


def query_matches_class(query: str, cls: str) -> bool:
    """Task text is often Chinese; keep the mapping explicit and auditable."""
    q = query.casefold()
    terms = {
        "fire_hydrant": ("hydrant", "消防栓", "消防栓"),
        "construction_cone": ("cone", "锥桶", "锥筒", "交通锥"),
        "backpack": ("backpack", "背包"),
        "postbox": ("postbox", "邮筒", "邮箱"),
        "suv": ("suv", "越野车"),
        "standing_person": ("standing person", "站立的人", "行人"),
        "walking_person": ("walking person", "行人"),
        "casual_female": ("woman", "女性"),
    }
    return any(term.casefold() in q for term in terms.get(cls, (cls,)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmarks", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--pattern", default="mission-suite-*2026102*")
    args = ap.parse_args()
    roots = sorted(p for p in args.benchmarks.glob(args.pattern) if p.is_dir())
    rows: list[dict] = []
    summary = Counter()
    missing_truth = Counter()
    for campaign in roots:
        for steps_path in sorted(campaign.rglob("steps.jsonl")):
            episode = steps_path.parent
            metadata = {}
            try:
                metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            world_id, truth = truth_for_episode(episode)
            if truth is None:
                missing_truth[world_id or "unknown"] += 1
                continue
            task = str(metadata.get("task_text") or "")
            for line_no, line in enumerate(steps_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                step = json.loads(line)
                onboard = ((step.get("labels") or {}).get("onboard") or {})
                tracks = {str(t.get("id")): t for t in onboard.get("tracks") or [] if isinstance(t, dict)}
                obs = step.get("obs_after") or step.get("obs_before") or {}
                pose = ((obs.get("pose") or {}).get("local_ned_m") or [])
                sim_s = (obs.get("pose_sim_stamp_ns") or 0) / 1e9 if obs.get("pose_sim_stamp_ns") else None
                rgb = obs.get("rgb_path")
                for call_index, call in enumerate(onboard.get("slow_calls") or []):
                    if not isinstance(call, dict):
                        continue
                    track = tracks.get(str(call.get("track_id"))) or {}
                    bbox = track.get("bbox")
                    ground = track.get("ground_ne")
                    if not isinstance(bbox, list) or len(bbox) < 4 or not isinstance(ground, list) or len(ground) < 2:
                        continue
                    try:
                        short = min(abs(float(bbox[2]) - float(bbox[0])), abs(float(bbox[3]) - float(bbox[1])))
                        distance = math.hypot(float(pose[0]) - float(ground[0]), float(pose[1]) - float(ground[1]))
                    except (IndexError, TypeError, ValueError):
                        continue
                    cls, truth_distance, count_member = nearest_truth(truth, ground, sim_s)
                    if cls is None:
                        continue
                    is_target = count_member or query_matches_class(task, cls)
                    positive = pred_positive(call)
                    outcome = "tp" if positive and is_target else "tn" if not positive and not is_target else "fp" if positive else "fn"
                    row = {
                        "campaign": campaign.name,
                        "world": world_id,
                        "episode": str(episode),
                        "step": step.get("step_index"),
                        "line": line_no,
                        "call_index": call_index,
                        "query": task,
                        "verdict": call.get("identity") or call.get("decision"),
                        "decision": call.get("decision"),
                        "track_id": call.get("track_id"),
                        "bbox": bbox,
                        "box_short_side_px": round(short, 3),
                        "range_m": round(distance, 3),
                        "candidate_class": cls,
                        "candidate_is_target": is_target,
                        "truth_distance_m": round(truth_distance, 3) if truth_distance is not None else None,
                        "outcome": outcome,
                        "frame": str((episode / str(rgb)).resolve()) if rgb else None,
                        "crop": {"frame": str((episode / str(rgb)).resolve()) if rgb else None, "bbox": bbox},
                    }
                    rows.append(row)
                    summary[(outcome, "all")] += 1
                    summary[(outcome, f"class:{cls}")] += 1
                    summary[(outcome, f"range:{range_bin(distance)}")] += 1
                    summary[(outcome, f"size:{size_bin(short)}")] += 1
                    summary[(outcome, f"task:{task}", f"class:{cls}")] += 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    jsonl = args.output.with_suffix(".jsonl")
    jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    def table(prefix: str, title: str) -> list[str]:
        keys = sorted({k[1] for k in summary if len(k) > 1 and k[1].startswith(prefix)})
        out = [f"### {title}", "", "| group | n | TP | TN | FP | FN |", "|---|---:|---:|---:|---:|---:|"]
        for key in keys:
            vals = [summary[(o, key)] for o in ("tp", "tn", "fp", "fn")]
            out.append(f"| {key.split(':', 1)[-1]} | {sum(vals)} | {vals[0]} | {vals[1]} | {vals[2]} | {vals[3]} |")
        return out + [""]
    report = ["# SITL slow-model hard-example mining", "", f"calls: {len(rows)}", f"campaign roots: {len(roots)}", "", *table("class:", "By class"), *table("range:", "By range"), *table("size:", "By box size")]
    report.extend(["### Missing truth", "", json.dumps(dict(missing_truth), ensure_ascii=False, indent=2), "", "The JSONL preserves original frame paths and bbox crops; no source artifact was modified."])
    args.output.with_suffix(".md").write_text("\n".join(report) + "\n", encoding="utf-8")
    args.output.write_text(json.dumps({"calls": len(rows), "campaigns": [p.name for p in roots], "missing_truth": dict(missing_truth), "summary": {"|".join(k): v for k, v in summary.items()}, "jsonl": str(jsonl)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"calls": len(rows), "campaigns": len(roots), "missing_truth": sum(missing_truth.values()), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure real identity calls and replay optional task-local place memory offline.

Only steps.jsonl is read. No simulator, images, models, or ground truth are used.
Recorded motion and call opportunities stay fixed; this is a call substitution
estimate, not a simulation of the counterfactual mission trajectory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_loop import LoopConfig, PlaceVerdictMemory  # noqa: E402
from scripts.replay_alert_votes import (  # noqa: E402
    _episode_paths,
    _rows,
    _track_position,
    _vote_kind,
)


def default_inputs(source: Path) -> list[Path]:
    benchmarks = source / "benchmarks"
    inputs = []
    for revision in ("mission-suite-v1-r8", "mission-suite-v1-r9"):
        inputs.extend(sorted((benchmarks / revision).glob("*/campaign")))
    datasets = source / "datasets" / "onboard-loop"
    if datasets.is_dir():
        inputs.extend(sorted(p for p in datasets.iterdir() if p.is_dir()))
    return inputs


def _task_key(onboard: dict[str, Any], newly_completed: list[dict[str, Any]]) -> str:
    # Mission labels use string step IDs; standalone find uses integer frame steps.
    # On transition rows action labels may already name the next task. The first
    # newly completed history entry identifies the task whose perception ran.
    if newly_completed:
        completed = newly_completed[0].get("step")
        if isinstance(completed, str):
            return completed
    step = onboard.get("step")
    return step if isinstance(step, str) else "find"


def _consensus(history: list[str]) -> str | None:
    """Previous strict-majority verdict; first calls and ties have no consensus."""
    if not history:
        return None
    value, count = Counter(history).most_common(1)[0]
    return value if count > len(history) / 2 else None


def _place(clusters: list[dict[str, Any]], point, radius: float) -> dict[str, Any]:
    place = min(clusters, key=lambda item: math.dist(point, item["center"]), default=None)
    if place is None or math.dist(point, place["center"]) > radius:
        place = {"center": point, "calls": 0, "identity": [], "decision": []}
        clusters.append(place)
    return place


def _pose_range(row: dict[str, Any], point) -> float | None:
    pose = ((row.get("obs_before") or {}).get("pose") or {}).get("local_ned_m")
    if point is None or not isinstance(pose, (list, tuple)) or len(pose) < 2:
        return None
    try:
        distance = math.dist(point, tuple(float(v) for v in pose[:2]))
    except (TypeError, ValueError):
        return None
    return distance if math.isfinite(distance) else None


def replay_rows(rows: list[dict[str, Any]], config: LoopConfig) -> dict[str, Any]:
    """Measure baseline and replay shared live memory on recorded call opportunities."""
    tasks: dict[str, list[dict[str, Any]]] = {}
    positions: dict[str, tuple[float, float]] = {}
    memory = PlaceVerdictMemory()
    previous_task = None
    previous_focus = None
    previous_second_look = False
    previous_history_length = 0
    result: dict[str, Any] = {
        "slow_calls": 0,
        "placed_calls": 0,
        "unplaced_calls": 0,
        "unknown_identity_calls": 0,
        "identity_consensus_comparable": 0,
        "identity_consensus_agree": 0,
        "decision_consensus_comparable": 0,
        "decision_consensus_agree": 0,
        "skipped_calls": 0,
        "skipped_latency_s": 0.0,
        "already_remembered_calls": 0,
        "disagreements": [],
        "skips": [],
    }
    for index, row in enumerate(rows):
        onboard = (row.get("labels") or {}).get("onboard") or {}
        history = (onboard.get("mission") or {}).get("history") or []
        newly_completed = history[previous_history_length:]
        previous_history_length = len(history)
        task = _task_key(onboard, newly_completed)
        if task != previous_task:
            memory = PlaceVerdictMemory()
            positions = {}
            previous_focus = None
            previous_second_look = False
        clusters = tasks.setdefault(task, [])
        for track in onboard.get("tracks") or []:
            if isinstance(track, dict) and track.get("id"):
                point = _track_position(row, track)
                if point is not None:
                    positions[str(track["id"])] = point
        for call in onboard.get("slow_calls") or []:
            if not isinstance(call, dict):
                continue
            if call.get("remembered"):
                result["already_remembered_calls"] += 1
                continue
            result["slow_calls"] += 1
            track_id = str(call.get("track_id") or "")
            point = positions.get(track_id)
            kind = _vote_kind(call)
            if kind is None:
                result["unknown_identity_calls"] += 1
            decision = call.get("decision")
            if point is None:
                result["unplaced_calls"] += 1
                continue
            result["placed_calls"] += 1
            place = _place(clusters, point, config.memory_radius_m)
            for key, value in (("identity", kind), ("decision", decision)):
                consensus = _consensus(place[key])
                if value is not None and consensus is not None:
                    result[f"{key}_consensus_comparable"] += 1
                    result[f"{key}_consensus_agree"] += value == consensus
                if value is not None:
                    place[key].append(value)
            count = place["calls"]
            place["center"] = tuple(
                (old * count + new) / (count + 1)
                for old, new in zip(place["center"], point, strict=True)
            )
            place["calls"] += 1
            range_m = _pose_range(row, point)
            remembered = memory.lookup(point, range_m, config)
            # Live host bypasses inspect captures. The loop also bypasses an
            # active second look and approach judgments of its active focus.
            if (
                onboard.get("skill") == "inspect"
                or onboard.get("state_before") == "inspect"
                or previous_second_look
                or (remembered == "approach_track" and track_id == previous_focus)
            ):
                remembered = None
            if remembered is None:
                memory.record(point, range_m, decision, config)
                continue
            result["skipped_calls"] += 1
            result["skipped_latency_s"] += float(call.get("latency_s") or 0.0)
            detail = {
                "step_index": row.get("step_index", index),
                "task": task,
                "track_id": track_id,
                "north_east": [round(v, 3) for v in point],
                "range_m": round(range_m, 3) if range_m is not None else None,
                "remembered": remembered,
                "recorded": decision,
                "identity_agrees": _vote_kind({"decision": remembered}) == kind,
            }
            result["skips"].append(detail)
            if remembered != decision:
                result["disagreements"].append(detail)
            # Synthetic judgments never train memory, including in replay.
        previous_task = task
        previous_focus = onboard.get("focus")
        previous_second_look = bool(onboard.get("second_look"))
    places = [p for clusters in tasks.values() for p in clusters]
    result["distinct_places"] = len(places)
    result["calls_per_place"] = result["placed_calls"] / len(places) if places else None
    for key in ("identity", "decision"):
        denominator = result[f"{key}_consensus_comparable"]
        numerator = result[f"{key}_consensus_agree"]
        result[f"{key}_consensus_fraction"] = numerator / denominator if denominator else None
        result[f"{key}_consensus_fraction_all_calls"] = (
            numerator / result["slow_calls"] if result["slow_calls"] else None
        )
    result["skipped_fraction"] = (
        result["skipped_calls"] / result["slow_calls"] if result["slow_calls"] else 0.0
    )
    result["skipped_latency_s"] = round(result["skipped_latency_s"], 4)
    return result


def _group(episode: Path) -> str:
    parts = episode.parts
    for marker in ("mission-suite-v1-r8", "mission-suite-v1-r9"):
        if marker in parts:
            return marker
    parent = parts[parts.index("onboard-loop") + 1] if "onboard-loop" in parts else "other"
    return "eval-*" if parent.startswith("eval-") else "supplemental"


def _aggregate(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "slow_calls",
        "placed_calls",
        "unplaced_calls",
        "distinct_places",
        "skipped_calls",
        "identity_consensus_comparable",
        "identity_consensus_agree",
        "decision_consensus_comparable",
        "decision_consensus_agree",
        "unknown_identity_calls",
        "skipped_latency_s",
    )
    result = {key: sum(e[key] for e in episodes) for key in keys}
    result["flights"] = len(episodes)
    result["disagreement_count"] = sum(len(e["disagreements"]) for e in episodes)
    return result


def _fraction(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.1f}%)" if b else "—"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Place verdict memory: "
        + (
            "offline measurement and replay"
            if report["config"]["memory_enabled"]
            else "baseline (memory OFF)"
        ),
        "",
        "Inputs are read-only recorded `steps.jsonl`; all skills are included. "
        "Primary coverage is r8/r9 benchmark campaigns and every eval-* dataset campaign; "
        "other onboard-loop datasets are supplemental. "
        "No model, simulator, GPU, or Docker was used.",
        "",
        "Places use online nearest-centroid 4 m clustering of actual calls, reset per mission "
        "step/task. Ground positions reuse `replay_alert_votes._track_position` (box bottom "
        "center, full camera/attitude projection); range is horizontal distance from obs_before. "
        "Calls/place excludes unprojectable calls. Previous consensus means strict majority "
        "among earlier known identity verdicts (match vs reject) at that place; first looks and "
        "ties are excluded from the comparable denominator. Tables show agreement over all "
        "calls and over comparable calls. Exact decision consensus is also in JSON.",
        "",
        "Replay uses the live `PlaceVerdictMemory` implementation, enabled with radius 4 m, "
        "minimum 3 unanimous real decisions, and range ratio 0.7. Only reject_track and "
        "approach_track can be substituted. Synthetic judgments do not train memory or move "
        "its centroids/closest judged range. Inspect is fresh; active focus and second-look "
        "guards use the preceding row's focus/second_look labels. Mission step changes reset "
        "memory. Transition rows are assigned to the first newly completed mission history "
        "step, since outgoing action labels may already name the next task. "
        "Historical logs omit private loop state, so these guards are reconstructed "
        "from labels rather than exact private state. Recorded frames, motion, call opportunities, "
        "and subsequent actual verdicts are held fixed. Thus disagreement counts compare "
        "substituted raw decisions, not counterfactual mission outcomes or final accuracy.",
        "",
        "## Aggregate results",
        "",
        "| Cohort | Flights | Calls | Places | Calls/place | Previous identity consensus "
        "(all calls; comparable) | Skipped | Disagree | Recorded latency saved (s) |",
        "|---|---:|---:|---:|---:|---|---|---:|---:|",
    ]
    for group, total in report["groups"].items():
        per_place = (
            total["placed_calls"] / total["distinct_places"] if total["distinct_places"] else 0
        )
        consensus = (
            _fraction(total["identity_consensus_agree"], total["slow_calls"])
            + "; "
            + _fraction(
                total["identity_consensus_agree"], total["identity_consensus_comparable"]
            )
        )
        lines.append(
            f"| {group} | {total['flights']} | {total['slow_calls']} | {total['distinct_places']} "
            f"| {per_place:.2f} | {consensus} "
            f"| {_fraction(total['skipped_calls'], total['slow_calls'])} "
            f"| {total['disagreement_count']} "
            f"| {total['skipped_latency_s']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Per-flight baseline and memory replay",
            "",
            "| Flight | Calls | Places | Calls/place | Previous identity consensus "
            "(all calls; comparable) "
            "| Skipped | Disagree | Unplaced |",
            "|---|---:|---:|---:|---|---|---:|---:|",
        ]
    )
    for e in report["episodes"]:
        per_place = f"{e['calls_per_place']:.2f}" if e["calls_per_place"] is not None else "—"
        lines.append(
            f"| {e['flight']} | {e['slow_calls']} | {e['distinct_places']} | {per_place} "
            f"| {_fraction(e['identity_consensus_agree'], e['slow_calls'])}; "
            f"{_fraction(e['identity_consensus_agree'], e['identity_consensus_comparable'])} "
            f"| {_fraction(e['skipped_calls'], e['slow_calls'])} "
            f"| {len(e['disagreements'])} | {e['unplaced_calls']} |"
        )
    lines.extend(["", "## Decision disagreements", ""])
    disagreements = [(e, d) for e in report["episodes"] for d in e["disagreements"]]
    if not disagreements:
        lines.append("No substituted decision disagreed with its recorded call.")
    else:
        lines.extend(
            [
                "| Flight | Row step | Task | Track | Remembered | Recorded "
                "| Identity agrees | Range m |",
                "|---|---:|---|---|---|---|---|---:|",
            ]
        )
        for e, d in disagreements:
            lines.append(
                f"| {e['flight']} | {d['step_index']} | {d['task']} | {d['track_id']} "
                f"| {d['remembered']} | {d['recorded']} | {d['identity_agrees']} | {d['range_m']} |"
            )
    lines.extend(
        [
            "",
            "Recorded latency saved sums only actual skipped calls' logged latency, not "
            "total flight runtime. Orin runtime savings are unmeasured. Full paths, raw consensus "
            "counts, unknown/unplaced counts, and every substituted call are in the adjacent JSON.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument(
        "--source-artifacts", type=Path, default=Path("D:/Projects/Multi-Detect/artifacts")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--radius-m", type=float, default=4.0)
    parser.add_argument("--min-verdicts", type=int, default=3)
    parser.add_argument("--max-range-ratio", type=float, default=0.7)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args(argv)
    config = LoopConfig(
        memory_enabled=not args.baseline_only,
        memory_radius_m=args.radius_m,
        memory_min_verdicts=args.min_verdicts,
        memory_max_range_ratio=args.max_range_ratio,
    )
    episodes = []
    for episode in _episode_paths(args.inputs or default_inputs(args.source_artifacts)):
        rows = _rows(episode)
        if not any((r.get("labels") or {}).get("onboard") for r in rows):
            continue
        baseline_result = replay_rows(rows, LoopConfig())
        result = replay_rows(rows, config)
        result["baseline"] = baseline_result
        # Hash exactly the rows analyzed, rather than re-reading a log which may
        # still be growing while the simulator runs in the separate checkout.
        result["rows_analyzed"] = len(rows)
        serialized = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        result["analyzed_rows_sha256"] = hashlib.sha256(serialized.encode()).hexdigest()
        result["episode"] = str(episode)
        try:
            result["flight"] = episode.relative_to(args.source_artifacts).as_posix()
        except ValueError:
            result["flight"] = episode.as_posix()
        result["group"] = _group(episode)
        episodes.append(result)
    groups = {
        group: _aggregate([e for e in episodes if e["group"] == group])
        for group in dict.fromkeys(e["group"] for e in episodes)
    }
    groups["ALL"] = _aggregate(episodes)
    baseline_groups = {
        group: _aggregate([e["baseline"] for e in episodes if e["group"] == group])
        for group in dict.fromkeys(e["group"] for e in episodes)
    }
    baseline_groups["ALL"] = _aggregate([e["baseline"] for e in episodes])
    report = {
        "config": {
            "memory_enabled": config.memory_enabled,
            "radius_m": config.memory_radius_m,
            "min_verdicts": config.memory_min_verdicts,
            "max_range_ratio": config.memory_max_range_ratio,
        },
        "groups": groups,
        "baseline_groups": baseline_groups,
        "episodes": episodes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(groups, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

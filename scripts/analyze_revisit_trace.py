"""Summarise revisit candidate snapshots recorded in ``steps.jsonl``.

This is deliberately read-only and does not replay the planner.  A snapshot is
normally stored under ``labels.onboard`` (or in a mission history item); the
loader accepts the spellings used by older recordings so that a campaign can
be compared across planner revisions.

Example::

    python scripts/analyze_revisit_trace.py artifacts/benchmarks/mission-suite
    python scripts/analyze_revisit_trace.py episode-dir --json trace.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.score_onboard_loop import _row_sim_time, target_pose_at_time, truth_for_episode  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _as_candidates(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, dict)]


def _walk_snapshots(value: Any) -> Iterable[list[dict[str, Any]]]:
    """Yield candidate arrays without depending on one exact field path."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_l = str(key).casefold()
            if key_l in {
                "revisit_candidates", "revisit_candidate_snapshot", "revisit_candidate_snapshots", "candidate_snapshot",
                "candidates_snapshot", "revisit_planning", "revisit_plan_candidates",
            }:
                candidates = _as_candidates(child)
                if candidates is not None:
                    # ``revisit_candidate_snapshots`` is a list of planning
                    # records; each record owns its own ``candidates`` list.
                    nested_records = [item.get("candidates") for item in candidates if isinstance(item, dict) and isinstance(item.get("candidates"), list)]
                    if nested_records and len(nested_records) == len(candidates):
                        for nested in nested_records:
                            yield [item for item in nested if isinstance(item, dict)]
                    else:
                        yield candidates
                elif isinstance(child, dict):
                    nested = child.get("candidates") or child.get("candidate_snapshot")
                    candidates = _as_candidates(nested)
                    if candidates is not None:
                        yield candidates
            yield from _walk_snapshots(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_snapshots(child)


def _ground(candidate: dict[str, Any]) -> tuple[float, float] | None:
    point = candidate.get("ground_ne") or candidate.get("centre") or candidate.get("center")
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        try:
            north, east = float(point[0]), float(point[1])
            if math.isfinite(north) and math.isfinite(east):
                return north, east
        except (TypeError, ValueError):
            pass
    return None


def _truth_targets(truth: dict[str, Any] | None, time_s: float | None) -> list[dict[str, Any]]:
    if not isinstance(truth, dict):
        return []
    result = []
    for target in truth.get("targets", []):
        if not isinstance(target, dict):
            continue
        pose = target_pose_at_time(target, time_s)
        if "y_north_m" not in pose or "x_east_m" not in pose:
            continue
        result.append({
            "target_id": target.get("target_id"),
            "label": target.get("label") or target.get("class") or target.get("category"),
            "true_target": bool(target.get("true_target")),
            "ground_ne": [float(pose["y_north_m"]), float(pose["x_east_m"])],
        })
    return result


def _rank(candidates: list[dict[str, Any]], truth: list[dict[str, Any]]) -> dict[str, Any] | None:
    true = [item for item in truth if item["true_target"]] or truth
    if not true:
        return None
    distances = []
    for index, candidate in enumerate(candidates):
        point = _ground(candidate)
        if point is None:
            continue
        distance, target = min(
            ((math.hypot(point[0] - t["ground_ne"][0], point[1] - t["ground_ne"][1]), t) for t in true),
            key=lambda item: item[0],
        )
        distances.append((index, distance, target))
    if not distances:
        return None
    distances.sort(key=lambda item: item[1])
    target_index, target_distance, target = distances[0]
    return {
        "nearest_true_candidate_index": target_index,
        "nearest_true_distance_m": round(target_distance, 3),
        "target_id": target.get("target_id"),
        "target_label": target.get("label"),
        "rank_by_planner_order": target_index + 1,
        "candidate_count": len(candidates),
    }


def analyze_episode(episode: Path, scenarios: Path | None = None) -> dict[str, Any]:
    rows = _read_jsonl(episode / "steps.jsonl")
    world_id, truth = truth_for_episode(episode, scenarios)
    events: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        sim_time = _row_sim_time(row)
        truth_targets = _truth_targets(truth, sim_time)
        for candidates in _walk_snapshots(row):
            key = (row_index, id(candidates))
            if key in seen:
                continue
            seen.add(key)
            selected = [i for i, c in enumerate(candidates) if c.get("selected") is True or c.get("chosen") is True]
            event = {
                "step_index": row.get("step_index"),
                "sim_time_s": sim_time,
                "candidates": candidates,
                "selected_indices": selected,
                "truth_rank": _rank(candidates, truth_targets),
            }
            events.append(event)
    return {
        "episode": episode.name,
        "episode_path": str(episode),
        "world_id": world_id,
        "truth_loaded": truth is not None,
        "planning_events": events,
        "event_count": len(events),
    }


def _episodes(root: Path) -> list[Path]:
    if root.name == "steps.jsonl" and root.is_file():
        return [root.parent]
    if (root / "steps.jsonl").is_file():
        return [root]
    return sorted(path.parent for path in root.rglob("steps.jsonl"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="episode directory, steps.jsonl, or campaign root")
    parser.add_argument("--scenarios", type=Path, help="optional truth directory, same as score_onboard_loop")
    parser.add_argument("--json", dest="json_path", type=Path, help="write machine-readable report")
    args = parser.parse_args(argv)
    episodes = _episodes(args.root)
    if not episodes:
        parser.error(f"no steps.jsonl found below {args.root}")
    report = {"episodes": [analyze_episode(path, args.scenarios) for path in episodes]}
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(payload, encoding="utf-8")
    for item in report["episodes"]:
        print(f"{item['episode']}: {item['event_count']} revisit planning events (truth={item['truth_loaded']})")
        for event in item["planning_events"]:
            chosen = ",".join(str(i + 1) for i in event["selected_indices"]) or "-"
            rank = event["truth_rank"]
            rank_text = f"true-rank={rank['rank_by_planner_order']} dist={rank['nearest_true_distance_m']}m" if rank else "true-rank=?"
            print(f"  step {event['step_index']}: candidates={len(event['candidates'])} selected={chosen} {rank_text}")
            for index, candidate in enumerate(event["candidates"], 1):
                point = _ground(candidate)
                point_text = f"({point[0]:.2f},{point[1]:.2f})" if point else "(?)"
                kind = candidate.get("type") or candidate.get("kind") or "?"
                priority = candidate.get("priority_key") or candidate.get("priority")
                status = "selected" if index - 1 in event["selected_indices"] else (
                    f"skip={candidate.get('skip_reason')}" if candidate.get("skip_reason") else "skipped"
                )
                size = candidate.get("predicted_min_side_px")
                size_text = f" size={size}" if size is not None else ""
                print(f"    {index}. {kind} ground_ne={point_text} priority={priority!r}{size_text} {status}")
    if args.json_path:
        print(f"wrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

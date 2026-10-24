#!/usr/bin/env python3
"""Offline acceptance checks for SITL teacher smoke episodes."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_inputs(episode: dict[str, Any], episode_dir: Path, intent_path, truth_path):
    command = episode.get("provenance", {}).get("command", [])
    scenario_path = None
    for index, value in enumerate(command[:-1]):
        if value == "--scenario":
            scenario_path = Path(command[index + 1])
            break
    intent = Path(intent_path) if intent_path else None
    truth = Path(truth_path) if truth_path else None
    if scenario_path:
        scenario_path = scenario_path.resolve()
        if intent is None:
            candidate = scenario_path.parent / f"{scenario_path.stem.removesuffix('.scenario')}.teacher_intent.json"
            if candidate.is_file():
                intent = candidate
        if truth is None:
            candidate = scenario_path.parent / f"{scenario_path.stem.removesuffix('.scenario')}.truth.json"
            if candidate.is_file():
                truth = candidate
    if intent is None or truth is None:
        raise ValueError("cannot resolve teacher intent/truth; pass --intent and --truth")
    return _read_json(intent), _read_json(truth)


def evaluate_episode(episode_dir: str | Path, *, intent_path=None, truth_path=None, minimum_frames=8):
    episode_dir = Path(episode_dir)
    episode = _read_json(episode_dir / "episode.json")
    intent, truth = _resolve_inputs(episode, episode_dir, intent_path, truth_path)
    observation = intent.get("observation", intent.get("task_intent", {}).get("observation", {}))
    expected_altitude = float(observation["camera_altitude_m"])
    views = observation.get("views") or [{"view": "default", "observation_waypoint": observation["observation_waypoint"]}]
    target_id = truth.get("true_target_id") or truth.get("target_id")
    target = next(
        (row for row in truth.get("targets", []) if row.get("target_id") == target_id or row.get("true_target")),
        None,
    )
    if target is None:
        raise ValueError("truth has no true target")
    target_pose = target["pose"]
    designed_distances = {
        str(view.get("view", index)): math.hypot(
            float(target_pose["x_east_m"]) - float(view["observation_waypoint"]["x_east_m"]),
            float(target_pose["y_north_m"]) - float(view["observation_waypoint"]["y_north_m"]),
        )
        for index, view in enumerate(views)
    }
    steps_path = episode_dir / "steps.jsonl"
    steps = [json.loads(line) for line in steps_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    valid = []
    for step in steps:
        labels = step.get("labels") or {}
        pose = (step.get("obs_before") or {}).get("pose") or {}
        rgb_path = (step.get("obs_before") or {}).get("rgb_path")
        if not pose.get("alt_m") or not rgb_path:
            continue
        frame_path = (episode_dir / rgb_path).resolve()
        if not frame_path.is_file() or not labels.get("target_in_fov"):
            continue
        valid.append({
            "step_index": step.get("step_index"),
            "altitude_m": float(pose["alt_m"]),
            "ground_distance_m": math.hypot(
                float(pose.get("local_ned_m", [0, 0, 0])[1]) - float(target_pose["x_east_m"]),
                float(pose.get("local_ned_m", [0, 0, 0])[0]) - float(target_pose["y_north_m"]),
            ),
            "phase": labels.get("phase"),
            "view_index": labels.get("observation_view_index"),
        })
    # Re-check each intended view independently so one well-sampled view cannot
    # hide a missing or geometrically wrong recovery/truncation view.
    view_reports = {}
    for view_index, (view_name, designed_distance) in enumerate(designed_distances.items()):
        rows = [
            row for row in valid
            if row["phase"] == "observe"
            and (row["view_index"] is None or row["view_index"] == view_index)
        ]
        in_range = [row for row in rows if designed_distance > 0 and abs(row["ground_distance_m"] / designed_distance - 1.0) <= 0.25]
        view_reports[view_name] = {
            "designed_ground_distance_m": designed_distance,
            "observed_frame_count": len(in_range),
            "altitude_mean_m": sum(row["altitude_m"] for row in in_range) / len(in_range) if in_range else None,
            "ground_distance_mean_m": sum(row["ground_distance_m"] for row in in_range) / len(in_range) if in_range else None,
        }
    mean_altitude = sum(row["altitude_m"] for row in valid) / len(valid) if valid else None
    mean_distance = sum(row["ground_distance_m"] for row in valid) / len(valid) if valid else None
    distance_ok = all(report["observed_frame_count"] >= int(minimum_frames) for report in view_reports.values())
    altitude_ok = bool(mean_altitude is not None and abs(mean_altitude - expected_altitude) <= 3.0)
    frames_ok = all(report["observed_frame_count"] >= int(minimum_frames) for report in view_reports.values())
    return {
        "episode_id": episode.get("episode_id"),
        "status": episode.get("status"),
        "expected_altitude_m": expected_altitude,
        "observed_frame_count": len(valid),
        "minimum_observed_frames": int(minimum_frames),
        "observed_altitude_mean_m": mean_altitude,
        "designed_ground_distance_m": next(iter(designed_distances.values())),
        "observed_ground_distance_mean_m": mean_distance,
        "views": view_reports,
        "checks": {
            "complete": episode.get("status") == "complete",
            "observation_frames": frames_ok,
            "altitude_within_3m": altitude_ok,
            "ground_distance_within_25pct": distance_ok,
        },
        "passed": episode.get("status") == "complete" and frames_ok and altitude_ok and distance_ok,
        "observations": valid,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument("--intent", type=Path)
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--minimum-frames", type=int, default=8)
    args = parser.parse_args(argv)
    result = evaluate_episode(args.episode_dir, intent_path=args.intent, truth_path=args.truth, minimum_frames=args.minimum_frames)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

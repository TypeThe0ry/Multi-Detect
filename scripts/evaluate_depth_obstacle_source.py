"""CPU-only evaluation of DepthGridSource on recorded SIM episodes."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.obstacle_source import DepthGridSource, PoseNed  # noqa: E402
from multidetect.sim_teacher.policies import (  # noqa: E402
    SDFObstacle,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)


def _episodes(root: Path) -> list[Path]:
    return [root] if (root / "steps.jsonl").is_file() else sorted(root.glob("*/episodes/*"))


def _records(episode: Path) -> Iterable[dict[str, Any]]:
    with (episode / "steps.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _pose(observation: dict[str, Any]) -> PoseNed | None:
    value = observation.get("pose", {}).get("local_ned_m")
    if not isinstance(value, list) or len(value) != 3:
        return None
    try:
        return tuple(float(v) for v in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _intrinsics(observation: dict[str, Any]) -> dict[str, float] | None:
    info = observation.get("depth_camera_info")
    if not isinstance(info, dict):
        return None
    try:
        return {key: float(info[key]) for key in ("fx_px", "fy_px", "cx_px", "cy_px")}
    except (KeyError, TypeError, ValueError):
        return None


def _depth_path(episode: Path, observation: dict[str, Any]) -> Path | None:
    value = observation.get("depth_path")
    if not isinstance(value, str):
        return None
    path = episode / value.replace("\\", "/")
    return path if path.is_file() else None


def _rotation(observation: dict[str, Any]) -> np.ndarray:
    attitude = observation.get("attitude", {})
    yaw = math.radians(float(attitude.get("yaw_deg", 0.0)))
    pitch = math.radians(float(attitude.get("pitch_deg", 0.0)) + 10.3)
    right = np.array((-math.sin(yaw), math.cos(yaw), 0.0), dtype=np.float32)
    forward = np.array((math.cos(yaw), math.sin(yaw), 0.0), dtype=np.float32)
    down = np.array((0.0, 0.0, 1.0), dtype=np.float32)
    cam_forward = math.cos(pitch) * forward + math.sin(pitch) * down
    cam_down = -math.sin(pitch) * forward + math.cos(pitch) * down
    return np.column_stack((right, cam_down, cam_forward)).astype(np.float32)


def _nearest_static_cell(
    north: float, east: float, obstacles: tuple[SDFObstacle, ...], margin_m: float
) -> bool:
    return any(
        math.hypot(north - obstacle.north_m, east - obstacle.east_m)
        <= math.hypot(obstacle.size_north_m, obstacle.size_east_m) / 2.0 + margin_m
        for obstacle in obstacles
    )


def _grid_cells(source: DepthGridSource) -> set[tuple[int, int]]:
    if source.last_pose is None:
        return set()
    anchor, half, resolution = (
        source.last_pose[:2],
        source.cells * source.resolution_m / 2.0,
        source.resolution_m,
    )
    rows, cols = np.nonzero(source.occupied)
    return {
        (
            round((anchor[0] + (col + 0.5) * resolution - half) / resolution),
            round((anchor[1] + (row + 0.5) * resolution - half) / resolution),
        )
        for row, col in zip(rows.tolist(), cols.tolist(), strict=True)
    }


def _all_grid_cells(source: DepthGridSource) -> set[tuple[int, int]]:
    """Return every world-quantized cell currently covered by the grid."""
    if source.last_pose is None:
        return set()
    anchor = source.last_pose[:2]
    half = source.cells * source.resolution_m / 2.0
    resolution = source.resolution_m
    return {
        (
            round((anchor[0] + (col + 0.5) * resolution - half) / resolution),
            round((anchor[1] + (row + 0.5) * resolution - half) / resolution),
        )
        for row in range(source.cells)
        for col in range(source.cells)
    }


def _static_cells(
    cells: Iterable[tuple[int, int]],
    obstacles: tuple[SDFObstacle, ...],
    resolution: float,
    margin: float,
) -> set[tuple[int, int]]:
    return {
        cell
        for cell in cells
        if _nearest_static_cell(cell[0] * resolution, cell[1] * resolution, obstacles, margin)
    }


def evaluate_episode(
    episode: Path,
    *,
    resolution_m: float = 0.5,
    grid_cells: int = 160,
    max_age_s: float = 0.0,
    static_margin_m: float = 3.0,
) -> dict[str, Any]:
    metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    world = Path(str(metadata.get("world_sdf", "")))
    if not world.is_file():
        candidate = ROOT / "simulation" / "gazebo" / "worlds" / world.name
        world = candidate if candidate.is_file() else world
    obstacles = load_sdf_obstacles(world)
    source = DepthGridSource(cells=grid_cells, resolution_m=resolution_m, max_age_s=max_age_s)
    records = list(_records(episode))
    detected: set[tuple[int, int]] = set()
    truth: set[tuple[int, int]] = set()
    timings: list[float] = []
    frames = valid_points = compared = same = blocked_static = blocked_depth = 0
    for record in records:
        before = record.get("obs_before", {})
        pose = _pose(before)
        intrinsics = _intrinsics(before)
        depth_path = _depth_path(episode, before)
        if pose is not None and intrinsics is not None and depth_path is not None:
            depth = np.load(depth_path, allow_pickle=False)
            started = time.perf_counter_ns()
            valid_points += source.update(
                depth,
                pose,
                {
                    "fx": intrinsics["fx_px"],
                    "fy": intrinsics["fy_px"],
                    "cx": intrinsics["cx_px"],
                    "cy": intrinsics["cy_px"],
                },
                timestamp=float(before.get("frame_sensor_stamp_ns", frames)),
                camera_rotation=_rotation(before),
            )
            timings.append((time.perf_counter_ns() - started) / 1e6)
            frames += 1
            detected.update(_grid_cells(source))
            truth.update(
                _static_cells(_all_grid_cells(source), obstacles, resolution_m, static_margin_m)
            )
        after = _pose(record.get("obs_after", {}))
        if pose is None or after is None or record.get("action", {}).get("kind") != "translate":
            continue
        static_clear = segment_clear_of_obstacles(
            pose, after, max(0.0, -pose[2]), obstacles, margin_m=static_margin_m
        )
        depth_clear = source.segment_clear(pose, after)
        compared += 1
        blocked_static += not static_clear
        blocked_depth += not depth_clear
        same += static_clear == depth_clear
    tp = len(detected & truth)
    return {
        "episode": str(episode),
        "world": str(world),
        "obstacles": len(obstacles),
        "frames": frames,
        "valid_depth_points": valid_points,
        "occupied_cells": len(detected),
        "static_occupied_cells": len(truth),
        "occupied_cell_precision": tp / len(detected) if detected else 0.0,
        "occupied_cell_recall": tp / len(truth) if truth else 0.0,
        "latency_ms_per_depth_frame": {
            "mean": statistics.mean(timings) if timings else None,
            "p50": statistics.median(timings) if timings else None,
            "p95": float(np.percentile(timings, 95)) if timings else None,
        },
        "legs_compared": compared,
        "static_blocked_legs": blocked_static,
        "depth_blocked_legs": blocked_depth,
        "same_leg_decision_rate": same / compared if compared else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--max-episodes", type=int, default=0)
    parser.add_argument("--resolution-m", type=float, default=0.5)
    parser.add_argument("--grid-cells", type=int, default=160)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    episodes = _episodes(args.dataset)
    episodes = episodes[: args.max_episodes] if args.max_episodes > 0 else episodes
    results = [
        evaluate_episode(ep, resolution_m=args.resolution_m, grid_cells=args.grid_cells)
        for ep in episodes
        if (ep / "steps.jsonl").is_file()
    ]
    payload = {"episodes": results, "episode_count": len(results)}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from multidetect.autonomy.ground_model import FlatGround  # noqa: E402
from multidetect.autonomy.obstacle_source import DepthGridSource, PoseNed  # noqa: E402
from multidetect.sim_teacher.policies import (  # noqa: E402
    SDFObstacle,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)
from multidetect.simulator.camera_projection import (  # noqa: E402
    CAMERA_MOUNT_PITCH_RAD,
    CAMERA_MOUNT_X_M,
    CAMERA_MOUNT_Z_DOWN_M,
    _body_to_world_ned,
)


def _episodes(root: Path) -> list[Path]:
    if (root / "steps.jsonl").is_file():
        return [root]
    return sorted({path.parent for path in root.rglob("steps.jsonl")})


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
    """Return calibrated SIM optical (right, down, forward) -> local NED."""
    attitude = observation.get("attitude", {})
    roll = math.radians(float(attitude.get("roll_deg", 0.0)))
    pitch_body = math.radians(float(attitude.get("pitch_deg", 0.0)))
    yaw = math.radians(float(attitude.get("yaw_deg", 0.0)))
    cp, sp = math.cos(CAMERA_MOUNT_PITCH_RAD), math.sin(CAMERA_MOUNT_PITCH_RAD)
    body_axes = ((0.0, 1.0, 0.0), (-sp, 0.0, cp), (cp, 0.0, sp))
    world_axes = [
        _body_to_world_ned(axis, roll_rad=roll, pitch_rad=pitch_body, yaw_rad=yaw)
        for axis in body_axes
    ]
    return np.asarray(world_axes, dtype=np.float32).T


def _camera_offset_ned(observation: dict[str, Any]) -> tuple[float, float, float]:
    attitude = observation.get("attitude", {})
    roll = math.radians(float(attitude.get("roll_deg", 0.0)))
    pitch = math.radians(float(attitude.get("pitch_deg", 0.0)))
    yaw = math.radians(float(attitude.get("yaw_deg", 0.0)))
    return _body_to_world_ned(
        (CAMERA_MOUNT_X_M, 0.0, -CAMERA_MOUNT_Z_DOWN_M),
        roll_rad=roll,
        pitch_rad=pitch,
        yaw_rad=yaw,
    )


def _depth_metres(array: np.ndarray, info: dict[str, Any]) -> np.ndarray:
    encoding = info.get("encoding")
    if not isinstance(encoding, str):
        raise ValueError("depth_camera_info.encoding is required (UINT16_MM or FLOAT32_M)")
    encoding = encoding.strip().upper()
    if encoding == "UINT16_MM":
        if array.dtype.kind in "ui":
            return np.asarray(array, dtype=np.float32) * 0.001
        # Recorded Gazebo artifacts are decoded by the recorder into float32
        # metres while retaining the transport encoding in camera_info.
        if array.dtype.kind == "f":
            finite = np.asarray(array, dtype=np.float32)
            if np.isfinite(finite).any():
                return finite
        raise ValueError("UINT16_MM depth must be integer millimetres or decoded float32 metres")
    if encoding == "FLOAT32_M":
        if array.dtype.kind != "f":
            raise ValueError("FLOAT32_M depth must be a floating-point array")
        return np.asarray(array, dtype=np.float32)
    raise ValueError(f"unsupported depth encoding {encoding!r}")


def _nearest_static_cell(
    north: float, east: float, obstacles: tuple[SDFObstacle, ...], margin_m: float
) -> bool:
    for obstacle in obstacles:
        dn, de = north - obstacle.north_m, east - obstacle.east_m
        c, s = math.cos(obstacle.yaw_rad), math.sin(obstacle.yaw_rad)
        local_e, local_n = c * de + s * dn, -s * de + c * dn
        north_ok = abs(local_n) <= obstacle.size_north_m / 2.0 + margin_m
        east_ok = abs(local_e) <= obstacle.size_east_m / 2.0 + margin_m
        if north_ok and east_ok:
            return True
    return False


def _segment_hits_sdf(
    start: PoseNed,
    end: PoseNed,
    altitude_up: float,
    obstacle: SDFObstacle,
    margin_m: float,
) -> bool:
    """Exact oriented-box slab test used for synthetic leg truth."""
    if obstacle.top_m <= altitude_up - 2.0:
        return False
    c, s = math.cos(obstacle.yaw_rad), math.sin(obstacle.yaw_rad)

    def local(point: PoseNed) -> tuple[float, float]:
        dn = point[0] - obstacle.north_m
        de = point[1] - obstacle.east_m
        return c * de + s * dn, -s * de + c * dn

    a, b = local(start), local(end)
    direction = (b[0] - a[0], b[1] - a[1])
    bounds = (
        (-obstacle.size_east_m / 2.0 - margin_m, obstacle.size_east_m / 2.0 + margin_m),
        (-obstacle.size_north_m / 2.0 - margin_m, obstacle.size_north_m / 2.0 + margin_m),
    )
    lower, upper = 0.0, 1.0
    for axis, (lo, hi) in enumerate(bounds):
        if abs(direction[axis]) <= 1e-12:
            if a[axis] < lo or a[axis] > hi:
                return False
            continue
        t1 = (lo - a[axis]) / direction[axis]
        t2 = (hi - a[axis]) / direction[axis]
        lower, upper = max(lower, min(t1, t2)), min(upper, max(t1, t2))
        if lower > upper:
            return False
    return True


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


def _synthetic_legs(
    source: DepthGridSource,
    obstacles: tuple[SDFObstacle, ...],
    *,
    margin_m: float,
) -> dict[str, int]:
    """Probe crossing, grazing and clear legs at several obstacle heights."""
    result = {"TP": 0, "FP": 0, "FN": 0, "near_misses": 0}
    for obstacle in obstacles:
        c, s = math.cos(obstacle.yaw_rad), math.sin(obstacle.yaw_rad)
        half = max(obstacle.size_east_m, obstacle.size_north_m) / 2 + margin_m
        direction = (c, s)
        for altitude_up in (obstacle.top_m - 1.0, obstacle.top_m + 1.0, obstacle.top_m + 8.0):
            start = (
                obstacle.north_m - direction[0] * (half + 8),
                obstacle.east_m - direction[1] * (half + 8),
                -altitude_up,
            )
            end = (
                obstacle.north_m + direction[0] * (half + 8),
                obstacle.east_m + direction[1] * (half + 8),
                -altitude_up,
            )
            truth = _segment_hits_sdf(start, end, altitude_up, obstacle, margin_m)
            observed = not source.segment_clear(start, end)
            if truth and observed:
                result["TP"] += 1
            elif truth and not observed:
                result["FN"] += 1
                result["near_misses"] += 1
            elif not truth and observed:
                result["FP"] += 1
        # A parallel clear leg outside the footprint is a negative control.
        clear_start = (obstacle.north_m + half + 6.0, obstacle.east_m, -max(obstacle.top_m, 10.0))
        clear_end = (clear_start[0] + 6.0, clear_start[1], clear_start[2])
        if source.segment_clear(clear_start, clear_end) is False:
            result["FP"] += 1
    return result


def evaluate_episode(
    episode: Path,
    *,
    resolution_m: float = 0.5,
    grid_cells: int = 160,
    max_age_s: float = 0.5,
    static_margin_m: float = 3.0,
) -> dict[str, Any]:
    metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    world = Path(str(metadata.get("world_sdf", "")))
    if not world.is_file():
        candidate = ROOT / "simulation" / "gazebo" / "worlds" / world.name
        world = candidate if candidate.is_file() else world
    obstacles = load_sdf_obstacles(world)
    source = DepthGridSource(
        cells=grid_cells,
        resolution_m=resolution_m,
        max_age_s=max_age_s,
        ground_model=FlatGround(0.0),
    )
    records = list(_records(episode))
    detected: set[tuple[int, int]] = set()
    truth: set[tuple[int, int]] = set()
    timings: list[float] = []
    query_timings: list[float] = []
    dropped = stale = out_of_order = invalid_frames = 0
    synthetic = {"TP": 0, "FP": 0, "FN": 0, "near_misses": 0}
    frames = valid_points = compared = same = blocked_static = blocked_depth = 0
    true_positive = false_positive = false_negative = near_miss = late_block = 0
    for record_index, record in enumerate(records):
        before = record.get("obs_before", {})
        pose = _pose(before)
        intrinsics = _intrinsics(before)
        depth_path = _depth_path(episode, before)
        frame_index = frames
        if record_index % 29 == 0 and record_index > 0:
            dropped += 1
            continue
        if pose is not None and intrinsics is not None and depth_path is not None:
            # The recorder stores decoded metric float32 arrays even when the
            # camera metadata retains the transport encoding (UINT16_MM).
            # Decode that representation here, then pass the source an
            # explicit physical encoding so it never has to infer units.
            depth = _depth_metres(
                np.load(depth_path, allow_pickle=False), before["depth_camera_info"]
            )
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
                timestamp_unit="ns",
                encoding="FLOAT32_M",
                camera_rotation=_rotation(before),
                camera_offset_ned=_camera_offset_ned(before),
            )
            timings.append((time.perf_counter_ns() - started) / 1e6)
            frames += 1
            if frame_index % 31 == 0:
                saved_receipt = source._received_monotonic
                source._received_monotonic -= max_age_s + 1.0
                stale += 1
                source.segment_clear(pose, pose)
                source._received_monotonic = saved_receipt
            # Force one deterministic old-stamp delivery per episode once a
            # second valid frame exists, so the evaluator exercises ordering
            # protection even for short episodes.
            if frames == 2:
                out_of_order += 1
                source.update(
                    depth,
                    pose,
                    {
                        "fx": intrinsics["fx_px"],
                        "fy": intrinsics["fy_px"],
                        "cx": intrinsics["cx_px"],
                        "cy": intrinsics["cy_px"],
                    },
                    timestamp=1.0,
                    timestamp_unit="s",
                    encoding="FLOAT32_M",
                )
            detected.update(_grid_cells(source))
            truth.update(
                _static_cells(_all_grid_cells(source), obstacles, resolution_m, static_margin_m)
            )
        elif any(value is not None for value in (pose, intrinsics, depth_path)):
            invalid_frames += 1
        after = _pose(record.get("obs_after", {}))
        if pose is None or after is None or record.get("action", {}).get("kind") != "translate":
            continue
        static_clear = segment_clear_of_obstacles(
            pose, after, max(0.0, -pose[2]), obstacles, margin_m=static_margin_m
        )
        query_started = time.perf_counter_ns()
        depth_clear = source.segment_clear(pose, after)
        query_timings.append((time.perf_counter_ns() - query_started) / 1e6)
        compared += 1
        blocked_static += not static_clear
        blocked_depth += not depth_clear
        same += static_clear == depth_clear
        if not static_clear and not depth_clear:
            true_positive += 1
        elif static_clear and not depth_clear:
            false_positive += 1
        elif not static_clear and depth_clear:
            false_negative += 1
            near_miss += 1
        if not static_clear and depth_clear:
            late_block += 1
    synthetic = _synthetic_legs(source, obstacles, margin_m=static_margin_m)
    tp = len(detected & truth)
    fp_cells = len(detected - truth)
    fn_cells = len(truth - detected)
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
        "occupied_cell_f1": (
            2 * tp / (2 * tp + fp_cells + fn_cells)
            if (2 * tp + fp_cells + fn_cells)
            else 0.0
        ),
        "occupied_cell_confusion": {"TP": tp, "FP": fp_cells, "FN": fn_cells},
        "latency_ms_per_depth_frame": {
            "mean": statistics.mean(timings) if timings else None,
            "p50": statistics.median(timings) if timings else None,
            "p95": float(np.percentile(timings, 95)) if timings else None,
        },
        "legs_compared": compared,
        "static_blocked_legs": blocked_static,
        "depth_blocked_legs": blocked_depth,
        "same_leg_decision_rate": same / compared if compared else None,
        "leg_confusion": {
            "TP": true_positive,
            "FP": false_positive,
            "FN": false_negative,
            "near_misses": near_miss,
            "late_blocks": late_block,
        },
        "ingestion_latency_ms": {
            "mean": statistics.mean(timings) if timings else None,
            "p95": float(np.percentile(timings, 95)) if timings else None,
        },
        "query_latency_ms": {
            "mean": statistics.mean(query_timings) if query_timings else None,
            "p95": float(np.percentile(query_timings, 95)) if query_timings else None,
        },
        "synthetic_leg_confusion": synthetic,
        "data_quality": {
            "dropped_frames_injected": dropped,
            "stale_frames_injected": stale,
            "out_of_order_frames_injected": out_of_order,
            "invalid_frames_skipped": invalid_frames,
        },
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
    if not episodes:
        parser.error(
            f"no valid episodes found below {args.dataset}; expected files matching **/steps.jsonl"
        )
    episodes = episodes[: args.max_episodes] if args.max_episodes > 0 else episodes
    results = []
    invalid: list[str] = []
    for ep in episodes:
        if not (ep / "episode.json").is_file():
            invalid.append(f"{ep}: missing episode.json")
            continue
        try:
            results.append(
                evaluate_episode(ep, resolution_m=args.resolution_m, grid_cells=args.grid_cells)
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            invalid.append(f"{ep}: {exc}")
    if not results:
        parser.error("no valid episodes: " + "; ".join(invalid[:3]))
    payload = {"episodes": results, "episode_count": len(results), "invalid_episodes": invalid}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

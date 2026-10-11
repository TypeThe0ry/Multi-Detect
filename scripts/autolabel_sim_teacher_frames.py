#!/usr/bin/env python3
# ruff: noqa: E501
"""Create YOLO labels for SIM teacher RGB frames from privileged geometry.

The truth path is deliberately an explicit command-line input.  This makes it
hard to accidentally train from evaluator labels while still allowing a
reproducible offline conversion.  Truth can be generated-world metric pose
(``target.pose``) or GPS position (converted with ``--origin``).  The camera
model follows the checked-in ``x500_mono_cam`` convention: vehicle forward is
the camera optical +X axis with a +0.18 rad downward pitch.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.sim_teacher.policies import load_sdf_obstacles  # noqa: E402
from multidetect.simulator.camera_projection import (  # noqa: E402
    project_target_bbox,
    visible_fraction,
)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _angle_delta(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sensor_path(episode: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observation rgb_path is missing")
    relative = Path(value.replace("\\", os.sep).replace("/", os.sep))
    return relative if relative.is_absolute() else episode / relative


def _target_pose(
    truth: dict[str, Any], *, origin: tuple[float, float, float]
) -> tuple[float, float, float]:
    targets = truth.get("targets")
    if not isinstance(targets, list) or not targets or not isinstance(targets[0], dict):
        raise ValueError("truth must contain a nonempty targets list")
    target = targets[0]
    pose = target.get("pose")
    if isinstance(pose, dict):
        east = _finite(pose.get("x_east_m", pose.get("east_m")))
        north = _finite(pose.get("y_north_m", pose.get("north_m")))
        up = _finite(pose.get("z_up_m", pose.get("up_m")))
        if east is not None and north is not None and up is not None:
            return north, east, up
    position = target.get("position")
    if not isinstance(position, dict):
        raise ValueError("target has neither metric pose nor GPS position")
    lat = _finite(position.get("latitude_deg"))
    lon = _finite(position.get("longitude_deg"))
    alt = _finite(position.get("altitude_m"))
    if lat is None or lon is None:
        raise ValueError("GPS target requires latitude_deg and longitude_deg")
    lat0, lon0, alt0 = origin
    # Equirectangular approximation is sub-millimetre at these SIM distances.
    north = (lat - lat0) * 111_132.0
    east = (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0))
    altitude_reference = str(position.get("altitude_reference", "")).casefold()
    if alt is None:
        up = 0.0
    elif altitude_reference in {"world", "local", "relative", "relative_home", "agl"}:
        # Generated truth altitude is relative to the Gazebo WORLD origin.
        up = alt
    else:
        up = alt - alt0
    return north, east, up


def _target_yaw(truth: dict[str, Any]) -> float:
    targets = truth.get("targets")
    target = (
        targets[0] if isinstance(targets, list) and targets and isinstance(targets[0], dict) else {}
    )
    pose = target.get("pose") if isinstance(target, dict) else None
    yaw = _finite(pose.get("yaw_rad")) if isinstance(pose, dict) else None
    return 0.0 if yaw is None else yaw


def _target_record(truth: dict[str, Any]) -> dict[str, Any]:
    targets = truth.get("targets")
    if not isinstance(targets, list) or not targets or not isinstance(targets[0], dict):
        raise ValueError("truth must contain a nonempty targets list")
    return targets[0]


def _target_geometry(
    truth: dict[str, Any], default_size: tuple[float, float, float]
) -> tuple[tuple[float, float, float], float, float | None]:
    target = _target_record(truth)
    raw_size = target.get("size_m", target.get("measured_size_m"))
    try:
        size = tuple(float(value) for value in raw_size) if raw_size is not None else default_size
    except (TypeError, ValueError):
        size = default_size
    if len(size) != 3 or any(value <= 0.0 or not math.isfinite(value) for value in size):
        size = default_size
    pose = target.get("pose")
    pose = pose if isinstance(pose, dict) else {}
    yaw = _finite(pose.get("yaw_rad"))
    offset = _finite(
        target.get("bottom_offset_m", target.get("mesh_bottom_offset_m", target.get("origin_bottom_z_m")))
    )
    return size, 0.0 if yaw is None else yaw, offset


def _label_line(
    class_id: int, bbox: tuple[float, float, float, float], image_size: tuple[int, int]
) -> str:
    x1, y1, x2, y2 = bbox
    width, height = image_size
    xc, yc = (x1 + x2) / 2.0 / width, (y1 + y2) / 2.0 / height
    bw, bh = (x2 - x1) / width, (y2 - y1) / height
    values = (xc, yc, bw, bh)
    if any(not math.isfinite(value) or value <= 0.0 for value in values[2:]):
        raise ValueError("projected YOLO box is invalid")
    return f"{class_id} " + " ".join(f"{value:.8f}" for value in values)


def autolabel_episode(
    episode: Path,
    truth: dict[str, Any],
    output: Path,
    *,
    class_id: int = 0,
    origin: tuple[float, float, float] = (1.352100, 103.819800, 18.0),
    target_size_m: tuple[float, float, float] = (3.0, 1.8, 1.5),
    visual_directory: Path | None = None,
    overwrite: bool = True,
    world_sdf: Path | None = None,
    min_visible_fraction: float = 0.3,
) -> int:
    target = _target_pose(truth, origin=origin)
    target_size_m, target_yaw, target_bottom_offset_m = _target_geometry(truth, target_size_m)
    steps = []
    with (episode / "steps.jsonl").open("r", encoding="utf-8") as stream:
        steps = [json.loads(line) for line in stream if line.strip()]
    if not overwrite and output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=True)
    if visual_directory is not None:
        visual_directory.mkdir(parents=True, exist_ok=True)
    if min_visible_fraction < 0.0 or min_visible_fraction > 1.0:
        raise ValueError("min_visible_fraction must be in [0, 1]")
    if world_sdf is None:
        scenario = str(truth.get("scenario_id", ""))
        candidate = ROOT / "simulation/gazebo/worlds" / f"{scenario}.sdf"
        world_sdf = candidate if candidate.is_file() else None
    obstacles = load_sdf_obstacles(world_sdf) if world_sdf is not None else ()
    count = 0
    dropped_occluded = 0
    label_metadata: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for observation_name in ("before", "after"):
            observation = step.get(f"obs_{observation_name}")
            if not isinstance(observation, dict):
                continue
            rgb_path = _sensor_path(episode, observation.get("rgb_path"))
            image = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(rgb_path)
            height, width = image.shape[:2]
            bbox = project_target_bbox(
                target_ned=target,
                pose=observation,
                camera_info=observation.get("camera_info", {}),
                image_size=(width, height),
                target_size_m=target_size_m,
                target_yaw_rad=target_yaw,
                target_bottom_offset_m=target_bottom_offset_m,
            )
            fraction = visible_fraction(
                pose=observation,
                target_ned=target,
                target_size_m=target_size_m,
                target_yaw_rad=target_yaw,
                obstacles=obstacles,
                target_bottom_offset_m=target_bottom_offset_m,
            )
            occluded = min_visible_fraction > 0.0 and fraction < min_visible_fraction
            if occluded:
                dropped_occluded += 1
                bbox = None
            name = f"{int(step.get('step_index', count)):06d}_{observation_name}"
            label_path = output / f"{name}.txt"
            encoded = "" if bbox is None else _label_line(class_id, bbox, (width, height)) + "\n"
            label_path.write_text(encoded, encoding="utf-8", newline="\n")
            label_metadata.append(
                {
                    "file": label_path.name,
                    "visible_fraction": fraction,
                    "occluded": occluded,
                    "has_label": bbox is not None,
                }
            )
            if visual_directory is not None:
                canvas = image.copy()
                if bbox is not None:
                    x1, y1, x2, y2 = (round(value) for value in bbox)
                    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.imwrite(str(visual_directory / f"{name}.png"), canvas)
            count += 1
    (output / "manifest.json").write_text(
        json.dumps({"frames": count, "labels": sum(bool(item["has_label"]) for item in label_metadata),
                    "dropped_occluded": dropped_occluded, "min_visible_fraction": min_visible_fraction,
                    "labels_metadata": label_metadata}, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="YOLO labels directory")
    parser.add_argument("--class-id", type=int, default=0)
    parser.add_argument(
        "--target-size-m",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        default=(3.0, 1.8, 1.5),
        help="target box dimensions in model metres (default: generated vehicle 3 1.8 1.5)",
    )
    parser.add_argument(
        "--origin",
        nargs=3,
        type=float,
        metavar=("LAT", "LON", "ALT"),
        default=(1.352100, 103.819800, 18.0),
    )
    parser.add_argument("--visual-directory", type=Path)
    parser.add_argument("--world-sdf", type=Path)
    parser.add_argument("--min-visible-fraction", type=float, default=0.3)
    parser.add_argument("--no-overwrite", action="store_true")
    args = parser.parse_args(argv)
    count = autolabel_episode(
        args.episode,
        _load(args.truth),
        args.output,
        class_id=args.class_id,
        origin=tuple(args.origin),
        target_size_m=tuple(args.target_size_m),
        visual_directory=args.visual_directory,
        overwrite=not args.no_overwrite,
        world_sdf=args.world_sdf,
        min_visible_fraction=args.min_visible_fraction,
    )
    print(
        json.dumps(
            {"episode": str(args.episode), "frames": count, "labels": str(args.output)},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

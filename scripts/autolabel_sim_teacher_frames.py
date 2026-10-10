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
from pathlib import Path
from typing import Any

import cv2


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
    return north, east, (alt if alt is not None else alt0) - alt0


def _target_yaw(truth: dict[str, Any]) -> float:
    targets = truth.get("targets")
    target = (
        targets[0] if isinstance(targets, list) and targets and isinstance(targets[0], dict) else {}
    )
    pose = target.get("pose") if isinstance(target, dict) else None
    yaw = _finite(pose.get("yaw_rad")) if isinstance(pose, dict) else None
    return 0.0 if yaw is None else yaw


def project_target_bbox(
    *,
    target_ned: tuple[float, float, float],
    pose: dict[str, Any],
    camera_info: dict[str, Any],
    image_size: tuple[int, int],
    target_size_m: tuple[float, float, float] = (3.0, 1.8, 1.5),
    camera_pitch_rad: float = 0.18,
    target_yaw_rad: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Project an upright vehicle box; return clipped pixel ``xyxy`` or None."""
    if any(size <= 0 or not math.isfinite(size) for size in target_size_m):
        raise ValueError("target_size_m must contain three finite positive dimensions")
    north, east, up = target_ned
    position = pose.get("local_ned_m")
    if not isinstance(position, (list, tuple)) or len(position) < 3:
        return None
    pn, pe, p_down = (_finite(position[i]) for i in range(3))
    yaw_deg = (
        _finite((pose.get("attitude") or {}).get("yaw_deg"))
        if isinstance(pose.get("attitude"), dict)
        else None
    )
    if pn is None or pe is None or p_down is None or yaw_deg is None:
        return None
    fx = _finite(camera_info.get("fx", camera_info.get("fx_px")))
    fy = _finite(camera_info.get("fy", camera_info.get("fy_px")))
    cx = _finite(camera_info.get("cx", camera_info.get("cx_px")))
    cy = _finite(camera_info.get("cy", camera_info.get("cy_px")))
    if fx is None or fy is None or cx is None or cy is None:
        return None
    width, height = image_size
    yaw = math.radians(yaw_deg)
    dn, de, dz_up = north - pn, east - pe, up + p_down
    # NED vehicle coordinates: x forward, y right, z down. Apply camera pitch
    # around vehicle-right to account for the x500 downward-tilted camera.
    cp, sp = math.cos(camera_pitch_rad), math.sin(camera_pitch_rad)
    sx, sy, sz = target_size_m
    corners = []
    for dx in (-sx / 2.0, sx / 2.0):
        for dy in (-sy / 2.0, sy / 2.0):
            for dz in (-sz / 2.0, sz / 2.0):
                # SDF dimensions are local x/east and y/north extents. Rotate
                # those extents by the target model yaw before projection.
                offset_east = math.cos(target_yaw_rad) * dx - math.sin(target_yaw_rad) * dy
                offset_north = math.sin(target_yaw_rad) * dx + math.cos(target_yaw_rad) * dy
                corner_north = dn + offset_north
                corner_east = de + offset_east
                xx = corner_north * math.cos(yaw) + corner_east * math.sin(yaw)
                yy = -corner_north * math.sin(yaw) + corner_east * math.cos(yaw)
                zz = -(dz_up + dz)
                xx, zz = cp * xx - sp * zz, sp * xx + cp * zz
                if xx <= 1e-6:
                    continue
                corners.append((cx + fx * yy / xx, cy + fy * zz / xx))
    if not corners:
        return None
    x1 = max(0.0, min(point[0] for point in corners))
    y1 = max(0.0, min(point[1] for point in corners))
    x2 = min(float(width), max(point[0] for point in corners))
    y2 = min(float(height), max(point[1] for point in corners))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


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
) -> int:
    target = _target_pose(truth, origin=origin)
    target_yaw = _target_yaw(truth)
    steps = []
    with (episode / "steps.jsonl").open("r", encoding="utf-8") as stream:
        steps = [json.loads(line) for line in stream if line.strip()]
    if not overwrite and output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=True)
    if visual_directory is not None:
        visual_directory.mkdir(parents=True, exist_ok=True)
    count = 0
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
            )
            name = f"{int(step.get('step_index', count)):06d}_{observation_name}"
            label_path = output / f"{name}.txt"
            encoded = "" if bbox is None else _label_line(class_id, bbox, (width, height)) + "\n"
            label_path.write_text(encoded, encoding="utf-8", newline="\n")
            if visual_directory is not None:
                canvas = image.copy()
                if bbox is not None:
                    x1, y1, x2, y2 = (round(value) for value in bbox)
                    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.imwrite(str(visual_directory / f"{name}.png"), canvas)
            count += 1
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

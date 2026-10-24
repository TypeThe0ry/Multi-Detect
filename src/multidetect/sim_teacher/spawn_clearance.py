"""Static spawn-point and vertical climb clearance checks for generated worlds."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _model_footprints(world_path: str | Path) -> list[dict[str, float | str]]:
    """Return horizontal collision footprints for SDF models.

    Coordinates use the repository ENU convention (x=east, y=north, z=up).
    PX4 and ground-plane models are intentionally ignored; all authored scene
    models, including targets and confusers, remain blockers.
    """
    try:
        root = ET.parse(world_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot parse world SDF {world_path}: {exc}") from exc
    world = root.find("world")
    if world is None:
        raise ValueError(f"world SDF has no <world>: {world_path}")
    result: list[dict[str, float | str]] = []
    for model in world.findall("model"):
        name = model.get("name", "")
        if name == "ground_plane" or name.startswith(("px4_", "x500_", "rc_cessna")):
            continue
        pose = (model.findtext("pose") or "0 0 0 0 0 0").split()
        try:
            x, y, z = (float(pose[i]) for i in range(3))
        except (ValueError, IndexError):
            continue
        collision = model.find(".//collision")
        geometry = collision.find("geometry") if collision is not None else None
        if geometry is None:
            continue
        box = geometry.find("box/size")
        cylinder = geometry.find("cylinder")
        try:
            if box is not None and box.text:
                sx, sy, sz = (float(v) for v in box.text.split()[:3])
                radius = math.hypot(sx, sy) / 2.0
            elif cylinder is not None:
                radius = float(cylinder.findtext("radius", "0"))
                sz = float(cylinder.findtext("length", "0"))
            else:
                continue
            if radius <= 0 or sz <= 0:
                continue
        except (TypeError, ValueError):
            continue
        result.append({"name": name, "east_m": x, "north_m": y, "radius_m": radius, "top_m": z + sz / 2.0})
    return result


def spawn_clearance_report(
    world_path: str | Path,
    spawn_pose: dict[str, Any],
    *,
    safety_radius_m: float = 15.0,
    climb_altitude_m: float | None = None,
    margin_m: float = 1.0,
    route_points: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Check spawn exclusion and vertical climb corridor, returning a report.

    ``safety_radius_m`` is measured from the vehicle spawn center.  An object
    intersects the exclusion if its collision footprint reaches that radius.
    Objects intersecting the vertical climb path are reported separately when
    their top is above the spawn and below the requested climb altitude.
    """
    radius = float(safety_radius_m)
    margin = float(margin_m)
    if radius <= 0 or margin < 0:
        raise ValueError("safety_radius_m must be positive and margin_m non-negative")
    east = float(spawn_pose.get("x_east_m", 0.0))
    north = float(spawn_pose.get("y_north_m", 0.0))
    spawn_z = float(spawn_pose.get("z_up_m", 0.0))
    climb_to = spawn_z if climb_altitude_m is None else float(climb_altitude_m)
    blockers: list[dict[str, Any]] = []
    climb_blockers: list[dict[str, Any]] = []
    route_blockers: list[dict[str, Any]] = []
    for item in _model_footprints(world_path):
        distance = math.hypot(float(item["east_m"]) - east, float(item["north_m"]) - north)
        edge_distance = distance - float(item["radius_m"])
        if edge_distance <= radius + margin and float(item["top_m"]) > spawn_z + margin:
            hit = {**item, "center_distance_m": round(distance, 3), "edge_distance_m": round(edge_distance, 3)}
            blockers.append(hit)
            if float(item["top_m"]) < climb_to - margin:
                climb_blockers.append(hit)
    route = []
    for point in route_points or ():
        route.append((float(point.get("x_east_m", 0.0)), float(point.get("y_north_m", 0.0))))
    for item in _model_footprints(world_path):
        # Route points are flown at the resolved observation altitude; a ground
        # footprint below that altitude cannot collide with the vehicle.
        if float(item["top_m"]) <= climb_to - margin:
            continue
        samples = list(route)
        for start, end in zip(route, route[1:]):
            length = math.dist(start, end)
            samples.extend(
                ((start[0] + (end[0] - start[0]) * i / max(1, int(length / 2.0)),
                  start[1] + (end[1] - start[1]) * i / max(1, int(length / 2.0)))
                 for i in range(max(1, int(length / 2.0)) + 1)
            )
            )
        if any(math.dist((float(item["east_m"]), float(item["north_m"])), p) - float(item["radius_m"]) <= margin for p in samples):
            route_blockers.append(item)
    return {
        "world_path": str(Path(world_path)),
        "spawn": {"x_east_m": east, "y_north_m": north, "z_up_m": spawn_z},
        "safety_radius_m": radius,
        "margin_m": margin,
        "climb_altitude_m": climb_to,
        "clear": not blockers and not climb_blockers and not route_blockers,
        "blockers": blockers,
        "climb_blockers": climb_blockers,
        "route_blockers": route_blockers,
    }


def require_spawn_clearance(*args, **kwargs) -> dict[str, Any]:
    """Raise ``ValueError`` when the world cannot safely launch."""
    report = spawn_clearance_report(*args, **kwargs)
    if not report["clear"]:
        names = ", ".join(str(x["name"]) for x in report["blockers"])
        raise ValueError(f"spawn point lacks clearance: {names or 'vertical climb corridor'}")
    return report


__all__ = ["spawn_clearance_report", "require_spawn_clearance"]

#!/usr/bin/env python3
"""Offline sanity checks for the generated delta tailsitter LiftDrag surfaces."""

from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Surface:
    name: str
    area: float
    a0: float
    cla: float
    cda: float
    alpha_stall: float
    cla_stall: float
    cda_stall: float
    upward_x: float
    upward: tuple[float, float, float]
    cp: tuple[float, float, float]


TRIM_SPEEDS_MPS = (12.0, 15.0, 19.0, 25.0)
TRIM_ALPHA_DEG = tuple(float(value) for value in range(-5, 21))


def surfaces_from_sdf(path: str | Path) -> tuple[Surface, ...]:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.parse(path, parser=parser).getroot()
    result = []
    for plugin in root.findall(".//plugin[@name='gz::sim::systems::LiftDrag']"):
        upward = tuple(float(x) for x in plugin.findtext("upward", "0 0 0").split())
        comment = next((child.text for child in plugin if child.tag is ET.Comment), "")
        result.append(
            Surface(
                name=comment,
                area=float(plugin.findtext("area", "0")),
                a0=float(plugin.findtext("a0", "0")),
                cla=float(plugin.findtext("cla", "0")),
                cda=float(plugin.findtext("cda", "0")),
                alpha_stall=float(plugin.findtext("alpha_stall", str(math.pi / 2))),
                cla_stall=float(plugin.findtext("cla_stall", "0")),
                cda_stall=float(plugin.findtext("cda_stall", "1")),
                upward_x=upward[0],
                upward=upward,
                cp=tuple(float(x) for x in plugin.findtext("cp", "0 0 0").split()),
            )
        )
    return tuple(result)


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return tuple(value / length for value in vector)


def _surface_wrench(
    surface: Surface,
    speed_mps: float,
    alpha_rad: float,
    *,
    rho: float,
    roll_rate_z_rad_s: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    float,
    float,
]:
    forward = (0.0, 0.0, 1.0)
    upward = _unit(surface.upward)
    spanwise = _unit(_cross(forward, upward))
    velocity = (
        speed_mps * math.sin(alpha_rad) - roll_rate_z_rad_s * surface.cp[1],
        roll_rate_z_rad_s * surface.cp[0],
        speed_mps * math.cos(alpha_rad),
    )
    velocity_length = math.sqrt(_dot(velocity, velocity))
    if velocity_length <= 1e-9 or spanwise == (0.0, 0.0, 0.0):
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, 0.0

    sweep_sine = _dot(spanwise, _unit(velocity))
    cos2_sweep = max(0.0, 1.0 - sweep_sine * sweep_sine)
    velocity_plane = tuple(
        velocity[index] - _dot(velocity, spanwise) * spanwise[index]
        for index in range(3)
    )
    plane_speed = math.sqrt(_dot(velocity_plane, velocity_plane))
    if plane_speed <= 1e-9:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, 0.0

    drag_direction = tuple(-value / plane_speed for value in velocity_plane)
    lift_direction = _unit(_cross(spanwise, velocity_plane))
    cos_alpha = max(-1.0, min(1.0, _dot(lift_direction, upward)))
    angle = surface.a0 - math.acos(cos_alpha)
    if _dot(lift_direction, forward) >= 0.0:
        angle = surface.a0 + math.acos(cos_alpha)
    while abs(angle) > math.pi / 2:
        angle += -math.pi if angle > 0 else math.pi

    q = 0.5 * rho * plane_speed * plane_speed
    if angle > surface.alpha_stall:
        cl = max(
            0.0,
            surface.cla * surface.alpha_stall
            + surface.cla_stall * (angle - surface.alpha_stall),
        )
        cd = (
            surface.cda * surface.alpha_stall
            + surface.cda_stall * (angle - surface.alpha_stall)
        )
    elif angle < -surface.alpha_stall:
        cl = min(
            0.0,
            -surface.cla * surface.alpha_stall
            + surface.cla_stall * (angle + surface.alpha_stall),
        )
        cd = (
            -surface.cda * surface.alpha_stall
            + surface.cda_stall * (angle + surface.alpha_stall)
        )
    else:
        cl = surface.cla * angle
        cd = surface.cda * angle
    cl *= cos2_sweep
    cd = abs(cd * cos2_sweep)
    lift_force = tuple(q * surface.area * cl * lift_direction[i] for i in range(3))
    drag_force = tuple(q * surface.area * cd * drag_direction[i] for i in range(3))
    force = tuple(lift_force[i] + drag_force[i] for i in range(3))
    moment = _cross(surface.cp, force)
    return force, moment, -lift_force[0], q * surface.area * cd


def aerodynamic_force(
    surfaces: tuple[Surface, ...],
    speed_mps: float,
    alpha_rad: float,
    *,
    rho: float = 1.15,
    include_advance_drag: bool = False,
) -> tuple[float, float]:
    """Return link-axis lift and drag magnitudes using Gazebo LiftDrag geometry."""
    if speed_mps <= 1e-6:
        return 0.0, 0.0
    lift = drag = 0.0
    for surface in surfaces:
        if not include_advance_drag and surface.name.startswith("propeller_advance_drag_"):
            continue
        _, _, part_lift, part_drag = _surface_wrench(
            surface,
            speed_mps,
            alpha_rad,
            rho=rho,
            roll_rate_z_rad_s=0.0,
        )
        lift += part_lift
        drag += part_drag
    return lift, drag


def aerodynamic_force_moment(
    surfaces: tuple[Surface, ...],
    speed_mps: float,
    alpha_rad: float,
    *,
    rho: float = 1.15,
    roll_rate_z_rad_s: float = 0.0,
    include_advance_drag: bool = False,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return the summed force and r×F moment in base_link coordinates."""
    if speed_mps <= 1e-6:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    force = [0.0, 0.0, 0.0]
    moment = [0.0, 0.0, 0.0]
    for surface in surfaces:
        if not include_advance_drag and surface.name.startswith("propeller_advance_drag_"):
            continue
        part_force, part_moment, _, _ = _surface_wrench(
            surface,
            speed_mps,
            alpha_rad,
            rho=rho,
            roll_rate_z_rad_s=roll_rate_z_rad_s,
        )
        for index in range(3):
            force[index] += part_force[index]
            moment[index] += part_moment[index]
    return tuple(force), tuple(moment)


def pitching_moment_slope(
    surfaces: tuple[Surface, ...], speed_mps: float, alpha_rad: float, *, rho: float = 1.15
) -> float:
    """Return dMy/dalpha using a symmetric finite difference in radian units."""
    delta = 1e-5
    lower = aerodynamic_force_moment(surfaces, speed_mps, alpha_rad - delta, rho=rho)[1][1]
    upper = aerodynamic_force_moment(surfaces, speed_mps, alpha_rad + delta, rho=rho)[1][1]
    return (upper - lower) / (2.0 * delta)


def trim_scan(
    surfaces: tuple[Surface, ...],
    *,
    speeds_mps: tuple[float, ...] = TRIM_SPEEDS_MPS,
    alpha_deg: tuple[float, ...] = TRIM_ALPHA_DEG,
) -> tuple[dict[str, float], ...]:
    """Build the requested speed/angle force and pitch-moment table."""
    rows: list[dict[str, float]] = []
    for speed in speeds_mps:
        for angle in alpha_deg:
            force, moment = aerodynamic_force_moment(surfaces, speed, math.radians(angle))
            lift, drag = aerodynamic_force(surfaces, speed, math.radians(angle))
            rows.append(
                {
                    "speed_mps": float(speed),
                    "alpha_deg": float(angle),
                    "lift_n": float(lift),
                    "drag_n": float(drag),
                    "pitch_moment_nm": float(moment[1]),
                    "pitch_moment_slope_nm_per_rad": pitching_moment_slope(
                        surfaces, speed, math.radians(angle)
                    ),
                }
            )
    return tuple(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdf", type=Path)
    parser.add_argument("--speed", type=float, default=15.0)
    parser.add_argument("--alpha-deg", type=float, default=7.4314663815)
    parser.add_argument(
        "--scan",
        action="store_true",
        help="print the 12/15/19/25 m/s by -5..20 deg trim table",
    )
    args = parser.parse_args(argv)
    surfaces = surfaces_from_sdf(args.sdf)
    lift, drag = aerodynamic_force(surfaces, args.speed, math.radians(args.alpha_deg))
    force, moment = aerodynamic_force_moment(surfaces, args.speed, math.radians(args.alpha_deg))
    print(f"lift_n={lift:.6f} drag_n={drag:.6f} ld={lift / drag if drag else float('inf'):.6f}")
    print(f"force_n={force} moment_nm={moment}")
    if args.scan:
        print("speed_mps alpha_deg lift_n drag_n pitch_moment_nm dMy_dalpha_nm_per_rad")
        for row in trim_scan(surfaces):
            print(
                f"{row['speed_mps']:.0f} {row['alpha_deg']:.0f} "
                f"{row['lift_n']:.6f} {row['drag_n']:.6f} "
                f"{row['pitch_moment_nm']:.6f} "
                f"{row['pitch_moment_slope_nm_per_rad']:.6f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
                upward_x=upward[0],
                upward=upward,
                cp=tuple(float(x) for x in plugin.findtext("cp", "0 0 0").split()),
            )
        )
    return tuple(result)


def aerodynamic_force(
    surfaces: tuple[Surface, ...],
    speed_mps: float,
    alpha_rad: float,
    *,
    rho: float = 1.15,
    include_advance_drag: bool = False,
) -> tuple[float, float]:
    """Return lift and drag magnitudes with finite zero speed and stall bounds."""
    if speed_mps <= 1e-6:
        return 0.0, 0.0
    q = 0.5 * rho * speed_mps * speed_mps
    lift = drag = 0.0
    for surface in surfaces:
        if not include_advance_drag and surface.name.startswith("propeller_advance_drag_"):
            continue
        alpha = alpha_rad + surface.a0
        cl = surface.cla * alpha
        if surface.cla:
            cl = max(-0.8, min(0.8, cl))
        cd = abs(surface.cda * alpha)
        lift += q * surface.area * cl * -surface.upward_x
        drag += q * surface.area * cd
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
    q = 0.5 * rho * speed_mps * speed_mps
    force = [0.0, 0.0, 0.0]
    moment = [0.0, 0.0, 0.0]
    for surface in surfaces:
        if not include_advance_drag and surface.name.startswith("propeller_advance_drag_"):
            continue
        # LiftDrag samples velocity at each CP: v_cp = v_link + omega x r_cp.
        # In forward flight (+Z), a positive z roll rate gives the +y panel
        # negative x flow and therefore a lower local angle of attack.
        flow_alpha = alpha_rad - math.atan2(
            roll_rate_z_rad_s * surface.cp[1], speed_mps
        )
        alpha = flow_alpha + surface.a0
        cl = max(-0.8, min(0.8, surface.cla * alpha)) if surface.cla else 0.0
        cd = abs(surface.cda * alpha)
        lift = q * surface.area * cl
        drag = q * surface.area * cd
        forward = (0.0, 0.0, 1.0)
        lift_direction = tuple(
            surface.upward[i] * math.cos(flow_alpha) + forward[i] * math.sin(flow_alpha)
            for i in range(3)
        )
        drag_direction = tuple(
            -(forward[i] * math.cos(flow_alpha) - surface.upward[i] * math.sin(flow_alpha))
            for i in range(3)
        )
        vector = [lift_direction[i] * lift + drag_direction[i] * drag for i in range(3)]
        for i in range(3):
            force[i] += vector[i]
        moment[0] += surface.cp[1] * vector[2] - surface.cp[2] * vector[1]
        moment[1] += surface.cp[2] * vector[0] - surface.cp[0] * vector[2]
        moment[2] += surface.cp[0] * vector[1] - surface.cp[1] * vector[0]
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
            rows.append(
                {
                    "speed_mps": float(speed),
                    "alpha_deg": float(angle),
                    "lift_n": float(force[0]),
                    "drag_n": float(-force[2]),
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

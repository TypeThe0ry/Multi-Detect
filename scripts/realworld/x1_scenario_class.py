"""Site-scoped distance policy shared by X1 planning and collection."""
from __future__ import annotations

import json
import math
from pathlib import Path

WIDE_AREA = "wide_area"
MOUNTAIN_SHORT = "mountain_short"
WIDE_DISTANCE_M = 4000
MOUNTAIN_DISTANCE_M = 2250
ALPINE_SITE = "rw_alpine_grindelwald_w6km"
ALPINE_WORLD = f"{ALPINE_SITE}__clear_noon"
ALPINE_ORIGIN = (46.615953933, 8.036630765)
ALPINE_LEGACY_CANDIDATE = "valley/5/0.5/140m/+0m/-1"
ALPINE_CANDIDATE = "valley/5/0.5/130m/-10m/-1"
AMSTERDAM_WORLD = "rw_canal_amsterdam_w6km__clear_noon"
FLIGHT_MARGIN_REVISION = "fly-fix-1"
ALPINE_PLANNED_AGL_M = (44.0, 113.0)
ALPINE_TRANSITION_MIN_AGL_M = 60.0
# Extrapolate the incoming Offboard tangent, with a separate position allowance.
# The square is more limiting for a prolonged missed capture near the corner.
AMSTERDAM_RADIAL_RUNOUT_M = 250.0
AMSTERDAM_SQUARE_RUNOUT_M = 150.0
AMSTERDAM_RUNOUT_ALLOWANCE_M = 10.0
CONFIG = Path(__file__).resolve().parents[2] / "configs/simulation/x1-worlds.json"


def configured_class(world_id, spec=None):
    if spec is None:
        spec = json.loads(CONFIG.read_text())
    site = world_id.partition("__")[0].removesuffix("_w6km")
    return spec.get("scenario_classes", {}).get(site, WIDE_AREA)


def distance_gate(world_id, scenario_class):
    if scenario_class == WIDE_AREA:
        return WIDE_DISTANCE_M
    if (scenario_class == MOUNTAIN_SHORT and world_id == ALPINE_WORLD
            and configured_class(world_id) == MOUNTAIN_SHORT):
        return MOUNTAIN_DISTANCE_M
    raise ValueError(f"scenario_class {scenario_class!r} is not authorized for {world_id}")


def verify_route_policy(document, world_id, scenario_class):
    threshold = distance_gate(world_id, scenario_class)
    # Old 4 km bundles predate these fields; never infer a short route.
    if (document.get("scenario_class", WIDE_AREA) != scenario_class
            or document.get("threshold_m", WIDE_DISTANCE_M) != threshold):
        raise ValueError(f"{world_id}: route scenario_class or threshold_m mismatch")
    if scenario_class == MOUNTAIN_SHORT:
        evidence = document.get("wide_area_unreachable")
        bounds = evidence.get("maximum_possible_distance_m", {}) if isinstance(evidence, dict) else {}
        if not (isinstance(bounds, dict) and type(bounds.get("30")) in (int, float)
                and type(bounds.get("40")) in (int, float)
                and 0 <= bounds["40"] <= bounds["30"] < WIDE_DISTANCE_M
                and evidence.get("threshold_m") == WIDE_DISTANCE_M):
            raise ValueError(f"{world_id}: route missing 4 km unreachability evidence")
    return threshold


def projected_fw_runout(points_ne, forward_m):
    """Worst radius and axis excursion along straight continuations of FW legs."""
    max_radius = max_axis = 0.0
    for (pn, pe), (n, e) in zip(points_ne, points_ne[1:]):
        length = math.hypot(n - pn, e - pe)
        if length <= 0:
            raise ValueError("FW runout requires distinct waypoints")
        end_n = n + forward_m * (n - pn) / length
        end_e = e + forward_m * (e - pe) / length
        max_radius = max(max_radius, math.hypot(n, e), math.hypot(end_n, end_e))
        max_axis = max(max_axis, abs(n), abs(e), abs(end_n), abs(end_e))
    return max_radius, max_axis


def amsterdam_runout(points_ne, mission_radius_m, square_half_m):
    radius, _ = projected_fw_runout(points_ne, AMSTERDAM_RADIAL_RUNOUT_M)
    _, axis = projected_fw_runout(points_ne, AMSTERDAM_SQUARE_RUNOUT_M)
    return {
        "radial_runout_m": AMSTERDAM_RADIAL_RUNOUT_M,
        "square_runout_m": AMSTERDAM_SQUARE_RUNOUT_M,
        "position_allowance_m": AMSTERDAM_RUNOUT_ALLOWANCE_M,
        "mission_radius_m": mission_radius_m,
        "radial_headroom_m": mission_radius_m - radius,
        "square_headroom_m": square_half_m - axis,
    }


def has_runout_margin(runout):
    return min(runout["radial_headroom_m"], runout["square_headroom_m"]) >= AMSTERDAM_RUNOUT_ALLOWANCE_M

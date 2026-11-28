"""X1 SIM planner dialect and static admission, not a controller or route generator."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .serialization import require_real

if TYPE_CHECKING:
    from .mission_package import MissionPackage
    from .mission_plan import MissionPlan

X1_CAPABILITY = "airframe:x1"
X1_PROFILE_IDS = frozenset({"delta_tailsitter_gimbal", "delta_tailsitter_gimbal_cam"})
X1_SKILLS = {
    "vtol_takeoff": ({"altitude_agl_m": "x1_agl"}, "climb vertically to the given AGL"),
    "fw_transition": ({}, "transition to fixed wing after takeoff"),
    "fw_goto_waypoint": ({"target_ref": "ref", "altitude_agl_m": "x1_agl"}, "fly to a named POI or home"),
    "loiter_observe": ({"target_ref": "ref", "altitude_agl_m": "x1_agl", "radius_m": "x1_radius",
                       "duration_s": "x1_duration"}, "orbit a named observation point"),
    "find_object_then_return": ({"region": "region", "query": "query", "altitude_agl_m": "x1_agl",
                                 "radius_m": "x1_radius", "duration_s": "x1_duration"},
                                "search a named zone for an object, bounded by time; return via the graph"),
    "gimbal_stare_at": ({"target_ref": "ref", "altitude_agl_m": "x1_agl", "radius_m": "x1_radius",
                         "duration_s": "x1_duration"}, "orbit while looking at the named point with the gimbal"),
    "back_transition": ({}, "return to multicopter mode before nominal recovery"),
}
# Airborne retask grammar. Keep it separate from the nominal prompt and schema.
# The runtime lowerer resolves route_ref to a package route of 2–4 waypoints.
X1_AIRBORNE_SKILLS = {
    "x1_search_leg": ({"route_ref": "x1_route_ref"}, "fly a named package route of 2–4 waypoints"),
    "x1_mc_reposition": ({"target_ref": "x1_target_ref", "altitude_agl_m": "x1_agl"},
                         "move in multicopter mode to a named POI or home"),
}
X1_OBSERVATION = frozenset({"loiter_observe", "find_object_then_return", "gimbal_stare_at"})
X1_PARAM_KEYS = {"x1_agl": "altitude_agl_m", "x1_radius": "observation_radius_m",
                 "x1_duration": "observation_duration_s"}
_CORRIDOR_KEYS = ("north_min_m", "north_max_m", "east_min_m", "east_max_m")
Point = tuple[float, float]
_EXPLICIT_AGL = re.compile(
    r"(?:离地|对地|距地面)\s*(\d+(?:\.\d+)?)\s*米"
    r"|(?<![\w.])(\d+(?:\.\d+)?)\s*(?:m|metres?|meters?)\s*"
    r"(?:agl\b|above (?:the )?(?:ground|terrain)\b|of (?:ground|terrain) clearance\b)", re.I,
)
_ALLOW_CLAMP = re.compile(
    r"(?:超出高度范围时可改用最接近的合法离地高度|if that altitude is outside limits, use the nearest legal AGL)[.。]?$", re.I,
)


@lru_cache(maxsize=1)
def _limits() -> tuple[dict, dict]:
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / "configs/onboard/x1-planner.json").read_text(encoding="utf-8"))
    profile = json.loads((root / config["airframe_profile"]).read_text(encoding="utf-8"))
    return config, profile


def x1_param_schema(kind: str) -> dict:
    config, profile = _limits()
    low, high = config[X1_PARAM_KEYS[kind]]
    if kind == "x1_agl":
        low = max(low, profile["transition"]["minimum_height_m"])
    if kind == "x1_radius":
        turn = profile["max_airspeed_mps"] ** 2 / (9.80665 * math.tan(math.radians(profile["max_bank_deg"])))
        low = max(low, math.ceil(turn))
    return {"type": "number", "minimum": low, "maximum": high}


def x1_altitude_intent(task: str, altitude: float) -> str | None:
    """Validate explicitly stated AGL; never choose a plan or slot on behalf of the model."""
    requested = {float(match[1] or match[2]) for match in _EXPLICIT_AGL.finditer(task)}
    if len(requested) > 1:
        return "one sortie altitude is supported for explicit X1 AGL requests"
    if not requested:
        return None
    value = next(iter(requested))
    bounds = x1_param_schema("x1_agl")
    if _ALLOW_CLAMP.search(task.strip()):
        value = min(bounds["maximum"], max(bounds["minimum"], value))
    if altitude != value:
        return "X1 plan altitude differs from explicit task AGL without a permitted clamp"
    return None


def x1_prompt_rules(capabilities: frozenset[str], context: Mapping | None) -> list[str]:
    config, profile = _limits()
    rules = ["X1 tailsitter simulation only. Use these skills (* required):"]
    for skill, (params, help_text) in X1_SKILLS.items():
        if skill == "gimbal_stare_at" and "gimbal_control" not in capabilities:
            continue
        slots = ", ".join(f"*{key}" for key in sorted(params))
        rules.append(f"- {skill} [{slots}]: {help_text}")
    min_leg = max(config["fw_min_leg_floor_m"], profile["cruise_airspeed_mps"] * config["fw_min_leg_time_s"])
    rules.extend([
        "- return_home []: end the sortie by returning to launch and vertically landing.",
        "Success: vtol_takeoff, fw_transition, one or more FW waypoints or observation skills, "
        "back_transition, return_home. Every on_fail points directly to return_home (phase-aware recovery). "
        "Terminal edges are null. Do not add unrequested observation skills or choose substitute places.",
        f"All flight altitude slots are AGL in metres, {config['altitude_agl_m'][0]}–{config['altitude_agl_m'][1]}; "
        f"nominal FW speed {profile['cruise_airspeed_mps']:g} m/s; minimum FW leg {min_leg:g} m. "
        f"Orbit radius {x1_param_schema('x1_radius')['minimum']}–{config['observation_radius_m'][1]} m; "
        f"duration {config['observation_duration_s'][0]}–{config['observation_duration_s'][1]} s.",
        "Keep the whole leg, orbit and transition runout inside the operator corridor. Use only named POIs "
        "or home, and only named zones for search. query is a short English object noun phrase. "
        "Never output raw coordinates or change the airframe, corridor, zone, or distance limits.",
        "Reject out-of-envelope tasks unless the operator explicitly permits the nearest legal altitude; "
        "then clamp only altitude to the AGL band, and only if the resulting task is otherwise legal.",
    ])
    if context:
        rules.append("Operator X1 limits (local NE metres): " + json.dumps(context, sort_keys=True, ensure_ascii=False))
    return rules


def _number(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("X1 context values must be finite numbers")
    return require_real(value, "X1 context value")


# SITL-only ceiling for the explicitly assumed Iowa long-range case. It is only
# reachable when the ASSUMED_SIM runner supplies a validated bounds template;
# the onboard pilot envelope above is never changed.
ASSUMED_SIM_MAX_DISTANCE_M = 4000.0


def validate_x1_context(raw: Any, area: Mapping, *, assumed_sim_max_distance_m: float | None = None) -> dict:
    """Accept only host-owned, explicitly bounded geometry in a mission package."""
    if not isinstance(raw, Mapping) or set(raw) != {
        "world", "max_distance_from_home_m", "corridor", "terrain_rel_home_m",
    }:
        raise ValueError("X1 requires world, max_distance_from_home_m, corridor and terrain_rel_home_m")
    if not isinstance(raw["world"], str) or not raw["world"].startswith("rw_"):
        raise ValueError("X1 requires an rw_* simulation world")
    config, _ = _limits()
    max_distance = _number(raw["max_distance_from_home_m"])
    limit = config["max_distance_from_home_m"]
    if assumed_sim_max_distance_m is not None:
        widened = _number(assumed_sim_max_distance_m)
        if not limit < widened <= ASSUMED_SIM_MAX_DISTANCE_M:
            raise ValueError("X1 assumed SIM envelope exceeds the SITL-only ceiling")
        limit = widened
    if not 0 < max_distance <= limit:
        raise ValueError("X1 maximum home distance exceeds the pilot envelope")
    rect = raw["corridor"]
    if not isinstance(rect, Mapping) or set(rect) != set(_CORRIDOR_KEYS):
        raise ValueError("X1 requires an explicit rectangular NE corridor")
    corridor = {key: _number(rect[key]) for key in _CORRIDOR_KEYS}
    for low, high in zip(_CORRIDOR_KEYS[::2], _CORRIDOR_KEYS[1::2], strict=True):
        if not area[low] <= corridor[low] < corridor[high] <= area[high]:
            raise ValueError("X1 corridor is outside the package operating area")
    terrain = raw["terrain_rel_home_m"]
    if not isinstance(terrain, (list, tuple)) or len(terrain) != 2:
        raise ValueError("X1 requires lower and upper relative-home terrain bounds")
    ground_low, ground_high = map(_number, terrain)
    if not ground_low <= 0 <= ground_high:
        raise ValueError("X1 terrain bounds must include launch ground elevation (0)")
    return {"world": raw["world"], "max_distance_from_home_m": max_distance,
            "corridor": corridor, "terrain_rel_home_m": [ground_low, ground_high]}


def x1_planner_context(package: MissionPackage) -> dict | None:
    """Render the same operator facts at training, evaluation and runtime."""
    doc = package.document
    if "x1_planning" not in doc:
        return None
    return {**doc["x1_planning"], "home_ne": doc["home"], "places_ne": doc["places"],
            "zones": doc["zones"], "operating_area": doc["operating_area"], "budget": doc["budget"]}


def _segment_distance(a: Point, b: Point, p: Point) -> float:
    dn, de = b[0] - a[0], b[1] - a[1]
    length2 = dn * dn + de * de
    t = 0.0 if not length2 else min(1.0, max(0.0, ((p[0] - a[0]) * dn + (p[1] - a[1]) * de) / length2))
    return math.dist(p, (a[0] + t * dn, a[1] + t * de))


def _x1_static_violations_base(plan: MissionPlan, package: MissionPackage, capabilities: frozenset[str],
                               *, no_fly_margin_m: float = 0.0) -> list[dict]:
    """Conservative nominal admission; verified terrain and tracking still required at execution."""
    findings: list[dict] = []

    def add(code: str, step_id: str | None, message: str) -> None:
        findings.append({"code": code, "step_id": step_id,
                         "message_zh": f"X1 静态准入失败：{message}", "message_en": message})

    if X1_CAPABILITY not in capabilities:
        add("X1_CAPABILITY", None, "X1 simulation planner is not enabled on the resolved airframe")
        return findings
    for step in plan.steps:
        if step.skill in X1_AIRBORNE_SKILLS:
            add("X1_AIRBORNE_ONLY", step.id, f"{step.skill} is only available in an airborne retask")
    if findings:
        return findings
    doc = package.document
    area = doc["operating_area"]
    try:
        context = validate_x1_context(doc.get("x1_planning"), area,
                                      assumed_sim_max_distance_m=package.assumed_sim_max_distance_m)
    except ValueError as exc:
        add("X1_CONTEXT", None, str(exc))
        return findings

    config, profile = _limits()
    cruise = profile["cruise_airspeed_mps"]
    min_leg = max(config["fw_min_leg_floor_m"], cruise * config["fw_min_leg_time_s"])
    track_margin = config["tracking_margin_m"]
    fw_margin = x1_param_schema("x1_radius")["minimum"] + track_margin
    runout = (profile["max_airspeed_mps"] + profile["endurance"]["wind_margin_mps"]) * profile["transition"]["timeout_s"]
    runout += config["transition_position_margin_m"]
    corridor = context["corridor"]
    ground_low, ground_high = context["terrain_rel_home_m"]
    home = tuple(doc["home"])
    cursor = home
    uncertainty = 0.0
    fw_distance = observation_s = 0.0

    def check_sweep(a: Point, b: Point, margin: float, sid: str) -> None:
        # A rectangle and circle are convex, so inflated endpoints bound the segment.
        for n, e in (a, b):
            if not (corridor["north_min_m"] + margin <= n <= corridor["north_max_m"] - margin
                    and corridor["east_min_m"] + margin <= e <= corridor["east_max_m"] - margin):
                add("X1_CORRIDOR", sid, "leg, orbit or transition runout leaves the corridor")
                break
        if max(math.dist(home, a), math.dist(home, b)) + margin > context["max_distance_from_home_m"]:
            add("X1_DISTANCE", sid, "swept path exceeds maximum distance from home")
        if any(_segment_distance(a, b, point) <= radius + margin + no_fly_margin_m
               for point, radius in package.no_fly_zones):
            add("X1_NO_FLY", sid, "swept path intersects a no-fly zone")

    from .mission_plan import validate_mission_plan

    # Validate hand-built plans before geometry so malformed values cannot be
    # reclassified as a terrain or corridor finding. The envelope below owns
    # the gimbal capability finding, independently of the shape check.
    try:
        validate_mission_plan({
            "plan_version": plan.plan_version, "goal": plan.goal, "report": list(plan.report),
            "steps": [{"id": s.id, "skill": s.skill, "params": s.params,
                       "on_success": s.on_success, "on_fail": s.on_fail} for s in plan.steps],
        }, capabilities=capabilities | {"gimbal_control"})
    except ValueError as exc:
        add("X1_PLAN_SCHEMA", None, str(exc))
        return findings

    # Strict success path; keep failure recovery independent from flight-mode assumptions.
    by_id = {step.id: step for step in plan.steps}
    path = []
    step = plan.steps[0] if plan.steps else None
    while step is not None and step.id not in {item.id for item in path}:
        path.append(step)
        step = by_id.get(step.on_success)
    skills = [item.skill for item in path]
    work = {"fw_goto_waypoint", *X1_OBSERVATION}
    if (len(path) != len(plan.steps) or not 5 <= len(path) <= 8 or skills[:2] != ["vtol_takeoff", "fw_transition"]
            or skills[-2:] != ["back_transition", "return_home"] or not set(skills[2:-2]) <= work
            or any(item.on_fail != path[-1].id for item in path[:-1])
            or path[-1].on_success is not None or path[-1].on_fail is not None or path[-1].params):
        add("X1_SEQUENCE", None, "X1 success path must take off, transition, work, back-transition and recover; failures go to recovery")
        return findings

    if doc["oversight_steps"]:
        add("X1_OVERSIGHT", None, "X1 pilot adapter does not support operator-gated steps")

    def static_target(step) -> Point | None:
        ref = step.params.get("target_ref")
        if ref == "home" and step.skill == "fw_goto_waypoint":
            return home
        if ref not in doc["places"] or not ref.startswith("poi:"):
            add("X1_TARGET", step.id, "expected a static onboard POI (or home for FW transit)")
            return None
        return tuple(package.refs[ref])

    for step in path:
        agl = step.params.get("altitude_agl_m")
        if agl is not None and not (area["min_altitude_m"] <= agl + ground_low
                                    and agl + ground_high <= area["max_altitude_m"]):
            add("X1_TERRAIN", step.id, "terrain envelope plus AGL exceeds package relative-home height limits")
        if step.skill == "vtol_takeoff":
            check_sweep(home, home, track_margin, step.id)
        elif step.skill in {"fw_transition", "back_transition"}:
            check_sweep(cursor, cursor, runout + uncertainty, step.id)
        elif step.skill == "return_home":
            mc_distance = math.dist(cursor, home) + runout + uncertainty
            check_sweep(cursor, home, runout + uncertainty, step.id)
        else:
            if step.skill == "gimbal_stare_at" and "gimbal_control" not in capabilities:
                add("X1_GIMBAL", step.id, "gimbal_control is not available")
            if step.skill == "find_object_then_return":
                zone = step.params["region"]
                if zone not in doc["zones"]:
                    add("X1_REGION", step.id, "search needs a static onboard zone")
                    continue
                n, e, zone_radius = package.refs[zone]
                target = (n, e)
                if step.params["radius_m"] + track_margin > zone_radius:
                    add("X1_REGION", step.id, "search orbit and tracking margin do not fit its zone")
            else:
                target = static_target(step)
                if target is None:
                    continue
            distance = math.dist(cursor, target)
            # An immediately adjacent observation at the current waypoint is not a short FW leg.
            if (step.skill == "fw_goto_waypoint" or distance > 0) and distance < min_leg + uncertainty:
                add("X1_MIN_LEG", step.id, f"FW leg shorter than {min_leg:g} m plus entry uncertainty")
            check_sweep(cursor, target, fw_margin + uncertainty, step.id)
            fw_distance += distance + uncertainty
            observation_s += step.params.get("duration_s", 0.0)
            uncertainty = step.params.get("radius_m", 0.0)
            if uncertainty:
                check_sweep(target, target, uncertainty + track_margin, step.id)
            cursor = target
    if not findings:
        budget, endurance = doc["budget"], profile["endurance"]
        height = max(step.params.get("altitude_agl_m", 0.0) for step in path) + ground_high
        climb_s, descent_s = height / profile["max_climb_rate_mps"], height / profile["max_descent_rate_mps"]
        transition_s = 2 * profile["transition"]["timeout_s"]
        fw_s = fw_distance / (cruise - endurance["wind_margin_mps"])
        mc_s = mc_distance / (8.0 - endurance["wind_margin_mps"])
        seconds = climb_s + descent_s + transition_s + fw_s + mc_s + observation_s
        energy = 2 * endurance["transition_energy_wh"] + (
            climb_s * endurance["climb_power_w"] + descent_s * endurance["descent_power_w"]
            + fw_s * endurance["cruise_power_w"] + mc_s * endurance["hover_power_w"]
            + observation_s * endurance["loiter_power_w"]
        ) / 3600
        if seconds > budget["time_s"] - budget["reserve_time_s"]:
            add("X1_TIME", None, "estimated sortie exceeds package time after reserve")
        if energy > min(budget["energy_wh"] - budget["reserve_energy_wh"],
                        endurance["battery_wh"] * (1 - endurance["reserve_fraction"])):
            add("X1_ENERGY", None, "estimated sortie exceeds usable package/airframe energy")
    return findings


def x1_static_violations(plan: MissionPlan, package: MissionPackage, capabilities: frozenset[str],
                         *, no_fly_margin_m: float = 0.0) -> list[dict]:
    """Static admission with an optional SITL-only corridor branch.

    ``assumed_sim_corridor is None`` keeps the single-waypoint path byte-for-byte
    identical. In corridor mode the existing planning-layer findings are kept and
    stricter corridor checks are appended (DESIGN-corridor.md section 7 step 3).
    """
    corridor = getattr(package, "assumed_sim_corridor", None)
    if corridor is None:
        return _x1_static_violations_base(
            plan, package, capabilities, no_fly_margin_m=no_fly_margin_m)
    from dataclasses import replace

    base_package = replace(package, assumed_sim_corridor=None)
    findings = _x1_static_violations_base(
        plan, base_package, capabilities, no_fly_margin_m=no_fly_margin_m)
    findings.extend(_x1_corridor_violations(
        plan, package, corridor, base_findings=findings,
        no_fly_margin_m=no_fly_margin_m))
    return findings


_FATAL_BASE_CODES = frozenset({
    "X1_CAPABILITY", "X1_AIRBORNE_ONLY", "X1_CONTEXT", "X1_PLAN_SCHEMA", "X1_SEQUENCE",
})


def _success_path(plan: MissionPlan) -> list:
    by_id = {step.id: step for step in plan.steps}
    path = []
    step = plan.steps[0] if plan.steps else None
    while step is not None and step.id not in {item.id for item in path}:
        path.append(step)
        step = by_id.get(step.on_success)
    return path


def _turn_angle_deg(a: Point, b: Point, c: Point) -> float:
    """Heading change at vertex ``b`` between ``a->b`` and ``b->c`` (0 = straight)."""
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if not n1 or not n2:
        return 0.0
    cosine = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(min(1.0, max(-1.0, cosine))))


def _rect_clear(point: Point, rect: Mapping[str, float], margin: float) -> bool:
    return (rect["north_min_m"] + margin <= point[0] <= rect["north_max_m"] - margin
            and rect["east_min_m"] + margin <= point[1] <= rect["east_max_m"] - margin)


def _x1_corridor_violations(plan: MissionPlan, package: MissionPackage, corridor,
                            *, base_findings: list[dict], no_fly_margin_m: float = 0.0) -> list[dict]:
    """Corridor-only static checks; every rule here is stricter than the base path."""
    findings: list[dict] = []
    if any(item["code"] in _FATAL_BASE_CODES for item in base_findings):
        return findings

    def add(code: str, step_id: str | None, message: str) -> None:
        findings.append({"code": code, "step_id": step_id,
                         "message_zh": f"X1 静态准入失败：{message}", "message_en": message})

    doc = package.document
    area = doc["operating_area"]
    try:
        context = validate_x1_context(
            doc.get("x1_planning"), area,
            assumed_sim_max_distance_m=package.assumed_sim_max_distance_m)
    except ValueError:
        return []  # The base path already reports X1_CONTEXT.

    config, profile = _limits()
    cruise = profile["cruise_airspeed_mps"]
    min_leg = max(config["fw_min_leg_floor_m"], cruise * config["fw_min_leg_time_s"])
    runout = (profile["max_airspeed_mps"] + profile["endurance"]["wind_margin_mps"]) * profile[
        "transition"]["timeout_s"]
    runout += config["transition_position_margin_m"]
    rect = context["corridor"]
    home = tuple(doc["home"])
    _, ground_high = context["terrain_rel_home_m"]

    path = _success_path(plan)

    fw_steps = [step for step in path if step.skill == "fw_goto_waypoint"]
    if len(fw_steps) > corridor.max_fw_legs:
        add("X1_CORRIDOR_LEGS", None,
            f"corridor plan has {len(fw_steps)} FW legs; maximum is {corridor.max_fw_legs}")

    vertices: list[Point] = [home]
    for step in fw_steps:
        ref = step.params.get("target_ref")
        if ref == "home":
            vertices.append(home)
        elif ref in package.refs:
            vertices.append(tuple(package.refs[ref]))
        else:
            return findings  # Unknown target is already X1_TARGET in the base path.

    from .x1_mission import FW_PASS_CROSS_M
    r_min = math.ceil(
        profile["max_airspeed_mps"] ** 2
        / (9.80665 * math.tan(math.radians(profile["max_bank_deg"]))))
    wind_drift = profile["endurance"]["wind_margin_mps"] * math.pi * r_min / cruise
    turn_pad = 2.0 * r_min + FW_PASS_CROSS_M + corridor.fw_tracking_m + wind_drift
    pads = [0.0] * len(vertices)
    for index in range(1, len(vertices) - 1):
        if _turn_angle_deg(vertices[index - 1], vertices[index], vertices[index + 1]) > 1.0:
            pads[index] = turn_pad

    for index, step in enumerate(fw_steps):
        a, b = vertices[index], vertices[index + 1]
        leg_no = index + 1
        pad = max(pads[index], pads[index + 1])
        for vertex, vertex_pad in ((a, pads[index]), (b, pads[index + 1])):
            if vertex_pad and not _rect_clear(vertex, rect, vertex_pad):
                add("X1_CORRIDOR_TURN", step.id,
                    f"turn pad at leg {leg_no} leaves the corridor")
                break
        margin = max(corridor.fw_tracking_m, pad)
        for n, e in (a, b):
            if not _rect_clear((n, e), rect, margin):
                add("X1_CORRIDOR", step.id, f"leg {leg_no} leaves the corridor")
                break
        if max(math.dist(home, a), math.dist(home, b)) + margin > context["max_distance_from_home_m"]:
            add("X1_DISTANCE", step.id, f"leg {leg_no} exceeds maximum distance from home")
        if any(_segment_distance(a, b, point) <= radius + margin + no_fly_margin_m
               for point, radius in package.no_fly_zones):
            add("X1_NO_FLY", step.id, f"leg {leg_no} intersects a no-fly zone")

    last_is_home = bool(fw_steps) and fw_steps[-1].params.get("target_ref") == "home"
    gate = corridor.standoff_gate_enabled
    standoff = corridor.standoff_m if gate else None
    if last_is_home:
        previous = vertices[-2]
        if len(fw_steps) >= 2:
            return_step_id = fw_steps[-2].id
        else:
            return_step_id = fw_steps[-1].id
        if gate and math.dist(previous, home) < corridor.standoff_m + min_leg:
            add("X1_RETURN_TOO_SHORT", return_step_id,
                "last outbound leg is shorter than the standoff gate plus minimum leg")
        outbound_m = sum(
            math.dist(vertices[i], vertices[i + 1]) for i in range(len(vertices) - 2))
        return_m = math.dist(previous, home)
        total_fw_m = outbound_m + return_m
        mc_m = max(corridor.standoff_m, runout - corridor.standoff_m) if gate else runout
    else:
        outbound_m = sum(
            math.dist(vertices[i], vertices[i + 1]) for i in range(len(vertices) - 1))
        return_m = 0.0
        total_fw_m = outbound_m
        mc_m = math.dist(vertices[-1], home) + (
            max(corridor.standoff_m, runout - corridor.standoff_m) if gate else runout)

    observation_s = sum(
        float(step.params.get("duration_s", 0.0)) for step in path if step.skill in X1_OBSERVATION)
    height = max((step.params.get("altitude_agl_m", 0.0) for step in path), default=0.0)
    height += ground_high

    from .x1_corridor_energy import (
        default_constants,
        proof_layer_available_wh,
        proof_layer_requirement_wh,
        proof_layer_reserve_wh,
    )

    constants = default_constants()
    fw_peak = corridor.fw_leg_peak_power_w
    if last_is_home:
        requirement = proof_layer_requirement_wh(
            return_m, agl_m=height - ground_high, ground_high_m=ground_high,
            return_mode="fw", fw_outbound_remaining_m=outbound_m, standoff_m=standoff,
            fw_peak_power_w=fw_peak, constants=constants)
    else:
        requirement = proof_layer_requirement_wh(
            0.0, agl_m=height - ground_high, ground_high_m=ground_high,
            return_mode="fw", mc_return_m=mc_m, fw_peak_power_w=fw_peak, constants=constants)
        requirement += fw_peak * outbound_m / constants.ground_fw_plan_mps / 3600.0
    requirement += observation_s * constants.loiter_power_w / 3600.0

    budget = doc["budget"]
    battery = min(constants.battery_wh, float(budget["energy_wh"]))
    available = proof_layer_available_wh(
        agl_m=height - ground_high, ground_high_m=ground_high,
        battery_wh=battery, constants=constants)
    reserve = proof_layer_reserve_wh(
        battery_wh=battery, package_reserve_wh=budget.get("reserve_energy_wh"),
        constants=constants)
    if requirement > available - reserve:
        add("X1_ENERGY_PROOF", None,
            "corridor proof-layer energy after reserve exceeds available energy")

    fw_s = total_fw_m / corridor.min_ground_speed_mps
    mc_s = mc_m / constants.ground_mc_plan_mps
    seconds = (
        height / constants.max_climb_rate_mps + height / constants.max_descent_rate_mps
        + 2 * constants.transition_timeout_s + fw_s + mc_s + observation_s)
    if seconds > budget["time_s"] - budget["reserve_time_s"]:
        add("X1_TIME_PROOF", None, "corridor proof-layer time after reserve exceeds package time")
    return findings


def corridor_primitives_input(plan: MissionPlan, package: MissionPackage,
                              capabilities: frozenset[str]) -> tuple[MissionPackage, MissionPlan, float]:
    """Dispatch to the single-waypoint adapter or the (step 4) corridor lowering.

    Step 3 only owns static admission and this dispatch skeleton. Multi-waypoint
    lowering is not implemented yet, so the corridor call site is fail-closed with
    ``CORRIDOR_NOT_ENABLED`` instead of half-lowering a route.
    """
    from .mission_package import check_plan_against_package

    if findings := check_plan_against_package(plan, package, capabilities=capabilities):
        raise ValueError(f"{findings[0]['code']}: {findings[0]['message_en']}")
    if len({step.id for step in plan.steps}) == 5:
        return one_waypoint_primitives_input(plan, package, capabilities)
    if package.assumed_sim_corridor is None:
        raise ValueError(
            "CORRIDOR_NOT_ENABLED: multi-waypoint X1 plans require the assumed SIM corridor")
    raise ValueError(
        "CORRIDOR_NOT_ENABLED: X1 corridor lowering is implemented in a later step")


def one_waypoint_primitives_input(plan: MissionPlan, package: MissionPackage,
                                  capabilities: frozenset[str]) -> tuple[MissionPackage, MissionPlan, float]:
    """Translate the first X1 graph supported by cx-x1-nl-prims' CPU lowering.

    The returned package is a local SIM-only view with tighter operating bounds;
    the model output, original package and step identifiers remain separate.
    """
    from dataclasses import replace

    from .mission_package import check_plan_against_package
    from .mission_plan import MissionPlan, MissionStep

    if findings := check_plan_against_package(plan, package, capabilities=capabilities):
        raise ValueError(f"{findings[0]['code']}: {findings[0]['message_en']}")
    by_id = {step.id: step for step in plan.steps}
    takeoff = plan.steps[0]
    if len(by_id) != 5:
        raise ValueError("X1 primitive adapter currently supports only one waypoint and recovery")
    outbound = by_id[by_id[takeoff.on_success].on_success]
    if (outbound.skill != "fw_goto_waypoint" or outbound.params["target_ref"] == "home"
            or outbound.params["altitude_agl_m"] != takeoff.params["altitude_agl_m"]):
        raise ValueError("X1 primitive adapter requires one named waypoint at the takeoff AGL")
    terminal = by_id[outbound.on_fail]
    adapted = MissionPlan(plan.plan_version, plan.goal, (
        MissionStep(outbound.id, "goto", {"target_ref": outbound.params["target_ref"],
                                         "altitude_m": outbound.params["altitude_agl_m"]}, terminal.id, terminal.id),
        terminal,
    ), plan.report)
    doc = package.document
    context = doc["x1_planning"]
    adapter_document = {**doc, "operating_area": {**doc["operating_area"], **context["corridor"]}}
    return replace(package, document=adapter_document, plan=adapted), adapted, context["max_distance_from_home_m"]

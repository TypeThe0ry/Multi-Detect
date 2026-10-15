"""Preflight admission for self-contained onboard mission packages."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mission_plan import MissionPlan, MissionStep, validate_mission_plan
from .mission_security import load_key, verify_document
from .serialization import strict_json_loads

POLICIES = ("continue", "continue_then_return", "hold_then_return", "return_now", "land_now")
RISK_DEFAULTS = {"low": "continue", "medium": "continue_then_return", "high": "return_now"}
# Nominal horizontal speed calibrated against recorded mission-suite-v1-r12
# translate outcomes. This is a planning estimate, not an upper bound.
CALIBRATED_TRAVEL_SPEED_MPS = 1.03
OPEN_ENDED_SKILLS = frozenset({"area_search", "count", "monitor", "patrol", "survey", "follow"})


def _is_terminal_step(step: MissionStep) -> bool:
    """Match the mission-plan validator's safe terminal semantics."""
    return step.skill in {"return_home", "land"} or (
        step.skill == "hover" and step.on_success is None
    )


def number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _object(value: Any, name: str, required: set[str], optional=()) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if required - value.keys() or value.keys() - required - set(optional):
        raise ValueError(f"{name}: missing or unknown fields")
    return dict(value)


def parse_policy(value: Any) -> tuple[str, float]:
    obj = _object(value, "link_loss_policy", {"action"}, {"timeout_s"})
    action = obj["action"]
    if action not in POLICIES:
        raise ValueError("unknown link-loss policy")
    timed = action in {"continue_then_return", "hold_then_return"}
    if timed and "timeout_s" not in obj:
        raise ValueError("timed policy requires timeout_s")
    timeout = number(obj.get("timeout_s", 0), "timeout_s")
    return action, timeout


@dataclass(frozen=True)
class MissionPackage:
    document: dict[str, Any]
    refs: dict[str, Any]
    plan: MissionPlan | None

    @property
    def mission_id(self) -> str:
        return self.document["mission_id"]

    @property
    def no_fly_zones(self) -> tuple[tuple[tuple[float, float], float], ...]:
        """Return no-fly circles for the executor's runtime leg guard."""
        zones = []
        for zone in self.document.get("zones", {}).values():
            if _zone_tag_is_no_fly(zone.get("risk_tag")):
                zones.append((tuple(zone["centre_ne"]), float(zone["radius_m"])))
        return tuple(zones)

    def validate_plan(self, plan: MissionPlan) -> None:
        """Admit both uploaded plans and plans compiled on board before flight."""
        ids = {s.id for s in plan.steps}
        if set(self.document["oversight_steps"]) - ids:
            raise ValueError("oversight_steps contains an unknown step")
        area = self.document["operating_area"]
        for step in plan.steps:
            for key in ("region", "target_ref", "route_ref", "track_ref", "payload_ref"):
                ref = step.params.get(key)
                if ref is None:
                    continue
                if key == "region" and ref != "zone:all" and ref not in self.document["zones"]:
                    raise ValueError(f"{ref} is not an onboard zone")
                if key == "region" and ref == "zone:all" and step.skill in {"survey", "monitor"}:
                    raise ValueError(f"{step.skill} requires a named onboard zone")
                if key == "route_ref" and ref not in self.document["routes"]:
                    raise ValueError(f"{ref} is not an onboard route")
                if key == "track_ref" and not ref.startswith("track:"):
                    raise ValueError(f"{ref} is not an onboard track")
                if ref == "home" or (ref == "zone:all" and key == "region"):
                    continue
                if ref.startswith(("finding:", "track:")):
                    if ref.startswith("track:") and key != "track_ref":
                        raise ValueError(f"{ref} is not an onboard position reference")
                    # Dynamic findings are produced onboard, never fetched from the GCS.
                    index = ref.split(":", 1)[1]
                    if not index.isdigit() or int(index) < 1:
                        raise ValueError(f"invalid dynamic reference {ref}")
                    continue
                if ref not in self.refs:
                    raise ValueError(f"unknown onboard reference {ref}")
                if key == "target_ref" and ref in self.document["routes"]:
                    raise ValueError(f"{ref} is not a place")
            altitude = step.params.get("altitude_m")
            if (
                altitude is not None
                and not area["min_altitude_m"] <= altitude <= area["max_altitude_m"]
            ):
                raise ValueError(f"{step.id}: altitude outside operating limits")


def _package_violation(code: str, step_id: str | None, zh: str, en: str) -> dict[str, Any]:
    """Build the stable, bilingual shape consumed by the planner and mission log."""
    return {
        "code": code,
        "step_id": step_id,
        "message_zh": zh,
        "message_en": en,
        "message": f"{zh} / {en}",
    }


def _inside_area(point: tuple[float, float], area: Mapping[str, float]) -> bool:
    return (
        area["north_min_m"] <= point[0] <= area["north_max_m"]
        and area["east_min_m"] <= point[1] <= area["east_max_m"]
    )


def _segment_circle_distance(
    start: tuple[float, float], end: tuple[float, float], centre: tuple[float, float]
) -> float:
    delta = (end[0] - start[0], end[1] - start[1])
    length2 = delta[0] ** 2 + delta[1] ** 2
    if length2 == 0:
        return math.dist(start, centre)
    t = max(
        0.0,
        min(
            1.0,
            ((centre[0] - start[0]) * delta[0] + (centre[1] - start[1]) * delta[1])
            / length2,
        ),
    )
    return math.dist(centre, (start[0] + t * delta[0], start[1] + t * delta[1]))


def _zone_tag_is_no_fly(tag: Any) -> bool:
    if not isinstance(tag, str):
        return False
    normalised = tag.strip().lower().replace("_", "-").replace(" ", "-")
    return normalised in {"no-fly", "nofly", "no-fly-zone", "prohibited"}


def _point_from_ref(ref: Any, refs: Mapping[str, Any], home: Any) -> tuple[float, float] | None:
    if ref == "home":
        value = home
    else:
        value = refs.get(ref)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
        return None
    try:
        point = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(v) for v in point) else None


def estimate_plan_duration(
    plan: MissionPlan,
    package: MissionPackage,
    *,
    executor_config: Any | None = None,
) -> float:
    """Return a nominal duration estimate calibrated from recorded flight data.

    The calibrated horizontal speed is a planning estimate, not a guaranteed
    upper bound; the supervisor remains responsible for reserve-return.
    """
    if executor_config is None:
        from .mission_executor import ExecutorConfig

        executor_config = ExecutorConfig()
    hold_s = max(0.1, float(executor_config.hold_s))
    speed_mps = CALIBRATED_TRAVEL_SPEED_MPS
    step_budget = max(1, int(executor_config.step_budget)) * hold_s
    travel_budget = max(1, int(executor_config.travel_budget)) * hold_s
    refs = package.refs
    home = tuple(float(v) for v in package.document["home"])
    total = 0.0
    previous = home
    for step in plan.steps:
        params = step.params
        skill = step.skill
        target = _point_from_ref(params.get("target_ref"), refs, home)
        route = refs.get(params.get("route_ref"))
        route_points = (
            [tuple(float(v) for v in point[:2]) for point in route]
            if isinstance(route, Sequence)
            and not isinstance(route, (str, bytes))
            and route
            and isinstance(route[0], Sequence)
            else []
        )
        distance = 0.0
        if skill in {"goto", "deliver"} and target is not None:
            distance = math.dist(previous, target) if previous is not None else 0.0
            previous = target
        elif skill == "return_home":
            distance = math.dist(previous, home) if previous is not None else 0.0
            previous = home
        elif skill == "patrol" and len(route_points) > 1:
            loops = int(params.get("loops", 1))
            entry = math.dist(previous, route_points[0]) if previous is not None else 0.0
            internal = sum(
                math.dist(a, b) for a, b in zip(route_points, route_points[1:], strict=False)
            )
            distance = entry + internal * loops
            if loops > 1:
                distance += (loops - 1) * math.dist(route_points[-1], route_points[0])
            previous = route_points[-1]
        elif skill == "survey":
            region = refs.get(params.get("region"))
            radius = float(region[2]) if isinstance(region, Sequence) and len(region) >= 3 else 0.0
            distance = 4.0 * radius
        elif skill == "inspect":
            distance = float(params.get("standoff_m", 0.0))
        travel = distance / speed_mps
        if skill in {"monitor", "follow", "hover"}:
            duration = float(params.get("duration_s", travel_budget))
        elif skill == "patrol":
            duration = max(float(params.get("duration_s", 0.0)), travel)
        elif skill in {"goto", "deliver", "return_home", "land"}:
            duration = travel + hold_s
        elif skill == "inspect":
            duration = travel + int(params.get("views", 1)) * hold_s
        elif skill in {"area_search", "count", "survey", "record"}:
            duration = max(
                travel, travel_budget if skill in {"area_search", "survey"} else step_budget
            )
        else:
            duration = step_budget
        total += max(0.0, duration)
        if skill in OPEN_ENDED_SKILLS and skill != "patrol":
            previous = None
        elif skill == "inspect":
            previous = target
    return total


def _static_step_anchor(
    step: MissionStep, refs: Mapping[str, Any], home: tuple[float, float]
) -> tuple[float, float] | None:
    """Return the first static ground point the executor must reach for a step."""
    skill = step.skill
    params = step.params
    if skill in {"goto", "deliver", "inspect"}:
        return _point_from_ref(params.get("target_ref"), refs, home)
    if skill == "return_home":
        return home
    if skill == "patrol":
        route = refs.get(params.get("route_ref"))
        if isinstance(route, Sequence) and route and isinstance(route[0], Sequence):
            try:
                point = (float(route[0][0]), float(route[0][1]))
            except (TypeError, ValueError, IndexError):
                return None
            return point if all(math.isfinite(v) for v in point) else None
        return None
    if skill in {"monitor", "survey", "area_search", "count"}:
        region = params.get("region")
        value = refs.get(region)
        if isinstance(value, Sequence) and len(value) >= 2:
            try:
                point = (float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                return None
            return point if all(math.isfinite(v) for v in point) else None
    return None


def _route_points(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    points: list[tuple[float, float]] = []
    for point in value:
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)) or len(point) < 2:
            return []
        try:
            parsed = (float(point[0]), float(point[1]))
        except (TypeError, ValueError):
            return []
        if not all(math.isfinite(v) for v in parsed):
            return []
        points.append(parsed)
    return points


def _minimum_mandatory_duration(
    plan: MissionPlan,
    package: MissionPackage,
    *,
    executor_config: Any | None = None,
) -> float:
    """Estimate work that must fit before reserve-return can take over.

    Open-ended observation and patrol work is omitted when both graph branches
    lead to a terminal. Their initial transit is mandatory; reserve-return
    handles the eventual leg from the runtime pose. A malformed graph is
    charged normally and is also reported by the terminal check, so it cannot
    gain budget admission by leaving an edge null.
    """
    if executor_config is None:
        from .mission_executor import ExecutorConfig

        executor_config = ExecutorConfig()
    hold_s = max(0.1, float(executor_config.hold_s))
    # Admission uses a lower bound: the executor can command at most one
    # max_translate_m leg per control cycle, and open-ended work may be cut
    # short when the supervisor starts reserve-return. The calibrated speed is
    # reserved for the nominal estimate above, which is intentionally not an
    # upper bound.
    max_translate_m = max(0.1, float(executor_config.max_translate_m))

    def transit_seconds(distance: float) -> float:
        return math.ceil(distance / max_translate_m) * hold_s if distance > 0 else 0.0

    step_budget = max(1, int(executor_config.step_budget)) * hold_s
    travel_budget = max(1, int(executor_config.travel_budget)) * hold_s
    home = tuple(float(v) for v in package.document["home"])
    refs = package.refs
    by_id = {step.id: step for step in plan.steps}
    terminals = {step.id for step in plan.steps if _is_terminal_step(step)}

    def reaches_terminal(step_id: str, seen: set[str]) -> bool:
        if step_id in terminals:
            return True
        if step_id in seen or step_id not in by_id:
            return False
        step = by_id[step_id]
        return all(
            ref is not None and reaches_terminal(ref, seen | {step_id})
            for ref in (step.on_success, step.on_fail)
        )

    total = 0.0
    previous = home
    for step in plan.steps:
        anchor = _static_step_anchor(step, refs, home)
        if step.skill == "patrol":
            route_points = _route_points(refs.get(step.params.get("route_ref")))
            if route_points:
                anchor = route_points[0]
        distance = (
            math.dist(previous, anchor)
            if previous is not None and anchor is not None
            else 0.0
        )
        open_ended = step.skill in OPEN_ENDED_SKILLS and reaches_terminal(step.id, set())
        if not open_ended:
            total += transit_seconds(distance) + (hold_s if anchor is not None else 0.0)
            if step.skill in OPEN_ENDED_SKILLS:
                total += float(
                    step.params.get(
                        "duration_s",
                        (
                            travel_budget
                            if step.skill in {"monitor", "follow", "patrol"}
                            else step_budget
                        ),
                    )
                )
            elif step.skill == "inspect":
                total += max(1, int(step.params.get("views", 1))) * hold_s
        elif anchor is not None:
            total += transit_seconds(distance) + hold_s
        if step.skill in OPEN_ENDED_SKILLS or step.skill == "inspect":
            # Observation can end at an executor-selected pose; reserve-return
            # handles the eventual leg once that pose is known.
            previous = None
        elif anchor is not None:
            previous = anchor
        elif step.skill in {"area_search", "count", "monitor", "survey", "follow", "inspect"}:
            # The eventual pose is observation-dependent; reserve-return will
            # handle that runtime leg instead of rejecting the package here.
            previous = None
    return total


def check_plan_against_package(
    plan: MissionPlan,
    package: MissionPackage,
    *,
    capabilities: frozenset[str] | None = None,
    executor_config: Any | None = None,
) -> list[dict[str, Any]]:
    """Check a compiled plan against operator constraints before takeoff.

    This function is deliberately side-effect free.  It accepts a validated
    ``MissionPlan`` but also tolerates hand-built plans so callers can report a
    useful refusal instead of allowing a model mistake to reach the executor.
    """
    document = package.document
    area = document["operating_area"]
    refs = package.refs
    violations: list[dict[str, Any]] = []
    no_fly = []
    for name, zone in document.get("zones", {}).items():
        if _zone_tag_is_no_fly(zone.get("risk_tag")):
            no_fly.append((name, tuple(zone["centre_ne"]), float(zone["radius_m"])))

    def add(code: str, step: MissionStep, zh: str, en: str) -> None:
        violations.append(_package_violation(code, step.id, zh, en))

    previous: tuple[float, float] | None = tuple(float(v) for v in document["home"])
    for step in plan.steps:
        params = step.params
        paths: list[list[tuple[float, float]]] = []
        regions: list[tuple[tuple[float, float], float, str]] = []
        region_ref = params.get("region")
        if region_ref and region_ref != "zone:all":
            value = refs.get(region_ref)
            if isinstance(value, Sequence) and len(value) >= 3:
                centre = (float(value[0]), float(value[1]))
                radius = float(value[2])
                regions.append((centre, radius, str(region_ref)))
                if previous is not None:
                    paths.append([previous, centre])
        target_ref = params.get("target_ref")
        if target_ref in document.get("zones", {}) and target_ref in refs:
            value = refs[target_ref]
            if isinstance(value, Sequence) and len(value) >= 3:
                centre = (float(value[0]), float(value[1]))
                radius = float(value[2])
                regions.append((centre, radius, str(target_ref)))
                if previous is not None:
                    paths.append([previous, centre])
        else:
            target = _point_from_ref(target_ref, refs, document["home"])
            if target is not None:
                if previous is not None:
                    paths.append([previous, target])
        route = refs.get(params.get("route_ref"))
        route_points = _route_points(route)
        if route_points:
            paths.append([previous, *route_points] if previous is not None else route_points)
            loops = max(1, int(params.get("loops", 1)))
            if loops > 1 and len(route_points) > 1:
                paths.append([route_points[-1], route_points[0]])
        elif step.skill == "return_home" and previous is not None:
            paths.append([previous, tuple(float(v) for v in document["home"])])

        # The executor flies a straight leg between every static target and
        # route waypoint. Keep the final static position for the next step.
        anchor = _static_step_anchor(step, refs, tuple(document["home"]))
        if step.skill == "patrol":
            if route_points:
                anchor = route_points[-1]
        if step.skill in {"area_search", "count", "monitor", "survey", "follow", "inspect"}:
            # These skills may finish at a pose known only after execution.
            previous = None
        elif step.skill == "patrol" and "duration_s" in params:
            previous = None
        elif anchor is not None:
            previous = anchor

        outside = False
        for path in paths:
            if any(not _inside_area(point, area) for point in path):
                outside = True
        for centre, radius, _ in regions:
            if not (
                area["north_min_m"] <= centre[0] - radius
                and centre[0] + radius <= area["north_max_m"]
                and area["east_min_m"] <= centre[1] - radius
                and centre[1] + radius <= area["east_max_m"]
            ):
                outside = True
        if outside:
            add(
                "OUTSIDE_OPERATING_AREA",
                step,
                f"步骤 {step.id} 的区域、目标或航线超出作业区域",
                f"Step {step.id} has a region, target, or route outside the operating area",
            )

        # zone:all and dynamic findings have no static preflight path. Their
        # actual legs are checked by the executor once the pose is known.
        entered_no_fly = False
        for path in paths:
            for point in path:
                if any(math.dist(point, centre) <= radius for _, centre, radius in no_fly):
                    entered_no_fly = True
            for start, end in zip(path, path[1:], strict=False):
                if any(
                    _segment_circle_distance(start, end, centre) <= radius
                    for _, centre, radius in no_fly
                ):
                    entered_no_fly = True
        for centre, radius, _ in regions:
            if any(
                math.dist(centre, forbidden_centre) <= radius + forbidden_radius
                for _, forbidden_centre, forbidden_radius in no_fly
            ):
                entered_no_fly = True
        if entered_no_fly:
            names = ", ".join(name for name, _, _ in no_fly)
            add(
                "NO_FLY_ZONE",
                step,
                f"步骤 {step.id} 进入禁飞区 {names}",
                f"Step {step.id} enters no-fly zone(s) {names}",
            )

        altitude = params.get("altitude_m")
        if altitude is not None and not (
            area["min_altitude_m"] <= altitude <= area["max_altitude_m"]
        ):
            add(
                "ALTITUDE_OUT_OF_BOUNDS",
                step,
                f"步骤 {step.id} 的高度超出允许范围",
                f"Step {step.id} altitude is outside the operating limits",
            )

        if step.skill == "deliver":
            available = capabilities
            if available is None:
                available = frozenset(document.get("capabilities", ()))
            if "payload" not in available:
                add(
                    "MISSING_CAPABILITY",
                    step,
                    f"步骤 {step.id} 需要载荷能力，但当前飞行器不具备该能力",
                    f"Step {step.id} requires the payload capability, which is unavailable",
                )

    reachable = {plan.steps[0].id} if plan.steps else set()
    by_id = {step.id: step for step in plan.steps}
    frontier = list(reachable)
    while frontier:
        current = by_id[frontier.pop()]
        for ref in (current.on_success, current.on_fail):
            if ref is not None and ref in by_id and ref not in reachable:
                reachable.add(ref)
                frontier.append(ref)
    terminals = [step for step in plan.steps if _is_terminal_step(step)]
    if not terminals or not any(step.id in reachable for step in terminals):
        terminal = terminals[0] if terminals else (plan.steps[-1] if plan.steps else None)
        violations.append(
            _package_violation(
                "TERMINAL_UNREACHABLE",
                terminal.id if terminal else None,
                (
                    f"终端步骤 {terminal.id} 从计划起点不可达"
                    if terminal
                    else "计划没有可达的终端步骤"
                ),
                (
                    f"Terminal step {terminal.id} is unreachable from the plan start"
                    if terminal
                    else "The plan has no reachable terminal step"
                ),
            )
        )

    def reaches_terminal(step_id: str, seen: set[str]) -> bool:
        if step_id in {step.id for step in terminals}:
            return True
        if step_id in seen or step_id not in by_id:
            return False
        current = by_id[step_id]
        return all(
            ref is not None and reaches_terminal(ref, seen | {step_id})
            for ref in (current.on_success, current.on_fail)
        )

    for step_id in sorted(reachable):
        step = by_id[step_id]
        if step in terminals:
            # return_home/land are terminal actions. A final hover is terminal
            # on success by contract; any failure edge must still be safe.
            if step.skill in {"return_home", "land"} and (
                step.on_success is not None or step.on_fail is not None
            ):
                violations.append(
                    _package_violation(
                        "TERMINAL_OUTGOING_EDGE",
                        step.id,
                        f"终端步骤 {step.id} 不得有后续分支",
                        f"Terminal step {step.id} must not have outgoing branches",
                    )
                )
            if step.skill == "hover" and step.on_fail is not None and not reaches_terminal(
                step.on_fail, {step.id}
            ):
                violations.append(
                    _package_violation(
                        "TERMINAL_UNREACHABLE",
                        step.id,
                        f"步骤 {step.id} 的failure分支无法到达安全终端",
                        f"Step {step.id} failure branch does not reach a safe terminal",
                    )
                )
            continue
        for branch, ref in (("success", step.on_success), ("failure", step.on_fail)):
            if ref is None or not reaches_terminal(ref, {step.id}):
                violations.append(
                    _package_violation(
                        "TERMINAL_UNREACHABLE",
                        step.id,
                        f"步骤 {step.id} 的{branch}分支无法到达安全终端",
                        f"Step {step.id} {branch} branch does not reach a safe terminal",
                    )
                )

    budget = document["budget"]
    available_time = float(budget["time_s"]) - float(budget["reserve_time_s"])
    estimate = _minimum_mandatory_duration(plan, package, executor_config=executor_config)
    if estimate > available_time:
        step_id = next(
            (
                step.id
                for step in plan.steps
                if step.params.get("duration_s") is not None
                or step.skill not in {"return_home", "land"}
            ),
            plan.steps[-1].id if plan.steps else None,
        )
        violations.append(
            _package_violation(
                "TIME_BUDGET_EXCEEDED",
                step_id,
                f"计划最低必需耗时 {estimate:.1f} 秒，"
                f"超过扣除余量后的时间预算 {available_time:.1f} 秒",
                "Minimum mandatory plan duration "
                f"{estimate:.1f}s exceeds the time budget after reserve "
                f"({available_time:.1f}s)",
            )
        )
    return violations


def load_mission_package(
    raw: Mapping[str, Any] | Path,
    *,
    key_path: str | Path | None = None,
    key: bytes | None = None,
    require_signature: bool = False,
    operator_key_path: str | Path | None = None,
) -> MissionPackage:
    if isinstance(raw, Path):
        raw = strict_json_loads(raw.read_text(encoding="utf-8"), name="mission package")
    raw = deepcopy(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("mission package must be an object")
    # Verify the exact uploaded document before normalisation or compilation.
    # Key material is never retained in the package or emitted to the log.
    key_path = key_path or operator_key_path
    if key is None and key_path is not None:
        key = load_key(key_path)
    has_signature = "signature" in raw or "signature_alg" in raw
    if require_signature and key is None:
        raise ValueError("signed mission package requires an operator key path")
    if key is not None:
        if not has_signature or not verify_document(raw, key):
            raise ValueError("mission package signature verification failed")
    elif has_signature:
        raise ValueError("signed mission package requires an operator key path")
    obj = _object(
        raw,
        "mission package",
        {
            "mission_id",
            "places",
            "zones",
            "routes",
            "operating_area",
            "budget",
            "home",
            "risk_level",
            "link_loss_policy",
            "oversight_steps",
        },
        {
            "task_text", "plan", "ref_labels", "heartbeat_timeout_s",
            "signature", "signature_alg", "capabilities",
        },
    )
    if not isinstance(obj["mission_id"], str) or not obj["mission_id"].strip():
        raise ValueError("mission_id is required")
    if not isinstance(obj["risk_level"], str) or obj["risk_level"] not in RISK_DEFAULTS:
        raise ValueError("invalid risk_level")
    parse_policy(obj["link_loss_policy"])
    obj["heartbeat_timeout_s"] = number(
        obj.get("heartbeat_timeout_s", 5), "heartbeat_timeout_s", minimum=0.001
    )
    if not isinstance(obj["oversight_steps"], list) or any(
        not isinstance(s, str) for s in obj["oversight_steps"]
    ):
        raise ValueError("oversight_steps must be an array of step ids")
    capabilities = obj.get("capabilities", [])
    if not isinstance(capabilities, list) or any(
        not isinstance(capability, str) or not capability.strip()
        for capability in capabilities
    ):
        raise ValueError("capabilities must be an array of names")
    obj["capabilities"] = list(dict.fromkeys(capabilities))
    area = _object(
        obj["operating_area"],
        "operating_area",
        {
            "north_min_m",
            "north_max_m",
            "east_min_m",
            "east_max_m",
            "min_altitude_m",
            "max_altitude_m",
        },
    )
    for key, value in area.items():
        area[key] = number(
            value, key, minimum=-math.inf if key.startswith(("north", "east")) else 0
        )
    if any(
        area[a] >= area[b]
        for a, b in (
            ("north_min_m", "north_max_m"),
            ("east_min_m", "east_max_m"),
            ("min_altitude_m", "max_altitude_m"),
        )
    ):
        raise ValueError("operating limits must have positive extent")
    obj["operating_area"] = area
    budget = _object(
        obj["budget"],
        "budget",
        {
            "time_s",
            "reserve_time_s",
            "energy_wh",
            "reserve_energy_wh",
        },
    )
    for key, value in budget.items():
        budget[key] = number(value, key)
    if (
        budget["time_s"] <= budget["reserve_time_s"]
        or budget["energy_wh"] <= budget["reserve_energy_wh"]
    ):
        raise ValueError("budget must exceed reserve")
    obj["budget"] = budget

    def point(value, name):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{name} must be [north_m, east_m]")
        n, e = (number(v, name, minimum=-math.inf) for v in value)
        if not (
            area["north_min_m"] <= n <= area["north_max_m"]
            and area["east_min_m"] <= e <= area["east_max_m"]
        ):
            raise ValueError(f"{name} outside operating area")
        return (n, e)

    obj["home"] = point(obj["home"], "home")
    refs = {}
    for kind, prefix in (("places", "poi:"), ("zones", "zone:"), ("routes", "route:")):
        if not isinstance(obj[kind], Mapping):
            raise ValueError(f"{kind} must be an object")
        obj[kind] = dict(obj[kind])
        for name, value in obj[kind].items():
            if not isinstance(name, str) or not name.startswith(prefix) or not name[len(prefix) :]:
                raise ValueError(f"invalid {kind} reference {name}")
            if name == "zone:all":
                raise ValueError("zone:all is reserved for the operating area")
            if kind == "places":
                refs[name] = point(value, name)
            elif kind == "routes":
                if not isinstance(value, list) or not value:
                    raise ValueError(f"{name} must contain waypoints")
                refs[name] = tuple(point(p, name) for p in value)
            else:
                zone = _object(
                    value, name, {"centre_ne", "radius_m"}, {"risk_tag", "oversight_required"}
                )
                centre = point(zone["centre_ne"], name)
                radius = number(zone["radius_m"], "radius_m", minimum=0.001)
                if not (
                    area["north_min_m"] <= centre[0] - radius
                    and centre[0] + radius <= area["north_max_m"]
                    and area["east_min_m"] <= centre[1] - radius
                    and centre[1] + radius <= area["east_max_m"]
                ):
                    raise ValueError(f"{name} extends outside operating area")
                if not isinstance(zone.get("oversight_required", False), bool):
                    raise ValueError("oversight_required must be boolean")
                if not isinstance(zone.get("risk_tag", ""), str):
                    raise ValueError("risk_tag must be text")
                obj[kind][name] = zone
                refs[name] = (*centre, radius)
    labels = obj.get("ref_labels", {})
    if not isinstance(labels, Mapping) or any(
        k not in refs or not isinstance(v, str) for k, v in labels.items()
    ):
        raise ValueError("ref_labels must label known onboard references")
    task = obj.get("task_text")
    if task is not None and (not isinstance(task, str) or not task.strip()):
        raise ValueError("task_text must be nonempty text")
    plan = (
        validate_mission_plan(obj["plan"], capabilities=frozenset(obj["capabilities"]))
        if "plan" in obj
        else None
    )
    if plan is None and not task:
        raise ValueError("mission package needs task_text or plan")
    package = MissionPackage(obj, refs, plan)
    if plan is not None:
        package.validate_plan(plan)
    return package


__all__ = [
    "MissionPackage",
    "check_plan_against_package",
    "estimate_plan_duration",
    "load_mission_package",
    "number",
    "parse_policy",
]

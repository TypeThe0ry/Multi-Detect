"""Preflight admission for self-contained onboard mission packages."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mission_plan import MissionPlan, validate_mission_plan
from .serialization import strict_json_loads

POLICIES = ("continue", "continue_then_return", "hold_then_return", "return_now", "land_now")
RISK_DEFAULTS = {"low": "continue", "medium": "continue_then_return", "high": "return_now"}


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


def load_mission_package(raw: Mapping[str, Any] | Path) -> MissionPackage:
    if isinstance(raw, Path):
        raw = strict_json_loads(raw.read_text(encoding="utf-8"), name="mission package")
    raw = deepcopy(raw)
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
        {"task_text", "plan", "ref_labels", "heartbeat_timeout_s"},
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
    plan = validate_mission_plan(obj["plan"]) if "plan" in obj else None
    if plan is None and not task:
        raise ValueError("mission package needs task_text or plan")
    package = MissionPackage(obj, refs, plan)
    if plan is not None:
        package.validate_plan(plan)
    return package

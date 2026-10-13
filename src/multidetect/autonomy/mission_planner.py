"""Compile free task text into a MissionPlan with the slow model (prompt + grammar).

The slow model is asked once per mission, text only. Constrained decoding uses a
per-skill JSON schema built from the SKILLS registry (exact parameter names,
types and bounds), so every generated plan is structurally valid JSON; the host
then runs ``validate_mission_plan`` (references, terminal step, cycles) and can
retry once with the validation error. Pure functions; no model I/O here.
"""

from __future__ import annotations

import json
from typing import Any

from multidetect.autonomy.mission_plan import SKILLS

_PARAM_SCHEMA: dict[str, dict[str, Any]] = {
    "ref": {"type": "string", "minLength": 4, "maxLength": 48},
    "query": {"type": "string", "minLength": 1, "maxLength": 64},
    "altitude": {"type": "number", "minimum": 1, "maximum": 120},
    "distance": {"type": "number", "minimum": 0.5, "maximum": 100},
    "overlap": {"type": "number", "minimum": 0.1, "maximum": 0.95},
    "duration": {"type": "number", "minimum": 1, "maximum": 86400},
    "count": {"type": "integer", "minimum": 1, "maximum": 10000},
    "views": {"type": "integer", "minimum": 1, "maximum": 24},
    "loops": {"type": "integer", "minimum": 1, "maximum": 100},
}

SKILL_HELP: dict[str, str] = {
    "goto": "fly to a place (target_ref)",
    "area_search": "search a region for one object matching query; succeeds when found",
    "count": "search a region and count all objects matching query",
    "inspect": "orbit a found object (target_ref=finding:1) and take views from standoff_m",
    "patrol": "fly a route repeatedly watching for query",
    "survey": "map a region with photo overlap",
    "follow": "follow a moving tracked object",
    "monitor": "watch a region for query for duration_s",
    "record": "save evidence of objects matching query",
    "deliver": "deliver a payload (only if payload capability)",
    "hover": "hold position for duration_s",
    "return_home": "fly back to the take-off point",
    "land": "land",
}

EXAMPLES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "找到红色轿车并记录位置",
        {
            "plan_version": 1,
            "goal": "task",
            "steps": [
                {
                    "id": "s1",
                    "skill": "area_search",
                    "params": {"region": "zone:all", "query": "red car"},
                    "on_success": "s2",
                    "on_fail": "s2",
                },
                {
                    "id": "s2",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["findings", "positions"],
        },
    ),
    (
        "Count the bicycles in the lot.",
        {
            "plan_version": 1,
            "goal": "Count the bicycles in the lot.",
            "steps": [
                {
                    "id": "s1",
                    "skill": "count",
                    "params": {"region": "zone:lot", "query": "bicycle"},
                    "on_success": "s2",
                    "on_fail": "s2",
                },
                {
                    "id": "s2",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["count", "positions"],
        },
    ),
    (
        "找到卡车，从四个方向检查后降落",
        {
            "plan_version": 1,
            "goal": "task",
            "steps": [
                {
                    "id": "s1",
                    "skill": "area_search",
                    "params": {"region": "zone:all", "query": "truck"},
                    "on_success": "s2",
                    "on_fail": "s3",
                },
                {
                    "id": "s2",
                    "skill": "inspect",
                    "params": {
                        "target_ref": "finding:1",
                        "query": "truck",
                        "views": 4,
                        "standoff_m": 12,
                    },
                    "on_success": "s3",
                    "on_fail": "s3",
                },
                {"id": "s3", "skill": "land", "params": {}, "on_success": None, "on_fail": None},
            ],
            "report": ["findings", "views"],
        },
    ),
    (
        "Patrol the north route three times looking for people.",
        {
            "plan_version": 1,
            "goal": "Patrol the north route three times looking for people.",
            "steps": [
                {
                    "id": "s1",
                    "skill": "patrol",
                    "params": {"route_ref": "route:north", "query": "person", "loops": 3},
                    "on_success": "s2",
                    "on_fail": "s2",
                },
                {
                    "id": "s2",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["alerts"],
        },
    ),
    (
        "飞到起降坪悬停一分钟",
        {
            "plan_version": 1,
            "goal": "task",
            "steps": [
                {
                    "id": "s1",
                    "skill": "goto",
                    "params": {"target_ref": "poi:pad"},
                    "on_success": "s2",
                    "on_fail": "s3",
                },
                {
                    "id": "s2",
                    "skill": "hover",
                    "params": {"duration_s": 60},
                    "on_success": "s3",
                    "on_fail": "s3",
                },
                {
                    "id": "s3",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["status"],
        },
    ),
    (
        "Watch the yard for dogs for ten minutes.",
        {
            "plan_version": 1,
            "goal": "Watch the yard for dogs for ten minutes.",
            "steps": [
                {
                    "id": "s1",
                    "skill": "monitor",
                    "params": {"region": "zone:yard", "query": "dog", "duration_s": 600},
                    "on_success": "s2",
                    "on_fail": "s2",
                },
                {
                    "id": "s2",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["alerts"],
        },
    ),
)


def _step_schema(skill: str) -> dict[str, Any]:
    spec = SKILLS[skill]
    params = {name: _PARAM_SCHEMA[spec.param_types[name]] for name in sorted(spec.allowed_params)}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "skill", "params", "on_success", "on_fail"],
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 8},
            "skill": {"type": "string", "enum": [skill]},
            "params": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(spec.required_params),
                "properties": params,
            },
            "on_success": {"type": ["string", "null"], "maxLength": 8},
            "on_fail": {"type": ["string", "null"], "maxLength": 8},
        },
    }


def plan_json_schema(*, capabilities: frozenset[str] = frozenset()) -> dict[str, Any]:
    """MissionPlan schema with exact per-skill parameters (gated skills need capability)."""
    skills = [name for name, spec in SKILLS.items() if not spec.gated or "payload" in capabilities]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["plan_version", "goal", "steps", "report"],
        "properties": {
            "plan_version": {"type": "integer", "enum": [1]},
            "goal": {"type": "string", "minLength": 1, "maxLength": 160},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"anyOf": [_step_schema(name) for name in skills]},
            },
            "report": {
                "type": "array",
                "maxItems": 6,
                "items": {"type": "string", "minLength": 1, "maxLength": 24},
            },
        },
    }


def repair_plan_document(document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Host repairs for common planner slips; returns the plan and what was changed.

    A step that points back to itself or to an earlier step (an unbounded retry)
    is redirected: on_success to the next step, on_fail to the terminal step; a
    non-terminal step whose on_success/on_fail is null continues to the terminal.
    Patrol steps keep their loops. A plan without a terminal step gets a final
    return_home. Every repaired plan still goes through validate_mission_plan.
    """
    steps = [dict(step) for step in document.get("steps", []) if isinstance(step, dict)]
    notes: list[str] = []
    ids = [str(step.get("id")) for step in steps]
    terminal = next(
        (
            str(step["id"])
            for step in reversed(steps)
            if step.get("skill") in {"return_home", "land"}
        ),
        None,
    )
    if terminal is None:
        terminal = "home"
        while terminal in ids:
            terminal += "_"
        steps.append(
            {
                "id": terminal,
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            }
        )
        ids.append(terminal)
        notes.append("appended return_home")
    for step in steps:
        # A non-terminal step must not end the mission: fall through to the terminal.
        if step.get("skill") not in {"return_home", "land"}:
            for key in ("on_success", "on_fail"):
                if step.get(key) is None and step.get("id") != terminal:
                    step[key] = terminal
                    notes.append(f"{step.get('id')}.{key}: None -> {terminal}")
    for index, step in enumerate(steps):
        if step.get("skill") == "patrol":
            continue
        following = ids[index + 1] if index + 1 < len(ids) else None
        for key, fallback in (("on_success", following), ("on_fail", terminal)):
            ref = step.get(key)
            if ref in ids and ids.index(ref) <= index:
                replacement = fallback if fallback != step.get("id") else None
                step[key] = replacement
                notes.append(f"{step.get('id')}.{key}: {ref} -> {replacement}")
    return {**document, "steps": steps}, notes


def plan_prompt(task_text: str, *, refs: tuple[str, ...] = (), error: str | None = None) -> str:
    """Instruction for the slow model: skills, reference rules, examples, task."""
    lines = [
        "You plan drone missions. Turn the task into a JSON mission plan of bounded skills.",
        "Skills (params in brackets, * = required):",
    ]
    for name, spec in SKILLS.items():
        if spec.gated:
            continue
        params = ", ".join(
            ("*" if p in spec.required_params else "") + p for p in sorted(spec.allowed_params)
        )
        lines.append(f"- {name} [{params}]: {SKILL_HELP[name]}")
    lines += [
        "References: home; zone:<name> for areas (zone:all = the whole area); poi:<name> for "
        "places; route:<name>; finding:N = the N-th object found earlier.",
        "query: a short noun phrase in English (colour + object), e.g. 'red car'.",
        "Every step names on_success and on_fail (a step id or null). Steps never point back "
        "to themselves or to an earlier step (no retries): on_fail goes to the next step or to "
        "the final step. The plan must end with return_home or land; use return_home unless the "
        "task says to land. Use only the steps the task asks for: no extra search, goto, "
        "record, inspect or hover that the task does not mention.",
    ]
    if refs:
        lines.append("Known places: " + ", ".join(refs) + ".")
    for text, plan in EXAMPLES:
        lines.append(f"Task: {text}\nPlan: {json.dumps(plan, ensure_ascii=False)}")
    if error:
        lines.append(f"The previous plan was rejected: {error}. Fix it.")
    lines.append(f"Task: {task_text}\nPlan:")
    return "\n".join(lines)


__all__ = [
    "EXAMPLES",
    "SKILL_HELP",
    "plan_json_schema",
    "plan_prompt",
    "repair_plan_document",
]

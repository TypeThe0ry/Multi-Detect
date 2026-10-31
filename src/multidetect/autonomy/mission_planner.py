"""Compile free task text into a MissionPlan with the slow model (prompt + grammar).

The slow model is asked once per mission, text only. Constrained decoding uses a
per-skill JSON schema built from the SKILLS registry (exact parameter names,
types and bounds), so every generated plan is structurally valid JSON; the host
then runs ``validate_mission_plan`` (references, terminal step, cycles) and can
retry once with the validation error. The validated plan is then checked against
the task text (``plan_intent``: return-home request, invented objects, places
the task does not name); a contradiction is fed back like any other rejection,
and the last attempt gets a deterministic repair or no plan. Model I/O is
supplied by the caller.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from multidetect.autonomy.mission_package import check_plan_against_package
from multidetect.autonomy.capability_contract import capability_violations
from multidetect.autonomy.mission_plan import (
    PLAN_DECISION_JSON_SCHEMAS,
    SKILLS,
    MissionPlan,
    PlanDecision,
    validate_mission_plan,
    validate_plan_decision,
)
from multidetect.autonomy.plan_intent import (
    check_plan_intent,
    intent_error,
    repair_plan_intent,
)
from multidetect.autonomy.serialization import AutonomyContractError

_PARAM_SCHEMA: dict[str, dict[str, Any]] = {
    "ref": {"type": "string", "minLength": 4, "maxLength": 48},
    "region": {"type": "string", "minLength": 6, "maxLength": 48, "pattern": "^zone:.+"},
    "query": {"type": "string", "minLength": 1, "maxLength": 64},
    "altitude": {"type": "number", "minimum": 1, "maximum": 120},
    "distance": {"type": "number", "minimum": 0.5, "maximum": 100},
    "overlap": {"type": "number", "minimum": 0.1, "maximum": 0.95},
    "duration": {"type": "number", "minimum": 1, "maximum": 86400},
    "count": {"type": "integer", "minimum": 1, "maximum": 10000},
    "views": {"type": "integer", "minimum": 1, "maximum": 24},
    "loops": {"type": "integer", "minimum": 1, "maximum": 100},
}

# Optional params the executor accepts but the onboard model was never trained to emit.
# Leaving them out keeps the planning prompt byte-identical to the training prompt;
# advertise them only together with planning SFT data that uses them.
_UNADVERTISED_PARAMS = frozenset({"min_views_seen"})

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


def plan_json_schema(
    *, capabilities: frozenset[str] = frozenset(), include_decision_contract: bool = False
) -> dict[str, Any]:
    """MissionPlan schema with exact per-skill parameters (gated skills need capability)."""
    skills = [
        name for name, spec in SKILLS.items()
        if not spec.gated or "payload" in capabilities
        or include_decision_contract and "payload_drop" in capabilities
    ]
    mission_schema = {
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
    if include_decision_contract:
        # Closed, mutually exclusive branches keep constrained decoding compatible
        # with llama.cpp while allowing legacy MissionPlan documents.
        return {"anyOf": [mission_schema, *PLAN_DECISION_JSON_SCHEMAS]}
    return mission_schema


def repair_plan_document(document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Host repairs for common planner slips; returns the plan and what was changed.

    A step that points back to itself or to an earlier step (an unbounded retry)
    is redirected: on_success to the next step, on_fail to the terminal step; a
    non-terminal step whose on_success/on_fail is null continues to the terminal.
    Patrol steps keep their loops. A plan without a terminal step gets a final
    return_home. Callers such as ``compile_mission_plan`` validate the repaired
    document before accepting it; this helper performs only the shallow rewrite.
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
    terminal_skills = {"return_home", "land"}
    for index, step in enumerate(steps):
        if step.get("skill") in terminal_skills:
            for key in ("on_success", "on_fail"):
                if step.get(key) is not None:
                    notes.append(f"{step.get('id')}.{key}: {step[key]} -> None (terminal)")
                    step[key] = None
            continue
        following = ids[index + 1] if index + 1 < len(ids) else None
        for key, fallback in (("on_success", following or terminal), ("on_fail", terminal)):
            ref = step.get(key)
            if ref is None or ref not in ids or ids.index(ref) <= index:
                replacement = fallback
                step[key] = replacement
                if ref != replacement:
                    notes.append(f"{step.get('id')}.{key}: {ref} -> {replacement}")
    return {**document, "steps": steps}, notes


PLAN_PROMPT_VARIANTS = ("default", "no_examples", "two_examples", "compact_rules", "compact")


def plan_prompt_parts(
    task_text: str,
    *,
    refs: tuple[str, ...] = (),
    error: str | None = None,
    prompt_variant: str = "default",
    capabilities: frozenset[str] = frozenset(),
    airframe: Any = None,
    include_decision_contract: bool = False,
) -> dict[str, str]:
    """Ordered prompt sections, including delimiters; concatenate values to render.

    Short variants are experimental opt-ins; the fine-tuned default stays exact.
    ``compact_rules`` changes only rules; ``compact`` also removes all examples.
    """
    if prompt_variant not in PLAN_PROMPT_VARIANTS:
        raise ValueError(f"unknown planning prompt variant: {prompt_variant}")
    lines = ["Skills (params in brackets, * = required):"]
    for name, spec in SKILLS.items():
        if spec.gated and not ("payload" in capabilities or include_decision_contract and "payload_drop" in capabilities):
            continue
        params = ", ".join(
            ("*" if p in spec.required_params else "") + p
            for p in sorted(spec.allowed_params - _UNADVERTISED_PARAMS)
        )
        lines.append(f"- {name} [{params}]: {SKILL_HELP[name]}")
    if capabilities:
        lines.append(
            "Available aircraft capabilities: " + ", ".join(sorted(capabilities)) + ". "
            "The mission package may remove capabilities but cannot grant new ones; never emit a "
            "skill whose required capability is unavailable."
        )
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
    if include_decision_contract:
        lines.append("If the task cannot be safely or faithfully performed, output a decision object instead of a mission plan: reject for unsupported/unsafe requests, clarify when a required detail is ambiguous. Use reason_code missing_capability, unknown_place, ungrounded_query, ambiguous_task, or unsafe_request. Example reject: {\"plan_version\":1,\"decision\":\"reject\",\"reason_code\":\"missing_capability\",\"reason\":\"No payload drop capability.\",\"missing_capabilities\":[\"payload_drop\"],\"unresolved_refs\":[],\"question\":null}. Example clarify: {\"plan_version\":1,\"decision\":\"clarify\",\"reason_code\":\"ambiguous_task\",\"reason\":\"Place is unclear.\",\"missing_capabilities\":[],\"unresolved_refs\":[],\"question\":\"Which place do you mean?\"}. Never substitute a different executable task.")
    if prompt_variant in {"compact_rules", "compact"}:
        # Keep the exact skill names/parameter list; grammar supplies their types.
        lines = ["Skills (* required):"]
        for name, spec in SKILLS.items():
            if not spec.gated or "payload" in capabilities or include_decision_contract and "payload_drop" in capabilities:
                params = ",".join(
                    ("*" if p in spec.required_params else "") + p
                    for p in sorted(spec.allowed_params - _UNADVERTISED_PARAMS)
                )
                lines.append(f"{name}[{params}]")
        lines += [
            "Refs: home; zone:name (zone:all=whole area); poi:name; route:name; "
            "finding:N=Nth object found earlier; track:N=tracked object.",
            "query: short English colour/object noun phrase.",
            "Each step has on_success/on_fail: later step id or null. No backward/self "
            "edges or retries; failure goes next/final. End return_home unless task "
            "requests land. Include only requested actions.",
        ]
        if include_decision_contract:
            lines.append("Unsupported, unsafe, unknown-place, ungrounded-query, or ambiguous tasks require the structured reject/clarify decision; never emit a substitute mission. Reject example: {\"plan_version\":1,\"decision\":\"reject\",\"reason_code\":\"missing_capability\",\"reason\":\"No payload capability.\",\"missing_capabilities\":[\"payload_drop\"],\"unresolved_refs\":[],\"question\":null}. Clarify example: {\"plan_version\":1,\"decision\":\"clarify\",\"reason_code\":\"ambiguous_task\",\"reason\":\"Place is unclear.\",\"missing_capabilities\":[],\"unresolved_refs\":[],\"question\":\"Which place do you mean?\"}.")
    examples = EXAMPLES
    if prompt_variant in {"no_examples", "compact"}:
        examples = ()
    elif prompt_variant == "two_examples":
        examples = EXAMPLES[:2]
    return {
        "system": "You plan drone missions. Turn the task into a JSON mission plan "
        "of bounded skills.\n",
        "rules": "\n".join(lines) + "\n",
        "refs": "Known places: " + ", ".join(refs) + ".\n" if refs else "",
        "examples": "".join(
            f"Task: {text}\nPlan: {json.dumps(plan, ensure_ascii=False)}\n"
            for text, plan in examples
        ),
        "repair": f"The previous plan was rejected: {error}. Fix it.\n" if error else "",
        "task": f"Task: {task_text}\nPlan:",
    }


def plan_prompt(
    task_text: str,
    *,
    refs: tuple[str, ...] = (),
    error: str | None = None,
    prompt_variant: str = "default",
    capabilities: frozenset[str] = frozenset(),
    include_decision_contract: bool = False,
) -> str:
    """Instruction for the slow model; defaults match the training prompt exactly."""
    return "".join(
        plan_prompt_parts(
            task_text,
            refs=refs,
            error=error,
            prompt_variant=prompt_variant,
            capabilities=capabilities,
            include_decision_contract=include_decision_contract,
        ).values()
    )


_REF_PARAMS = ("region", "target_ref", "route_ref", "track_ref")


def _builtin_ref(ref: str) -> bool:
    return ref in {"home", "zone:all"} or ref.split(":", 1)[0] in {"finding", "track"}


def check_plan_refs(
    document: dict[str, Any], refs: tuple[str, ...], *, substitute: bool
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Find references the host does not know; optionally map each to the only same-kind ref.

    Returns (document, notes, unknown). Wrong-kind or unknown region references
    stay unknown unless exactly one known zone is available for an explicit
    host-side substitution; they are never widened to ``zone:all``.
    """
    # Known places may carry a label: "zone:site (the site)".
    known = {ref.split(" ", 1)[0] for ref in refs}
    notes: list[str] = []
    unknown: list[str] = []
    for step in document.get("steps", []):
        params = step.get("params") or {}
        for key in _REF_PARAMS:
            ref = params.get(key)
            if not isinstance(ref, str):
                continue
            prefix = ref.split(":", 1)[0]
            # Region parameters describe bounded areas, never points, routes,
            # tracks or findings. Check role before known/builtin short-circuit.
            if key == "region" and prefix != "zone":
                zones = sorted(r for r in known if r.startswith("zone:"))
                if substitute and len(zones) == 1:
                    replacement = zones[0]
                    params[key] = replacement
                    notes.append(f"{step.get('id')}.{key}: {ref} -> {replacement}")
                else:
                    # A wrong-kind region cannot safely fall back to the
                    # unbounded whole-area spelling.  Keep it unknown so the
                    # planner retries/rejects it and the executor reports a
                    # bounded-region failure instead of searching globally.
                    unknown.append(ref)
                continue
            if _builtin_ref(ref) or ref in known:
                continue
            same_kind = sorted(r for r in known if r.split(":", 1)[0] == prefix)
            if substitute and len(same_kind) == 1:
                params[key] = same_kind[0]
                notes.append(f"{step.get('id')}.{key}: {ref} -> {same_kind[0]}")
            else:
                unknown.append(ref)
    return document, notes, unknown


# Intent findings the host may repair on the last attempt (land -> return_home,
# drop an invented-object step).  A place the task does not name is never
# replaced by another: the plan is rejected instead.
_INTENT_REPAIRABLE = frozenset({"return_intent_land", "ungrounded_query"})


def compile_mission_plan(
    post: Callable[[str, dict], str],
    task_text: str,
    *,
    refs: tuple[str, ...] = (),
    schema: dict[str, Any] | None = None,
    attempts: int = 2,
    prompt_variant: str = "default",
    plan_checker: Callable[[MissionPlan], Any] | None = None,
    capabilities: frozenset[str] = frozenset(),
    airframe: Any = None,
    check_intent: bool = True,
    include_decision_contract: bool = False,
) -> tuple[MissionPlan | PlanDecision | None, dict[str, Any]]:
    """Compile, repair and validate a plan, retrying with the rejection reason.

    Unknown references are always rejected (with no known places, every named
    place is unknown). With ``check_intent`` the plan must also agree with the
    task text (see ``plan_intent``); on the last attempt a return-home or
    invented-object contradiction is repaired deterministically, an
    ungrounded place never is.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if prompt_variant not in PLAN_PROMPT_VARIANTS:
        raise ValueError(f"unknown planning prompt variant: {prompt_variant}")
    schema = schema if schema is not None else plan_json_schema(
        capabilities=capabilities, include_decision_contract=include_decision_contract
    )
    report: dict[str, Any] = {"raw": "", "attempts": [], "repairs": [], "error": None}
    error: str | None = None
    for attempt in range(attempts):
        started = time.perf_counter()
        report.pop("package_violations", None)
        report.pop("intent_violations", None)
        report["raw"] = ""
        last = attempt == attempts - 1
        try:
            raw = post(
                plan_prompt(
                    task_text,
                    refs=refs,
                    error=error,
                    prompt_variant=prompt_variant,
                    capabilities=capabilities,
                    include_decision_contract=include_decision_contract,
                ),
                schema,
            )
        except Exception as exc:  # A model/transport failure must leave the caller a safe fallback.
            error = str(exc)[:200]
        else:
            report["raw"] = raw
            try:
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError("mission plan must be a JSON object")
                if document.get("decision") in {"reject", "clarify"}:
                    decision = validate_plan_decision(document)
                    report["decision"] = decision
                    report["error"] = None
                    return decision, report
                document, notes = repair_plan_document(document)
                document, ref_notes, unknown = check_plan_refs(
                    document, tuple(refs), substitute=last
                )
                if unknown:
                    raise ValueError(
                        f"unknown reference(s) {', '.join(sorted(set(unknown)))}; "
                        "use only: "
                        + ", ".join(sorted(r.split(" ", 1)[0] for r in refs) + ["zone:all", "home"])
                    )
                notes = notes + ref_notes
                capability_findings = capability_violations(document, capabilities, airframe=airframe)
                if capability_findings:
                    report["intent_violations"] = capability_findings
                    raise ValueError(
                        "mission capability violations: "
                        + "; ".join(item["message"] for item in capability_findings)
                    )
                plan = validate_mission_plan(document, capabilities=capabilities)
                capability_findings = capability_violations(plan, capabilities, airframe=airframe)
                if capability_findings:
                    report["intent_violations"] = capability_findings
                    raise ValueError(
                        "mission capability violations: "
                        + "; ".join(item["message"] for item in capability_findings)
                    )
                intent_warnings: list[dict[str, Any]] = []
                if check_intent:
                    findings = check_plan_intent(plan, task_text, refs)
                    blocking = [f for f in findings if f.blocking]
                    if blocking and last and all(f.code in _INTENT_REPAIRABLE for f in blocking):
                        document, intent_notes = repair_plan_intent(document, blocking)
                        plan = validate_mission_plan(document, capabilities=capabilities)
                        notes = notes + intent_notes
                        findings = check_plan_intent(plan, task_text, refs)
                        blocking = [f for f in findings if f.blocking]
                    if blocking:
                        report["intent_violations"] = [f.as_dict() for f in blocking]
                        raise ValueError(intent_error(blocking))
                    intent_warnings = [f.as_dict() for f in findings if not f.blocking]
                if plan_checker is not None:
                    violations = plan_checker(plan)
                    if violations:
                        report["package_violations"] = list(violations)
                        messages = []
                        for violation in violations:
                            if isinstance(violation, Mapping):
                                code = violation.get("code", "package policy")
                                message = violation.get("message") or (
                                    f"{violation.get('message_zh', '')} / "
                                    f"{violation.get('message_en', '')}"
                                )
                            else:
                                code = getattr(violation, "code", "package policy")
                                message = getattr(violation, "message", str(violation))
                            messages.append(f"{code}: {message}")
                        raise ValueError(
                            "mission package policy violations: "
                            + "; ".join(messages)
                        )
            except (AutonomyContractError, ValueError, TypeError, KeyError, AttributeError) as exc:
                error = str(exc)[:200]
            else:
                report["repairs"] = notes
                report.pop("package_violations", None)
                report.pop("intent_violations", None)
                if intent_warnings:
                    report["intent_warnings"] = intent_warnings
                report["error"] = None
                return plan, report
        finally:
            report["attempts"].append(round(time.perf_counter() - started, 2))
    report["error"] = error
    return None, report


__all__ = [
    "check_plan_against_package",
    "check_plan_refs",
    "EXAMPLES",
    "PLAN_PROMPT_VARIANTS",
    "SKILL_HELP",
    "compile_mission_plan",
    "plan_json_schema",
    "plan_prompt",
    "plan_prompt_parts",
    "repair_plan_document",
]

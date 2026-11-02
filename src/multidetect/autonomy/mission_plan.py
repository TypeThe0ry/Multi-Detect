# ruff: noqa
"""Strict mission-plan contract for multi-purpose onboard autonomy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .onboard_decision import OnboardDecision
from .serialization import (
    AutonomyContractError,
    require_int,
    require_real,
    require_text,
    strict_json_loads,
    strict_object,
)


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    allowed_params: frozenset[str]
    required_params: frozenset[str]
    completion: str
    needs_query: bool = False
    gated: bool = False
    param_types: Mapping[str, str] = field(default_factory=dict)


def _spec(name: str, required: set[str], allowed: set[str], completion: str, *, query=False, gated=False, types=None) -> SkillSpec:
    return SkillSpec(name, frozenset(allowed), frozenset(required), completion, query, gated, types or {})


SKILLS: dict[str, SkillSpec] = {
    "goto": _spec("goto", {"target_ref"}, {"target_ref", "altitude_m"}, "arrived", types={"target_ref": "ref", "altitude_m": "altitude"}),
    "area_search": _spec("area_search", {"region", "query"}, {"region", "query", "altitude_m"}, "region_covered", query=True, types={"region": "region", "query": "query", "altitude_m": "altitude"}),
    "count": _spec("count", {"region", "query"}, {"region", "query", "max_count", "altitude_m"}, "count_reported", query=True, types={"region": "region", "query": "query", "max_count": "count", "altitude_m": "altitude"}),
    "inspect": _spec("inspect", {"target_ref", "query", "views", "standoff_m"}, {"target_ref", "query", "views", "min_views_seen", "standoff_m", "altitude_m"}, "views_captured", query=True, types={"target_ref": "ref", "query": "query", "views": "views", "min_views_seen": "views", "standoff_m": "distance", "altitude_m": "altitude"}),
    "patrol": _spec("patrol", {"route_ref", "query", "loops"}, {"route_ref", "query", "loops", "duration_s", "altitude_m"}, "duration_elapsed", query=True, types={"route_ref": "ref", "query": "query", "loops": "loops", "duration_s": "duration", "altitude_m": "altitude"}),
    "survey": _spec("survey", {"region", "overlap"}, {"region", "overlap", "altitude_m"}, "region_covered", types={"region": "region", "overlap": "overlap", "altitude_m": "altitude"}),
    "follow": _spec("follow", {"track_ref", "query"}, {"track_ref", "query", "duration_s", "altitude_m"}, "target_lost_timeout", query=True, types={"track_ref": "ref", "query": "query", "duration_s": "duration", "altitude_m": "altitude"}),
    "monitor": _spec("monitor", {"region", "query", "duration_s"}, {"region", "query", "duration_s", "altitude_m"}, "duration_elapsed", query=True, types={"region": "region", "query": "query", "duration_s": "duration", "altitude_m": "altitude"}),
    "record": _spec("record", {"query"}, {"query", "max_count"}, "views_captured", query=True, types={"query": "query", "max_count": "count"}),
    "deliver": _spec("deliver", {"target_ref", "payload_ref"}, {"target_ref", "payload_ref", "altitude_m"}, "arrived", gated=True, types={"target_ref": "ref", "payload_ref": "ref", "altitude_m": "altitude"}),
    "hover": _spec("hover", {"duration_s"}, {"duration_s", "region", "altitude_m"}, "duration_elapsed", types={"duration_s": "duration", "region": "region", "altitude_m": "altitude"}),
    "return_home": _spec("return_home", set(), {"altitude_m"}, "arrived", types={"altitude_m": "altitude"}),
    "land": _spec("land", set(), {"target_ref"}, "landed", types={"target_ref": "ref"}),
}
SKILLS = MappingProxyType(SKILLS)

MISSION_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["plan_version", "goal", "steps", "report"],
    "properties": {
        "plan_version": {"type": "integer", "const": 1},
        "goal": {"type": "string", "minLength": 1, "maxLength": 240},
        "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "skill", "params", "on_success", "on_fail"],
            "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 32}, "skill": {"type": "string", "enum": list(SKILLS)}, "params": {"type": "object"}, "on_success": {"type": ["string", "null"]}, "on_fail": {"type": ["string", "null"]}},
        }},
        "report": {"type": "array", "maxItems": 16, "items": {"type": "string", "minLength": 1, "maxLength": 32}},
    },
}

PLAN_DECISION_CODES = frozenset({"missing_capability", "unknown_place", "ungrounded_query", "ambiguous_task", "unsafe_request"})
_PLAN_DECISION_PROPERTIES: dict[str, Any] = {
    "plan_version": {"type": "integer", "enum": [1]},
    "reason_code": {"type": "string", "enum": sorted(PLAN_DECISION_CODES)},
    "reason": {"type": "string", "minLength": 1, "maxLength": 240},
    "missing_capabilities": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 64}},
    "unresolved_refs": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 96}},
}
_PLAN_DECISION_REQUIRED = ["plan_version", "decision", "reason_code", "reason", "missing_capabilities", "unresolved_refs", "question"]
PLAN_REJECT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": _PLAN_DECISION_REQUIRED,
    "properties": {**_PLAN_DECISION_PROPERTIES, "decision": {"type": "string", "enum": ["reject"]}, "question": {"type": "null"}},
}
PLAN_CLARIFY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": _PLAN_DECISION_REQUIRED,
    "properties": {**_PLAN_DECISION_PROPERTIES, "decision": {"type": "string", "enum": ["clarify"]}, "question": {"type": "string", "minLength": 1, "maxLength": 240}},
}
PLAN_DECISION_JSON_SCHEMAS = (PLAN_REJECT_JSON_SCHEMA, PLAN_CLARIFY_JSON_SCHEMA)


@dataclass(frozen=True, slots=True)
class PlanDecision:
    plan_version: int
    decision: str
    reason_code: str
    reason: str
    missing_capabilities: tuple[str, ...] = ()
    unresolved_refs: tuple[str, ...] = ()
    question: str | None = None


def validate_plan_decision(raw: str | bytes | Mapping[str, Any]) -> PlanDecision:
    doc = strict_json_loads(raw, name="plan decision") if isinstance(raw, (str, bytes)) else raw
    obj = strict_object(doc, name="plan decision", required=frozenset(_PLAN_DECISION_REQUIRED))
    if set(obj) != set(_PLAN_DECISION_REQUIRED):
        raise AutonomyContractError("plan decision cannot contain mission-plan fields")
    version = require_int(obj["plan_version"], "plan_version", minimum=1, maximum=1)
    decision = require_text(obj["decision"], "decision")
    if decision not in {"reject", "clarify"}: raise AutonomyContractError("decision must be reject or clarify")
    code = require_text(obj["reason_code"], "reason_code")
    if code not in PLAN_DECISION_CODES: raise AutonomyContractError("unsupported reason_code")
    reason = require_text(obj["reason"], "reason")
    if len(reason) > 240: raise AutonomyContractError("reason must be at most 240 characters")
    def strings(key: str, maximum: int) -> tuple[str, ...]:
        value = obj[key]
        if not isinstance(value, list) or len(value) > maximum: raise AutonomyContractError(f"{key} must be an array")
        result = tuple(require_text(item, f"{key}[]") for item in value)
        if any(len(item) > (64 if key == "missing_capabilities" else 96) for item in result): raise AutonomyContractError(f"{key} item too long")
        return result
    missing = strings("missing_capabilities", 8)
    unresolved = strings("unresolved_refs", 8)
    question = obj["question"]
    if decision == "clarify":
        question = require_text(question, "question")
        if len(question) > 240: raise AutonomyContractError("question must be at most 240 characters")
    elif question is not None:
        raise AutonomyContractError("reject decision question must be null")
    return PlanDecision(version, decision, code, reason, missing, unresolved, question)


def _param(value: object, kind: str, name: str) -> object:
    if kind == "region":
        text = require_text(value, name)
        # Region parameters are deliberately narrower than ordinary symbolic
        # references.  A point, route, track, or finding cannot bound a search
        # area; ``zone:all`` is the explicit whole-area form.
        if len(text) > 96 or not text.startswith("zone:") or len(text) == len("zone:"):
            raise AutonomyContractError(f"{name} must be a zone reference")
        return text
    if kind in {"ref"}:
        text = require_text(value, name)
        if len(text) > 96 or ":" not in text and text != "home":
            raise AutonomyContractError(f"{name} must be a symbolic reference")
        return text
    if kind == "query":
        text = require_text(value, name)
        if len(text.split()) > 8 or len(text) > 96:
            raise AutonomyContractError(f"{name} must be at most 8 words")
        return text
    if kind == "altitude": return require_real(value, name, minimum=1.0, maximum=120.0)
    if kind == "distance": return require_real(value, name, minimum=0.5, maximum=100.0)
    if kind == "overlap": return require_real(value, name, minimum=0.1, maximum=0.95)
    if kind == "duration": return require_real(value, name, minimum=1.0, maximum=86400.0)
    if kind == "count": return require_int(value, name, minimum=1, maximum=10000)
    if kind == "views": return require_int(value, name, minimum=1, maximum=24)
    if kind == "loops": return require_int(value, name, minimum=1, maximum=100)
    raise AutonomyContractError(f"unsupported parameter type {kind}")


@dataclass(frozen=True, slots=True)
class MissionStep:
    id: str
    skill: str
    params: Mapping[str, object]
    on_success: str | None
    on_fail: str | None


@dataclass(frozen=True, slots=True)
class MissionPlan:
    plan_version: int
    goal: str
    steps: tuple[MissionStep, ...]
    report: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillStatus:
    step_id: str
    state: str
    completion: str
    progress: float = 0.0


@dataclass(frozen=True, slots=True)
class MissionReport:
    findings: tuple[str, ...] = ()
    count: int | None = None
    evidence_frame_ids: tuple[str, ...] = ()
    track_ids: tuple[str, ...] = ()
    positions: tuple[str, ...] = ()
    state_answers: tuple[str, ...] = ()
    alerts: tuple[str, ...] = ()
    landing_positions: tuple[str, ...] = ()


def validate_mission_plan(raw: str | bytes | Mapping[str, Any], *, capabilities: frozenset[str] = frozenset()) -> MissionPlan:
    doc = strict_json_loads(raw, name="mission plan") if isinstance(raw, (str, bytes)) else raw
    obj = strict_object(doc, name="mission plan", required=frozenset({"plan_version", "goal", "steps", "report"}))
    version = require_int(obj["plan_version"], "plan_version", minimum=1, maximum=1)
    goal = require_text(obj["goal"], "goal")
    if len(goal) > 240: raise AutonomyContractError("goal must be at most 240 characters")
    steps_raw = obj["steps"]
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= 12: raise AutonomyContractError("steps must contain 1 to 12 items")
    report_raw = obj["report"]
    if not isinstance(report_raw, list) or len(report_raw) > 16: raise AutonomyContractError("report must be an array of at most 16 items")
    report = tuple(require_text(v, f"report[{i}]") for i, v in enumerate(report_raw))
    ids: set[str] = set(); steps: list[MissionStep] = []
    for i, value in enumerate(steps_raw):
        item = strict_object(value, name=f"steps[{i}]", required=frozenset({"id", "skill", "params", "on_success", "on_fail"}))
        sid = require_text(item["id"], f"steps[{i}].id")
        if sid in ids: raise AutonomyContractError(f"duplicate step id {sid!r}")
        if len(sid) > 32: raise AutonomyContractError("step id must be at most 32 characters")
        ids.add(sid)
        skill = require_text(item["skill"], f"steps[{i}].skill")
        spec = SKILLS.get(skill)
        if spec is None: raise AutonomyContractError(f"unknown skill {skill!r}")
        if spec.gated and not ({"payload", "payload_drop"} & set(capabilities)):
            raise AutonomyContractError("deliver requires payload capability (payload_drop)")
        params = strict_object(item["params"], name=f"steps[{i}].params", required=spec.required_params, optional=spec.allowed_params - spec.required_params)
        parsed = {key: _param(value, spec.param_types[key], f"steps[{i}].params.{key}") for key, value in params.items()}
        ons = item["on_success"]; onf = item["on_fail"]
        for label, ref in (("on_success", ons), ("on_fail", onf)):
            if ref is not None and (not isinstance(ref, str) or not ref.strip()): raise AutonomyContractError(f"steps[{i}].{label} must be a step id or null")
        steps.append(MissionStep(sid, skill, parsed, ons, onf))
    known = ids
    for step in steps:
        for label, ref in (("on_success", step.on_success), ("on_fail", step.on_fail)):
            if ref is not None and ref not in known: raise AutonomyContractError(f"{step.id}.{label} references unknown step {ref!r}")
    terminals = {s.id for s in steps if s.skill in {"return_home", "land"}}
    if not terminals: raise AutonomyContractError("mission plan must have a terminal return_home or land step")
    _check_graph_termination(steps, terminals)
    return MissionPlan(version, goal, tuple(steps), report)


def _check_graph_termination(steps: list[MissionStep], terminals: set[str]) -> None:
    """Ensure every graph branch reaches return_home/land in bounded steps.

    Skills such as ``patrol`` have an internal finite loop count, but that does
    not make a graph edge back to an earlier step safe.  A graph cycle or a
    null edge on a non-terminal step can otherwise leave the vehicle executing
    forever, so both are rejected by the contract.
    """
    by_id = {s.id: s for s in steps}
    for step in steps:
        if step.id in terminals and (step.on_success is not None or step.on_fail is not None):
            raise AutonomyContractError(
                f"terminal step {step.id!r} must have on_success/on_fail null"
            )

    def visit(sid: str, path: tuple[str, ...]) -> None:
        if sid in path:
            raise AutonomyContractError("step graph contains a cycle")
        step = by_id[sid]
        if sid in terminals:
            return
        for label, ref in (("on_success", step.on_success), ("on_fail", step.on_fail)):
            if ref is None:
                raise AutonomyContractError(
                    f"non-terminal step {step.id!r}.{label} must reach a terminal step"
                )
            # Unknown refs are checked before this function, but keep the
            # guard here so this invariant remains local if callers evolve.
            if ref not in by_id:
                raise AutonomyContractError(f"{step.id}.{label} references unknown step {ref!r}")
            visit(ref, path + (sid,))

    # Validate all nodes so an unreachable malformed branch cannot be activated
    # later by a host-side edge change.  The first-step requirement is included
    # naturally because it is the first node visited.
    for sid in by_id:
        visit(sid, ())


def interpret_step_decision(step: MissionStep, decision: OnboardDecision) -> str:
    """Map legacy per-observation decisions into the active skill semantics."""
    if decision.decision == "confirm_target" and step.skill == "count": return "candidate_count_pending_host_verification"
    if decision.decision == "confirm_target" and step.skill in {"inspect", "record"}: return "target_verified_for_evidence"
    if decision.decision == "follow_track" and step.skill == "follow": return "track_follow_requested"
    if decision.decision == "record": return "evidence_capture_requested"
    return decision.decision


def adapt_skill_for_airframe(step: MissionStep, airframe: Any) -> tuple[str, str | None]:
    """Host-side semantic adaptation that leaves the planning prompt untouched.

    The language model still emits the trained ``hover``/``deliver`` skills.
    Execution records the replacement explicitly so a fixed-wing vehicle never
    receives a physically impossible zero-speed command.
    """
    category = getattr(airframe, "category", str(airframe))
    if category == "fixed_wing" and step.skill == "hover":
        return "loiter", "hover replaced with fixed-wing observation loiter"
    if category == "fixed_wing" and step.skill == "deliver":
        return "reject", "fixed-wing delivery requires an explicitly validated fly-by window"
    return step.skill, None


__all__ = ["SKILLS", "SkillSpec", "MissionStep", "MissionPlan", "PlanDecision", "PLAN_DECISION_CODES", "SkillStatus", "MissionReport", "MISSION_PLAN_JSON_SCHEMA", "PLAN_REJECT_JSON_SCHEMA", "PLAN_CLARIFY_JSON_SCHEMA", "PLAN_DECISION_JSON_SCHEMAS", "validate_mission_plan", "validate_plan_decision", "interpret_step_decision", "adapt_skill_for_airframe"]

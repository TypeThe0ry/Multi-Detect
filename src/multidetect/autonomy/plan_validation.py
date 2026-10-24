"""Prompt-aware deterministic admission for model-authored mission plans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .mission_package import MissionPackage, check_plan_against_package
from .mission_plan import MissionPlan, validate_mission_plan
from .plan_intent import check_plan_intent


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    decision: str
    plan: MissionPlan | None
    reasons_zh: tuple[str, ...] = ()
    questions_zh: tuple[str, ...] = ()
    repairs: tuple[str, ...] = ()


_VIOLATION_ZH = {
    "OUTSIDE_OPERATING_AREA": "任务区域或航线超出任务包作业区。",
    "NO_FLY_ZONE": "任务区域或航线进入禁飞区。",
    "ALTITUDE_OUT_OF_BOUNDS": "计划高度超出任务包高度包络。",
    "TIME_BUDGET_EXCEEDED": "计划时长超过扣除预留后的任务预算。",
    "ENERGY_BUDGET_EXCEEDED": "计划能耗超过扣除预留后的能源预算。",
    "MISSING_CAPABILITY": "计划需要当前平台不具备的能力。",
    "TERMINAL_UNREACHABLE": "步骤图不能保证到达安全终点。",
    "NO_FLY_ZONE_LIMIT_EXCEEDED": "任务包禁飞区数量超出校验上限。",
}


def _as_plan(plan: Any, capabilities: frozenset[str]) -> MissionPlan:
    if isinstance(plan, MissionPlan):
        # Re-parse hand-built objects through the established strict contract.
        return validate_mission_plan({
            "plan_version": plan.plan_version,
            "goal": plan.goal,
            "steps": [{"id": s.id, "skill": s.skill, "params": dict(s.params),
                       "on_success": s.on_success, "on_fail": s.on_fail} for s in plan.steps],
            "report": list(plan.report),
        }, capabilities=capabilities)
    return validate_mission_plan(plan, capabilities=capabilities)


def _repair_missing_terminal(plan: Any) -> tuple[Any, str | None]:
    """Add a bounded return edge only when the model omitted a terminal."""
    if isinstance(plan, MissionPlan):
        raw = {
            "plan_version": plan.plan_version,
            "goal": plan.goal,
            "steps": [{"id": s.id, "skill": s.skill, "params": dict(s.params),
                       "on_success": s.on_success, "on_fail": s.on_fail} for s in plan.steps],
            "report": list(plan.report),
        }
    elif isinstance(plan, dict):
        raw = dict(plan)
        raw["steps"] = [dict(step) for step in raw.get("steps", [])]
    else:
        return plan, None
    steps = raw.get("steps", [])
    if not steps or any(step.get("skill") in {"return_home", "land"} for step in steps):
        return raw, None
    terminal_id = "auto_return_home"
    ids = {str(step.get("id")) for step in steps}
    suffix = 1
    while terminal_id in ids:
        suffix += 1
        terminal_id = f"auto_return_home_{suffix}"
    for step in steps:
        if step.get("on_success") is None:
            step["on_success"] = terminal_id
        if step.get("on_fail") is None:
            step["on_fail"] = terminal_id
    steps.append({"id": terminal_id, "skill": "return_home", "params": {},
                  "on_success": None, "on_fail": None})
    raw["steps"] = steps
    return raw, "缺少安全返航收尾，已补充 return_home。"


def validate_plan_for_execution(
    plan: MissionPlan | Any,
    package: MissionPackage,
    prompt: str,
    *,
    capabilities: frozenset[str] | None = None,
    altitude_repair_tolerance_m: float = 3.0,
) -> PlanValidationResult:
    """Validate a plan against its prompt and package without side effects."""
    available = capabilities if capabilities is not None else frozenset(
        package.document.get("capabilities", ())
    )
    try:
        candidate, terminal_repair = _repair_missing_terminal(plan)
        checked = _as_plan(candidate, available)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return PlanValidationResult("reject", None, (f"计划结构或能力不合法：{exc}",))

    repairs: list[str] = [terminal_repair] if terminal_repair else []
    area = package.document["operating_area"]
    steps = list(checked.steps)
    for index, step in enumerate(steps):
        altitude = step.params.get("altitude_m")
        if altitude is None:
            continue
        low, high = float(area["min_altitude_m"]), float(area["max_altitude_m"])
        if low <= altitude <= high:
            continue
        delta = low - altitude if altitude < low else altitude - high
        if delta > altitude_repair_tolerance_m:
            return PlanValidationResult("reject", None,
                (f"步骤 {step.id} 高度 {altitude:g} 米超出任务包高度包络 {low:g}–{high:g} 米。",))
        bounded = min(high, max(low, float(altitude)))
        params = dict(step.params)
        params["altitude_m"] = bounded
        steps[index] = replace(step, params=params)
        repairs.append(
            f"步骤 {step.id} 高度由 {altitude:g} 米钳制为 {bounded:g} 米（任务包包络）。"
        )
    if repairs:
        checked = replace(checked, steps=tuple(steps))

    violations = check_plan_against_package(checked, package, capabilities=available)
    hard = [v for v in violations if v.get("code") != "ALTITUDE_OUT_OF_BOUNDS"]
    if hard:
        messages = tuple(
            _VIOLATION_ZH.get(str(v.get("code")), str(v.get("message_zh") or v.get("code")))
            for v in hard
        )
        return PlanValidationResult("reject", None, messages, repairs=tuple(repairs))

    # The intent checker already handles grounded objects/places and return intent.
    intent = check_plan_intent(checked, prompt, package.refs)
    blocking = [item for item in intent if item.blocking]
    hard_intent = [
        item for item in blocking if item.code in {"ungrounded_place", "return_intent_land"}
    ]
    if hard_intent:
        return PlanValidationResult(
            "reject",
            None,
            tuple(item.message for item in hard_intent),
            repairs=tuple(repairs),
        )
    questions: list[str] = []
    if blocking:
        for item in blocking[:3]:
            questions.append(f"请确认任务目标：{item.message}？")

    lower_prompt = prompt.lower()
    count_steps = [s for s in checked.steps if s.skill == "count"]
    if count_steps and ("所有" in prompt or "全部" in prompt or "all " in lower_prompt):
        if any("max_count" in s.params for s in count_steps):
            questions.append("您要求统计全部目标，但计划设置了数量上限；是否移除上限或指定具体数量？")
    for step in checked.steps:
        if step.skill == "follow" and not str(step.params.get("query", "")).strip():
            questions.append("需要跟随什么目标？请描述类别、颜色或可识别特征。")
    wants_image = any(
        token in lower_prompt
        for token in (
            "take a photo",
            "take a picture",
            "save an image",
            "archive an image",
            "keep a still image",
            "capture a photo",
            "record a photo",
            "拍摄照片",
            "拍一张",
            "保存影像",
            "存档照片",
            "留存照片",
            "拍照留存",
        )
    )
    if wants_image and not any(step.skill in {"record", "inspect"} for step in checked.steps):
        questions.append(
            "任务要求保留影像，但计划没有 record/inspect 步骤；是否需要拍照或检查目标？"
        )
    if any(
        token in lower_prompt
        for token in (" or ", "或者", "或")
    ):
        questions.append(
            "提示包含多个可能的目标类别；请确认要搜索哪一个目标。"
        )
    questions = questions[:3]
    if questions:
        return PlanValidationResult("clarify", checked,
            ("任务提示与计划目标存在歧义，等待操作员确认。",), tuple(questions), tuple(repairs))
    return PlanValidationResult("repair" if repairs else "accept", checked,
        tuple(f"已自动修复：{item}" for item in repairs), (), tuple(repairs))


__all__ = ["PlanValidationResult", "validate_plan_for_execution"]

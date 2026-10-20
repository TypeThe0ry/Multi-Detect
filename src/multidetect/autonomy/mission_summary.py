"""Deterministic operator summaries of recorded observations, without inference."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from .mission_plan import MissionPlan, MissionReport


def summary_json(summary: Mapping[str, Any]) -> str:
    """Wire encoding; the 2048-byte limit includes this exact UTF-8 encoding."""
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _position(value: Any) -> list[float] | None:
    try:
        parts = value.split(",") if isinstance(value, str) else value
        n, e = float(parts[0]), float(parts[1])
        if math.isfinite(n) and math.isfinite(e):
            return [round(n, 1) or 0.0, round(e, 1) or 0.0]
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return None


def summarize_mission(
    report: MissionReport | Mapping[str, Any], plan: MissionPlan, task_text: str
) -> tuple[dict[str, Any], str, str]:
    """Return a <=2 KiB JSON object, Chinese text and English text.

    A mapping may enrich MissionReport with executor history, structured findings,
    per-step counts and coverage. Missing history means unknown, never success.
    Evidence arrays are not zipped with positions: inspection adds extra frames.
    Truncation is deterministic and explicitly counted in ``omitted``. Text uses
    the normalized report with explicit excerpts for long fields and lists.
    """
    raw = asdict(report) if is_dataclass(report) else dict(report)
    history = list(raw.get("history", ()))
    steps = []
    undone = []
    for step in plan.steps:
        outcomes = [h for h in history if h.get("step") == step.id]
        outcome = outcomes[-1] if outcomes else {}
        state = outcome.get("state", "unknown")
        reason = outcome.get("reason")
        steps.append({"id": step.id, "skill": step.skill, "state": state, "reason": reason})
        if state != "succeeded":
            undone.append({"step": step.id, "reason": reason or "no_recorded_completion"})
        elif reason in {"budget_reached", "budget_exhausted", "search_exhausted", "max_count"}:
            undone.append({"step": step.id, "reason": f"full_search_not_established:{reason}"})
    landing_unconfirmed = any(step["reason"] == "landing_commanded_unconfirmed" for step in steps)
    counts = []
    for sid, value in raw.get("counts", {}).items():
        outcome = next((s for s in steps if s["id"] == sid), {})
        reason = outcome.get("reason")
        history_outcome = next((h for h in reversed(history) if h.get("step") == sid), {})
        counts.append(
            {
                "step": sid,
                "value": value,
                "lower_bound": bool(history_outcome.get("count_is_lower_bound", True)),
                "reason": reason or "full_search_not_established",
            }
        )
    if not counts and raw.get("count") is not None:
        counts.append(
            {
                "step": None,
                "value": raw["count"],
                "lower_bound": True,
                "reason": "full_search_not_established",
            }
        )
    findings = []
    positions = raw.get("positions", ())
    for index, finding in enumerate(raw.get("findings", ())):
        if isinstance(finding, Mapping):
            findings.append(
                {
                    "step": finding.get("step"),
                    "query": finding.get("query"),
                    "position_ne": _position(finding.get("position_ne")),
                    "frame_id": finding.get("frame_id"),
                }
            )
            findings[-1].update(
                {
                    key: value
                    for key, value in finding.items()
                    if key.startswith("position_") and key != "position_ne"
                }
            )
        else:
            findings.append(
                {
                    "step": None,
                    "query": finding,
                    "position_ne": _position(positions[index]) if index < len(positions) else None,
                    "frame_id": None,
                }
            )
    # Recorded labels may retain positions without retaining finding identities.
    if not findings:
        findings = [
            {"step": None, "query": None, "position_ne": _position(p), "frame_id": None}
            for p in positions
        ]
    answers = list(raw.get("state_answers", raw.get("answers", ())))
    coverage = dict(raw.get("coverage", {}))
    for answer in answers:
        parts = str(answer).split(":coverage:")
        if len(parts) == 2:
            try:
                value = float(parts[1])
                if math.isfinite(value):
                    coverage[parts[0]] = value
            except ValueError:
                pass
    for sid, value in coverage.items():
        if value < 1:
            undone.append({"step": sid, "reason": f"partial_coverage:{value}"})
    for alert in raw.get("alerts", ()):
        if "_blocked" in str(alert):
            undone.append({"step": str(alert).split(":", 1)[0], "reason": str(alert)})
    for step in steps:
        if step["reason"] == "target_lost_timeout":
            undone.append({"step": step["id"], "reason": "target_lost_timeout"})
    result = {
        "task": task_text,
        "skills": [s.skill for s in plan.steps],
        "steps": steps,
        "counts": counts,
        "findings": findings,
        "alerts": list(raw.get("alerts", ())),
        "answers": answers,
        "coverage": coverage,
        "evidence": list(raw.get("evidence_frame_ids", raw.get("evidence", ()))),
        "not_done": undone,
        "omitted": {},
    }
    if raw.get("model_runtime") is not None:
        result["model_runtime"] = dict(raw["model_runtime"])
    if landing_unconfirmed:
        # A dispatched landing command is not confirmed mission completion.
        result["status"] = "landing_commanded_unconfirmed"
    zh, en = _text(result)
    _bound(result)
    return result, zh, en


def _bound(result: dict[str, Any]) -> None:
    # Remove optional payloads first; preserve failures and count qualifiers longer.
    order = (
        "evidence",
        "findings",
        "answers",
        "alerts",
        "coverage",
        "task",
        "not_done",
        "steps",
        "counts",
        "skills",
        "model_runtime",
    )
    while len(summary_json(result).encode("utf-8")) > 2048:
        for key in order:
            value = result[key]
            if not value:
                continue
            if isinstance(value, str):
                removed = len(value) - len(value) // 2
                result[key] = value[: len(value) // 2]
            elif isinstance(value, dict):
                value.pop(next(reversed(value)))
                removed = 1
            else:
                value.pop()
                removed = 1
            label = f"{key}_chars" if isinstance(value, str) else key
            result["omitted"][label] = result["omitted"].get(label, 0) + removed
            break


def _reason_zh(reason: str) -> str:
    meanings = {
        "budget_reached": "搜索预算耗尽",
        "budget_exhausted": "步数预算用完，未证实完整覆盖",
        "step_budget": "步骤预算耗尽",
        "search_exhausted": "搜索已结束，未证实完整覆盖",
        "coverage_complete": "已完成一次完整覆盖",
        "max_count": "达到计数上限",
        "no_recorded_completion": "未记录完成，状态未知",
        "full_search_not_established": "未证实完整搜索",
        "target_lost_timeout": "目标丢失超时",
        "arrived": "已到达",
        "landed": "已降落",
        "confirmed": "已确认目标",
        "views_captured": "已采集视角（不代表所有视角均完成）",
        "duration_elapsed": "报告记录的持续时间已到",
        "region_covered": "已执行覆盖（范围见覆盖率）",
    }
    prefix = "full_search_not_established:"
    if reason.startswith(prefix):
        return "完整搜索未证实：" + _reason_zh(reason.removeprefix(prefix))
    if reason.endswith("_blocked"):
        return "视角或航点受阻（" + reason + "）"
    if reason.startswith("partial_coverage:"):
        return "未完整覆盖（" + reason.split(":", 1)[1] + "）"
    return meanings.get(reason, reason)


def _text(s: dict[str, Any]) -> tuple[str, str]:
    zh = [f"任务：{_excerpt(s['task'], 240)}"]
    en = [f"Task: {_excerpt(s['task'], 240)}"]
    if s.get("status") == "landing_commanded_unconfirmed":
        zh.append("总体状态：已发出降落指令，但尚未确认落地")
        en.append("Overall status: landing commanded but not confirmed")
    values = s["counts"]
    zh_result = [
        f"{c['step'] or 'total'}：至少 {c['value']} 个（下限；{_reason_zh(c['reason'])}）"
        for c in values
    ]
    en_result = [
        f"{c['step'] or 'total'}: at least {c['value']} (lower bound; {c['reason']})"
        for c in values
    ]
    for finding in s["findings"][:6]:
        p = finding["position_ne"]
        position = f"N {p[0]:.1f} m, E {p[1]:.1f} m" if p else "?"
        evidence = finding["frame_id"]
        zh_result.append(
            f"{_excerpt(finding['query'] or '目标名称未记录', 80)} ({position}；"
            f"证据帧={_excerpt(evidence or '对应关系未记录', 80)})"
        )
        en_result.append(
            f"{_excerpt(finding['query'] or 'identity not recorded', 80)} ({position}; "
            f"frame={_excerpt(evidence or 'association not recorded', 80)})"
        )
    if len(s["findings"]) > 6:
        zh_result.append(f"另有 {len(s['findings']) - 6} 条发现，见报告")
        en_result.append(f"{len(s['findings']) - 6} more findings in report")
    home = next((step for step in s["steps"] if step["skill"] == "return_home"), None)
    if home and home["state"] == "succeeded":
        zh_result.append("已返航（报告记录）")
        en_result.append("returned home (reported)")
    zh.append("结果：" + ("；".join(zh_result) or "无发现/计数记录，不能据此断言不存在目标"))
    en.append(
        "Result: "
        + ("; ".join(en_result) or "no finding/count recorded; absence is not established")
    )
    states = {"succeeded": "成功", "failed": "失败", "unknown": "未知"}
    zh.append(
        "步骤："
        + _excerpt(
            "；".join(
                f"{step['id']} {step['skill']}：{states.get(step['state'], step['state'])}"
                f"（{_reason_zh(step['reason']) if step['reason'] else '原因未记录'}）"
                for step in s["steps"]
            )
        )
    )
    en.append(
        "Steps: "
        + _excerpt(
            "; ".join(
                f"{step['id']} {step['skill']}: {step['state']} "
                f"({step['reason'] or 'reason not recorded'})"
                for step in s["steps"]
            )
        )
    )
    for key, zlabel, elabel in (
        ("alerts", "告警", "Alerts"),
        ("answers", "检查回答", "Inspect answers"),
        ("coverage", "覆盖率", "Coverage"),
        (
            "evidence",
            "证据帧（未关联的不推断对应目标）",
            "Evidence frames (unlinked unless recorded)",
        ),
    ):
        if s[key]:
            value = s[key]
            if isinstance(value, list) and len(value) > 6:
                displayed = json.dumps(value[:6], ensure_ascii=False)
                zh.append(f"{zlabel}：{_excerpt(displayed)}；另有 {len(value) - 6} 条，见报告")
                en.append(f"{elabel}: {_excerpt(displayed)}; {len(value) - 6} more in report")
            else:
                displayed = _excerpt(json.dumps(value, ensure_ascii=False))
                zh.append(f"{zlabel}：{displayed}")
                en.append(f"{elabel}: {displayed}")
    failures = [
        f"{step['id']} ({step['reason'] or 'failed'})"
        for step in s["steps"]
        if step["state"] == "failed"
    ]
    if failures:
        zh.append(
            "步骤失败："
            + _excerpt(
                "；".join(
                    f"{step['id']}（{_reason_zh(step['reason'] or 'failed')}）"
                    for step in s["steps"]
                    if step["state"] == "failed"
                )
            )
        )
        en.append("Failed steps: " + _excerpt("; ".join(failures)))
    unfinished = [f"{item['step']}: {item['reason']}" for item in s["not_done"]]
    zh_unfinished = [f"{item['step']}: {_reason_zh(item['reason'])}" for item in s["not_done"]]
    zh.append("未完成/未证实：" + _excerpt("；".join(zh_unfinished) or "无（按步骤报告）"))
    en.append(
        "Not done / not established: " + _excerpt("; ".join(unfinished) or "none (per step report)")
    )
    return "\n".join(zh), "\n".join(en)


def _excerpt(value: str, limit: int = 480) -> str:
    if len(value) > limit:
        return value[:limit] + "…（节选，见报告 / excerpt; see report）"
    return value

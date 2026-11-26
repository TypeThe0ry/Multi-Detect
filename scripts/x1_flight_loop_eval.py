#!/usr/bin/env python3
"""Score raw Altair replies on the fixed, teacher-forced CPU flight-loop holdout."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

if __package__:
    from .build_x1_flight_loop_sft import ADMITTED, ROOT, _identity, generate, rows_for_window
else:
    from build_x1_flight_loop_sft import ADMITTED, ROOT, _identity, generate, rows_for_window

from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA,
    MAX_DECISION_BYTES,
    decode_decision,
)
from multidetect.autonomy.x1_perception_tools import TOOL_SCHEMA, ToolCall

from multidetect.autonomy.serialization import canonical_json, strict_json_loads


def read_jsonl(path):
    with path.open("rb") as stream:
        return [strict_json_loads(line) for line in stream if line.strip()]


def holdout(root=ROOT):
    for category, window, receipt, _ in generate("holdout", root):
        if receipt["status"] not in ADMITTED:
            continue
        for (row, label), (request, _) in zip(
            rows_for_window("holdout", category, window), window.turns, strict=True
        ):
            yield row, label, request, window


def score_turn(category, request, label, window, raw):
    tool_expected = label["schema"] == TOOL_SCHEMA
    retask_expected = label.get("action") == "retask"
    quiet = category == "quiet_cruise"
    result = dict(
        schema_valid=False,
        identity_valid=False,
        tool_use_correct=False,
        action_correct=None if tool_expected else False,
        executor_admitted=None if tool_expected else False,
        unnecessary_action=False if quiet else None,
        unnecessary_tool=False if quiet else None,
        retask_plan_valid=None,
        required_retask_valid=False if retask_expected else None,
        reason="invalid_json_or_schema",
        expected=label.get("action", label.get("tool")),
        observed=None,
    )
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DECISION_BYTES:
            return result
        reply = strict_json_loads(raw)
        if not isinstance(reply, dict):
            return result
        canonical_json(reply).encode("utf-8")
        schema = reply.get("schema")
        proposed_action = reply.get("action")
        if quiet:
            result["unnecessary_action"] = (
                isinstance(proposed_action, str) and proposed_action != "continue"
            )
            result["unnecessary_tool"] = schema == TOOL_SCHEMA
        if proposed_action == "retask":
            result["retask_plan_valid"] = False
        if schema == TOOL_SCHEMA:
            call = ToolCall.parse(reply)
            perception = request["perception"]
            identity = (
                call.request_id == request["request_id"]
                and call.call_index == perception["call_index"]
            )
            offered = perception["calls_remaining"] > 0 and call.tool in perception["tools"]
            correct = identity and offered and tool_expected and call.tool == label["tool"]
            result.update(
                schema_valid=True,
                identity_valid=identity,
                tool_use_correct=correct,
                observed=call.tool,
                reason=(
                    "tool_budget_or_offer"
                    if not offered
                    else "tool_identity"
                    if not identity
                    else "tool_correct"
                    if correct
                    else "tool_unnecessary_or_incorrect"
                ),
            )
            return result
        if schema != DECISION_SCHEMA:
            return result
        decision = decode_decision(raw, capabilities=frozenset(request["capabilities"]))
    except (ValueError, RecursionError):
        return result

    identity = all(
        type(reply[key]) is type(value) and reply[key] == value
        for key, value in _identity(request).items()
    )
    offered = decision.action in request["actions"]
    action_correct = (
        identity and offered and not tool_expected and decision.action == label["action"]
    )
    if action_correct and decision.action == "loiter":
        action_correct = decision.params == label["params"]
    result.update(
        schema_valid=True,
        identity_valid=identity,
        tool_use_correct=identity and offered and not tool_expected,
        observed=decision.action,
        reason="final_at_tool_turn" if tool_expected else "not_offered_or_identity",
    )
    if not tool_expected:
        result["action_correct"] = action_correct
    if not identity or not offered:
        return result
    admission, installed, _ = window.replay_final(request, raw)
    admitted = admission.status in ADMITTED
    if not tool_expected:
        result.update(executor_admitted=admitted, reason=admission.reason)
    if decision.action == "retask":
        result["retask_plan_valid"] = (
            admitted and admission.status == "installed" and bool(installed)
        )
        if retask_expected:
            # Valid alternate plans may be admitted. They still need to perform
            # the requested evidence acquisition, not just pass an empty suffix.
            work = [(step.skill, step.params.get("target_ref")) for step in decision.plan.steps]
            approach = ("fw_goto_waypoint", "finding:1")
            observes = {("loiter_observe", "finding:1"), ("gimbal_stare_at", "finding:1")}
            result["required_retask_valid"] = (
                result["retask_plan_valid"]
                and approach in work
                and any(step in observes for step in work[work.index(approach) + 1 :])
            )
    return result


def summarize(rows):
    def ratio(key):
        values = [row[key] for row in rows if row[key] is not None]
        return sum(values) / len(values) if values else None

    return {
        "turns": len(rows),
        "final_turns": sum(row["action_correct"] is not None for row in rows),
        "tool_turns": sum(row["action_correct"] is None for row in rows),
        "quiet_cruise_turns": sum(row["unnecessary_action"] is not None for row in rows),
        "retasks_proposed": sum(row["retask_plan_valid"] is not None for row in rows),
        "retasks_required": sum(row["required_retask_valid"] is not None for row in rows),
        **{
            f"{name}_rate": ratio(key)
            for name, key in (
                ("schema_valid", "schema_valid"),
                ("identity_valid", "identity_valid"),
                ("action_accuracy", "action_correct"),
                ("executor_admission", "executor_admitted"),
                ("unnecessary_action", "unnecessary_action"),
                ("unnecessary_tool", "unnecessary_tool"),
                ("tool_use_correct", "tool_use_correct"),
                ("retask_plan_valid", "retask_plan_valid"),
                ("required_retask_valid", "required_retask_valid"),
            )
        },
    }


def evaluate(tasks, predictions, root=ROOT, *, reference_smoke=False):
    items = list(holdout(root))
    expected = [item[0] for item in items]
    if tasks != expected:
        raise ValueError("tasks must match the regenerated, ordered fixed holdout exactly")
    if reference_smoke:
        predictions = [
            {"sample_id": row["sample_id"], "source": "oracle_smoke", "raw": canonical_json(label)}
            for row, label, _, _ in items
        ]
    if len(predictions) != len(items) or any(
        not isinstance(pred, dict)
        or set(pred) != {"sample_id", "source", "raw"}
        or pred["sample_id"] != row["sample_id"]
        or pred["source"] != ("oracle_smoke" if reference_smoke else "model")
        for (row, _, _, _), pred in zip(items, predictions, strict=True)
    ):
        raise ValueError(
            "predictions must contain one ordered reply per holdout ID with the correct source"
        )
    rows, categories, tickets = [], defaultdict(list), defaultdict(list)
    for (row, label, request, window), pred in zip(items, predictions, strict=True):
        category = row["category"]
        score = {
            "sample_id": row["sample_id"],
            "episode_id": row["episode_id"],
            "category": category,
            **score_turn(category, request, label, window, pred["raw"]),
        }
        rows.append(score)
        categories[category].append(score)
        tickets[request["request_id"]].append(score)
    summary = summarize(rows)
    summary["tickets"] = len(tickets)
    summary["ticket_reference_path_exact_rate"] = sum(
        all(
            r["tool_use_correct"]
            and r["action_correct"] is not False
            and r["executor_admitted"] is not False
            and r["required_retask_valid"] is not False
            for r in ticket
        )
        for ticket in tickets.values()
    ) / len(tickets)
    return {
        "source": "oracle_smoke" if reference_smoke else "model",
        "mode": "teacher_forced_cpu_replay",
        "summary": summary,
        "by_category": {key: summarize(value) for key, value in sorted(categories.items())},
        "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks", required=True, type=Path, help="builder's x1-flight-loop-holdout.jsonl"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--predictions", type=Path, help="JSONL: sample_id, source='model', raw")
    mode.add_argument(
        "--reference-smoke", action="store_true", help="score oracle answers; never a model result"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reference-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        result = evaluate(
            read_jsonl(args.tasks),
            read_jsonl(args.predictions) if args.predictions else [],
            args.reference_root.resolve(),
            reference_smoke=args.reference_smoke,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical_json(result) + "\n", encoding="utf-8")
    print(canonical_json(result["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

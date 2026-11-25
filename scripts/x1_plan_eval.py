#!/usr/bin/env python3
"""Score raw X1 planner responses from plan_from_task.py, without running a model."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.autonomy.mission_plan import validate_mission_plan, validate_plan_decision  # noqa: E402
from multidetect.autonomy.plan_validation import validate_plan_for_execution  # noqa: E402
from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402


def x1_eval_package(item):
    return load_mission_package({**item["x1_package"], "task_text": item["text"]}, mode="sim")


def _slots(plan):
    by_id = {step.id: step for step in plan.steps}
    step = plan.steps[0]
    result = []
    while step is not None:
        result.append((step.skill, step.params))
        step = by_id.get(step.on_success)
    return result


def score_x1_task(item, raw, *, package=None):
    """Exact ordered skill/slot match ignores prose, IDs and key order, not flight parameters."""
    package = package or x1_eval_package(item)
    caps = frozenset(package.document["capabilities"])
    expected = item["expected_target"]
    invalid = item.get("expect_invalid", False)
    row = {"valid_json": False, "legal": False, "valid": False, "exact": False,
           "exact_slots": False, "same_skills": False, "expect_invalid": invalid,
           "correct_rejection": False, "accepted_plan": False, "predicted": [],
           "expected": item.get("skills", []), "reason_code": None}
    try:
        doc = strict_json_loads(raw, name="X1 planner response")
        row["valid_json"] = True
        if not isinstance(doc, dict):
            return row
        if "decision" in doc:
            decision = validate_plan_decision(doc)
            row["reason_code"] = decision.reason_code
            correct = (invalid and decision.decision == expected["decision"]
                       and decision.reason_code == expected["reason_code"]
                       and set(decision.missing_capabilities) == set(expected["missing_capabilities"]))
            row.update(valid=True, legal=True, correct_rejection=correct, exact=correct, same_skills=correct)
            return row
        plan = validate_mission_plan(doc, capabilities=caps)
        validation = validate_plan_for_execution(plan, package, item["text"], capabilities=caps)
        legal = validation.decision == "accept"
        slots = _slots(plan)
        predicted = [skill for skill, _ in slots]
        exact = not invalid and legal and slots == _slots(validate_mission_plan(expected, capabilities=caps))
        row.update(valid=legal, legal=legal, accepted_plan=legal, exact=exact, exact_slots=exact,
                   predicted=predicted, same_skills=legal and predicted == row["expected"],
                   validator_decision=validation.decision, validator_reasons=list(validation.reasons_zh + validation.questions_zh))
    except (ValueError, TypeError, KeyError) as exc:
        row["validator_error"] = str(exc)
    return row


def summarize_x1(rows):
    positives = [row for row in rows if not row["expect_invalid"]]
    negatives = [row for row in rows if row["expect_invalid"]]

    def ratio(items, key):
        return sum(bool(row[key]) for row in items) / len(items) if items else None

    return {"tasks": len(rows), "valid_json_rate": ratio(rows, "valid_json"),
            "legal_response_rate": ratio(rows, "legal"),
            "positive_legal_plan_rate": ratio(positives, "accepted_plan"),
            "positive_exact_slots_rate": ratio(positives, "exact_slots"),
            "negative_correct_decision_rate": ratio(negatives, "correct_rejection"),
            "unsafe_admitted_on_negative": sum(bool(row["accepted_plan"]) for row in negatives)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path, help="plan_from_task.py output (raw, model-sourced rows)")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))["tasks"]
    responses = json.loads(args.predictions.read_text(encoding="utf-8"))["rows"]
    ids = [row.get("sample_id") for row in responses]
    if ids != [item["sample_id"] for item in tasks] or len(set(ids)) != len(ids) or not ids:
        parser.error("prediction IDs must match the ordered complete holdout exactly")
    if any(row.get("source") != "model" or row.get("text") != item["text"]
           for row, item in zip(responses, tasks, strict=True)):
        parser.error("prediction texts must match and results must be model-sourced")
    rows = [{"sample_id": item["sample_id"], "language": item["language"], "category": item["category"],
             **score_x1_task(item, pred.get("raw"))} for item, pred in zip(tasks, responses, strict=True)]
    summary = summarize_x1(rows)
    details = {"summary": summary, "by_language": {
        language: summarize_x1([row for row in rows if row["language"] == language])
        for language in sorted({row["language"] for row in rows})}, "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

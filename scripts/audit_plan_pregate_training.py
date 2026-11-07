"""Read-only audit of preflight hits against planner training targets."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.capability_contract import capability_violations  # noqa: E402
from multidetect.autonomy.mission_plan import validate_mission_plan  # noqa: E402
from multidetect.autonomy.plan_pregate import preflight_plan_task  # noqa: E402


_BUILTIN_REFS = {"home", "zone:all"}
_REF_FIELDS = ("region", "target_ref", "route_ref", "track_ref")


def _ref_ids(refs: Any) -> set[str]:
    values = refs.keys() if isinstance(refs, dict) else refs or ()
    if isinstance(values, str):
        values = (values,)
    return {str(value).split(" (", 1)[0].strip() for value in values}


def _target_issue(row: dict[str, Any]) -> str | None:
    target = row.get("assistant_target")
    if not isinstance(target, dict) or "steps" not in target:
        return "target_is_not_a_plan"
    capabilities = frozenset(row.get("capabilities") or ())
    try:
        plan = validate_mission_plan(target, capabilities=capabilities)
    except (TypeError, ValueError, KeyError) as exc:
        return f"invalid_plan: {exc}"
    capability_errors = capability_violations(
        plan,
        capabilities,
        airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
    )
    if capability_errors:
        return "unsupported_capability"
    known = _ref_ids(row.get("refs"))
    for step in plan.steps:
        for field in _REF_FIELDS:
            ref = step.params.get(field)
            if ref and ref not in _BUILTIN_REFS and ref not in known:
                return f"unresolved_{field}: {ref}"
    return None


def audit_training_file(path: Path) -> dict[str, Any]:
    hits_by_reason: Counter[str] = Counter()
    executable_false_positives: list[dict[str, Any]] = []
    annotation_gaps: list[dict[str, Any]] = []
    rows = 0
    for line_number, line in enumerate(path.open(encoding="utf-8"), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        rows += 1
        decision = preflight_plan_task(
            row.get("task_text", ""), row.get("refs"), row.get("capabilities", ())
        )
        if decision is None:
            continue
        hits_by_reason[decision.reason_code] += 1
        target_issue = _target_issue(row)
        detail = {
            "line": line_number,
            "sample_id": row.get("sample_id"),
            "family": row.get("family"),
            "reason_code": decision.reason_code,
            "task_text": row.get("task_text", ""),
        }
        if target_issue is None and row.get("expected_valid") is not False:
            executable_false_positives.append(detail)
        elif row.get("expected_valid") is None and target_issue:
            detail["target_issue"] = target_issue
            detail["refs"] = row.get("refs")
            annotation_gaps.append(detail)

    return {
        "path": str(path),
        "rows": rows,
        "hits": sum(hits_by_reason.values()),
        "hits_by_reason": dict(sorted(hits_by_reason.items())),
        "executable_false_positive_count": len(executable_false_positives),
        "executable_false_positives": executable_false_positives,
        "unlabelled_annotation_gap_count": len(annotation_gaps),
        "unlabelled_annotation_gaps": annotation_gaps,
    }


def audit_eval_file(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    refs = document.get("refs", ())
    hits: Counter[str] = Counter()
    false_positives: list[dict[str, Any]] = []
    for index, item in enumerate(document.get("tasks", ()), 1):
        capabilities = item.get("capabilities", ())
        decision = preflight_plan_task(
            item.get("text", ""), item.get("refs", refs), capabilities,
            "airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
        )
        if decision is None:
            continue
        hits[decision.reason_code] += 1
        if not item.get("expect_invalid", False):
            false_positives.append({"task": index, "text": item.get("text", "")})
    return {
        "path": str(path),
        "tasks": len(document.get("tasks", ())),
        "hits": sum(hits.values()),
        "hits_by_reason": dict(sorted(hits.items())),
        "normal_task_false_positive_count": len(false_positives),
        "normal_task_false_positives": false_positives,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--r24", type=Path,
        default=ROOT / "artifacts/scratch/cx-planner-round/r24/dataset/train.jsonl",
    )
    parser.add_argument(
        "--r27", type=Path,
        default=ROOT / "artifacts/scratch/cx-planner-round/r27/dataset/train.jsonl",
    )
    parser.add_argument(
        "--eval", dest="eval_paths", type=Path, action="append",
        default=None,
        help="evaluation JSON file; may be repeated (defaults to v3/v4/v5 and return probe)",
    )
    args = parser.parse_args(argv)
    training_reports = [audit_training_file(path) for path in (args.r24, args.r27)]
    eval_paths = args.eval_paths or [
        ROOT / "configs/onboard/plan-eval-tasks-v3.json",
        ROOT / "configs/onboard/plan-eval-tasks-v4.json",
        ROOT / "configs/onboard/plan-eval-tasks-v5.json",
        ROOT / "configs/onboard/plan-probe-tasks-return-home.json",
    ]
    report = {
        "training": training_reports,
        "evaluation": [audit_eval_file(path) for path in eval_paths],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(
        any(item["executable_false_positive_count"] for item in training_reports)
        or any(item["normal_task_false_positive_count"] for item in report["evaluation"])
    )


if __name__ == "__main__":
    raise SystemExit(main())

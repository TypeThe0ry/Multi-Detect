"""Re-score cached planning-v4 outputs through the execution capability gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.plan_from_task import score_task  # noqa: E402
from multidetect.autonomy.mission_plan import validate_mission_plan  # noqa: E402

CAPABILITY_CATEGORIES = {
    "fixed_wing_hover", "fixed_wing_target_land", "missing_payload_drop"
}


def recompute(document: dict[str, Any], tasks_doc: dict[str, Any]) -> dict[str, Any]:
    tasks = {item["text"]: item for item in tasks_doc["tasks"]}
    rows = document["rows"]
    for row in rows:
        item = tasks.get(row["text"])
        if not item or item.get("category") not in CAPABILITY_CATEGORIES:
            continue
        plan = None
        try:
            plan = validate_mission_plan(row["raw"])
        except (TypeError, ValueError, KeyError):
            pass
        scored = score_task(item, plan, row.get("repairs") or row.get("error"))
        for key in ("exact", "same_skills", "correct_rejection", "capability_violations", "validator_decision"):
            row[key] = scored.get(key)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("category", "uncategorized"), []).append(row)
    category_metrics = document.setdefault("summary", {}).setdefault("category_metrics", {})
    for category in sorted(CAPABILITY_CATEGORIES):
        group = groups[category]
        invalid = [row for row in group if row.get("expect_invalid")]
        category_metrics[category] = {
            "count": len(group),
            "accuracy": sum(bool(row.get("exact")) for row in group) / len(group),
            "rejection_correct_rate": (
                sum(bool(row.get("correct_rejection")) for row in invalid) / len(invalid)
                if invalid else None
            ),
            "intent_violation_rate": sum(bool(row.get("intent_violations")) for row in group) / len(group),
            "capability_violation_rate": sum(bool(row.get("capability_violations")) for row in group) / len(group),
        }
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--tasks", type=Path, default=ROOT / "configs/onboard/plan-eval-tasks-v4.json")
    args = parser.parse_args()
    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))
    result = {}
    for path in args.results:
        document = json.loads(path.read_text(encoding="utf-8"))
        recompute(document, tasks)
        result[str(path)] = {
            category: document["summary"]["category_metrics"][category]
            for category in sorted(CAPABILITY_CATEGORIES)
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Convert incumbent/candidate evaluation outputs into eval_altair gate reports.

Inputs (all existing formats, nothing re-run):
  * identity: ``eval-v3.json`` style ``{harness, metrics, rows}``;
  * planning: ``summary.json`` from plan_from_task.py with ``valid_rate``,
    ``exact_sequence_rate``... and ``category_metrics``.
Output: ``{"planning": {...}, "identity": {...}, "fixed_wing": {...}}``.
Latency is deliberately not gated (a 0.01 s absolute tolerance would trip on noise).
``fixed_wing`` collects every ``fixed_wing*`` planning category, so the gate
covers cruise/land behaviour as soon as those categories exist in the eval set.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PLANNING_KEYS = ("valid_rate", "exact_sequence_rate", "same_skill_set_rate", "first_try_valid_rate")
IDENTITY_KEYS = ("identity_accuracy", "decision_accuracy", "distractor_rejection_rate",
                 "identity_match_recall", "confirm_recall", "json_validity")
CATEGORY_KEYS = ("accuracy", "intent_violation_rate", "capability_violation_rate")


def _numeric(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def planning_section(summary: dict) -> tuple[dict, dict]:
    planning = {k: _numeric(summary[k]) for k in PLANNING_KEYS if _numeric(summary.get(k)) is not None}
    if not planning:
        raise ValueError("planning summary has none of the expected metrics")
    fixed_wing = {}
    for name, cat in (summary.get("category_metrics") or {}).items():
        if not name.startswith("fixed_wing") or not cat.get("count"):
            continue
        for key in CATEGORY_KEYS:
            value = _numeric(cat.get(key))
            if value is not None:
                fixed_wing[f"{name}_{key}"] = value
    return planning, fixed_wing


def identity_section(report: dict) -> dict:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("identity eval has no metrics object")
    section = {k: _numeric(metrics[k]) for k in IDENTITY_KEYS if _numeric(metrics.get(k)) is not None}
    if not section:
        raise ValueError("identity eval has none of the expected metrics")
    return section


def collect(planning_summary: dict, identity_eval: dict) -> dict:
    planning, fixed_wing = planning_section(planning_summary)
    return {"planning": planning, "identity": identity_section(identity_eval), "fixed_wing": fixed_wing}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planning-summary", type=Path, required=True)
    parser.add_argument("--identity-eval", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args(argv)
    report = collect(
        json.loads(args.planning_summary.read_text(encoding="utf-8")),
        json.loads(args.identity_eval.read_text(encoding="utf-8")),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

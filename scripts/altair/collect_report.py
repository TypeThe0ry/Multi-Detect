"""Convert incumbent/candidate evaluation outputs into eval_altair gate reports.

Inputs (all existing formats, nothing re-run):
  * identity: ``eval-v3.json`` style ``{harness, metrics, rows}``;
  * planning (plan_from_task.py): ``planning-v*.json`` (metrics under ``summary``), a
    bare summary, or the stage manifest ``summary.json`` that references it; the metrics
    are ``valid_rate``, ``exact_sequence_rate``... and ``category_metrics``.
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


def _json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _name(ref: str) -> str:
    return Path(ref.replace("\\", "/")).name


def load_planning_summary(path: Path, _depth: int = 0) -> dict:
    """Accept a bare summary, a planning-v*.json (metrics under ``summary``) or a
    stage manifest whose ``stages`` reference a planning-v*.json file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data.get("summary"), dict):
        return data["summary"]
    if any(k in data for k in PLANNING_KEYS):
        return data
    if _depth == 0 and "stages" in data:
        refs = sorted(
            {s for s in _json_strings(data["stages"]) if s.endswith(".json") and _name(s).startswith("planning")},
            key=_name,
            reverse=True,  # planning-v4 before planning-v3
        )
        for ref in refs:
            candidate = Path(ref.replace("\\", "/"))
            options = [candidate] if candidate.is_absolute() else [path.parent / candidate, path.parent / candidate.name]
            for option in options:
                if option.is_file():
                    return load_planning_summary(option, 1)
        # Manifest without a usable reference: use the newest planning file beside it.
        for sibling in sorted(path.parent.glob("planning-v*.json"), reverse=True):
            return load_planning_summary(sibling, 1)
    raise ValueError(f"no planning metrics found in {path}")


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


def write_paired_report(
    incumbent_identity: Path,
    candidate_identity: Path,
    output: Path,
    *,
    bootstrap: int = 10000,
    seed: int = 7,
    dataset: Path | None = None,
) -> dict:
    """Write the optional row-level paired report beside the aggregate report."""
    from .paired_report import DEFAULT_DATASET, write_report

    return write_report(
        incumbent_identity,
        candidate_identity,
        output,
        bootstrap=bootstrap,
        seed=seed,
        dataset=dataset or DEFAULT_DATASET,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planning-summary", type=Path, required=True)
    parser.add_argument("--identity-eval", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--incumbent-identity", type=Path,
                        help="optional incumbent per-sample eval JSON for a paired report")
    parser.add_argument("--paired-output", type=Path,
                        help="where to write the optional paired report")
    parser.add_argument("--paired-dataset", type=Path,
                        help="identity dataset JSONL used for area bins")
    parser.add_argument("--paired-bootstrap", type=int, default=10000)
    parser.add_argument("--paired-seed", type=int, default=7)
    args = parser.parse_args(argv)
    report = collect(
        load_planning_summary(args.planning_summary),
        json.loads(args.identity_eval.read_text(encoding="utf-8")),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.incumbent_identity:
        paired_output = args.paired_output or args.output_json.with_name("paired-report.json")
        write_paired_report(
            args.incumbent_identity,
            args.identity_eval,
            paired_output,
            bootstrap=args.paired_bootstrap,
            seed=args.paired_seed,
            dataset=args.paired_dataset,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

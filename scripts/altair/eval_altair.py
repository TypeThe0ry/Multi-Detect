"""Acceptance gate for Altair candidates against the incumbent reports (CPU only).

Reports are JSON ``{"<task>": {"<metric>": number, ...}, ...}`` with tasks such as
``planning``, ``identity`` and ``fixed_wing``. Rules, fail closed:
  * every numeric baseline metric must not regress by more than ``tolerance``
    (lower-is-better for names containing false_/error/invalid/violation/latency);
  * absolute thresholds from the gates file must hold when their metric is reported;
  * every task in ``required_tasks`` must be present in the candidate, and a
    baseline metric missing from the candidate fails (so fixed_wing categories
    cannot silently disappear).
Exit code 0 always when the evaluation ran; the verdict is in the output JSON.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_GATES = {
    "tolerance": 0.01,
    "absolute": {
        # Optional hard floors; the main gate is "no regression vs incumbent".
        # Add e.g. the eval_r21id_identity.py THRESHOLDS via --gates when those
        # metrics are present in the reports.
        "identity": {"distractor_rejection_rate": {"min": 0.97}},
    },
    "required_tasks": ["planning", "identity", "fixed_wing"],
}
LOWER_HINTS = ("false_", "error", "invalid", "violation", "latency")


def lower_is_better(name: str) -> bool:
    return any(hint in name for hint in LOWER_HINTS)


def _num(value):
    return value.get("value") if isinstance(value, dict) else value


def evaluate(baseline: dict, candidate: dict, gates: dict | None = None) -> dict:
    gates = gates or DEFAULT_GATES
    tol = float(gates.get("tolerance", 0.01))
    reasons, rows = [], []
    for task in gates.get("required_tasks", []):
        if task not in candidate:
            reasons.append(f"{task}: candidate report missing")
    for task, metrics in baseline.items():
        for name, ref in metrics.items():
            ref = _num(ref)
            if not isinstance(ref, (int, float)):
                continue
            cand = _num(candidate.get(task, {}).get(name))
            if cand is None:
                reasons.append(f"{task}.{name}: missing in candidate")
                continue
            worse = cand > ref + tol if lower_is_better(name) else cand < ref - tol
            rows.append({"task": task, "metric": name, "baseline": ref, "candidate": cand, "regressed": worse})
            if worse:
                reasons.append(f"{task}.{name}: {cand} vs baseline {ref}")
    for task, limits in gates.get("absolute", {}).items():
        for name, bound in limits.items():
            cand = _num(candidate.get(task, {}).get(name))
            if cand is None:
                if task in candidate or task in gates.get("required_tasks", []):
                    reasons.append(f"{task}.{name}: missing in candidate")
                continue
            if "min" in bound and cand < bound["min"]:
                reasons.append(f"{task}.{name}: {cand} < {bound['min']}")
            if "max" in bound and cand > bound["max"]:
                reasons.append(f"{task}.{name}: {cand} > {bound['max']}")
    return {"pass": not reasons, "reasons": reasons, "rows": rows,
            "decision": "replace incumbent" if not reasons else "keep incumbent"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--gates", type=Path, help="JSON overriding DEFAULT_GATES")
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args(argv)
    gates = json.loads(args.gates.read_text(encoding="utf-8")) if args.gates else None
    report = evaluate(
        json.loads(args.baseline.read_text(encoding="utf-8")),
        json.loads(args.candidate.read_text(encoding="utf-8")),
        gates,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report["decision"], *report["reasons"], sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

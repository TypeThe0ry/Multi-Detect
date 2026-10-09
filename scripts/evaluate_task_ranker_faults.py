"""Evaluate a frozen existing ranker against deterministic planning-input faults."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.task_ranker import load_task_ranker  # noqa: E402
from multidetect.simulation.task_training import evaluate_task_ranker  # noqa: E402
from scripts.train_task_ranker import load_dataset  # noqa: E402

# Versioned, fixed perturbations: evaluation only, never parameter selection.
FAULTS = (
    ("gps_fix", {"gps_fix_type": 2}, "GPS fix does not meet the planning threshold"),
    ("gps_accuracy", {"gps_horizontal_accuracy_m": 7.0},
     "GPS horizontal accuracy exceeds the planning threshold"),
    ("geofence", {"geofence_valid": False}, "geofence is not valid"),
    ("camera", {"camera_available": False}, "camera resource is unavailable"),
    ("perception", {"perception_available": False}, "perception resource is unavailable"),
    ("map_cache", {"map_cache_available": False}, "map cache resource is unavailable"),
    ("dem", {"dem_available": False}, "DEM resource is unavailable"),
    ("battery", {"battery_remaining_pct": 0.0},
     "battery is not above return reserve plus planning margin"),
    ("endurance", {"estimated_flight_time_s": 0.0}, "estimated flight time must be positive"),
)


def evaluate_faults(groups, ranker):
    rows = []
    for split, cases in groups.items():
        for case in cases:
            # A nominal control prevents all-rejected broken datasets from passing.
            nominal = evaluate_task_ranker([case], ranker)
            nominal_pass = nominal["completed_count"] > 0
            expected_ids = {c.task.task_id for c in case.candidates}
            for name, patch, expected_reason in FAULTS:
                perturbed = replace(case, context=replace(case.context, **patch))
                result = evaluate_task_ranker([perturbed], ranker)
                rejected = result["cases"][0]["rejected"]
                passed = (
                    nominal_pass
                    and result["completed_count"] == 0
                    and len(rejected) == len(expected_ids)
                    and {item["task_id"] for item in rejected} == expected_ids
                    and all(item["reason"] == expected_reason for item in rejected)
                )
                rows.append({
                    "case_id": case.case_id, "split": split, "fault": name,
                    "context_patch": patch, "base_input_hash": case.input_hash,
                    "perturbed_input_hash": perturbed.input_hash,
                    "expected_reason": expected_reason, "nominal_control_passed": nominal_pass,
                    "nominal_completed_count": nominal["completed_count"],
                    "result": result, "passed": passed,
                })
    return {
        "schema_version": 1, "method": "frozen_ranker_planning_faults_v1",
        "ranker_sha256": ranker.canonical_hash, "rows": rows,
        "passed": bool(rows) and all(row["passed"] for row in rows),
        "checks": len(rows), "failed_checks": sum(not row["passed"] for row in rows),
        "candidate_promoted": False, "used_for_training": False,
        "physical_or_dynamics_performance_claimed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")
    groups, _, identity = load_dataset(args.dataset)
    ranker = load_task_ranker(args.candidate, expected_sha256=args.candidate_sha256)
    report = evaluate_faults(groups, ranker)
    report["dataset"] = {"path": str(args.dataset), **identity}
    report["candidate_file_sha256"] = args.candidate_sha256
    report["evaluator_file_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in ("passed", "checks", "failed_checks")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

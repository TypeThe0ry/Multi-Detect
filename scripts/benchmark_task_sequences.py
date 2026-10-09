"""Exhaustive small-instance reference for the existing return-home budget model.

This is an offline benchmark, not a deployed planner or a dynamics simulator.
No branches are pruned by ranker score. A node limit aborts without claiming an
optimum. Each edge uses the original planner and mission compiler via episode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.task_selection import select_next_task  # noqa: E402
from multidetect.autonomy.task_sequence_reference import (  # noqa: E402
    SEQUENCE_REFERENCE_OBJECTIVE_ID,
    exact_sequence_reference,
)
from multidetect.simulation.task_episode import simulate_task_budget_episode  # noqa: E402
from scripts.train_task_ranker import load_dataset  # noqa: E402


def exact_reference(case, *, max_nodes=10000):
    result = exact_sequence_reference(
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
        max_nodes=max_nodes,
    )
    return result.to_mapping()


def benchmark(case, *, max_nodes=10000):
    initial = select_next_task(case.candidates, case.context, case.vehicle_class)
    start = perf_counter()
    greedy = simulate_task_budget_episode(
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )
    wall = perf_counter() - start
    sequence = [s.compilation.estimate.task_id for s in greedy.steps]
    priorities = {c.task.task_id: c.priority for c in case.candidates}
    baseline = {
        "sequence": sequence,
        "reward": sum(1 + priorities[k] for k in sequence),
        "completed_count": len(sequence),
        "estimated_elapsed_s": sum(
            s.compilation.estimate.estimated_duration_s for s in greedy.steps
        ),
        "wall_s": wall,
    }
    reference = exact_reference(case, max_nodes=max_nodes)
    # Numerical tolerance for repeated floating-point budget additions only.
    within_time = max(
        (
            p
            for p in reference["pareto_frontier"]
            if p["estimated_elapsed_s"] <= baseline["estimated_elapsed_s"] + 1e-9
        ),
        key=lambda p: (p["reward"], p["completed_count"], -p["estimated_elapsed_s"]),
    )
    return {
        "case_id": case.case_id,
        "input_hash": case.input_hash,
        "greedy": baseline,
        "reference": reference,
        "reward_gap": reference["reward"] - baseline["reward"],
        "estimated_elapsed_delta_s": (
            reference["estimated_elapsed_s"] - baseline["estimated_elapsed_s"]
        ),
        "reference_within_greedy_time": within_time,
        "same_time_reward_gap": within_time["reward"] - baseline["reward"],
        "initial_feasible_count": len(initial.ranked),
        "initial_rejected": [
            {"task_id": item.task_id, "reason": item.reason} for item in initial.rejected
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-nodes", type=int, default=10000)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")
    groups, _, identity = load_dataset(args.dataset)
    rows = [
        {"split": split, **benchmark(case, max_nodes=args.max_nodes)}
        for split, cases in groups.items()
        for case in cases
    ]
    report = {
        "schema_version": 1,
        "method": "exhaustive_return_budget_reference_v2",
        "objective_id": SEQUENCE_REFERENCE_OBJECTIVE_ID,
        "dataset": {"path": str(args.dataset), **identity},
        "evaluator_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "max_nodes_per_case": args.max_nodes,
        "rows": rows,
        "greedy_reward": sum(r["greedy"]["reward"] for r in rows),
        "reference_reward": sum(r["reference"]["reward"] for r in rows),
        "improved_cases": sum(r["reward_gap"] > 0 for r in rows),
        "same_time_improved_cases": sum(r["same_time_reward_gap"] > 0 for r in rows),
        "same_time_reference_reward": sum(
            r["reference_within_greedy_time"]["reward"] for r in rows
        ),
        "deployed": False,
        "physical_optimality_claimed": False,
        "independent_holdout_claimed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps({k: report[k] for k in ("greedy_reward", "reference_reward", "improved_cases")})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

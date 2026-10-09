"""Evaluate bounded sequence proposals on a frozen dataset.

This is a truth-free, offline comparison of the current shared policy against
the bounded exact reference.  It is not a deployment or flight-acceptance
tool, and it never produces Mission/actuator authorization.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.task_sequence_reference import (  # noqa: E402
    SEQUENCE_REFERENCE_OBJECTIVE_ID,
    build_sequence_proposal,
)
from scripts.sequence_proposal_evidence import (  # noqa: E402
    bind_algorithm_sources,
    load_bound_dataset,
    load_bound_policy,
    validate_known_holdout_separation,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-dataset", type=Path, required=True)
    parser.add_argument("--training-dataset-sha256", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--ranker", type=Path, required=True)
    parser.add_argument("--ranker-sha256", required=True)
    parser.add_argument("--policy-artifact", type=Path, required=True)
    parser.add_argument("--policy-artifact-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-nodes", type=int, default=10_000)
    parser.add_argument("--max-tasks", type=int, default=6)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")

    training_groups, training_identity, training_source = load_bound_dataset(
        args.training_dataset, args.training_dataset_sha256
    )
    groups, dataset_identity, evaluation_source = load_bound_dataset(
        args.dataset, args.dataset_sha256
    )
    policy, ranker_source, artifact_source = load_bound_policy(
        args.ranker,
        args.ranker_sha256,
        args.policy_artifact,
        args.policy_artifact_sha256,
    )
    holdout_binding = validate_known_holdout_separation(
        training_groups, training_identity, groups, dataset_identity, policy
    )
    algorithm_sources, algorithm_source_objects = bind_algorithm_sources(Path(__file__))
    rows: list[dict[str, object]] = []
    for split, cases in groups.items():
        for case in cases:
            proposal = build_sequence_proposal(
                policy,
                case.candidates,
                case.context,
                case.vehicle_class,
                runtimes=case.runtimes,
                airframe_profile_hash=case.airframe_profile_hash,
                max_nodes=args.max_nodes,
                max_tasks=args.max_tasks,
            )
            rows.append(
                {
                    "split": split,
                    "case_id": case.case_id,
                    "input_hash": case.input_hash,
                    "context_sha256": case.context.canonical_hash,
                    "airframe_profile_hash": case.airframe_profile_hash,
                    "baseline_sequence": list(proposal.baseline_sequence),
                    "proposed_sequence": list(proposal.proposed_sequence),
                    "baseline_reward": proposal.baseline_reward,
                    "proposed_reward": proposal.proposed_reward,
                    "reward_delta": proposal.reward_delta,
                    "baseline_elapsed_s": proposal.baseline_elapsed_s,
                    "proposed_elapsed_s": proposal.proposed_elapsed_s,
                    "baseline_completed_count": proposal.baseline_completed_count,
                    "proposed_completed_count": proposal.proposed_completed_count,
                    "baseline_objective": list(proposal.baseline_objective),
                    "proposed_objective": list(proposal.proposed_objective),
                    "objective_id": proposal.objective_id,
                    "objective_relation": proposal.objective_relation,
                    "planner_revalidated": proposal.planner_revalidated,
                    "truth_free": proposal.truth_free,
                    "policy_applied_to_actuator": proposal.policy_applied_to_actuator,
                    "deployed": proposal.deployed,
                    "candidate_promoted": proposal.candidate_promoted,
                    "final_release_allowed": proposal.final_release_allowed,
                }
            )
    if any(row["objective_id"] != SEQUENCE_REFERENCE_OBJECTIVE_ID for row in rows):
        raise ValueError("sequence proposal objective does not match evaluator objective")
    aggregate = {
        "case_count": len(rows),
        "baseline_reward": sum(int(row["baseline_reward"]) for row in rows),
        "proposed_reward": sum(int(row["proposed_reward"]) for row in rows),
        "reward_delta": sum(int(row["reward_delta"]) for row in rows),
        "improved_cases": sum(int(row["reward_delta"]) > 0 for row in rows),
        "regressed_cases": sum(int(row["reward_delta"]) < 0 for row in rows),
        "unchanged_cases": sum(int(row["reward_delta"]) == 0 for row in rows),
        "objective_improved_cases": sum(
            row["objective_relation"] == "improved" for row in rows
        ),
        "objective_regressed_cases": sum(
            row["objective_relation"] == "regressed" for row in rows
        ),
        "objective_tie_cases": sum(row["objective_relation"] == "tie" for row in rows),
    }
    for source in (
        training_source,
        evaluation_source,
        ranker_source,
        artifact_source,
        *algorithm_source_objects,
    ):
        source.revalidate()
    report = {
        "schema_version": 1,
        "contract": "multidetect.sequence-proposal-holdout-report",
        "method": "shared_policy_vs_bounded_exact_reference_v1",
        "training_dataset": {"path": str(args.training_dataset), **training_identity},
        "dataset": {"path": str(args.dataset), **dataset_identity},
        "holdout_binding": holdout_binding,
        "algorithm_sources": algorithm_sources,
        "ranker": {
            "path": str(args.ranker),
            "file_sha256": ranker_source.sha256,
            "bytes": ranker_source.byte_count,
            "canonical_sha256": policy.ranker.canonical_hash,
        },
        "policy_artifact": {
            "path": str(args.policy_artifact),
            "file_sha256": artifact_source.sha256,
            "bytes": artifact_source.byte_count,
            "canonical_sha256": policy.artifact.canonical_hash,
        },
        "max_nodes_per_case": args.max_nodes,
        "max_tasks_per_case": args.max_tasks,
        "rows": rows,
        "aggregate": aggregate,
        "objective_id": SEQUENCE_REFERENCE_OBJECTIVE_ID,
        "independent_holdout_evaluation": (
            holdout_binding["input_hashes_disjoint"]
            and holdout_binding["scenario_families_disjoint"]
        ),
        "objective_matched": all(
            row["objective_id"] == SEQUENCE_REFERENCE_OBJECTIVE_ID for row in rows
        ),
        "advisory_only": True,
        "mission_upload_allowed": False,
        "mission_start_allowed": False,
        "actuator_output_allowed": False,
        "deployed": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(aggregate, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

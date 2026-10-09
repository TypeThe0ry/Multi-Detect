"""Emit one bounded sequence proposal for offline SIM analysis.

The command compares the hash-bound shared task policy with the exhaustive
small-instance reference.  The output is an advisory report only: it cannot
upload, start, authorize, arm, disarm, or issue actuator commands.
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
    build_sequence_proposal,
)
from scripts.sequence_proposal_evidence import (  # noqa: E402
    bind_algorithm_sources,
    load_bound_dataset,
    load_bound_policy,
)


def _find_case(groups, case_id: str):
    matches = [
        (split, case)
        for split, cases in groups.items()
        for case in cases
        if case.case_id == case_id
    ]
    if not matches:
        raise ValueError(f"case not found: {case_id}")
    if len(matches) != 1:
        raise ValueError(f"case id is not unique: {case_id}")
    split, case = matches[0]
    return split, case


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--case-id", required=True)
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

    groups, dataset_identity, dataset_source = load_bound_dataset(
        args.dataset, args.dataset_sha256
    )
    split, case = _find_case(groups, args.case_id)
    policy, ranker_source, artifact_source = load_bound_policy(
        args.ranker,
        args.ranker_sha256,
        args.policy_artifact,
        args.policy_artifact_sha256,
    )
    algorithm_sources, algorithm_source_objects = bind_algorithm_sources(Path(__file__))
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
    for source in (dataset_source, ranker_source, artifact_source, *algorithm_source_objects):
        source.revalidate()
    report = {
        "schema_version": 1,
        "contract": "multidetect.sequence-proposal-report",
        "case_id": case.case_id,
        "split": split,
        "dataset": {
            "path": str(args.dataset),
            **dataset_identity,
        },
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
        "proposal": proposal.to_mapping(),
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
    print(
        json.dumps(
            {
                "case_id": case.case_id,
                "baseline_sequence": proposal.baseline_sequence,
                "proposed_sequence": proposal.proposed_sequence,
                "reward_delta": proposal.reward_delta,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

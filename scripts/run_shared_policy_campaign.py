"""Run a hash-bound shared-policy comparison on an independent SIM dataset.

Training selection and evaluation are deliberately separate.  The training
dataset is used only to select a bounded ``TaskRanker`` candidate; every case
in the evaluation dataset must have a distinct input hash.  Both parent and
candidate are then executed through ``SharedTaskPolicy`` and the unchanged
budget-episode planner.  This is deterministic synthetic SIM evidence, not
flight-dynamics, HIL/REAL, GNSS, or release evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.policy_artifact import PolicyArtifact  # noqa: E402
from multidetect.autonomy.task_ranker import TaskRanker, load_task_ranker  # noqa: E402
from multidetect.simulation.task_training import (  # noqa: E402
    evaluate_task_ranker,
    fit_task_ranker,
)
from scripts.train_task_ranker import load_dataset  # noqa: E402


def _contract_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_factory(
    *,
    parent: TaskRanker,
    training_dataset_sha256: str,
    airframe_profile_hash: str,
    policy_id: str,
    policy_version: str,
):
    def make(model: TaskRanker) -> PolicyArtifact:
        return PolicyArtifact.for_ranker(
            model,
            policy_id=policy_id,
            policy_version=policy_version,
            airframe_profile_hash=airframe_profile_hash,
            mission_contract_hash=_contract_hash("multidetect.mission-compile-runtime-v1"),
            safety_policy_hash=_contract_hash("multidetect.safety-policy-v1"),
            parent_model_sha256=parent.canonical_hash,
            training_dataset_sha256=training_dataset_sha256,
        )

    return make


def _evaluate_groups(groups, ranker, artifact, forbidden_input_hashes):
    return {
        split: evaluate_task_ranker(
            cases,
            ranker,
            forbidden_input_hashes=forbidden_input_hashes,
            policy_artifact=artifact,
        )
        for split, cases in groups.items()
    }


def run_campaign(
    *,
    training_dataset: Path,
    evaluation_dataset: Path,
    parent_path: Path,
    parent_sha256: str,
    policy_id: str = "task-ranker-independent-sim-v1",
    policy_version: str = "0.3.0-pre.8",
) -> dict:
    train_groups, coefficients, train_identity = load_dataset(training_dataset)
    eval_groups, _, eval_identity = load_dataset(evaluation_dataset)
    parent = load_task_ranker(parent_path, expected_sha256=parent_sha256)

    train_cases = tuple(train_groups["train"])
    eval_cases = tuple(case for cases in eval_groups.values() for case in cases)
    train_hashes = {case.input_hash for case in train_cases}
    eval_hashes = {case.input_hash for case in eval_cases}
    if train_hashes & eval_hashes:
        raise ValueError("training and evaluation input hashes overlap")
    train_families = set(train_identity["families"])
    eval_families = set(eval_identity["families"])
    if train_families & eval_families:
        raise ValueError("training and evaluation scenario families overlap")

    train_airframes = {case.airframe_profile_hash for case in train_cases}
    eval_airframes = {case.airframe_profile_hash for case in eval_cases}
    if len(train_airframes) != 1 or len(eval_airframes) != 1:
        raise ValueError("campaign requires one explicit airframe profile per dataset")
    if train_airframes != eval_airframes:
        raise ValueError("training and evaluation airframe profiles differ")
    airframe_profile_hash = next(iter(train_airframes))
    artifact_factory = _artifact_factory(
        parent=parent,
        training_dataset_sha256=train_identity["sha256"],
        airframe_profile_hash=airframe_profile_hash,
        policy_id=policy_id,
        policy_version=policy_version,
    )
    candidate, search = fit_task_ranker(
        train_cases,
        parent,
        coefficients,
        policy_artifact_factory=artifact_factory,
    )
    parent_artifact = artifact_factory(parent)
    candidate_artifact = artifact_factory(candidate)
    evaluations = {
        "parent": _evaluate_groups(train_groups, parent, parent_artifact, ()),
        "candidate": _evaluate_groups(train_groups, candidate, candidate_artifact, ()),
        "independent_parent": _evaluate_groups(
            eval_groups, parent, parent_artifact, train_hashes
        ),
        "independent_candidate": _evaluate_groups(
            eval_groups, candidate, candidate_artifact, train_hashes
        ),
    }
    return {
        "schema_version": 1,
        "method": "hash_bound_shared_policy_independent_sim_campaign_v1",
        "training_dataset": {"path": str(training_dataset), **train_identity},
        "evaluation_dataset": {"path": str(evaluation_dataset), **eval_identity},
        "parent_file_sha256": parent_sha256,
        "parent_ranker_sha256": parent.canonical_hash,
        "candidate_ranker_sha256": candidate.canonical_hash,
        "parent_policy_artifact_sha256": parent_artifact.canonical_hash,
        "candidate_policy_artifact_sha256": candidate_artifact.canonical_hash,
        "parent_policy_artifact": parent_artifact.to_mapping(),
        "candidate_policy_artifact": candidate_artifact.to_mapping(),
        "training_input_count": len(train_hashes),
        "evaluation_input_count": len(eval_hashes),
        "input_hashes_disjoint": True,
        "training_families": sorted(train_families),
        "evaluation_families": sorted(eval_families),
        "scenario_families_disjoint": True,
        "search": search,
        "evaluations": evaluations,
        "truth_free": True,
        "planner_revalidated": True,
        "candidate_promoted": False,
        "learned_weights_loaded": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
        "evaluator_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-dataset", type=Path, required=True)
    parser.add_argument("--evaluation-dataset", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--parent-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy-id", default="task-ranker-independent-sim-v1")
    parser.add_argument("--policy-version", default="0.3.0-pre.8")
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")
    report = run_campaign(
        training_dataset=args.training_dataset,
        evaluation_dataset=args.evaluation_dataset,
        parent_path=args.parent,
        parent_sha256=args.parent_sha256,
        policy_id=args.policy_id,
        policy_version=args.policy_version,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "candidate_ranker_sha256": report["candidate_ranker_sha256"],
                "candidate_policy_artifact_sha256": report["candidate_policy_artifact_sha256"],
                "input_hashes_disjoint": report["input_hashes_disjoint"],
                "candidate_promoted": report["candidate_promoted"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

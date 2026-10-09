from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.policy_contract import SharedTaskPolicy
from multidetect.autonomy.task_ranker import TaskRanker
from multidetect.autonomy.task_sequence_reference import (
    SequenceProposal,
    build_sequence_proposal,
)
from scripts.evaluate_sequence_proposals import main as evaluate_main
from scripts.propose_task_sequence import main as propose_main
from scripts.sequence_proposal_evidence import load_bound_dataset
from scripts.train_task_ranker import load_dataset

DATASET = (
    Path(__file__).resolve().parents[1]
    / "configs/training/task-ranker-budget-v1.json"
)


def _case():
    groups, _, _ = load_dataset(DATASET)
    return groups["train"][0]


def _policy(case):
    ranker = TaskRanker((0.0, 0.0, 0.0), "sequence-proposal-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="sequence-proposal-test",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=case.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
        parent_model_sha256="3" * 64,
        training_dataset_sha256="4" * 64,
    )
    return SharedTaskPolicy(ranker, artifact)


def test_sequence_proposal_is_identity_bound_and_advisory_only():
    case = _case()
    policy = _policy(case)
    proposal = build_sequence_proposal(
        policy,
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )

    assert proposal.policy_artifact_sha256 == policy.artifact_sha256
    assert proposal.ranker_sha256 == policy.ranker.canonical_hash
    assert proposal.context_sha256 == case.context.canonical_hash
    assert proposal.airframe_profile_hash == case.airframe_profile_hash
    assert proposal.proposed_sequence == proposal.reference.sequence
    assert proposal.first_task_id == (
        proposal.proposed_sequence[0] if proposal.proposed_sequence else None
    )
    assert proposal.reward_delta == proposal.proposed_reward - proposal.baseline_reward
    assert proposal.planner_revalidated is True
    assert proposal.truth_free is True
    assert proposal.policy_applied_to_actuator is False
    assert proposal.deployed is False
    assert proposal.candidate_promoted is False
    assert proposal.final_release_allowed is False

    mapping = proposal.to_mapping()
    assert mapping["contract"] == "multidetect.sequence-proposal"
    assert mapping["proposal_source"] == "bounded_exact_reference_v1"
    assert mapping["policy_applied_to_actuator"] is False
    assert "actuator_command" not in mapping


def test_sequence_proposal_objective_counts_completion_before_time():
    groups, _, _ = load_dataset(DATASET)
    case = next(item for item in groups["validation"] if item.case_id == "budget-validation-1")
    policy = _policy(case)
    proposal = build_sequence_proposal(
        policy,
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )

    assert proposal.baseline_reward == proposal.proposed_reward == 9
    assert proposal.baseline_completed_count == 2
    assert proposal.proposed_completed_count == 3
    assert proposal.objective_relation == "improved"
    assert proposal.to_mapping()["objective_relation"] == "improved"


def test_sequence_proposal_rejects_non_exhaustive_reference():
    case = _case()
    policy = _policy(case)
    proposal = build_sequence_proposal(
        policy,
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )
    with pytest.raises(ValueError, match="exhaustive"):
        SequenceProposal(
            policy_artifact_sha256=proposal.policy_artifact_sha256,
            ranker_sha256=proposal.ranker_sha256,
            context_sha256=proposal.context_sha256,
            vehicle_class=proposal.vehicle_class,
            airframe_profile_hash=proposal.airframe_profile_hash,
            baseline_sequence=proposal.baseline_sequence,
            proposed_sequence=proposal.proposed_sequence,
            baseline_reward=proposal.baseline_reward,
            proposed_reward=proposal.proposed_reward,
            baseline_elapsed_s=proposal.baseline_elapsed_s,
            proposed_elapsed_s=proposal.proposed_elapsed_s,
            reference=replace(proposal.reference, exhaustive=False),
            baseline_compiled_edges=proposal.baseline_compiled_edges,
        )


def test_sequence_proposal_fails_closed_on_identity_and_bounds():
    case = _case()
    policy = _policy(case)
    wrong_airframe = "a" * 64
    with pytest.raises(ValueError, match="airframe"):
        build_sequence_proposal(
            policy,
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=wrong_airframe,
        )
    with pytest.raises(ValueError, match="requires 1..1"):
        build_sequence_proposal(
            policy,
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
            max_tasks=1,
        )
    with pytest.raises(ValueError, match="missing"):
        build_sequence_proposal(
            policy,
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes={},
            airframe_profile_hash=case.airframe_profile_hash,
        )

    with pytest.raises(ValueError, match="context airframe profile mismatch"):
        build_sequence_proposal(
            policy,
            case.candidates,
            replace(case.context, airframe_profile_hash="b" * 64),
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
        )


def test_sequence_proposal_reports_safe_empty_result_when_gps_is_unqualified():
    case = _case()
    policy = _policy(case)
    no_fix = replace(case.context, gps_fix_type=2)
    proposal = build_sequence_proposal(
        policy,
        case.candidates,
        no_fix,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )

    assert proposal.baseline_sequence == ()
    assert proposal.proposed_sequence == ()
    assert proposal.first_task_id is None
    assert proposal.baseline_reward == 0
    assert proposal.proposed_reward == 0
    assert proposal.reward_delta == 0
    assert proposal.planner_revalidated is True
    assert proposal.policy_applied_to_actuator is False


def test_sequence_proposal_cli_binds_files_and_emits_advisory_report(tmp_path):
    output = tmp_path / "proposal.json"
    ranker = Path(
        "artifacts/evaluation/task-ranker-training-20261001-r1/candidate.json"
    )
    artifact = Path(
        "artifacts/evaluation/task-ranker-training-20261001-r1/"
        "candidate-policy-artifact.json"
    )
    if not ranker.exists() or not artifact.exists():
        pytest.skip("training artifact fixture is not present")
    assert (
        propose_main(
            [
                "--dataset",
                str(DATASET),
                "--dataset-sha256",
                "fcd1693881c1c7e2d51e738d8126b0388e161e62a5ea07b0ecbbbb69ca5fb6e6",
                "--case-id",
                "budget-train-0",
                "--ranker",
                str(ranker),
                "--ranker-sha256",
                "9dccfc3391a228b093df58e7aabe3867c977c081d5ec61d0519b6215901cec23",
                "--policy-artifact",
                str(artifact),
                "--policy-artifact-sha256",
                "418bd5bc9762dce80e1ccd2f4f4d267d39e9af8732feca0786aa7a8662b8d169",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = output.read_text(encoding="utf-8")
    assert '"contract": "multidetect.sequence-proposal-report"' in report
    assert '"advisory_only": true' in report
    assert '"mission_upload_allowed": false' in report


def test_sequence_proposal_holdout_evaluator_preserves_no_gain_claim(tmp_path):
    output = tmp_path / "holdout.json"
    ranker = Path(
        "artifacts/evaluation/task-ranker-training-20261001-r1/candidate.json"
    )
    artifact = Path(
        "artifacts/evaluation/task-ranker-training-20261001-r1/"
        "candidate-policy-artifact.json"
    )
    if not ranker.exists() or not artifact.exists():
        pytest.skip("training artifact fixture is not present")
    assert (
        evaluate_main(
            [
                "--dataset",
                str(
                    Path("configs/training/task-policy-independent-holdout-v1.json")
                ),
                "--dataset-sha256",
                "9cbb52fe5c690fe970d9bd3aa39b5c0b618fe221f0b54f7af8e12da8f2d7be75",
                "--training-dataset",
                str(Path("configs/training/task-ranker-budget-v1.json")),
                "--training-dataset-sha256",
                "fcd1693881c1c7e2d51e738d8126b0388e161e62a5ea07b0ecbbbb69ca5fb6e6",
                "--ranker",
                str(ranker),
                "--ranker-sha256",
                "9dccfc3391a228b093df58e7aabe3867c977c081d5ec61d0519b6215901cec23",
                "--policy-artifact",
                str(artifact),
                "--policy-artifact-sha256",
                "418bd5bc9762dce80e1ccd2f4f4d267d39e9af8732feca0786aa7a8662b8d169",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = output.read_text(encoding="utf-8")
    assert '"case_count": 12' in report
    assert '"reward_delta": 0' in report
    assert '"improved_cases": 0' in report
    assert '"candidate_promoted": false' in report


def test_bound_dataset_rejects_wrong_bytes_hash():
    with pytest.raises(ValueError, match="hash mismatch"):
        load_bound_dataset(DATASET, "0" * 64)


def test_holdout_evaluator_rejects_training_input_reuse(tmp_path):
    output = tmp_path / "overlap.json"
    ranker = Path(
        "artifacts/evaluation/task-ranker-training-20261001-r1/candidate.json"
    )
    artifact = Path(
        "artifacts/evaluation/task-ranker-training-20261001-r1/"
        "candidate-policy-artifact.json"
    )
    if not ranker.exists() or not artifact.exists():
        pytest.skip("training artifact fixture is not present")
    args = [
        "--training-dataset",
        str(DATASET),
        "--training-dataset-sha256",
        "fcd1693881c1c7e2d51e738d8126b0388e161e62a5ea07b0ecbbbb69ca5fb6e6",
        "--dataset",
        str(DATASET),
        "--dataset-sha256",
        "fcd1693881c1c7e2d51e738d8126b0388e161e62a5ea07b0ecbbbb69ca5fb6e6",
        "--ranker",
        str(ranker),
        "--ranker-sha256",
        "9dccfc3391a228b093df58e7aabe3867c977c081d5ec61d0519b6215901cec23",
        "--policy-artifact",
        str(artifact),
        "--policy-artifact-sha256",
        "418bd5bc9762dce80e1ccd2f4f4d267d39e9af8732feca0786aa7a8662b8d169",
        "--output",
        str(output),
    ]
    with pytest.raises(ValueError, match="input hashes overlap"):
        evaluate_main(args)
    assert not output.exists()

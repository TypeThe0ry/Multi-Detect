"""Accounting invariants for modeled budgets, not physical battery validation."""

from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.planner import PlannerPolicy
from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.policy_contract import SharedTaskPolicy
from multidetect.autonomy.task_ranker import TaskRanker
from multidetect.simulation.task_episode import simulate_task_budget_episode
from scripts.train_task_ranker import load_dataset


@pytest.mark.parametrize("case_index", range(12))
@pytest.mark.parametrize("coefficients", [(0, 0, 0), (0, 0, -1), (5, -0.05, -0.2)])
def test_reserve_not_double_debited_and_budget_rate_is_invariant(case_index, coefficients):
    path = (
        Path(__file__).resolve().parents[1] / "configs/training/task-sequence-heterogeneous-v1.json"
    )
    groups, _, _ = load_dataset(path)
    case = [c for name in ("train", "validation", "test") for c in groups[name]][case_index]
    episode = simulate_task_budget_episode(
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
        ranker=TaskRanker(coefficients, "accounting invariant test"),
    )
    assert episode.steps
    rate = case.context.battery_remaining_pct / case.context.estimated_flight_time_s
    constraints = {c.task.task_id: c.task.constraints for c in case.candidates}
    elapsed_sum = 0.0
    for step in episode.steps:
        before, after = step.before, step.after
        elapsed = step.compilation.estimate.estimated_duration_s
        reserve = constraints[step.compilation.estimate.task_id].minimum_return_battery_pct
        reserve += PlannerPolicy().minimum_battery_margin_pct
        # Under this explicitly linear model the reserved time remains fixed,
        # while usable time falls by exactly one mission cost, not cost+reserve.
        reserved_time = reserve / rate
        before_usable = before.estimated_flight_time_s - reserved_time
        after_usable = after.estimated_flight_time_s - reserved_time
        assert step.compilation.estimate.available_duration_s == pytest.approx(before_usable)
        assert before_usable - after_usable == pytest.approx(elapsed)
        assert before.battery_remaining_pct - after.battery_remaining_pct == pytest.approx(
            rate * elapsed
        )
        assert after.battery_remaining_pct / after.estimated_flight_time_s == pytest.approx(rate)
        assert after.captured_at_s - before.captured_at_s == pytest.approx(elapsed)
        assert after.battery_remaining_pct >= reserve - 1e-9
        elapsed_sum += elapsed
    assert episode.final_context.estimated_flight_time_s == pytest.approx(
        case.context.estimated_flight_time_s - elapsed_sum
    )


def _shared_policy_case():
    path = (
        Path(__file__).resolve().parents[1] / "configs/training/task-sequence-heterogeneous-v1.json"
    )
    groups, _, _ = load_dataset(path)
    return groups["train"][0]


def _shared_policy(case):
    ranker = TaskRanker((0, 0, 0), "budget-shared-policy-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=case.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    return SharedTaskPolicy(ranker, artifact)


def test_budget_episode_shared_policy_validates_profile_and_decision_identity(monkeypatch):
    case = _shared_policy_case()
    policy = _shared_policy(case)
    episode = simulate_task_budget_episode(
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
        shared_policy=policy,
    )
    assert episode.steps
    assert episode.policy_artifact_sha256 == policy.artifact_sha256

    with pytest.raises(ValueError, match="airframe profile mismatch"):
        simulate_task_budget_episode(
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash="a" * 64,
            shared_policy=policy,
        )

    original_decide = SharedTaskPolicy.decide

    def drifted_decide(self, entries, snapshot, vehicle):
        decision, selected = original_decide(self, entries, snapshot, vehicle)
        return replace(decision, policy_artifact_sha256="0" * 64), selected

    monkeypatch.setattr(SharedTaskPolicy, "decide", drifted_decide)
    with pytest.raises(ValueError, match="shared policy decision identity mismatch"):
        simulate_task_budget_episode(
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
            shared_policy=policy,
        )

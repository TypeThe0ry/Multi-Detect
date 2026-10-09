"""Deterministic planning-budget episodes, not flight-dynamics evidence.

Reuse task selection and PX4 compilation for independent return-home sorties.
Durations are planner estimates and energy uses a constant-rate approximation;
neither constitutes measured execution or a calibrated aircraft energy model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from ..autonomy.contracts import ContextSnapshot, PlanAction
from ..autonomy.policy_contract import (
    PolicyAction,
    SharedTaskPolicy,
    policy_decision_matches_selected_task,
)
from ..autonomy.task_ranker import TaskRanker
from ..autonomy.task_selection import RejectedTask, TaskCandidate, select_next_task
from ..clock import ManualClock
from ..mission_planning import PlannedMissionCompilation, compile_planned_task_mission_from_runtime


@dataclass(frozen=True, slots=True)
class BudgetEpisodeStep:
    before: ContextSnapshot
    after: ContextSnapshot
    compilation: PlannedMissionCompilation


@dataclass(frozen=True, slots=True)
class BudgetEpisode:
    steps: tuple[BudgetEpisodeStep, ...]
    final_context: ContextSnapshot
    rejected: tuple[RejectedTask, ...]
    ranker_sha256: str | None = None
    policy_artifact_sha256: str | None = None


def simulate_task_budget_episode(
    candidates: Iterable[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtimes: Mapping[str, object],
    airframe_profile_hash: str,
    ranker: TaskRanker | None = None,
    shared_policy: SharedTaskPolicy | None = None,
) -> BudgetEpisode:
    """Replan after every projected sortie without replenishing its battery.

    Missing/invalid compile inputs abort rather than counting a mission as
    completed. All candidates must have RETURN completion: arbitrary landing
    site state and continuous in-flight task chaining are not modeled here.
    """
    if ranker is not None and not isinstance(ranker, TaskRanker):
        raise ValueError("TaskRanker required")
    if shared_policy is not None and not isinstance(shared_policy, SharedTaskPolicy):
        raise ValueError("SharedTaskPolicy required")
    if shared_policy is not None:
        if ranker is not None and ranker.canonical_hash != shared_policy.ranker.canonical_hash:
            raise ValueError("ranker and shared_policy do not bind the same candidate")
        if airframe_profile_hash != shared_policy.artifact.airframe_profile_hash:
            raise ValueError("shared policy airframe profile mismatch")
        ranker = shared_policy.ranker
    pending = list(candidates)
    # Validate IDs, types and context even for an empty episode.
    select_next_task(pending, context, vehicle_class)
    for entry in pending:
        if entry.task.completion_action is not PlanAction.RETURN:
            raise ValueError("budget episode requires RETURN completion")
        if entry.task.task_id not in runtimes:
            raise ValueError(f"missing compile runtime for {entry.task.task_id}")
    clock = ManualClock(
        initial_monotonic_s=context.captured_at_s,
        initial_utc=datetime.fromisoformat(context.captured_at_utc.replace("Z", "+00:00")),
    )
    steps: list[BudgetEpisodeStep] = []
    current = context
    rejected: tuple[RejectedTask, ...] = ()
    while pending:
        decision = select_next_task(pending, current, vehicle_class)
        policy_decision = None
        if shared_policy is not None:
            policy_decision, selected = shared_policy.decide(
                pending, current, vehicle_class
            )
            if policy_decision.action is PolicyAction.ABSTAIN or selected is None:
                rejected = decision.rejected
                break
            if not policy_decision_matches_selected_task(
                policy_decision,
                selected.candidate.task.task_id,
                shared_policy.artifact_sha256,
            ):
                raise ValueError("shared policy decision identity mismatch")
        elif ranker is not None:
            selected = ranker.select(pending, current, vehicle_class)
        else:
            selected = decision.selected
        if selected is None:
            rejected = decision.rejected
            break
        task = selected.candidate.task
        compiled = compile_planned_task_mission_from_runtime(
            task,
            current,
            vehicle_class,
            runtime=runtimes[task.task_id],
            airframe_profile_hash=airframe_profile_hash,
        )
        if compiled.plan != selected.plan:
            raise ValueError("episode compilation changed selected plan")
        elapsed = compiled.estimate.estimated_duration_s
        # The planner already reserves energy; do not subtract reserve again.
        remaining = max(0.0, current.estimated_flight_time_s - elapsed)
        battery = current.battery_remaining_pct * remaining / current.estimated_flight_time_s
        clock.advance(elapsed)
        after = replace(
            current,
            snapshot_id=f"{context.snapshot_id}:budget-step-{len(steps) + 1}",
            captured_at_s=clock.monotonic(),
            captured_at_utc=clock.utc_now().isoformat(),
            aircraft_position=current.home_position,
            battery_remaining_pct=battery,
            estimated_flight_time_s=remaining,
            ground_speed_mps=0.0,
            airspeed_mps=0.0,
        )
        steps.append(BudgetEpisodeStep(current, after, compiled))
        current = after
        pending = [entry for entry in pending if entry.task.task_id != task.task_id]
    return BudgetEpisode(
        tuple(steps),
        current,
        rejected,
        ranker.canonical_hash if ranker is not None else None,
        shared_policy.artifact_sha256 if shared_policy is not None else None,
    )

"""Shared observed-result task sequencing, separate from budget projection.

The backend callback owns existing transaction/permission/execution machinery.
This module never sends commands, grants permission, or fabricates telemetry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ..clock import Clock
from ..mission_planning import PlannedMissionCompilation, compile_planned_task_mission_from_runtime
from ..px4_mission_feedback import PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
from .contracts import ContextSnapshot
from .mission_outcome import MissionOutcome, MissionOutcomeMonitor
from .policy_contract import (
    PolicyAction,
    SharedTaskPolicy,
    policy_decision_matches_selected_task,
)
from .serialization import require_real
from .task_policy_runtime import TaskPolicyRuntimeBinding
from .task_ranker import TaskRanker
from .task_selection import RejectedTask, TaskCandidate, select_next_task

ObserveTask = Callable[[PlannedMissionCompilation, MissionOutcomeMonitor], ContextSnapshot | None]


@dataclass(frozen=True, slots=True)
class ObservedTaskStep:
    compilation: PlannedMissionCompilation
    outcome: MissionOutcome
    after: ContextSnapshot | None
    ranker_sha256: str | None = None
    policy_artifact_sha256: str | None = None
    policy_binding: TaskPolicyRuntimeBinding | None = None


@dataclass(frozen=True, slots=True)
class ObservedTaskSession:
    steps: tuple[ObservedTaskStep, ...]
    context: ContextSnapshot
    remaining_task_ids: tuple[str, ...]
    stop_reason: str
    rejected: tuple[RejectedTask, ...] = ()


def _check_snapshot(context: ContextSnapshot, clock: Clock) -> None:
    if not isinstance(context, ContextSnapshot):
        raise ValueError("observed ContextSnapshot required")
    now = require_real(clock.monotonic(), "clock", minimum=0.0)
    age = now - context.captured_at_s
    utc_age = (
        clock.utc_now() - datetime.fromisoformat(context.captured_at_utc.replace("Z", "+00:00"))
    ).total_seconds()
    maximum = PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
    if not (0 <= age <= maximum and 0 <= utc_age <= maximum):
        raise ValueError("snapshot is stale or from a different clock domain")


def run_observed_task_session(
    candidates: Iterable[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtimes: Mapping[str, object],
    airframe_profile_hash: str,
    clock: Clock,
    observe_task: ObserveTask,
    expected_system_id: int,
    expected_component_id: int,
    ranker: TaskRanker | None = None,
    shared_policy: SharedTaskPolicy | None = None,
) -> ObservedTaskSession:
    """Select again only after a completed mission and a fresh observed context.

    A callback must pump the monitor until terminal and return measured state
    from the same runtime/vehicle. Early return, failure and timeout stop the
    session without executing another task. No battery/time estimation is used
    to construct the next context. Every callback still needs its own existing
    transaction and operator authorization; selection is not authorization.
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
    select_next_task(pending, context, vehicle_class)
    _check_snapshot(context, clock)
    for candidate in pending:
        if candidate.task.task_id not in runtimes:
            raise ValueError(f"missing runtime for {candidate.task.task_id}")
    steps: list[ObservedTaskStep] = []
    current = context

    def result(reason: str, rejected: tuple[RejectedTask, ...] = ()) -> ObservedTaskSession:
        return ObservedTaskSession(
            tuple(steps),
            current,
            tuple(sorted(entry.task.task_id for entry in pending)),
            reason,
            rejected,
        )

    while pending:
        _check_snapshot(current, clock)
        baseline_decision = select_next_task(pending, current, vehicle_class)
        if baseline_decision.selected is None:
            return result("no_feasible_task", baseline_decision.rejected)
        policy_decision = None
        if shared_policy is not None:
            policy_decision, selected = shared_policy.decide(
                pending, current, vehicle_class
            )
            if policy_decision.action is PolicyAction.ABSTAIN or selected is None:
                return result("policy_abstained", baseline_decision.rejected)
            if not policy_decision_matches_selected_task(
                policy_decision,
                selected.candidate.task.task_id,
                shared_policy.artifact_sha256,
            ):
                raise ValueError("shared policy decision identity mismatch")
        elif ranker is not None:
            selected = ranker.select(pending, current, vehicle_class)
        else:
            selected = baseline_decision.selected
        if selected is None:
            return result("policy_abstained", baseline_decision.rejected)
        task = selected.candidate.task
        compilation = compile_planned_task_mission_from_runtime(
            task,
            current,
            vehicle_class,
            runtime=runtimes[task.task_id],
            airframe_profile_hash=airframe_profile_hash,
        )
        if compilation.plan != selected.plan:
            raise ValueError("compilation changed selected plan")
        started = clock.monotonic()
        monitor = MissionOutcomeMonitor(
            compilation.compiled_mission.executable_mission,
            clock,
            timeout_s=task.constraints.maximum_duration_s,
            expected_system_id=expected_system_id,
            expected_component_id=expected_component_id,
        )
        after = observe_task(compilation, monitor)
        outcome = monitor.poll()
        steps.append(
            ObservedTaskStep(
                compilation,
                outcome,
                after,
                ranker.canonical_hash if ranker is not None else None,
                shared_policy.artifact_sha256 if shared_policy is not None else None,
                TaskPolicyRuntimeBinding.from_decision(
                    context=current,
                    airframe_profile_hash=airframe_profile_hash,
                    vehicle_class=vehicle_class,
                    decision=policy_decision,
                    selected=selected,
                    policy_artifact_sha256=(
                        shared_policy.artifact_sha256 if shared_policy is not None else None
                    ),
                    ranker_sha256=ranker.canonical_hash if ranker is not None else None,
                    plan_sha256=compilation.plan.canonical_hash,
                    executable_mission_sha256=compilation.executable_mission_hash,
                    wire_hash=compilation.wire_hash,
                    reason=(
                        policy_decision.reason
                        if policy_decision is not None
                        else ("ranker_selected" if ranker is not None else "baseline_selected")
                    ),
                ),
            )
        )
        if outcome.status != "completed":
            return result(f"mission_{outcome.status}")
        if after is None:
            return result("missing_post_mission_snapshot")
        try:
            _check_snapshot(after, clock)
        except ValueError:
            return result("invalid_post_mission_snapshot")
        if (
            after.captured_at_s < started + outcome.elapsed_s
            or after.snapshot_id == current.snapshot_id
            or after.airframe_profile_id != current.airframe_profile_id
            or after.home_position != current.home_position
        ):
            return result("post_mission_identity_or_time_mismatch")
        current = after
        pending = [entry for entry in pending if entry.task.task_id != task.task_id]
    return result("all_tasks_completed")

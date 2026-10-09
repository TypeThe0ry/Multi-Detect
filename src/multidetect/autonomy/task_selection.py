"""Incremental next-task selection using the existing planner unchanged.

This is an offline/shared-core API, not an execution authority or a learned
model. Each candidate is an independent complete mission from one observation.
Rankings must not be concatenated into a multi-task flight: after a mission or
context change, callers must select again from a fresh ContextSnapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .contracts import ContextSnapshot, PlatformNeutralPlan, ResolvedTaskSpec
from .planner import (
    PlannerError,
    PlanningEstimate,
    estimate_task,
    normalize_vehicle_class,
    plan_task,
)
from .serialization import require_int


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    task: ResolvedTaskSpec
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task, ResolvedTaskSpec):
            raise PlannerError("candidate task must be a ResolvedTaskSpec")
        object.__setattr__(self, "priority", require_int(self.priority, "priority", minimum=0))


@dataclass(frozen=True, slots=True)
class EvaluatedTask:
    candidate: TaskCandidate
    estimate: PlanningEstimate
    plan: PlatformNeutralPlan


@dataclass(frozen=True, slots=True)
class RejectedTask:
    task_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class TaskSelection:
    context_snapshot_hash: str
    ranked: tuple[EvaluatedTask, ...]
    rejected: tuple[RejectedTask, ...]

    @property
    def selected(self) -> EvaluatedTask | None:
        return self.ranked[0] if self.ranked else None


def select_next_task(
    candidates: Iterable[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
) -> TaskSelection:
    """Rank feasible missions by priority, duration, distance, then stable ID.

    Priority never overrides planner rejection. No safety policy override is
    exposed to the ranking layer. Returned plans are exactly plan_task outputs
    and still require downstream validation/operator authority before use.
    """
    if not isinstance(context, ContextSnapshot):
        raise PlannerError("context must be a ContextSnapshot")
    vehicle = normalize_vehicle_class(vehicle_class)
    entries = tuple(candidates)
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, TaskCandidate):
            raise PlannerError("candidates must contain TaskCandidate values")
        if entry.task.task_id in seen:
            raise PlannerError("candidate task IDs must be unique")
        seen.add(entry.task.task_id)

    ranked: list[EvaluatedTask] = []
    rejected: list[RejectedTask] = []
    for entry in sorted(entries, key=lambda value: value.task.task_id):
        try:
            estimate = estimate_task(entry.task, context, vehicle)
            plan = plan_task(entry.task, context, vehicle)
        except PlannerError as exc:
            rejected.append(RejectedTask(entry.task.task_id, str(exc)))
        else:
            ranked.append(EvaluatedTask(entry, estimate, plan))
    ranked.sort(
        key=lambda value: (
            -value.candidate.priority,
            value.estimate.estimated_duration_s,
            value.estimate.total_distance_m,
            value.candidate.task.task_id,
        )
    )
    return TaskSelection(context.canonical_hash, tuple(ranked), tuple(rejected))

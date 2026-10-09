"""Shared task-ranking observation/action boundary, extending the existing planner.

No model, transport or execution authority lives here. A future learned ranker
may choose a feasible task or abstain; its action is revalidated against current
inputs before the original compilation/approval path can consume the plan.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .contracts import ContextSnapshot
from .planner import normalize_vehicle_class
from .serialization import (
    canonical_sha256,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .task_selection import EvaluatedTask, TaskCandidate, select_next_task


def build_policy_observation(
    candidates: Iterable[TaskCandidate], context: ContextSnapshot, vehicle_class: str
) -> dict:
    """Expose named SI-unit estimates only after the unchanged feasibility gate.

    Candidate order is the baseline ranking. Hashes bind complete task/plan and
    context identities even when their model-facing numeric features coincide.
    Simulator truth and mission execution permissions are not observations.
    """
    selection = select_next_task(candidates, context, vehicle_class)
    return {
        "schema_version": 1,
        "contract": "multidetect.task-ranking-observation",
        "context_sha256": context.canonical_hash,
        "vehicle_class": normalize_vehicle_class(vehicle_class),
        "feasible": [
            {
                "task_id": item.candidate.task.task_id,
                "task_sha256": item.candidate.task.canonical_hash,
                "plan_sha256": item.plan.canonical_hash,
                "priority": item.candidate.priority,
                "total_distance_m": item.estimate.total_distance_m,
                "estimated_duration_s": item.estimate.estimated_duration_s,
                "available_duration_s": item.estimate.available_duration_s,
            }
            for item in selection.ranked
        ],
        "rejected": [
            {"task_id": item.task_id, "reason": item.reason} for item in selection.rejected
        ],
        "truth_free": True,
    }


@dataclass(frozen=True, slots=True)
class TaskPolicyAction:
    observation_sha256: str
    task_id: str | None

    def __post_init__(self):
        object.__setattr__(
            self,
            "observation_sha256",
            require_sha256(self.observation_sha256, "observation_sha256"),
        )
        if self.task_id is not None:
            object.__setattr__(self, "task_id", require_text(self.task_id, "task_id"))

    def to_mapping(self) -> dict:
        return {
            "schema_version": 1,
            "observation_sha256": self.observation_sha256,
            "task_id": self.task_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> TaskPolicyAction:
        data = strict_object(
            value,
            name="task policy action",
            required=frozenset({"schema_version", "observation_sha256", "task_id"}),
        )
        require_schema_version(data["schema_version"])
        return cls(data["observation_sha256"], data["task_id"])


def baseline_policy_action(
    candidates: Iterable[TaskCandidate], context: ContextSnapshot, vehicle_class: str
) -> TaskPolicyAction:
    observation = build_policy_observation(candidates, context, vehicle_class)
    feasible = observation["feasible"]
    return TaskPolicyAction(
        canonical_sha256(observation), feasible[0]["task_id"] if feasible else None
    )


def resolve_policy_action(
    action: TaskPolicyAction,
    candidates: Iterable[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
) -> EvaluatedTask | None:
    """Recompute feasibility and binding; never trust a supplied score or plan.

    None is explicit abstention, not a hover/land/disarm command. Callers still
    enforce freshness, compile validation and Mission authorization separately.
    """
    if not isinstance(action, TaskPolicyAction):
        raise ValueError("TaskPolicyAction required")
    entries = tuple(candidates)
    observation = build_policy_observation(entries, context, vehicle_class)
    if action.observation_sha256 != canonical_sha256(observation):
        raise ValueError("policy observation changed; select again")
    if action.task_id is None:
        return None
    for item in select_next_task(entries, context, vehicle_class).ranked:
        if item.candidate.task.task_id == action.task_id:
            return item
    raise ValueError("policy selected a task outside the feasible set")

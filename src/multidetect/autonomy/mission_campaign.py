"""Vehicle-neutral task selection and Mission compilation seam.

This module deliberately stops before transport.  It is the reusable core for
SIM, HIL and REAL adapters: a fresh truth-free ``ContextSnapshot`` is given to
the existing planner/shared policy, the selected plan is compiled through the
existing PX4 dispatcher, and the adapter receives a hash-bound result.  The
vehicle lifecycle (telemetry, upload, permit, flight completion and cleanup)
remains an injected adapter concern, so VTOL-specific evidence cannot be
silently relabeled as fixed-wing or multirotor evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .contracts import ContextSnapshot
from .planner import normalize_vehicle_class
from .policy_contract import (
    PolicyAction,
    PolicyDecision,
    SharedTaskPolicy,
    policy_decision_matches_selected_task,
)
from .task_policy_runtime import TaskPolicyRuntimeBinding
from .task_ranker import TaskRanker
from .task_selection import EvaluatedTask, TaskCandidate, select_next_task

if TYPE_CHECKING:
    from ..mission_planning import PlannedMissionCompilation


@dataclass(frozen=True, slots=True)
class CampaignTaskBinding:
    """One policy/planner/compiler result, with no actuator authority."""

    vehicle_class: str
    context: ContextSnapshot
    context_sha256: str
    selected: EvaluatedTask | None
    decision: PolicyDecision | None
    compilation: PlannedMissionCompilation | None

    @property
    def selected_task_id(self) -> str | None:
        return self.selected.candidate.task.task_id if self.selected is not None else None

    @property
    def planner_revalidated(self) -> bool:
        return self.decision is None or self.decision.planner_revalidated

    @property
    def mission_bound(self) -> bool:
        return self.compilation is not None

    def to_mapping(self) -> dict[str, object]:
        return {
            "vehicle_class": self.vehicle_class,
            "context_sha256": self.context_sha256,
            "selected_task_id": self.selected_task_id,
            "decision": self.decision.to_mapping() if self.decision is not None else None,
            "plan_sha256": self.compilation.plan.canonical_hash
            if self.compilation is not None
            else None,
            "executable_mission_sha256": self.compilation.executable_mission_hash
            if self.compilation is not None
            else None,
            "wire_hash": self.compilation.wire_hash if self.compilation is not None else None,
            "mission_bound": self.mission_bound,
            "planner_revalidated": self.planner_revalidated,
            "policy_applied_to_actuator": False,
            "truth_free": self.decision.truth_free if self.decision is not None else True,
        }

    def to_runtime_binding(
        self,
        *,
        airframe_profile_hash: str,
        ranker_sha256: str | None = None,
        reason: str | None = None,
    ) -> TaskPolicyRuntimeBinding:
        """Expose the same result as a read-only cross-mode UI status."""

        return TaskPolicyRuntimeBinding.from_decision(
            context=self.context,
            airframe_profile_hash=airframe_profile_hash,
            vehicle_class=self.vehicle_class,
            decision=self.decision,
            selected=self.selected,
            policy_artifact_sha256=(
                self.decision.policy_artifact_sha256 if self.decision is not None else None
            ),
            ranker_sha256=ranker_sha256,
            plan_sha256=self.compilation.plan.canonical_hash if self.compilation else None,
            executable_mission_sha256=(
                self.compilation.executable_mission_hash if self.compilation else None
            ),
            wire_hash=self.compilation.wire_hash if self.compilation else None,
            reason=reason,
        )


def select_and_compile_task(
    candidates: Sequence[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtimes: Mapping[str, object],
    airframe_profile_hash: str,
    shared_policy: SharedTaskPolicy | None = None,
    ranker: TaskRanker | None = None,
    mission_id: str | None = None,
) -> CampaignTaskBinding:
    """Select and compile exactly one task through the shared path.

    ``shared_policy`` and ``ranker`` are mutually exclusive.  A policy
    abstention or a planner rejection returns an unbound result; it never
    creates an executable Mission.  The function does not send MAVLink or
    grant execution authority.
    """

    if not isinstance(context, ContextSnapshot):
        raise TypeError("context must be a ContextSnapshot")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
        raise TypeError("candidates must be a sequence")
    entries = tuple(candidates)
    if not entries or any(not isinstance(item, TaskCandidate) for item in entries):
        raise ValueError("candidates must be a non-empty TaskCandidate sequence")
    if not isinstance(runtimes, Mapping):
        raise TypeError("runtimes must be a mapping")
    if shared_policy is not None and not isinstance(shared_policy, SharedTaskPolicy):
        raise TypeError("shared_policy must be a SharedTaskPolicy")
    if ranker is not None and not isinstance(ranker, TaskRanker):
        raise TypeError("ranker must be a TaskRanker")
    if shared_policy is not None and ranker is not None:
        raise ValueError("shared_policy and ranker are mutually exclusive")
    if not isinstance(airframe_profile_hash, str) or len(airframe_profile_hash) != 64:
        raise ValueError("airframe_profile_hash must be a SHA-256 string")
    if (
        shared_policy is not None
        and shared_policy.artifact.airframe_profile_hash != airframe_profile_hash
    ):
        raise ValueError("shared policy airframe profile mismatch")

    selected: EvaluatedTask | None
    decision: PolicyDecision | None = None
    normalized_vehicle = normalize_vehicle_class(vehicle_class)
    if shared_policy is not None:
        decision, selected = shared_policy.decide(entries, context, normalized_vehicle)
        if decision.action is PolicyAction.ABSTAIN or selected is None:
            return CampaignTaskBinding(
                vehicle_class=normalized_vehicle,
                context=context,
                context_sha256=context.canonical_hash,
                selected=None,
                decision=decision,
                compilation=None,
            )
        if not policy_decision_matches_selected_task(
            decision,
            selected.candidate.task.task_id,
            shared_policy.artifact_sha256,
        ):
            raise ValueError("shared policy decision identity mismatch")
    elif ranker is not None:
        selected = ranker.select(entries, context, normalized_vehicle)
    else:
        selected = select_next_task(entries, context, normalized_vehicle).selected
    if selected is None:
        return CampaignTaskBinding(
            vehicle_class=normalized_vehicle,
            context=context,
            context_sha256=context.canonical_hash,
            selected=None,
            decision=decision,
            compilation=None,
        )
    task_id = selected.candidate.task.task_id
    if task_id not in runtimes:
        raise ValueError(f"missing compile runtime for {task_id}")
    # Import lazily: mission_planning imports the public autonomy contracts,
    # while this module is re-exported by autonomy.__init__.  A top-level
    # import would make fresh imports of mission_planning/px4_mission order
    # dependent and fail before any runtime code is reached.
    from ..mission_planning import compile_planned_task_mission_from_runtime

    compilation = compile_planned_task_mission_from_runtime(
        selected.candidate.task,
        context,
        normalized_vehicle,
        runtime=runtimes[task_id],
        airframe_profile_hash=airframe_profile_hash,
        mission_id=mission_id,
    )
    if compilation.plan != selected.plan:
        raise ValueError("planner compilation changed the selected plan")
    return CampaignTaskBinding(
        vehicle_class=compilation.vehicle_class,
        context=context,
        context_sha256=context.canonical_hash,
        selected=selected,
        decision=decision,
        compilation=compilation,
    )


__all__ = ["CampaignTaskBinding", "select_and_compile_task"]

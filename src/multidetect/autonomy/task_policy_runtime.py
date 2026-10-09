"""Read-only runtime binding for the shared autonomous task policy.

The policy is deliberately kept above the actuator boundary.  This document
is the small, serializable status object that SIM, REPLAY, HIL and REAL
adapters can expose to an operator/UI without giving the policy a transport,
arming, or actuator capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .contracts import ContextSnapshot
from .policy_contract import PolicyAction, PolicyDecision
from .serialization import require_sha256, require_text
from .task_selection import EvaluatedTask


@dataclass(frozen=True, slots=True)
class TaskPolicyRuntimeBinding:
    """One hash-bound, display-only policy decision.

    ``mission_bound`` means that the existing planner/compiler produced an
    executable mission document.  It does *not* mean the mission was uploaded
    or started.  Those operations remain adapter-owned and are represented by
    the explicit ``mission_started`` field, which is always false for a
    policy-only binding.
    """

    vehicle_class: str
    context_sha256: str
    airframe_profile_hash: str
    action: str
    reason: str
    selected_task_id: str | None
    policy_artifact_sha256: str | None
    ranker_sha256: str | None
    plan_sha256: str | None
    executable_mission_sha256: str | None
    wire_hash: str | None
    planner_revalidated: bool
    mission_bound: bool
    mission_started: bool = False
    policy_applied_to_actuator: bool = False
    truth_free: bool = True
    produced_at_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "vehicle_class", require_text(self.vehicle_class, "vehicle_class"))
        object.__setattr__(self, "action", require_text(self.action, "action"))
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "context_sha256",
            require_sha256(self.context_sha256, "context_sha256"),
        )
        object.__setattr__(
            self,
            "airframe_profile_hash",
            require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        for field in (
            "policy_artifact_sha256",
            "ranker_sha256",
            "plan_sha256",
            "executable_mission_sha256",
            "wire_hash",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_sha256(value, field))
        for field in (
            "planner_revalidated",
            "mission_bound",
            "mission_started",
            "policy_applied_to_actuator",
            "truth_free",
        ):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be a boolean")
        if self.selected_task_id is not None:
            object.__setattr__(
                self,
                "selected_task_id",
                require_text(self.selected_task_id, "selected_task_id"),
            )
        if not isfinite(self.produced_at_s) or self.produced_at_s < 0.0:
            raise ValueError("produced_at_s must be finite and non-negative")
        if self.action == PolicyAction.SELECT.value:
            if self.selected_task_id is None or not self.mission_bound:
                raise ValueError("a selected policy binding requires a task and bound mission")
            if not self.planner_revalidated:
                raise ValueError("a selected policy binding requires planner revalidation")
        if self.action == PolicyAction.ABSTAIN.value:
            if self.selected_task_id is not None or self.mission_bound:
                raise ValueError("an abstained policy binding cannot bind a mission")
        if self.mission_bound and self.plan_sha256 is None:
            raise ValueError("a bound mission requires plan_sha256")
        if self.mission_started:
            raise ValueError("policy runtime binding cannot start a mission")
        if self.policy_applied_to_actuator:
            raise ValueError("policy runtime binding cannot control an actuator")
        if not self.truth_free:
            raise ValueError("policy runtime binding must be truth-free")

    @classmethod
    def from_decision(
        cls,
        *,
        context: ContextSnapshot,
        airframe_profile_hash: str,
        vehicle_class: str,
        decision: PolicyDecision | None,
        selected: EvaluatedTask | None,
        policy_artifact_sha256: str | None = None,
        ranker_sha256: str | None = None,
        plan_sha256: str | None = None,
        executable_mission_sha256: str | None = None,
        wire_hash: str | None = None,
        reason: str | None = None,
    ) -> TaskPolicyRuntimeBinding:
        """Create a binding after planner compilation (or an abstention)."""

        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if decision is not None and not isinstance(decision, PolicyDecision):
            raise TypeError("decision must be a PolicyDecision or None")
        if selected is not None and not isinstance(selected, EvaluatedTask):
            raise TypeError("selected must be an EvaluatedTask or None")
        action = decision.action.value if decision is not None else (
            PolicyAction.SELECT.value if selected is not None else PolicyAction.ABSTAIN.value
        )
        if selected is None:
            action = PolicyAction.ABSTAIN.value
        return cls(
            vehicle_class=vehicle_class,
            context_sha256=context.canonical_hash,
            airframe_profile_hash=airframe_profile_hash,
            action=action,
            reason=reason
            or (
                "policy_abstained"
                if action == PolicyAction.ABSTAIN.value
                else "selected"
            ),
            selected_task_id=selected.candidate.task.task_id if selected is not None else None,
            policy_artifact_sha256=policy_artifact_sha256,
            ranker_sha256=ranker_sha256,
            plan_sha256=plan_sha256,
            executable_mission_sha256=executable_mission_sha256,
            wire_hash=wire_hash,
            planner_revalidated=(decision is None or decision.planner_revalidated)
            if selected is not None
            else False,
            mission_bound=selected is not None,
            produced_at_s=context.captured_at_s,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "vehicle_class": self.vehicle_class,
            "context_sha256": self.context_sha256,
            "airframe_profile_hash": self.airframe_profile_hash,
            "action": self.action,
            "reason": self.reason,
            "selected_task_id": self.selected_task_id,
            "policy_artifact_sha256": self.policy_artifact_sha256,
            "ranker_sha256": self.ranker_sha256,
            "plan_sha256": self.plan_sha256,
            "executable_mission_sha256": self.executable_mission_sha256,
            "wire_hash": self.wire_hash,
            "planner_revalidated": self.planner_revalidated,
            "mission_bound": self.mission_bound,
            "mission_started": self.mission_started,
            "policy_applied_to_actuator": self.policy_applied_to_actuator,
            "truth_free": self.truth_free,
            "produced_at_s": self.produced_at_s,
        }


__all__ = ["TaskPolicyRuntimeBinding"]

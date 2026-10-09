"""Shared, truth-free task-policy observation and decision contract.

This is an adapter around the existing planner and ``TaskRanker``.  It adds a
typed boundary and a hash-bound policy identity without replacing the current
planner, training format, or Mission compiler.  Both synthetic SIM episodes
and observed HIL/REAL sessions can therefore call the same object.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..compat import StrEnum
from .contracts import ContextSnapshot
from .policy_artifact import PolicyArtifact
from .serialization import canonical_sha256, require_bool, require_sha256, require_text
from .task_policy import TaskPolicyAction, build_policy_observation, resolve_policy_action
from .task_ranker import TaskRanker
from .task_selection import EvaluatedTask, TaskCandidate

POLICY_OBSERVATION_CONTRACT = "multidetect.shared-task-policy-observation"
POLICY_DECISION_CONTRACT = "multidetect.shared-task-policy-decision"


class PolicyAction(StrEnum):
    SELECT = "select"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class PolicyObservation:
    """Truth-free planner observation with stable feature ordering."""

    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, dict):
            raise TypeError("policy observation payload must be a dict")
        if self.payload.get("contract") != POLICY_OBSERVATION_CONTRACT:
            raise ValueError("unsupported policy observation contract")
        if self.payload.get("truth_free") is not True:
            raise ValueError("policy observation must be truth-free")
        object.__setattr__(self, "payload", deepcopy(self.payload))

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.payload)

    def to_mapping(self) -> dict[str, Any]:
        return deepcopy(self.payload)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A proposal, never an actuator command or execution authorization."""

    action: PolicyAction
    observation_sha256: str
    policy_artifact_sha256: str
    task_id: str | None
    reason: str
    planner_revalidated: bool
    truth_free: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.action, PolicyAction):
            raise TypeError("action must be PolicyAction")
        object.__setattr__(
            self,
            "observation_sha256",
            require_sha256(self.observation_sha256, "observation_sha256"),
        )
        object.__setattr__(
            self,
            "policy_artifact_sha256",
            require_sha256(self.policy_artifact_sha256, "policy_artifact_sha256"),
        )
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        if self.task_id is not None:
            object.__setattr__(self, "task_id", require_text(self.task_id, "task_id"))
        object.__setattr__(
            self,
            "planner_revalidated",
            require_bool(self.planner_revalidated, "planner_revalidated"),
        )
        object.__setattr__(self, "truth_free", require_bool(self.truth_free, "truth_free"))
        if self.action is PolicyAction.SELECT and self.task_id is None:
            raise ValueError("select decision requires task_id")
        if self.action is PolicyAction.ABSTAIN and self.task_id is not None:
            raise ValueError("abstain decision cannot name task_id")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": POLICY_DECISION_CONTRACT,
            "action": self.action.value,
            "observation_sha256": self.observation_sha256,
            "policy_artifact_sha256": self.policy_artifact_sha256,
            "task_id": self.task_id,
            "reason": self.reason,
            "planner_revalidated": self.planner_revalidated,
            "truth_free": self.truth_free,
        }


def policy_decision_matches_selected_task(
    decision: PolicyDecision | None,
    selected_task_id: str | None,
    expected_artifact_sha256: str,
) -> bool:
    """Return whether a selected decision is identity-bound and safe to use."""

    return (
        isinstance(decision, PolicyDecision)
        and decision.action is PolicyAction.SELECT
        and selected_task_id is not None
        and decision.task_id == selected_task_id
        and decision.policy_artifact_sha256 == expected_artifact_sha256
        and decision.truth_free is True
        and decision.planner_revalidated is True
    )


@dataclass(frozen=True, slots=True)
class SharedTaskPolicy:
    """The one high-level task decision implementation shared by SIM/HIL/REAL."""

    ranker: TaskRanker
    artifact: PolicyArtifact

    def __post_init__(self) -> None:
        if not isinstance(self.ranker, TaskRanker):
            raise TypeError("ranker must be a TaskRanker")
        if not isinstance(self.artifact, PolicyArtifact):
            raise TypeError("artifact must be a PolicyArtifact")
        if self.artifact.ranker_sha256 != self.ranker.canonical_hash:
            raise ValueError("policy artifact does not bind the supplied ranker")

    def _validate_context_profile(self, context: ContextSnapshot) -> None:
        """Reject an explicitly bound context from another airframe.

        Context documents from the original v1 contract may omit the optional
        profile hash, so absence remains backwards compatible.  Whenever a
        producer supplies the hash, however, the policy artifact is the
        authority for the shared SIM/HIL/REAL airframe identity and a mismatch
        must fail closed before feature extraction or selection.
        """

        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if (
            context.airframe_profile_hash is not None
            and context.airframe_profile_hash != self.artifact.airframe_profile_hash
        ):
            raise ValueError("policy context airframe profile mismatch")

    @property
    def artifact_sha256(self) -> str:
        return self.artifact.canonical_hash

    def observe(
        self,
        candidates: tuple[TaskCandidate, ...] | list[TaskCandidate],
        context: ContextSnapshot,
        vehicle_class: str,
    ) -> PolicyObservation:
        self._validate_context_profile(context)
        base = build_policy_observation(candidates, context, vehicle_class)
        return self._observation_from_base(base)

    def _observation_from_base(self, base: dict[str, Any]) -> PolicyObservation:
        payload = {
            "schema_version": 1,
            "contract": POLICY_OBSERVATION_CONTRACT,
            "base_observation": base,
            "policy_artifact_sha256": self.artifact_sha256,
            "truth_free": True,
        }
        return PolicyObservation(payload)

    def decide(
        self,
        candidates: tuple[TaskCandidate, ...] | list[TaskCandidate],
        context: ContextSnapshot,
        vehicle_class: str,
    ) -> tuple[PolicyDecision, EvaluatedTask | None]:
        self._validate_context_profile(context)
        entries = tuple(candidates)
        base_observation = build_policy_observation(entries, context, vehicle_class)
        observation = self._observation_from_base(base_observation)
        if not base_observation["feasible"]:
            return (
                PolicyDecision(
                    PolicyAction.ABSTAIN,
                    observation.canonical_hash,
                    self.artifact_sha256,
                    None,
                    "no_feasible_task",
                    True,
                ),
                None,
            )
        selected = self.ranker.select_from_observation(
            base_observation, entries, context, vehicle_class
        )
        if selected is None:
            return (
                PolicyDecision(
                    PolicyAction.ABSTAIN,
                    observation.canonical_hash,
                    self.artifact_sha256,
                    None,
                    "policy_abstained",
                    True,
                ),
                None,
            )
        # Recompute and bind the legacy action through the unchanged resolver.
        resolved = resolve_policy_action(
            TaskPolicyAction(
                canonical_sha256(base_observation),
                selected.candidate.task.task_id,
            ),
            entries,
            context,
            vehicle_class,
        )
        if resolved is None:
            raise ValueError("ranker selected task was rejected during policy revalidation")
        return (
            PolicyDecision(
                PolicyAction.SELECT,
                observation.canonical_hash,
                self.artifact_sha256,
                resolved.candidate.task.task_id,
                "selected_feasible_task",
                True,
            ),
            resolved,
        )



__all__ = [
    "POLICY_DECISION_CONTRACT",
    "POLICY_OBSERVATION_CONTRACT",
    "PolicyAction",
    "PolicyDecision",
    "PolicyObservation",
    "SharedTaskPolicy",
    "policy_decision_matches_selected_task",
]

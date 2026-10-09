"""Bounded exact reference for the existing return-home budget model.

This module is an offline diagnostic, not a deployed policy or dynamics
simulator. Every edge reuses the existing task selector, Mission compiler and
budget episode. The reference is intentionally kept outside the
``SharedTaskPolicy``/``PolicyArtifact`` contract until an independent,
objective-matched gain justifies a deployment design.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .contracts import ContextSnapshot, PlanAction
from .policy_contract import SharedTaskPolicy
from .serialization import require_sha256
from .task_selection import TaskCandidate, select_next_task

SEQUENCE_REFERENCE_OBJECTIVE_ID = (
    "sum_priority_plus_one_then_count_then_minus_modeled_time_v1"
)


@dataclass(frozen=True, slots=True)
class SequenceReferenceResult:
    """Immutable, serializable result of one complete bounded search."""

    sequence: tuple[str, ...]
    reward: int
    completed_count: int
    estimated_elapsed_s: float
    expanded_nodes: int
    compiled_edges: int
    rejected_edges: int
    search_wall_s: float
    exhaustive: bool
    pareto_frontier: tuple[dict[str, object], ...]
    max_nodes: int
    max_tasks: int
    objective_id: str = SEQUENCE_REFERENCE_OBJECTIVE_ID
    deployed: bool = False
    physical_optimality_claimed: bool = False
    independent_holdout_claimed: bool = False

    def to_mapping(self) -> dict[str, object]:
        return {
            "sequence": list(self.sequence),
            "reward": self.reward,
            "completed_count": self.completed_count,
            "estimated_elapsed_s": self.estimated_elapsed_s,
            "nodes": self.expanded_nodes,
            "compiled_edges": self.compiled_edges,
            "rejected_edges": self.rejected_edges,
            "search_wall_s": self.search_wall_s,
            "exhaustive": self.exhaustive,
            "max_nodes_per_case": self.max_nodes,
            "max_tasks_per_case": self.max_tasks,
            "objective_id": self.objective_id,
            "pareto_frontier": [dict(item) for item in self.pareto_frontier],
            "deployed": self.deployed,
            "physical_optimality_claimed": self.physical_optimality_claimed,
            "independent_holdout_claimed": self.independent_holdout_claimed,
        }


@dataclass(frozen=True, slots=True)
class SequenceProposal:
    """A bounded, truth-free sequence suggestion for offline evaluation.

    The proposal compares the currently deployed one-step policy with the
    exact bounded reference.  It deliberately remains an advisor artifact:
    it does not authorize Mission upload/start, arm/disarm, or actuator I/O.
    """

    policy_artifact_sha256: str
    ranker_sha256: str
    context_sha256: str
    vehicle_class: str
    airframe_profile_hash: str
    baseline_sequence: tuple[str, ...]
    proposed_sequence: tuple[str, ...]
    baseline_reward: int
    proposed_reward: int
    baseline_elapsed_s: float
    proposed_elapsed_s: float
    reference: SequenceReferenceResult
    baseline_compiled_edges: int

    def __post_init__(self) -> None:
        """Reject hand-built evidence that cannot satisfy the audit contract.

        A proposal is only reportable when the exact reference really completed
        and the baseline compiled one edge for every selected task.  The
        deployment/release flags below are intentionally derived properties,
        not caller-controlled dataclass fields.
        """

        if not isinstance(self.reference, SequenceReferenceResult):
            raise TypeError("reference must be a SequenceReferenceResult")
        if not self.reference.exhaustive:
            raise ValueError("sequence proposal requires an exhaustive reference")
        if self.proposed_sequence != self.reference.sequence:
            raise ValueError("proposed sequence must match the reference result")
        if self.proposed_reward != self.reference.reward:
            raise ValueError("proposed reward must match the reference result")
        if self.proposed_elapsed_s != self.reference.estimated_elapsed_s:
            raise ValueError("proposed elapsed time must match the reference result")
        if self.baseline_compiled_edges < 0:
            raise ValueError("baseline_compiled_edges must be non-negative")

    @property
    def planner_revalidated(self) -> bool:
        return self.reference.exhaustive and (
            self.baseline_compiled_edges == len(self.baseline_sequence)
        )

    @property
    def truth_free(self) -> bool:
        # This artifact accepts only ContextSnapshot/TaskCandidate inputs; no
        # simulator truth object is part of the proposal contract.
        return True

    @property
    def policy_applied_to_actuator(self) -> bool:
        return False

    @property
    def deployed(self) -> bool:
        return False

    @property
    def candidate_promoted(self) -> bool:
        return False

    @property
    def final_release_allowed(self) -> bool:
        return False

    @property
    def first_task_id(self) -> str | None:
        return self.proposed_sequence[0] if self.proposed_sequence else None

    @property
    def reward_delta(self) -> int:
        return self.proposed_reward - self.baseline_reward

    @property
    def baseline_completed_count(self) -> int:
        return len(self.baseline_sequence)

    @property
    def proposed_completed_count(self) -> int:
        return self.reference.completed_count

    @property
    def objective_id(self) -> str:
        return self.reference.objective_id

    @property
    def baseline_objective(self) -> tuple[int, int, float]:
        return (self.baseline_reward, self.baseline_completed_count, -self.baseline_elapsed_s)

    @property
    def proposed_objective(self) -> tuple[int, int, float]:
        return (
            self.proposed_reward,
            self.proposed_completed_count,
            -self.proposed_elapsed_s,
        )

    @property
    def objective_relation(self) -> str:
        if self.proposed_objective > self.baseline_objective:
            return "improved"
        if self.proposed_objective < self.baseline_objective:
            return "regressed"
        return "tie"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "contract": "multidetect.sequence-proposal",
            "proposal_source": "bounded_exact_reference_v1",
            "policy_artifact_sha256": self.policy_artifact_sha256,
            "ranker_sha256": self.ranker_sha256,
            "context_sha256": self.context_sha256,
            "vehicle_class": self.vehicle_class,
            "airframe_profile_hash": self.airframe_profile_hash,
            "baseline_sequence": list(self.baseline_sequence),
            "proposed_sequence": list(self.proposed_sequence),
            "first_task_id": self.first_task_id,
            "baseline_reward": self.baseline_reward,
            "proposed_reward": self.proposed_reward,
            "reward_delta": self.reward_delta,
            "baseline_elapsed_s": self.baseline_elapsed_s,
            "proposed_elapsed_s": self.proposed_elapsed_s,
            "baseline_completed_count": self.baseline_completed_count,
            "proposed_completed_count": self.proposed_completed_count,
            "baseline_objective": list(self.baseline_objective),
            "proposed_objective": list(self.proposed_objective),
            "objective_id": self.objective_id,
            "objective_relation": self.objective_relation,
            "baseline_compiled_edges": self.baseline_compiled_edges,
            "reference": self.reference.to_mapping(),
            "planner_revalidated": self.planner_revalidated,
            "truth_free": self.truth_free,
            "policy_applied_to_actuator": self.policy_applied_to_actuator,
            "deployed": self.deployed,
            "candidate_promoted": self.candidate_promoted,
            "final_release_allowed": self.final_release_allowed,
        }


def exact_sequence_reference(
    candidates: Sequence[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtimes: Mapping[str, object],
    airframe_profile_hash: str,
    max_nodes: int = 10_000,
    max_tasks: int = 6,
) -> SequenceReferenceResult:
    """Exhaustively enumerate every feasible task prefix.

    ``max_tasks`` is a hard input bound, not a truncation setting. Inputs
    larger than the bound are rejected so a partial search can never be
    mislabeled as an optimum. ``max_nodes`` likewise raises before returning
    a result when the complete reference cannot be established.
    """

    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("positive integer max_nodes required")
    if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or not 1 <= max_tasks <= 6:
        raise ValueError("max_tasks must be an integer in [1, 6]")
    if not isinstance(context, ContextSnapshot):
        raise TypeError("context must be a ContextSnapshot")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
        raise TypeError("candidates must be a sequence")
    entries = tuple(candidates)
    if not 1 <= len(entries) <= max_tasks:
        raise ValueError(f"exact reference requires 1..{max_tasks} tasks")
    if not isinstance(runtimes, Mapping):
        raise TypeError("runtimes must be a mapping")
    airframe_profile_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    if (
        context.airframe_profile_hash is not None
        and context.airframe_profile_hash != airframe_profile_hash
    ):
        raise ValueError("context airframe profile mismatch")
    select_next_task(entries, context, vehicle_class)
    for entry in entries:
        if entry.task.completion_action is not PlanAction.RETURN:
            raise ValueError("sequence reference requires RETURN completion")
        if entry.task.task_id not in runtimes:
            raise ValueError(f"missing compile runtime for {entry.task.task_id}")

    # Import lazily to avoid making the autonomy contract depend on the
    # simulation package at module initialization time.
    from ..simulation.task_episode import simulate_task_budget_episode

    nodes = 0
    edges = 0
    rejected_edges = 0
    best: tuple[int, int, float, tuple[str, ...]] = (0, 0, 0.0, ())
    outcomes: dict[tuple[int, int], dict[str, object]] = {}
    start = perf_counter()

    def better(candidate: tuple[int, int, float, tuple[str, ...]], incumbent):
        left = (candidate[0], candidate[1], -candidate[2])
        right = (incumbent[0], incumbent[1], -incumbent[2])
        if left != right:
            return left > right
        return candidate[3] < incumbent[3]

    def visit(
        pending: tuple[TaskCandidate, ...],
        current: ContextSnapshot,
        sequence: tuple[str, ...],
        reward: int,
        elapsed: float,
    ) -> None:
        nonlocal best, nodes, edges, rejected_edges
        nodes += 1
        if nodes > max_nodes:
            raise ValueError("node limit exceeded; optimum not established")
        score = (reward, len(sequence), elapsed, sequence)
        key = (reward, len(sequence))
        previous = outcomes.get(key)
        if previous is None or elapsed < previous["estimated_elapsed_s"]:
            outcomes[key] = {
                "sequence": list(sequence),
                "reward": reward,
                "completed_count": len(sequence),
                "estimated_elapsed_s": elapsed,
            }
        if better(score, best):
            best = score
        if not pending:
            return
        decision = select_next_task(pending, current, vehicle_class)
        rejected_edges += len(decision.rejected)
        for item in sorted(decision.ranked, key=lambda value: value.candidate.task.task_id):
            candidate = item.candidate
            task_id = candidate.task.task_id
            episode = simulate_task_budget_episode(
                (candidate,),
                current,
                vehicle_class,
                runtimes=runtimes,
                airframe_profile_hash=airframe_profile_hash,
            )
            if len(episode.steps) != 1:
                raise ValueError("feasible edge did not compile one task")
            edges += 1
            step = episode.steps[0]
            if step.compilation.plan != item.plan:
                raise ValueError("sequence edge compilation changed the selected plan")
            visit(
                tuple(entry for entry in pending if entry.task.task_id != task_id),
                step.after,
                sequence + (task_id,),
                reward + 1 + candidate.priority,
                elapsed + step.compilation.estimate.estimated_duration_s,
            )

    visit(entries, context, (), 0, 0.0)
    points = list(outcomes.values())
    frontier = [
        point
        for point in points
        if not any(
            other["reward"] >= point["reward"]
            and other["completed_count"] >= point["completed_count"]
            and other["estimated_elapsed_s"] <= point["estimated_elapsed_s"]
            and (
                other["reward"] > point["reward"]
                or other["completed_count"] > point["completed_count"]
                or other["estimated_elapsed_s"] < point["estimated_elapsed_s"]
            )
            for other in points
        )
    ]
    return SequenceReferenceResult(
        sequence=best[3],
        reward=best[0],
        completed_count=best[1],
        estimated_elapsed_s=best[2],
        expanded_nodes=nodes,
        compiled_edges=edges,
        rejected_edges=rejected_edges,
        search_wall_s=perf_counter() - start,
        exhaustive=True,
        pareto_frontier=tuple(
            sorted(
                frontier,
                key=lambda point: (
                    point["estimated_elapsed_s"],
                    -point["reward"],
                    -point["completed_count"],
                ),
            )
        ),
        max_nodes=max_nodes,
        max_tasks=max_tasks,
    )


def build_sequence_proposal(
    shared_policy: SharedTaskPolicy,
    candidates: Sequence[TaskCandidate],
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtimes: Mapping[str, object],
    airframe_profile_hash: str,
    max_nodes: int = 10_000,
    max_tasks: int = 6,
) -> SequenceProposal:
    """Compare the shared policy with the bounded exact reference.

    This helper is intentionally advisory.  It executes the same deterministic
    budget episode and compiler used by the shared policy, then computes an
    independently exhaustive reference over the same truth-free inputs.  No
    result from this function is an execution authorization.
    """

    if not isinstance(shared_policy, SharedTaskPolicy):
        raise TypeError("shared_policy must be a SharedTaskPolicy")
    if not isinstance(context, ContextSnapshot):
        raise TypeError("context must be a ContextSnapshot")
    airframe_profile_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    if shared_policy.artifact.airframe_profile_hash != airframe_profile_hash:
        raise ValueError("shared policy airframe profile mismatch")
    entries = tuple(candidates)

    reference = exact_sequence_reference(
        entries,
        context,
        vehicle_class,
        runtimes=runtimes,
        airframe_profile_hash=airframe_profile_hash,
        max_nodes=max_nodes,
        max_tasks=max_tasks,
    )

    from ..simulation.task_episode import simulate_task_budget_episode

    baseline = simulate_task_budget_episode(
        entries,
        context,
        vehicle_class,
        runtimes=runtimes,
        airframe_profile_hash=airframe_profile_hash,
        shared_policy=shared_policy,
    )
    if baseline.policy_artifact_sha256 != shared_policy.artifact_sha256:
        raise ValueError("baseline episode lost shared policy identity")
    priorities = {entry.task.task_id: entry.priority for entry in entries}
    baseline_sequence = tuple(
        step.compilation.estimate.task_id for step in baseline.steps
    )
    baseline_reward = sum(1 + priorities[task_id] for task_id in baseline_sequence)

    return SequenceProposal(
        policy_artifact_sha256=shared_policy.artifact_sha256,
        ranker_sha256=shared_policy.ranker.canonical_hash,
        context_sha256=context.canonical_hash,
        vehicle_class=vehicle_class,
        airframe_profile_hash=airframe_profile_hash,
        baseline_sequence=baseline_sequence,
        proposed_sequence=reference.sequence,
        baseline_reward=baseline_reward,
        proposed_reward=reference.reward,
        baseline_elapsed_s=sum(
            step.compilation.estimate.estimated_duration_s for step in baseline.steps
        ),
        proposed_elapsed_s=reference.estimated_elapsed_s,
        reference=reference,
        baseline_compiled_edges=len(baseline.steps),
    )


__all__ = [
    "SEQUENCE_REFERENCE_OBJECTIVE_ID",
    "SequenceProposal",
    "SequenceReferenceResult",
    "build_sequence_proposal",
    "exact_sequence_reference",
]

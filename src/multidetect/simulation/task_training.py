"""Deterministic bounded parameter fitting for the existing TaskRanker extension.

Objectives use planning-budget episodes, not observed flight performance.
Only training cases participate in selection; held-out evaluation is separate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ..autonomy.contracts import ContextSnapshot
from ..autonomy.planner import normalize_vehicle_class
from ..autonomy.policy_artifact import PolicyArtifact
from ..autonomy.policy_contract import SharedTaskPolicy
from ..autonomy.serialization import canonical_sha256, require_sha256, require_text
from ..autonomy.task_ranker import TaskRanker
from ..autonomy.task_selection import TaskCandidate
from .task_episode import simulate_task_budget_episode


@dataclass(frozen=True)
class TaskTrainingCase:
    case_id: str
    context: ContextSnapshot
    vehicle_class: str
    candidates: tuple[TaskCandidate, ...]
    runtimes: Mapping[str, object]
    airframe_profile_hash: str

    def __post_init__(self):
        require_text(self.case_id, "case_id")
        normalize_vehicle_class(self.vehicle_class)
        require_sha256(self.airframe_profile_hash, "airframe_profile_hash")
        if not isinstance(self.context, ContextSnapshot):
            raise ValueError("ContextSnapshot required")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or any(not isinstance(c, TaskCandidate) for c in self.candidates)
        ):
            raise ValueError("nonempty immutable TaskCandidate tuple required")

    @property
    def input_hash(self):
        return canonical_sha256(
            {
                "context": self.context.to_mapping(),
                "vehicle_class": normalize_vehicle_class(self.vehicle_class),
                "candidates": [
                    {"task": c.task.to_mapping(), "priority": c.priority}
                    for c in sorted(self.candidates, key=lambda c: c.task.task_id)
                ],
                "runtimes": dict(self.runtimes),
                "airframe_profile_hash": self.airframe_profile_hash,
            }
        )


def evaluate_task_ranker(
    cases: Iterable[TaskTrainingCase],
    ranker: TaskRanker,
    *,
    forbidden_input_hashes=(),
    policy_artifact: PolicyArtifact | None = None,
) -> dict:
    """Report score components; exact training input reuse can be forbidden.

    This does not prove semantic scenario independence: dataset construction
    must also separate related geometries and perturbation families.
    """
    entries = tuple(cases)
    if not entries or len(entries) > 512 or not isinstance(ranker, TaskRanker):
        raise ValueError("1..512 cases and TaskRanker required")
    if policy_artifact is not None:
        if not isinstance(policy_artifact, PolicyArtifact):
            raise ValueError("PolicyArtifact required")
        if policy_artifact.ranker_sha256 != ranker.canonical_hash:
            raise ValueError("policy artifact does not bind ranker")
        if any(
            case.airframe_profile_hash != policy_artifact.airframe_profile_hash
            for case in entries
        ):
            raise ValueError("policy artifact airframe does not bind evaluation cases")
    if len({case.case_id for case in entries}) != len(entries):
        raise ValueError("duplicate case ID")
    identities = [case.input_hash for case in entries]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate case inputs")
    if set(identities).intersection(forbidden_input_hashes):
        raise ValueError("evaluation overlaps training inputs")
    rows = []
    for case, identity in zip(entries, identities, strict=True):
        episode_kwargs = dict(
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
        )
        if policy_artifact is None:
            episode_kwargs["ranker"] = ranker
        else:
            episode_kwargs["shared_policy"] = SharedTaskPolicy(ranker, policy_artifact)
        episode = simulate_task_budget_episode(
            case.candidates, case.context, case.vehicle_class, **episode_kwargs
        )
        priorities = {c.task.task_id: c.priority for c in case.candidates}
        completed = [step.compilation.estimate.task_id for step in episode.steps]
        rows.append(
            {
                "case_id": case.case_id,
                "input_hash": identity,
                "completed": completed,
                "reward": sum(1 + priorities[task_id] for task_id in completed),
                "estimated_elapsed_s": sum(
                    s.compilation.estimate.estimated_duration_s for s in episode.steps
                ),
                "remaining_duration_s": episode.final_context.estimated_flight_time_s,
                "rejected": [
                    {"task_id": item.task_id, "reason": item.reason} for item in episode.rejected
                ],
                "policy_artifact_sha256": (
                    policy_artifact.canonical_hash if policy_artifact is not None else None
                ),
            }
        )
    return {
        "ranker_sha256": ranker.canonical_hash,
        "policy_artifact_sha256": (
            policy_artifact.canonical_hash if policy_artifact is not None else None
        ),
        "cases": rows,
        "reward": sum(row["reward"] for row in rows),
        "completed_count": sum(len(row["completed"]) for row in rows),
        "estimated_elapsed_s": sum(row["estimated_elapsed_s"] for row in rows),
        "dynamics_or_physical_performance_claimed": False,
    }


def fit_task_ranker(
    training_cases: Iterable[TaskTrainingCase],
    parent: TaskRanker,
    coefficient_candidates: Iterable[tuple[float, float, float]],
    *,
    policy_artifact_factory: Callable[[TaskRanker], PolicyArtifact] | None = None,
) -> tuple[TaskRanker, dict]:
    """Evaluate a bounded search from an explicit parent; retain parent on ties.

    Lexicographic objective: sum(priority+1), completed count, then less modeled
    time. No safety violation is traded for reward; planner/compiler failures
    are never hidden as successful episodes. No held-out cases enter this API.
    """
    cases = tuple(training_cases)
    coefficients = tuple(coefficient_candidates)
    if not isinstance(parent, TaskRanker) or not 1 <= len(coefficients) <= 256:
        raise ValueError("explicit parent and 1..256 candidate coefficient tuples required")
    models = [parent] + [
        TaskRanker(values, f"budget-fit parent={parent.canonical_hash} trial={i}")
        for i, values in enumerate(coefficients)
    ]
    evaluations = [
        evaluate_task_ranker(
            cases,
            model,
            policy_artifact=(policy_artifact_factory(model) if policy_artifact_factory else None),
        )
        for model in models
    ]
    winner = max(
        range(len(models)),
        key=lambda i: (
            evaluations[i]["reward"],
            evaluations[i]["completed_count"],
            -evaluations[i]["estimated_elapsed_s"],
        ),
    )
    return models[winner], {
        "method": "bounded_parameter_search_v1",
        "parent_sha256": parent.canonical_hash,
        "selected_sha256": models[winner].canonical_hash,
        "selected_trial": winner,
        "training_input_hashes": [case.input_hash for case in cases],
        "evaluations": evaluations,
        "holdout_used_for_selection": False,
        "trained_improvement_claimed": False,
    }

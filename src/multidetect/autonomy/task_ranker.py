"""Data-only optional ranking extension over the shared feasible-task planner.

This is not a perception replacement or a trained checkpoint. Coefficients may
come from a future training run; loading a file does not prove its quality.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContextSnapshot
from .serialization import (
    canonical_sha256,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .task_policy import TaskPolicyAction, build_policy_observation, resolve_policy_action
from .task_selection import EvaluatedTask, TaskCandidate

FEATURES = ("priority", "total_distance_m", "estimated_duration_s")
MAX_ARTIFACT_BYTES = 65536


@dataclass(frozen=True, slots=True)
class TaskRanker:
    coefficients: tuple[float, float, float]
    provenance: str

    def __post_init__(self):
        if not isinstance(self.coefficients, tuple) or len(self.coefficients) != len(FEATURES):
            raise ValueError("three immutable coefficients required")
        object.__setattr__(
            self,
            "coefficients",
            tuple(
                require_real(value, name, minimum=-1e6, maximum=1e6)
                for name, value in zip(FEATURES, self.coefficients, strict=True)
            ),
        )
        object.__setattr__(self, "provenance", require_text(self.provenance, "provenance"))

    def to_mapping(self) -> dict:
        return {
            "schema_version": 1,
            "extension": "linear-feasible-task-ranker-v1",
            "base": "shared-planner-baseline-v1",
            "provenance": self.provenance,
            "coefficients": dict(zip(FEATURES, self.coefficients, strict=True)),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(cls, value: object) -> TaskRanker:
        data = strict_object(
            value,
            name="task ranker",
            required=frozenset(
                {
                    "schema_version",
                    "extension",
                    "base",
                    "provenance",
                    "coefficients",
                }
            ),
        )
        require_schema_version(data["schema_version"])
        if (
            data["extension"] != "linear-feasible-task-ranker-v1"
            or data["base"] != "shared-planner-baseline-v1"
        ):
            raise ValueError("unsupported ranker extension or base")
        coefficients = strict_object(
            data["coefficients"], name="coefficients", required=frozenset(FEATURES)
        )
        return cls(tuple(coefficients[name] for name in FEATURES), data["provenance"])

    def select(
        self, candidates: Iterable[TaskCandidate], context: ContextSnapshot, vehicle_class: str
    ) -> EvaluatedTask | None:
        entries = tuple(candidates)
        observation = build_policy_observation(entries, context, vehicle_class)
        return self.select_from_observation(observation, entries, context, vehicle_class)

    def select_from_observation(
        self,
        observation: dict,
        candidates: Iterable[TaskCandidate],
        context: ContextSnapshot,
        vehicle_class: str,
    ) -> EvaluatedTask | None:
        """Rank one already-built truth-free observation, then revalidate it."""
        entries = tuple(candidates)
        chosen, best_score = None, None
        for item in observation["feasible"]:
            score = require_real(
                sum(
                    weight * require_real(item[name], name)
                    for name, weight in zip(FEATURES, self.coefficients, strict=True)
                ),
                "score",
            )
            # Strict greater-than preserves the baseline's deterministic tie order.
            if best_score is None or score > best_score:
                chosen, best_score = item["task_id"], score
        action = TaskPolicyAction(canonical_sha256(observation), chosen)
        return resolve_policy_action(action, entries, context, vehicle_class)



def load_task_ranker(path: Path, *, expected_sha256: str) -> TaskRanker:
    """Require independently supplied byte identity; parse no executable formats."""
    expected = require_sha256(expected_sha256, "expected_sha256")
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("ranker artifact exceeds size limit")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("ranker artifact hash mismatch")
    return TaskRanker.from_mapping(strict_json_loads(raw.decode("utf-8")))

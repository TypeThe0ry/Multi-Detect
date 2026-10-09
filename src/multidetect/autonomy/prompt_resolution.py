"""Fail-closed Prompt-to-task resolution for the shared OnboardAI path.

The resolver is intentionally a catalog, not a free-form language model and
not a flight controller.  MultiQGC (or a future operator-facing parser) must
turn a PromptEnvelope into an approved catalog key before the shared planner
is allowed to compile a Mission.  Gazebo and Jetson can therefore consume the
same immutable task template without putting arbitrary text in the realtime
flight loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .contracts import PromptEnvelope
from .serialization import (
    canonical_json,
    canonical_sha256,
    require_sha256,
    require_text,
    strict_json_loads,
)
from .task_selection import TaskCandidate


class PromptResolutionError(ValueError):
    """The prompt is not bound to an approved task template."""


def _freeze_json(value: object) -> object:
    """Return a recursively immutable JSON snapshot for a runtime document."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _thaw_json(value: object) -> object:
    """Normalize an existing frozen snapshot before canonical validation."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(nested) for nested in value]
    return value


def _freeze_runtime(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PromptResolutionError("template runtimes must contain JSON objects")
    # canonical_json validates finite numbers, string keys, and JSON-only
    # values before the snapshot is frozen.  A round trip also rejects custom
    # Mapping/Sequence implementations that could change semantics later.
    normalized = strict_json_loads(
        canonical_json(_thaw_json(value)), name="Prompt runtime"
    )
    if not isinstance(normalized, Mapping):  # pragma: no cover - guarded above
        raise PromptResolutionError("template runtime must be a JSON object")
    frozen = _freeze_json(normalized)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise PromptResolutionError("template runtime must be a JSON object")
    return frozen


def runtime_sha256(value: object) -> str:
    """Hash a JSON runtime whether it is mutable input or a frozen snapshot."""

    if not isinstance(value, Mapping):
        raise PromptResolutionError("runtime must be a JSON object")
    return canonical_sha256(_thaw_json(value))


@dataclass(frozen=True, slots=True)
class PromptTaskTemplate:
    """One approved task/runtime bundle referenced by a Prompt ID."""

    candidates: tuple[TaskCandidate, ...]
    runtimes: Mapping[str, object]
    aircraft_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if not candidates or any(not isinstance(item, TaskCandidate) for item in candidates):
            raise PromptResolutionError("template requires a non-empty TaskCandidate sequence")
        ids = tuple(item.task.task_id for item in candidates)
        if len(ids) != len(set(ids)):
            raise PromptResolutionError("template task IDs must be unique")
        if not isinstance(self.runtimes, Mapping):
            raise TypeError("template runtimes must be a mapping")
        runtime_map = {
            task_id: _freeze_runtime(runtime) for task_id, runtime in self.runtimes.items()
        }
        if set(runtime_map) != set(ids):
            raise PromptResolutionError("template runtimes must match candidate task IDs")
        aircraft_ids = tuple(self.aircraft_ids)
        if any(not isinstance(value, str) or not value.strip() for value in aircraft_ids):
            raise PromptResolutionError("template aircraft IDs must be non-empty strings")
        if len(aircraft_ids) != len(set(aircraft_ids)):
            raise PromptResolutionError("template aircraft IDs must be unique")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "runtimes", MappingProxyType(runtime_map))
        object.__setattr__(self, "aircraft_ids", aircraft_ids)


@dataclass(frozen=True, slots=True)
class PromptTaskResolution:
    """A hash-bound resolution consumed by OnboardAIExecutor."""

    prompt_sha256: str
    resolver_id: str
    candidates: tuple[TaskCandidate, ...]
    runtimes: Mapping[str, object]
    runtime_hashes: Mapping[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "prompt_sha256", require_sha256(self.prompt_sha256, "prompt_sha256")
        )
        object.__setattr__(self, "resolver_id", require_text(self.resolver_id, "resolver_id"))
        template = PromptTaskTemplate(self.candidates, self.runtimes)
        object.__setattr__(self, "candidates", template.candidates)
        object.__setattr__(self, "runtimes", template.runtimes)
        object.__setattr__(
            self,
            "runtime_hashes",
            MappingProxyType(
                {
                    task_id: runtime_sha256(runtime)
                    for task_id, runtime in template.runtimes.items()
                }
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.prompt-task-resolution",
            "prompt_sha256": self.prompt_sha256,
            "resolver_id": self.resolver_id,
            "tasks": [
                {
                    "task_id": candidate.task.task_id,
                    "task_sha256": candidate.task.canonical_hash,
                    "priority": candidate.priority,
                    "runtime_sha256": self.runtime_hashes[candidate.task.task_id],
                }
                for candidate in self.candidates
            ],
            "truth_free": True,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


class PromptTaskResolver:
    """Resolve only explicitly registered Prompt IDs; unknown prompts abstain."""

    __slots__ = ("resolver_id", "_templates")

    def __init__(
        self,
        resolver_id: str,
        templates: Mapping[str, PromptTaskTemplate],
    ) -> None:
        self.resolver_id = require_text(resolver_id, "resolver_id")
        if not isinstance(templates, Mapping) or not templates:
            raise PromptResolutionError("resolver requires a non-empty template catalog")
        normalized: dict[str, PromptTaskTemplate] = {}
        for prompt_id, template in templates.items():
            if not isinstance(prompt_id, str) or not prompt_id.strip():
                raise PromptResolutionError("template prompt IDs must be non-empty strings")
            if not isinstance(template, PromptTaskTemplate):
                raise TypeError("templates must contain PromptTaskTemplate values")
            normalized[prompt_id] = template
        self._templates = MappingProxyType(normalized)

    def resolve(self, prompt: PromptEnvelope) -> PromptTaskResolution:
        if not isinstance(prompt, PromptEnvelope):
            raise TypeError("prompt must be a PromptEnvelope")
        template = self._templates.get(prompt.prompt_id)
        if template is None:
            raise PromptResolutionError(f"prompt is not registered: {prompt.prompt_id}")
        if template.aircraft_ids and prompt.aircraft_id not in template.aircraft_ids:
            raise PromptResolutionError("prompt aircraft is not approved for this template")
        return PromptTaskResolution(
            prompt_sha256=prompt.canonical_hash,
            resolver_id=self.resolver_id,
            candidates=template.candidates,
            runtimes=template.runtimes,
        )


__all__ = [
    "PromptResolutionError",
    "PromptTaskResolver",
    "PromptTaskResolution",
    "PromptTaskTemplate",
    "runtime_sha256",
]

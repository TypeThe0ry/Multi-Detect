"""Frozen, hashable contracts for the onboard policy v2 boundary.

The module deliberately has no dependency on a policy implementation.  It is
the small wire contract shared by replay, SIM and the future live adapter.
Unknown fields and non-finite numbers are rejected before an object can be
hashed.  Values that are not observed are represented by zero together with a
validity mask; zero is never used as an implicit measurement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    strict_json_loads,
    strict_object,
)

PolicyContractError = AutonomyContractError
"""Typed error alias used by the policy-v2 boundary."""


def _finite(
    value: object, name: str, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyContractError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise PolicyContractError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise PolicyContractError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise PolicyContractError(f"{name} must be at most {maximum}")
    return result


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyContractError(f"{name} must be a non-empty string")
    return value.strip()


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise PolicyContractError(f"{name} must be a SHA-256 hex string")
    return text


def _json_safe(value: object, name: str = "document") -> Any:
    """Copy a JSON value while rejecting NaN/Infinity and non-string keys."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise PolicyContractError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PolicyContractError(f"{name} keys must be strings")
            result[key] = _json_safe(item, f"{name}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, f"{name}[{index}]") for index, item in enumerate(value)]
    raise PolicyContractError(f"{name} contains a non-JSON value")


class _Document:
    def to_mapping(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def sha256(self) -> str:
        return self.canonical_hash

    def to_json(self) -> str:
        return self.canonical_json


@dataclass(frozen=True, slots=True)
class TaskSpecV1(_Document):
    prompt_sha256: str
    intent: str = "search_and_report"
    target_class: str | None = None
    target_color: str | None = None
    attributes: tuple[str, ...] = ()
    relative: str = "nearest"
    search_frame: str = "local_ned"
    search_radius_m: float | None = None
    record_location: bool = True
    return_home: bool = True
    confidence: float = 0.0
    unknown_fields: tuple[str, ...] = ()
    resolver_id: str = "unknown"

    schema: str = field(default="multidetect.task-spec-v1", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt_sha256", _sha(self.prompt_sha256, "prompt_sha256"))
        object.__setattr__(self, "intent", _text(self.intent, "intent"))
        if self.intent != "search_and_report":
            raise PolicyContractError("unsupported TaskSpec intent")
        for name in ("target_class", "target_color"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        for name in ("attributes", "unknown_fields"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise PolicyContractError(f"{name} must contain non-empty strings")
            object.__setattr__(self, name, tuple(v.strip() for v in values))
        if not isinstance(self.relative, str) or self.relative not in {"nearest", "any", "unknown"}:
            raise PolicyContractError("unsupported target.relative")
        if self.search_frame != "local_ned":
            raise PolicyContractError("search_area.frame must be local_ned")
        object.__setattr__(
            self,
            "search_radius_m",
            None
            if self.search_radius_m is None
            else _finite(self.search_radius_m, "search_radius_m", minimum=0.0),
        )
        if type(self.record_location) is not bool or type(self.return_home) is not bool:
            raise PolicyContractError("record_location and return_home must be booleans")
        object.__setattr__(
            self, "confidence", _finite(self.confidence, "confidence", minimum=0.0, maximum=1.0)
        )
        object.__setattr__(self, "resolver_id", _text(self.resolver_id, "resolver_id"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "prompt_sha256": self.prompt_sha256,
            "intent": self.intent,
            "target": {
                "class": self.target_class,
                "color": self.target_color,
                "attributes": list(self.attributes),
                "relative": self.relative,
            },
            "search_area": {"frame": self.search_frame, "radius_m": self.search_radius_m},
            "record_location": self.record_location,
            "return_home": self.return_home,
            "confidence": self.confidence,
            "unknown_fields": list(self.unknown_fields),
            "resolver_id": self.resolver_id,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> TaskSpecV1:
        value = strict_object(
            raw,
            name="TaskSpecV1",
            required=frozenset(
                {
                    "schema",
                    "prompt_sha256",
                    "intent",
                    "target",
                    "search_area",
                    "record_location",
                    "return_home",
                    "confidence",
                    "unknown_fields",
                    "resolver_id",
                }
            ),
        )
        if value["schema"] != "multidetect.task-spec-v1":
            raise PolicyContractError("unsupported TaskSpec schema")
        target = strict_object(
            value["target"],
            name="TaskSpecV1.target",
            required=frozenset({"class", "color", "attributes", "relative"}),
        )
        area = strict_object(
            value["search_area"],
            name="TaskSpecV1.search_area",
            required=frozenset({"frame", "radius_m"}),
        )
        if not isinstance(target["attributes"], (list, tuple)) or any(
            not isinstance(item, str) for item in target["attributes"]
        ):
            raise PolicyContractError("target.attributes must be an array of strings")
        if not isinstance(value["unknown_fields"], (list, tuple)) or any(
            not isinstance(item, str) for item in value["unknown_fields"]
        ):
            raise PolicyContractError("unknown_fields must be an array of strings")
        return cls(
            prompt_sha256=_sha(value["prompt_sha256"], "prompt_sha256"),
            intent=_text(value["intent"], "intent"),
            target_class=None
            if target["class"] is None
            else _text(target["class"], "target.class"),
            target_color=None
            if target["color"] is None
            else _text(target["color"], "target.color"),
            attributes=tuple(target["attributes"]),
            relative=_text(target["relative"], "target.relative"),
            search_frame=_text(area["frame"], "search_area.frame"),
            search_radius_m=area["radius_m"],
            record_location=value["record_location"],
            return_home=value["return_home"],
            confidence=value["confidence"],
            unknown_fields=tuple(value["unknown_fields"]),
            resolver_id=_text(value["resolver_id"], "resolver_id"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> TaskSpecV1:
        try:
            return cls.from_mapping(strict_json_loads(payload, name="TaskSpecV1"))
        except AutonomyContractError as exc:
            raise PolicyContractError(str(exc)) from exc


TaskSpec = TaskSpecV1


_ACTION_BOUNDS = {
    "translate": {"forward_m": (-2.0, 8.0), "right_m": (-6.0, 6.0), "up_m": (-2.0, 2.0)},
    "turn": {"yaw_delta_deg": (-90.0, 90.0)},
    "hold": {"seconds": (0.2, 2.0)},
    "stop": {},
}


@dataclass(frozen=True, slots=True)
class PolicyActionV2(_Document):
    kind: str
    forward_m: float = 0.0
    right_m: float = 0.0
    up_m: float = 0.0
    yaw_delta_deg: float = 0.0
    seconds: float = 0.0
    action_mask: tuple[bool, ...] = (True, True, True, True)
    schema: str = field(default="multidetect.policy-action-v2", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _ACTION_BOUNDS:
            raise PolicyContractError(f"unsupported action kind: {self.kind}")
        for name in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        bounds = _ACTION_BOUNDS[self.kind]
        for name, (minimum, maximum) in bounds.items():
            _finite(getattr(self, name), name, minimum=minimum, maximum=maximum)
        active = set(bounds)
        for name in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds"):
            if name not in active and getattr(self, name) != 0.0:
                raise PolicyContractError(f"{name} must be zero for {self.kind}")
        if (
            not isinstance(self.action_mask, (tuple, list))
            or len(self.action_mask) != 4
            or any(type(v) is not bool for v in self.action_mask)
        ):
            raise PolicyContractError("action_mask must contain four booleans")
        object.__setattr__(self, "action_mask", tuple(self.action_mask))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "forward_m": self.forward_m,
            "right_m": self.right_m,
            "up_m": self.up_m,
            "yaw_delta_deg": self.yaw_delta_deg,
            "seconds": self.seconds,
            "action_mask": list(self.action_mask),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> PolicyActionV2:
        value = strict_object(
            raw,
            name="PolicyActionV2",
            required=frozenset(
                {
                    "schema",
                    "kind",
                    "forward_m",
                    "right_m",
                    "up_m",
                    "yaw_delta_deg",
                    "seconds",
                    "action_mask",
                }
            ),
        )
        if value["schema"] != "multidetect.policy-action-v2":
            raise PolicyContractError("unsupported PolicyAction schema")
        return cls(
            kind=_text(value["kind"], "kind"),
            forward_m=value["forward_m"],
            right_m=value["right_m"],
            up_m=value["up_m"],
            yaw_delta_deg=value["yaw_delta_deg"],
            seconds=value["seconds"],
            action_mask=tuple(value["action_mask"]),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> PolicyActionV2:
        try:
            return cls.from_mapping(strict_json_loads(payload, name="PolicyActionV2"))
        except AutonomyContractError as exc:
            raise PolicyContractError(str(exc)) from exc

    @classmethod
    def from_primitive(cls, primitive: object) -> PolicyActionV2:
        """Adapt the read-only SIM ``ActionPrimitive`` contract."""
        values = {
            name: getattr(primitive, name, 0.0)
            for name in ("kind", "forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")
        }
        return cls(**values)

    def to_primitive(self) -> object:
        from ...sim_teacher.primitives import ActionPrimitive

        return ActionPrimitive(
            self.kind,
            self.forward_m,
            self.right_m,
            self.up_m,
            self.yaw_delta_deg,
            self.seconds,
        )


PolicyAction = PolicyActionV2


@dataclass(frozen=True, slots=True)
class StructuredSceneState(_Document):
    """One truth-free state tick and its fixed 16-tick history."""

    captured_at_s: float
    dt_s: float
    frame_id: str
    task_spec_hash: str
    valid_mask: Mapping[str, bool]
    reset_mask: Mapping[str, bool]
    health: Mapping[str, Any] = field(default_factory=dict)
    pose: Mapping[str, Any] = field(default_factory=dict)
    task: Mapping[str, Any] = field(default_factory=dict)
    tracks: tuple[Mapping[str, Any], ...] = ()
    scene: Mapping[str, Any] = field(default_factory=dict)
    memory: tuple[tuple[tuple[float, ...], ...], ...] = ()
    event: Mapping[str, Any] = field(default_factory=dict)
    history: tuple[Mapping[str, Any], ...] = ()
    truth_free: bool = True
    schema: str = field(default="multidetect.onboard-state-v2", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "captured_at_s", _finite(self.captured_at_s, "captured_at_s", minimum=0.0)
        )
        object.__setattr__(self, "dt_s", _finite(self.dt_s, "dt_s", minimum=0.0))
        object.__setattr__(self, "frame_id", _text(self.frame_id, "frame_id"))
        object.__setattr__(self, "task_spec_hash", _sha(self.task_spec_hash, "task_spec_hash"))
        if type(self.truth_free) is not bool or not self.truth_free:
            raise PolicyContractError("StructuredSceneState must be truth_free")
        for name in ("valid_mask", "reset_mask", "health", "pose", "task", "scene", "event"):
            value = _json_safe(getattr(self, name), name)
            if not isinstance(value, dict):
                raise PolicyContractError(f"{name} must be an object")
            object.__setattr__(self, name, value)
        for key, value in self.valid_mask.items():
            if type(value) is not bool or not isinstance(key, str):
                raise PolicyContractError("valid_mask values must be booleans")
        for key, value in self.reset_mask.items():
            if type(value) is not bool or not isinstance(key, str):
                raise PolicyContractError("reset_mask values must be booleans")
        tracks = tuple(_json_safe(track, "tracks") for track in self.tracks)
        if len(tracks) > 8 or any(not isinstance(track, dict) for track in tracks):
            raise PolicyContractError("tracks must contain at most eight objects")
        object.__setattr__(self, "tracks", tracks)
        history = tuple(_json_safe(item, "history") for item in self.history)
        if len(history) != 16 or any(not isinstance(item, dict) for item in history):
            raise PolicyContractError("history must contain exactly sixteen objects")
        object.__setattr__(self, "history", history)
        memory = _json_safe(self.memory, "memory")
        if not isinstance(memory, (list, tuple)):
            raise PolicyContractError("memory must be a 16x16 grid")
        if len(memory) != 16 or any(len(row) != 16 for row in memory):
            raise PolicyContractError("memory must be a 16x16 grid")
        if any(len(cell) != 5 for row in memory for cell in row):
            raise PolicyContractError("memory cells must contain five channels")
        if any(
            isinstance(channel, bool)
            or not isinstance(channel, (int, float))
            or not math.isfinite(float(channel))
            for row in memory
            for cell in row
            for channel in cell
        ):
            raise PolicyContractError("memory channels must be finite numbers")
        forbidden = {"labels", "truth", "privileged", "ground_truth", "expert"}
        if any(
            key.casefold() in forbidden
            for block in (self.health, self.pose, self.task, self.scene, self.event)
            for key in block
        ):
            raise PolicyContractError("privileged labels cannot enter policy state")
        object.__setattr__(
            self,
            "memory",
            tuple(tuple(tuple(float(v) for v in cell) for cell in row) for row in memory),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "captured_at_s": self.captured_at_s,
            "dt_s": self.dt_s,
            "frame_id": self.frame_id,
            "task_spec_hash": self.task_spec_hash,
            "valid_mask": dict(self.valid_mask),
            "reset_mask": dict(self.reset_mask),
            "truth_free": True,
            "health": dict(self.health),
            "pose": dict(self.pose),
            "task": dict(self.task),
            "tracks": list(self.tracks),
            "scene": dict(self.scene),
            "memory": [[list(cell) for cell in row] for row in self.memory],
            "event": dict(self.event),
            "history": list(self.history),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> StructuredSceneState:
        required = {
            "schema",
            "captured_at_s",
            "dt_s",
            "frame_id",
            "task_spec_hash",
            "valid_mask",
            "reset_mask",
            "truth_free",
            "health",
            "pose",
            "task",
            "tracks",
            "scene",
            "memory",
            "event",
            "history",
        }
        value = strict_object(raw, name="StructuredSceneState", required=frozenset(required))
        if value["schema"] != "multidetect.onboard-state-v2":
            raise PolicyContractError("unsupported state schema")
        return cls(
            captured_at_s=value["captured_at_s"],
            dt_s=value["dt_s"],
            frame_id=value["frame_id"],
            task_spec_hash=value["task_spec_hash"],
            valid_mask=value["valid_mask"],
            reset_mask=value["reset_mask"],
            truth_free=value["truth_free"],
            health=value["health"],
            pose=value["pose"],
            task=value["task"],
            tracks=tuple(value["tracks"]),
            scene=value["scene"],
            memory=tuple(tuple(tuple(cell) for cell in row) for row in value["memory"]),
            event=value["event"],
            history=tuple(value["history"]),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> StructuredSceneState:
        try:
            return cls.from_mapping(strict_json_loads(payload, name="StructuredSceneState"))
        except AutonomyContractError as exc:
            raise PolicyContractError(str(exc)) from exc


__all__ = [
    "PolicyAction",
    "PolicyActionV2",
    "PolicyContractError",
    "StructuredSceneState",
    "TaskSpec",
    "TaskSpecV1",
]

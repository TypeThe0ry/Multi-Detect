"""Small, simulator agnostic closed loop runner for policy-v2.

The SIM collector remains the owner of PX4, Docker, frame acquisition and
teardown.  This module only coordinates one observation/action tick, which is
also useful for unit tests and campaign wrappers.  ``StructuredSceneState`` is
built from detector output; target pose and expert labels are kept in the
recording labels and are never passed to the policy.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...sim_teacher.primitives import ActionPrimitive
from .contracts import PolicyActionV2, PolicyContractError, TaskSpecV1
from .model import states_to_tensors
from .state_builder import StructuredSceneStateBuilder


@dataclass(slots=True)
class RolloutConfig:
    max_steps: int = 200
    standoff_success_m: float = 18.0
    action_mask: tuple[bool, bool, bool, bool] = (True, True, True, True)
    search_bounds: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.standoff_success_m <= 0 or not math.isfinite(self.standoff_success_m):
            raise ValueError("standoff_success_m must be positive and finite")
        if len(self.action_mask) != 4 or any(type(item) is not bool for item in self.action_mask):
            raise ValueError("action_mask must contain four booleans")
        if self.search_bounds is not None:
            if len(self.search_bounds) != 4 or not all(
                math.isfinite(float(value)) for value in self.search_bounds
            ):
                raise ValueError(
                    "search_bounds must be finite (min_north,max_north,min_east,max_east)"
                )
            if (
                self.search_bounds[0] > self.search_bounds[1]
                or self.search_bounds[2] > self.search_bounds[3]
            ):
                raise ValueError("search_bounds minimum exceeds maximum")


@dataclass(slots=True)
class EpisodeMetrics:
    steps: int = 0
    task_success: bool = False
    steps_to_acquire: int | None = None
    path_length_m: float = 0.0
    safety_events: dict[str, int] = field(default_factory=dict)
    policy_latencies_ms: list[float] = field(default_factory=list)
    detector_latencies_ms: list[float] = field(default_factory=list)

    def event(self, name: str) -> None:
        self.safety_events[name] = self.safety_events.get(name, 0) + 1

    @staticmethod
    def percentiles(values: Sequence[float]) -> dict[str, float]:
        if not values:
            return {"p50": 0.0, "p95": 0.0}
        ordered = sorted(float(value) for value in values)

        def pick(q: float) -> float:
            index = min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered)) - 1)))
            return ordered[index]

        return {"p50": pick(0.50), "p95": pick(0.95)}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "task_success": self.task_success,
            "steps_to_acquire": self.steps_to_acquire,
            "path_length_m": self.path_length_m,
            "safety_events": dict(self.safety_events),
            "policy_latency_ms": self.percentiles(self.policy_latencies_ms),
            "detector_latency_ms": self.percentiles(self.detector_latencies_ms),
        }


def _pose(observation: Mapping[str, Any]) -> tuple[float, float, float] | None:
    value: Any = observation.get("pose", observation)
    if isinstance(value, Mapping):
        value = value.get("local_ned_m", value.get("ned", value))
        if isinstance(value, Mapping):
            value = [value.get(name) for name in ("north_m", "east_m", "down_m")]
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) >= 3
    ):
        try:
            numbers = tuple(float(item) for item in value[:3])
        except (TypeError, ValueError):
            return None
        return numbers if all(math.isfinite(item) for item in numbers) else None
    return None


def _yaw(observation: Mapping[str, Any]) -> float:
    attitude = observation.get("attitude", {})
    value = attitude.get("yaw_deg", 0.0) if isinstance(attitude, Mapping) else 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _raw_action(value: Any) -> PolicyActionV2:
    if isinstance(value, PolicyActionV2):
        return value
    if isinstance(value, ActionPrimitive):
        return PolicyActionV2(
            value.kind,
            forward_m=value.forward_m,
            right_m=value.right_m,
            up_m=value.up_m,
            yaw_delta_deg=value.yaw_delta_deg,
            seconds=value.seconds,
        )
    if isinstance(value, Mapping):
        kind = str(value.get("kind", "hold"))
        return PolicyActionV2(
            kind,
            forward_m=value.get("forward_m", 0.0),
            right_m=value.get("right_m", 0.0),
            up_m=value.get("up_m", 0.0),
            yaw_delta_deg=value.get("yaw_delta_deg", 0.0),
            seconds=value.get("seconds", 0.0),
            action_mask=tuple(value.get("action_mask", (True, True, True, True))),
        )
    raise PolicyContractError("policy returned an unsupported action")


def _safe_action(
    value: Any, mask: tuple[bool, bool, bool, bool], metrics: EpisodeMetrics
) -> PolicyActionV2:
    """Validate and bound a policy output, failing closed to HOLD."""
    try:
        if isinstance(value, ActionPrimitive) or isinstance(value, Mapping):
            kind = (
                value.kind if isinstance(value, ActionPrimitive) else str(value.get("kind", "hold"))
            )
            raw = {
                "forward_m": value.forward_m
                if isinstance(value, ActionPrimitive)
                else value.get("forward_m", 0.0),
                "right_m": value.right_m
                if isinstance(value, ActionPrimitive)
                else value.get("right_m", 0.0),
                "up_m": value.up_m
                if isinstance(value, ActionPrimitive)
                else value.get("up_m", 0.0),
                "yaw_delta_deg": value.yaw_delta_deg
                if isinstance(value, ActionPrimitive)
                else value.get("yaw_delta_deg", 0.0),
                "seconds": value.seconds
                if isinstance(value, ActionPrimitive)
                else value.get("seconds", 0.0),
            }
            values = list(raw.values())
            if not all(math.isfinite(float(item)) for item in values):
                raise ValueError("non-finite action")
            if kind not in {"translate", "turn", "hold", "stop"}:
                raise ValueError("unsupported action kind")
            ranges = {
                "translate": {
                    "forward_m": (-2.0, 8.0),
                    "right_m": (-6.0, 6.0),
                    "up_m": (-2.0, 2.0),
                },
                "turn": {"yaw_delta_deg": (-90.0, 90.0)},
                "hold": {"seconds": (0.2, 2.0)},
                "stop": {},
            }[kind]
            for name, (low, high) in ranges.items():
                original = float(raw[name])
                raw[name] = min(high, max(low, original))
                if raw[name] != original:
                    metrics.event("bounds_clipped")
            action = PolicyActionV2(
                kind,
                forward_m=raw["forward_m"] if kind == "translate" else 0.0,
                right_m=raw["right_m"] if kind == "translate" else 0.0,
                up_m=raw["up_m"] if kind == "translate" else 0.0,
                yaw_delta_deg=raw["yaw_delta_deg"] if kind == "turn" else 0.0,
                seconds=raw["seconds"] if kind == "hold" else 0.0,
            )
        else:
            action = _raw_action(value)
        values = [
            action.forward_m,
            action.right_m,
            action.up_m,
            action.yaw_delta_deg,
            action.seconds,
        ]
        if not all(math.isfinite(float(item)) for item in values):
            raise ValueError("non-finite action")
        allowed = tuple(bool(item) for item in mask)
        index = {"translate": 0, "turn": 1, "hold": 2, "stop": 3}[action.kind]
        if not allowed[index]:
            metrics.event("action_mask")
            index = 2 if allowed[2] else next((i for i, item in enumerate(allowed) if item), 2)
            action = (
                PolicyActionV2("translate")
                if index == 0
                else PolicyActionV2("turn")
                if index == 1
                else PolicyActionV2("stop")
                if index == 3
                else PolicyActionV2("hold", seconds=0.2)
            )
        bounds = {
            "translate": ((-2.0, 8.0), (-6.0, 6.0), (-2.0, 2.0)),
            "turn": ((-90.0, 90.0),),
            "hold": ((0.2, 2.0),),
            "stop": (),
        }[action.kind]
        fields = {
            "translate": ("forward_m", "right_m", "up_m"),
            "turn": ("yaw_delta_deg",),
            "hold": ("seconds",),
            "stop": (),
        }[action.kind]
        updates: dict[str, float] = {}
        clipped = False
        for name, (low, high) in zip(fields, bounds, strict=True):
            current = float(getattr(action, name))
            bounded = min(high, max(low, current))
            clipped |= bounded != current
            updates[name] = bounded
        if clipped:
            metrics.event("bounds_clipped")
        return PolicyActionV2(action.kind, action_mask=allowed, **updates)
    except (PolicyContractError, TypeError, ValueError, KeyError, StopIteration) as exc:
        text = f"{value} {exc}".lower()
        metrics.event(
            "nan_hold"
            if any(token in text for token in ("nan", "inf", "non-finite"))
            else "invalid_action"
        )
        return PolicyActionV2("hold", seconds=0.2, action_mask=mask)


def _expert_labels(expert: Any, observation: Mapping[str, Any]) -> dict[str, Any]:
    if expert is None:
        return {}
    pose = _pose(observation)
    if pose is None:
        return {
            "expert_action": None,
            "target_in_fov": False,
            "target_bearing_deg": None,
            "target_range_m": None,
        }
    yaw = _yaw(observation)
    try:
        labels = dict(expert.labels(pose_ned=pose, yaw_deg=yaw))
        would = expert.next_action(pose_ned=pose, yaw_deg=yaw)
        labels["expert_action"] = would.to_dict() if hasattr(would, "to_dict") else dict(would)
        return labels
    except Exception:
        return {
            "expert_action": None,
            "target_in_fov": False,
            "target_bearing_deg": None,
            "target_range_m": None,
        }


def _detect(
    detector: Any, observation: Mapping[str, Any], rgb: Any, depth: Any
) -> Mapping[str, Any]:
    if detector is None:
        return {"detections": []}
    if callable(getattr(detector, "annotate_observation", None)):
        return detector.annotate_observation(observation, rgb, depth)
    if callable(detector):
        result = detector(rgb, depth) if depth is not None else detector(rgb)
        return result if isinstance(result, Mapping) else {"detections": result}
    raise TypeError("detector must expose annotate_observation or be callable")


def _act(policy: Any, state: Any, mask: tuple[bool, bool, bool, bool]) -> Any:
    method = getattr(policy, "act", None) or getattr(policy, "predict", None)
    if not callable(method):
        if callable(policy):
            method = policy
        else:
            raise TypeError("policy must expose act/predict or be callable")
    try:
        return method(state)
    except TypeError:
        state_t, memory_t = states_to_tensors(state)
        return method(state_t, memory_t, mask)


def _outside_search_area(
    builder: StructuredSceneStateBuilder, observation: Mapping[str, Any]
) -> bool:
    """Check the frozen 80 m square around the memory anchor."""
    pose = _pose(observation)
    anchor = builder.memory.anchor
    if pose is None or anchor is None:
        return False
    half = builder.memory.size * builder.memory.cell_size_m / 2.0
    return not (-half <= pose[0] - anchor[0] < half and -half <= pose[1] - anchor[1] < half)


def rollout_episode(
    observations: Iterable[Mapping[str, Any]] | Callable[[], Mapping[str, Any]],
    *,
    detector: Any,
    policy: Any,
    task_spec: TaskSpecV1,
    state_builder: StructuredSceneStateBuilder | None = None,
    executor: Any | None = None,
    expert: Any | None = None,
    recorder: Any | None = None,
    after_observation: Callable[[int, Mapping[str, Any], Any], Mapping[str, Any]] | None = None,
    target_ned: Sequence[float] | None = None,
    config: RolloutConfig | None = None,
) -> tuple[EpisodeMetrics, list[dict[str, Any]]]:
    """Run bounded policy steps against an observation source.

    ``executor`` may be the collector's executor or a fake exposing
    ``execute(ActionPrimitive)``.  An observation source callable is polled for
    each step; an iterable is consumed one item per step.
    """
    cfg = config or RolloutConfig()
    builder = state_builder or StructuredSceneStateBuilder()
    metrics = EpisodeMetrics()
    rows: list[dict[str, Any]] = []
    iterator = iter(observations) if not callable(observations) else None
    previous_pose: tuple[float, float, float] | None = None
    target = tuple(float(item) for item in target_ned[:3]) if target_ned is not None else None
    for index in range(cfg.max_steps):
        before = observations() if iterator is None else next(iterator, None)
        if before is None:
            break
        before = dict(before)
        rgb, depth = before.get("rgb"), before.get("depth")
        started = time.perf_counter()
        perception = _detect(detector, before, rgb, depth)
        metrics.detector_latencies_ms.append((time.perf_counter() - started) * 1000.0)
        record = {"obs_before": before, "perception": perception}
        try:
            state = builder.from_sim_step_record(record, task_spec)
        except Exception:
            state = None
        if _outside_search_area(builder, before):
            metrics.event("out_of_search_area")
        labels = _expert_labels(expert, before)
        started = time.perf_counter()
        try:
            if state is None:
                raise ValueError("state construction failed")
            action_value = _act(policy, state, cfg.action_mask)
        except Exception:
            action_value = PolicyActionV2("hold", seconds=0.2)
        metrics.policy_latencies_ms.append((time.perf_counter() - started) * 1000.0)
        action = _safe_action(action_value, cfg.action_mask, metrics)
        outcome: dict[str, Any] = {}
        if executor is not None:
            try:
                result = executor.execute(action.to_primitive())
                outcome = (
                    result.to_dict()
                    if hasattr(result, "to_dict")
                    else dict(getattr(result, "__dict__", {}))
                )
            except Exception as exc:
                outcome = {"status": "error", "error": str(exc)}
        after = after_observation(index, before, outcome) if after_observation else before
        pose = _pose(after)
        if pose is not None and cfg.search_bounds is not None:
            min_north, max_north, min_east, max_east = cfg.search_bounds
            if not (min_north <= pose[0] <= max_north and min_east <= pose[1] <= max_east):
                metrics.event("out_of_search_area")
        if previous_pose is not None and pose is not None:
            metrics.path_length_m += math.dist(previous_pose[:2], pose[:2])
        previous_pose = pose
        if labels.get("target_in_fov") and metrics.steps_to_acquire is None:
            metrics.steps_to_acquire = index + 1
        if target is not None and pose is not None and action.kind in {"stop", "hold"}:
            metrics.task_success = math.dist(target[:2], pose[:2]) <= cfg.standoff_success_m
        row = {
            "step_index": index,
            "obs_before": before,
            "perception": perception,
            "action": action.to_primitive().to_dict(),
            "obs_after": after,
            "labels": labels,
            "outcome": outcome,
        }
        rows.append(row)
        metrics.steps = index + 1
        if recorder is not None:
            recorder.record_step(
                before,
                action.to_primitive(),
                ack={},
                outcome=outcome,
                obs_after=dict(after),
                labels=labels,
            )
        if action.kind == "stop":
            break
    return metrics, rows


run_rollout = rollout_episode

__all__ = ["EpisodeMetrics", "RolloutConfig", "rollout_episode", "run_rollout"]

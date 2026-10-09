"""Primitive schema and coordinate helpers for SIM teacher collection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ActionPrimitive:
    kind: str
    forward_m: float = 0.0
    right_m: float = 0.0
    up_m: float = 0.0
    yaw_delta_deg: float = 0.0
    seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in {"translate", "turn", "hold", "stop"}:
            raise ValueError(f"unsupported primitive kind: {self.kind}")
        vals = (self.forward_m, self.right_m, self.up_m, self.yaw_delta_deg, self.seconds)
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v))
            for v in vals
        ):
            raise ValueError("primitive values must be finite numbers")
        if self.kind == "turn" and abs(self.yaw_delta_deg) > 360:
            raise ValueError("yaw_delta_deg outside supported range")
        if self.kind == "hold" and self.seconds < 0:
            raise ValueError("hold seconds must be non-negative")

    @classmethod
    def translate(cls, forward_m: float, right_m: float, up_m: float = 0.0) -> ActionPrimitive:
        return cls("translate", forward_m, right_m, up_m)

    @classmethod
    def turn(cls, yaw_delta_deg: float) -> ActionPrimitive:
        return cls("turn", yaw_delta_deg=yaw_delta_deg)

    @classmethod
    def hold(cls, seconds: float) -> ActionPrimitive:
        return cls("hold", seconds=seconds)

    @classmethod
    def stop(cls) -> ActionPrimitive:
        return cls("stop")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "forward_m": self.forward_m,
            "right_m": self.right_m,
            "up_m": self.up_m,
            "yaw_delta_deg": self.yaw_delta_deg,
            "seconds": self.seconds,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ActionPrimitive:
        if not isinstance(value, dict):
            raise TypeError("primitive must be an object")
        return cls(
            kind=str(value["kind"]),
            forward_m=float(value.get("forward_m", 0)),
            right_m=float(value.get("right_m", 0)),
            up_m=float(value.get("up_m", 0)),
            yaw_delta_deg=float(value.get("yaw_delta_deg", 0)),
            seconds=float(value.get("seconds", 0)),
        )


def body_to_ned(
    forward_m: float, right_m: float, up_m: float, yaw_deg: float
) -> tuple[float, float, float]:
    """Convert body-forward/right/up displacement to local NED."""
    yaw = math.radians(yaw_deg)
    return (
        forward_m * math.cos(yaw) - right_m * math.sin(yaw),
        forward_m * math.sin(yaw) + right_m * math.cos(yaw),
        -up_m,
    )


def primitive_to_ned(action: ActionPrimitive, yaw_deg: float) -> tuple[float, float, float, float]:
    n, e, d = body_to_ned(action.forward_m, action.right_m, action.up_m, yaw_deg)
    return n, e, d, action.yaw_delta_deg


@dataclass(frozen=True, slots=True)
class CompletionCriterion:
    position_tolerance_m: float = 0.5
    yaw_tolerance_deg: float = 8.0
    speed_tolerance_mps: float = 0.5
    timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if (
            self.position_tolerance_m <= 0
            or self.yaw_tolerance_deg <= 0
            or self.speed_tolerance_mps < 0
            or self.timeout_s <= 0
        ):
            raise ValueError("invalid completion criterion")

    def check(
        self, *, position_error_m: float, yaw_error_deg: float, speed_mps: float, elapsed_s: float
    ) -> str:
        if elapsed_s >= self.timeout_s:
            return "timeout"
        if (
            position_error_m <= self.position_tolerance_m
            and abs(yaw_error_deg) <= self.yaw_tolerance_deg
            and speed_mps <= self.speed_tolerance_mps
        ):
            return "success"
        return "pending"

    def __call__(self, **kwargs: float) -> str:
        return self.check(**kwargs)

"""Versioned simulation-only fault declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..autonomy.serialization import (
    AutonomyContractError,
    optional_real,
    require_real,
    require_text,
    strict_object,
)
from ..compat import StrEnum


class FaultKind(StrEnum):
    CAMERA_DROPOUT = "camera_dropout"
    BATTERY_STEP_DROP = "battery_step_drop"
    GPS_DROPOUT = "gps_dropout"
    GPS_DRIFT = "gps_drift"
    # ``magnitude`` is a signed metres-per-second radial drift.  The alias
    # keeps the short name convenient for scenario authors while the explicit
    # distance spelling remains available to Python callers.
    TARGET_DRIFT = "target_drift"
    TARGET_DISTANCE_DRIFT = "target_drift"
    # ``magnitude`` is a signed degrees-per-second bearing drift.
    TARGET_BEARING_DRIFT = "target_bearing_drift"
    # ``magnitude`` is a signed seconds offset applied to sensor freshness
    # cursors.  Positive values create a future sample; negative values make
    # the sample stale (or unavailable before t=0).
    TIMESTAMP_SKEW = "timestamp_skew"
    AIRSPEED_FREEZE = "airspeed_freeze"
    WIND_GUST = "wind_gust"
    VTOL_TRANSITION_TIMEOUT = "vtol_transition_timeout"
    AUTONOMY_PROCESS_EXIT = "autonomy_process_exit"


@dataclass(frozen=True, slots=True)
class FaultEvent:
    event_id: str
    at_s: float
    kind: FaultKind
    duration_s: float | None = None
    magnitude: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", require_text(self.event_id, "event_id"))
        object.__setattr__(
            self, "at_s", require_real(self.at_s, "at_s", minimum=0.0)
        )
        if not isinstance(self.kind, FaultKind):
            raise AutonomyContractError("fault kind must be a FaultKind")
        object.__setattr__(
            self,
            "duration_s",
            optional_real(self.duration_s, "duration_s", minimum=0.0),
        )
        object.__setattr__(
            self, "magnitude", optional_real(self.magnitude, "magnitude")
        )

    @classmethod
    def from_mapping(cls, raw: object) -> FaultEvent:
        value = strict_object(
            raw,
            name="FaultEvent",
            required=frozenset({"event_id", "at_s", "kind"}),
            optional=frozenset({"duration_s", "magnitude"}),
        )
        try:
            kind = FaultKind(require_text(value["kind"], "kind"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported simulation fault: {exc}") from exc
        return cls(
            event_id=require_text(value["event_id"], "event_id"),
            at_s=require_real(value["at_s"], "at_s", minimum=0.0),
            kind=kind,
            duration_s=optional_real(value.get("duration_s"), "duration_s", minimum=0.0),
            magnitude=optional_real(value.get("magnitude"), "magnitude"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "at_s": self.at_s,
            "kind": self.kind.value,
        }
        if self.duration_s is not None:
            result["duration_s"] = self.duration_s
        if self.magnitude is not None:
            result["magnitude"] = self.magnitude
        return result


__all__ = ["FaultEvent", "FaultKind"]

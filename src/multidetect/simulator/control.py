"""Fail-closed manual/AI control arbitration for SIM."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from time import monotonic


class ControlAuthorityError(RuntimeError):
    """Raised when a caller attempts an invalid authority transition."""


class ControlAuthorityState(str, Enum):
    MANUAL = "manual"
    AI = "ai"
    PAUSED = "paused"
    FAILSAFE = "failsafe"


@dataclass(frozen=True, slots=True)
class ControlSetpoint:
    """Normalized simulator setpoint; values are SI-like and bounded later."""

    source: str
    roll: float
    pitch: float
    yaw: float
    throttle: float
    timestamp_s: float


class SimulatorControlAuthority:
    """Arbitrate manual and AI control with an explicit human override.

    AI is opt-in.  Once a manual setpoint is received, AI setpoints are
    rejected until :meth:`release_manual_control` is called.  Pause and
    failsafe clear the active setpoint so a stale command cannot continue.
    """

    def __init__(self, *, initial: ControlAuthorityState = ControlAuthorityState.PAUSED) -> None:
        if not isinstance(initial, ControlAuthorityState):
            raise ControlAuthorityError("initial authority must be a ControlAuthorityState")
        self._lock = threading.RLock()
        self._state = initial
        self._ai_enabled = False
        self._manual_override = initial is ControlAuthorityState.MANUAL
        self._last_setpoint: ControlSetpoint | None = None
        self._events: list[dict[str, object]] = []
        self._record("initialized", state=self._state.value)

    @property
    def state(self) -> ControlAuthorityState:
        with self._lock:
            return self._state

    @property
    def ai_enabled(self) -> bool:
        with self._lock:
            return self._ai_enabled

    @property
    def manual_override(self) -> bool:
        with self._lock:
            return self._manual_override

    @property
    def last_setpoint(self) -> ControlSetpoint | None:
        with self._lock:
            return self._last_setpoint

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(self._events)

    def enable_ai(self) -> None:
        with self._lock:
            self._ai_enabled = True
            self._record("ai_enabled")

    def disable_ai(self) -> None:
        with self._lock:
            self._ai_enabled = False
            if self._state is ControlAuthorityState.AI:
                self._state = ControlAuthorityState.PAUSED
                self._last_setpoint = None
            self._record("ai_disabled", state=self._state.value)

    def take_manual_control(self) -> None:
        with self._lock:
            self._manual_override = True
            self._state = ControlAuthorityState.MANUAL
            self._last_setpoint = None
            self._record("manual_takeover")

    def release_manual_control(self) -> None:
        with self._lock:
            self._manual_override = False
            self._last_setpoint = None
            self._state = (
                ControlAuthorityState.AI
                if self._ai_enabled
                else ControlAuthorityState.PAUSED
            )
            self._record("manual_released", state=self._state.value)

    def pause(self) -> None:
        with self._lock:
            self._state = ControlAuthorityState.PAUSED
            self._last_setpoint = None
            self._record("paused")

    def resume_manual(self) -> None:
        with self._lock:
            self._manual_override = True
            self._state = ControlAuthorityState.MANUAL
            self._record("manual_resumed")

    def failsafe(self, reason: str) -> None:
        if not reason:
            raise ControlAuthorityError("failsafe reason must not be empty")
        with self._lock:
            self._state = ControlAuthorityState.FAILSAFE
            self._last_setpoint = None
            self._record("failsafe", reason=reason)

    def submit_manual(
        self,
        *,
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        timestamp_s: float | None = None,
    ) -> ControlSetpoint:
        with self._lock:
            # Validate before changing authority.  A malformed packet must
            # never turn a paused/AI stream into MANUAL merely because the
            # caller attempted to submit it.
            self._validate_setpoint_values(roll, pitch, yaw, throttle, timestamp_s)
            self._manual_override = True
            self._state = ControlAuthorityState.MANUAL
            return self._accept("manual", roll, pitch, yaw, throttle, timestamp_s)

    def submit_ai(
        self,
        *,
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        timestamp_s: float | None = None,
    ) -> ControlSetpoint:
        with self._lock:
            if not self._ai_enabled:
                raise ControlAuthorityError("AI control requires explicit enable_ai()")
            if self._manual_override or self._state is ControlAuthorityState.MANUAL:
                raise ControlAuthorityError("manual control has priority over AI")
            if self._state in {ControlAuthorityState.PAUSED, ControlAuthorityState.FAILSAFE}:
                raise ControlAuthorityError(f"AI control unavailable while {self._state.value}")
            # Do this check while the old state is still intact; invalid AI
            # input must not mutate the authority or leave a stale setpoint.
            self._validate_setpoint_values(roll, pitch, yaw, throttle, timestamp_s)
            self._state = ControlAuthorityState.AI
            return self._accept("ai", roll, pitch, yaw, throttle, timestamp_s)

    # Names used by control adapters and tests.
    request_manual = submit_manual
    request_ai = submit_ai
    enable_ai_control = enable_ai
    disable_ai_control = disable_ai

    def _accept(
        self,
        source: str,
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        timestamp_s: float | None,
    ) -> ControlSetpoint:
        self._validate_setpoint_values(roll, pitch, yaw, throttle, timestamp_s)
        stamp = monotonic() if timestamp_s is None else float(timestamp_s)
        setpoint = ControlSetpoint(
            source,
            float(roll),
            float(pitch),
            float(yaw),
            float(throttle),
            stamp,
        )
        self._last_setpoint = setpoint
        self._record("setpoint", source=source, timestamp_s=stamp)
        return setpoint

    @staticmethod
    def _validate_setpoint_values(
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        timestamp_s: float | None,
    ) -> None:
        values = (roll, pitch, yaw, throttle)
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in values
        ):
            raise ControlAuthorityError("setpoint values must be numeric")
        if any(
            not math.isfinite(float(value)) or abs(float(value)) > 1.0
            for value in values[:3]
        ):
            raise ControlAuthorityError("setpoint values must be within [-1, 1]")
        if not 0.0 <= float(throttle) <= 1.0:
            raise ControlAuthorityError("throttle must be within [0, 1]")
        if timestamp_s is not None and (
            not isinstance(timestamp_s, (int, float))
            or isinstance(timestamp_s, bool)
            or not math.isfinite(float(timestamp_s))
        ):
            raise ControlAuthorityError("timestamp_s must be finite")

    def _record(self, event: str, **details: object) -> None:
        self._events.append({"event": event, "state": self._state.value, **details})


__all__ = [
    "ControlAuthorityError",
    "ControlAuthorityState",
    "ControlSetpoint",
    "SimulatorControlAuthority",
]

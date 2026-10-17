"""Fixed-wing and VTOL SIM-teacher primitive executor.

The existing :mod:`executor` remains the multirotor OFFBOARD implementation.
This adapter uses high-level PX4 guided commands for airframes that cannot
hover: translations are reposition commands, turns are heading reposition
commands, and holds are orbits.  A fake vehicle can implement the same small
methods and record a deterministic command sequence in tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .executor import ExecutionResult
from .primitives import ActionPrimitive, CompletionCriterion, body_to_ned


class FixedWingVtolExecutor:
    """Map teacher primitives to fixed-wing/VTOL PX4 command methods."""

    def __init__(
        self,
        vehicle: Any,
        *,
        airframe: str = "fixed_wing",
        takeoff_method: str = "runway",
        landing_method: str = "runway",
        altitude_m: float = 30.0,
        loiter_radius_m: float = 80.0,
        ground_speed_mps: float = 15.0,
        criterion: CompletionCriterion | None = None,
        position: Callable[[], tuple[float, float, float]] | None = None,
        yaw_deg: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized = airframe.strip().lower().replace("-", "_")
        if normalized not in {"fixed_wing", "fw", "vtol"}:
            raise ValueError("airframe must be fixed_wing or vtol")
        if altitude_m <= 0 or loiter_radius_m <= 0 or ground_speed_mps <= 0:
            raise ValueError("altitude, loiter radius and ground speed must be positive")
        self.vehicle = vehicle
        self.airframe = "vtol" if normalized == "vtol" else "fixed_wing"
        self.takeoff_method = takeoff_method
        self.landing_method = landing_method
        self.altitude_m = float(altitude_m)
        self.loiter_radius_m = float(loiter_radius_m)
        self.ground_speed_mps = float(ground_speed_mps)
        self.criterion = criterion or CompletionCriterion()
        self._position = position or (lambda: (0.0, 0.0, -self.altitude_m))
        self._yaw = yaw_deg or (lambda: 0.0)
        self._clock = clock
        self._commanded = tuple(float(v) for v in self._position())
        self._commanded_yaw = float(self._yaw())
        self._armed = False

    @staticmethod
    def _ok(result: Any) -> bool:
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        value = getattr(result, "ok", None)
        if value is not None:
            return bool(value)
        return getattr(result, "status", "") in {"accepted", "sent", "success"}

    def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self.vehicle, name, None)
        if not callable(method):
            raise RuntimeError(f"vehicle lacks {name}()")
        return method(*args, **kwargs)

    def arm_and_takeoff(self, altitude_m: float | None = None) -> ExecutionResult:
        started = self._clock()
        altitude = self.altitude_m if altitude_m is None else float(altitude_m)
        try:
            arm = (getattr(self.vehicle, "arm", None)
                   or getattr(self.vehicle, "arducopter_arm", None))
            if callable(arm):
                arm()
            method = "vtol" if self.airframe == "vtol" else self.takeoff_method
            result = self._invoke("fw_takeoff", method, alt=altitude,
                                  point=self._commanded[:2], pitch_deg=0.0,
                                  yaw_deg=self._commanded_yaw, timeout_s=0.0)
            if not self._ok(result):
                return ExecutionResult("failed", self._clock() - started, ack={"result": result})
            if self.airframe == "vtol":
                transition = self._invoke("vtol_transition", "FW", timeout_s=0.0)
                if not self._ok(transition):
                    return ExecutionResult("failed", self._clock() - started,
                                           ack={"takeoff": result, "transition": transition})
            self._armed = True
            self._commanded = (self._commanded[0], self._commanded[1], -altitude)
            return ExecutionResult("success", self._clock() - started, ack={"takeoff": result})
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})

    def execute(self, action: ActionPrimitive) -> ExecutionResult:
        started = self._clock()
        origin = self._commanded
        yaw = self._commanded_yaw
        try:
            if action.kind == "hold":
                result = self._invoke("orbit", origin[:2], self.loiter_radius_m,
                                      self.ground_speed_mps, alt=-origin[2], timeout_s=0.0)
                return ExecutionResult("success" if self._ok(result) else "failed",
                                       self._clock() - started, ack={"orbit": result})
            if action.kind == "stop":
                return ExecutionResult("success", self._clock() - started)
            if action.kind == "turn":
                target_yaw = yaw + action.yaw_delta_deg
                result = self._invoke("reposition", origin[:2], -origin[2],
                                      self.ground_speed_mps, yaw_deg=target_yaw, timeout_s=0.0)
                self._commanded_yaw = target_yaw
                return ExecutionResult("success" if self._ok(result) else "failed",
                                       self._clock() - started, ack={"reposition": result})
            north, east, down = body_to_ned(action.forward_m, action.right_m,
                                             action.up_m, yaw)
            target = (origin[0] + north, origin[1] + east, origin[2] + down)
            result = self._invoke("reposition", target[:2], -target[2],
                                  self.ground_speed_mps, timeout_s=0.0)
            self._commanded = target
            return ExecutionResult("success" if self._ok(result) else "failed",
                                   self._clock() - started,
                                   (north, east, down), ack={"reposition": result})
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})

    def rtl(self) -> None:
        method = getattr(self.vehicle, "rtl", None)
        if callable(method):
            method()
        self._armed = False

    def land(self) -> ExecutionResult:
        started = self._clock()
        try:
            if self.airframe == "vtol":
                transition = self._invoke("vtol_transition", "MC", timeout_s=0.0)
                if not self._ok(transition):
                    return ExecutionResult("failed", self._clock() - started,
                                           ack={"transition": transition})
            result = self._invoke("fw_land", self.landing_method, point=self._commanded[:2],
                                  alt=0.0, timeout_s=0.0)
            self._armed = False
            return ExecutionResult("success" if self._ok(result) else "failed",
                                   self._clock() - started, ack={"land": result})
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})


__all__ = ["FixedWingVtolExecutor"]

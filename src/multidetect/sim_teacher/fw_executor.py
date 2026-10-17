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
from multidetect.autonomy.motion_intent import MotionCommand


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
        self.mode = "FW"
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
            if isinstance(action, MotionCommand):
                return self.execute_command(action)
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

    def execute_command(self, command: MotionCommand) -> ExecutionResult:
        """Execute the shared intent command vocabulary.

        A vehicle may implement either ``orbit``/``reposition`` (the PX4
        protocol) or the equivalent ``loiter_at`` convenience method.  The
        duck-typed calls keep this executor usable by deterministic SIM fakes.
        """
        started = self._clock()
        try:
            if command.rejected:
                return ExecutionResult("failed", self._clock() - started, ack={"reason": command.reason})
            centre = command.target_ned or self._commanded
            altitude = -centre[2]
            speed = command.speed_mps or self.ground_speed_mps
            if command.kind in {"loiter", "orbit", "mc_hold"}:
                radius = command.radius_m or self.loiter_radius_m
                orbit_method = getattr(self.vehicle, "orbit", None)
                loiter_method = getattr(self.vehicle, "loiter_at", None)
                if callable(orbit_method):
                    result = orbit_method(centre[:2], radius, speed, alt=altitude, timeout_s=0.0)
                elif callable(loiter_method):
                    result = loiter_method(centre[:2], radius, altitude, speed=speed, timeout_s=0.0)
                else:
                    raise RuntimeError("vehicle lacks orbit()/loiter_at()")
                return ExecutionResult("success" if self._ok(result) else "failed", self._clock() - started, ack={"orbit": result})
            if command.kind in {"reposition", "mc_reposition"}:
                target = command.target_ned or self._commanded
                result = self._invoke("reposition", target[:2], -target[2], speed, radius=command.radius_m or 0.0, yaw_deg=command.yaw_deg or 0.0, timeout_s=0.0)
                self._commanded = target
                if command.yaw_deg is not None:
                    self._commanded_yaw = command.yaw_deg
                return ExecutionResult("success" if self._ok(result) else "failed", self._clock() - started, ack={"reposition": result})
            if command.kind == "transition":
                result = self._invoke("vtol_transition", command.mode or "FW", timeout_s=0.0)
                if self._ok(result):
                    self.mode = command.mode or "FW"
                return ExecutionResult("success" if self._ok(result) else "failed", self._clock() - started, ack={"transition": result})
            if command.kind == "fw_land":
                result = self._invoke("fw_land", (command.metadata or {}).get("method", self.landing_method), point=centre[:2], alt=0.0, timeout_s=0.0)
                self._armed = False
                return ExecutionResult("success" if self._ok(result) else "failed", self._clock() - started, ack={"land": result})
            if command.kind == "mc_land":
                if self.airframe == "vtol":
                    self._invoke("vtol_transition", "MC", timeout_s=0.0)
                land = getattr(self.vehicle, "land", None)
                result = land() if callable(land) else self._invoke("fw_land", "vtol", point=centre[:2], alt=0.0, timeout_s=0.0)
                self._armed = False
                return ExecutionResult("success" if self._ok(result) else "failed", self._clock() - started, ack={"land": result})
            if command.kind == "fly_by_deliver":
                # Delivery never stops a fixed-wing vehicle.  The command is a
                # safe fly-through window for a payload controller to consume.
                result = self._invoke("reposition", centre[:2], altitude, speed, radius=command.radius_m or self.loiter_radius_m, yaw_deg=command.yaw_deg or 0.0, timeout_s=0.0)
                return ExecutionResult("success" if self._ok(result) else "failed", self._clock() - started, ack={"fly_by": result})
            if command.kind == "stop":
                return ExecutionResult("success", self._clock() - started)
            raise RuntimeError(f"unsupported motion command: {command.kind}")
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

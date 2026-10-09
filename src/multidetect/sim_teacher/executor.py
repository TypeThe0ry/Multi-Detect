"""Small PX4 offboard executor used by the SIM teacher collector.

The executor accepts a pymavlink-like vehicle (or a test fake), keeps safety
and transport policy outside the teacher policy, and never chooses a remote
endpoint.  It streams local NED position targets while waiting for a completion
criterion; callers can record the returned result without refusing samples for
sensor age.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .isolation import validate_local_endpoint
from .primitives import ActionPrimitive, CompletionCriterion, primitive_to_ned


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    elapsed_s: float
    achieved_delta_ned: tuple[float, float, float] = (0.0, 0.0, 0.0)
    achieved_yaw_delta_deg: float = 0.0
    ack: dict[str, Any] = field(default_factory=dict)


class VehicleLike(Protocol):
    def mav(self) -> Any: ...


class PrimitiveExecutor(Protocol):
    def execute(self, action: ActionPrimitive) -> ExecutionResult: ...

    def rtl(self) -> None: ...


class PymavlinkPrimitiveExecutor:
    """Execute primitives with SET_POSITION_TARGET_LOCAL_NED.

    ``vehicle`` may be a pymavlink connection object or a fake exposing
    ``mav``, ``recv_match`` and optionally ``set_mode``/``arducopter_arm``.
    Position/yaw accessors are intentionally injectable for deterministic CPU
    tests and for adapters that already decode PX4 telemetry.
    """

    def __init__(
        self,
        vehicle: Any,
        *,
        endpoint: str = "udp://127.0.0.1:14540",
        criterion: CompletionCriterion | None = None,
        position: callable | None = None,
        yaw_deg: callable | None = None,
        speed_mps: callable | None = None,
        clock: callable = time.monotonic,
        sleep: callable = time.sleep,
        stream_hz: float = 10.0,
    ) -> None:
        validate_local_endpoint(endpoint)
        if stream_hz <= 2.0 or not math.isfinite(stream_hz):
            raise ValueError("stream_hz must be greater than 2 Hz")
        self.vehicle = vehicle
        self.criterion = criterion or CompletionCriterion()
        self._position = position or (lambda: (0.0, 0.0, 0.0))
        self._yaw = yaw_deg or (lambda: 0.0)
        self._speed = speed_mps or (lambda: 0.0)
        self._clock = clock
        self._sleep = sleep
        self.stream_hz = float(stream_hz)
        self._armed = False

    def _mav(self) -> Any:
        mav = getattr(self.vehicle, "mav", None)
        if callable(mav):
            mav = mav()
        if mav is None:
            raise RuntimeError("vehicle has no MAVLink sender")
        return mav

    def _send_setpoint(self, n: float, e: float, d: float, yaw: float) -> None:
        mav = self._mav()
        type_mask = 0b101111111000  # ignore velocity/acceleration/yaw-rate
        sender = getattr(mav, "set_position_target_local_ned_send", None)
        if sender is None:
            raise RuntimeError("vehicle MAVLink sender lacks local NED setpoint method")
        sender(
            int(self._clock() * 1000),
            getattr(self.vehicle, "target_system", 1),
            getattr(self.vehicle, "target_component", 1),
            1,  # MAV_FRAME_LOCAL_NED
            type_mask,
            float(n),
            float(e),
            float(d),
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            math.radians(yaw),
            0.0,
        )

    def arm_and_takeoff(self, altitude_m: float) -> None:
        if altitude_m <= 0 or not math.isfinite(altitude_m):
            raise ValueError("takeoff altitude must be positive and finite")
        set_mode = getattr(self.vehicle, "set_mode", None)
        if callable(set_mode):
            set_mode("OFFBOARD")
        arm = getattr(self.vehicle, "arducopter_arm", None) or getattr(self.vehicle, "arm", None)
        if callable(arm):
            arm()
        self._armed = True
        start = self._position()
        target = (float(start[0]), float(start[1]), -float(altitude_m))
        self._send_setpoint(*target, float(self._yaw()))

    def execute(self, action: ActionPrimitive) -> ExecutionResult:
        start_time = self._clock()
        start_pos = tuple(float(x) for x in self._position())
        start_yaw = float(self._yaw())
        if action.kind == "hold":
            while self._clock() - start_time < action.seconds:
                self._send_setpoint(*start_pos, start_yaw)
                self._sleep(1.0 / self.stream_hz)
            return ExecutionResult(
                "success", self._clock() - start_time, ack={"stream_hz": self.stream_hz}
            )
        if action.kind == "stop":
            self._send_setpoint(*start_pos, start_yaw)
            return ExecutionResult("success", self._clock() - start_time)
        n, e, d, yaw_delta = primitive_to_ned(action, start_yaw)
        target = (start_pos[0] + n, start_pos[1] + e, start_pos[2] + d)
        target_yaw = start_yaw + yaw_delta
        result = "pending"
        while self._clock() - start_time < self.criterion.timeout_s:
            self._send_setpoint(*target, target_yaw)
            current = tuple(float(x) for x in self._position())
            error = math.sqrt(sum((current[i] - target[i]) ** 2 for i in range(3)))
            yaw_error = (float(self._yaw()) - target_yaw + 180.0) % 360.0 - 180.0
            result = self.criterion(
                position_error_m=error,
                yaw_error_deg=yaw_error,
                speed_mps=float(self._speed()),
                elapsed_s=self._clock() - start_time,
            )
            if result != "pending":
                break
            self._sleep(1.0 / self.stream_hz)
        elapsed = self._clock() - start_time
        if result == "pending":
            result = "timeout"
        return ExecutionResult(
            result,
            elapsed,
            tuple(current[i] - start_pos[i] for i in range(3)),
            float(self._yaw()) - start_yaw,
            {"stream_hz": self.stream_hz},
        )

    def rtl(self) -> None:
        set_mode = getattr(self.vehicle, "set_mode", None)
        if callable(set_mode):
            set_mode("RTL")
        self._armed = False

    def land(self) -> None:
        set_mode = getattr(self.vehicle, "set_mode", None)
        if callable(set_mode):
            set_mode("LAND")
        self._armed = False


__all__ = ["ExecutionResult", "PrimitiveExecutor", "PymavlinkPrimitiveExecutor"]



"""Small PX4 offboard executor used by the SIM teacher collector.

The executor accepts a pymavlink-like vehicle (or a test fake), keeps safety
and transport policy outside the teacher policy, and never chooses a remote
endpoint.  It streams local NED position targets while waiting for a completion
criterion; callers can record the returned result without refusing samples for
sensor age.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Protocol

from .isolation import validate_local_endpoint
from .primitives import ActionPrimitive, CompletionCriterion, primitive_to_ned


MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW = 1000
MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE = 1001
GIMBAL_MANAGER_FLAGS_ROLL_LOCK = 4
GIMBAL_MANAGER_FLAGS_PITCH_LOCK = 8
GIMBAL_MANAGER_FLAGS_YAW_IN_VEHICLE_FRAME = 32
GIMBAL_MANAGER_DEVICE_ID = 1
# PX4 publishes GIMBAL_MANAGER_STATUS at a low vehicle-clock rate. At the
# collector's supported 0.1x realtime floor, allow one full 2 s vehicle-clock
# reporting interval plus scheduling margin before declaring ownership absent.
GIMBAL_MANAGER_STATUS_WALL_TIMEOUT_S = 30.0
GCS_SOURCE_SYSTEM = 255
GCS_SOURCE_COMPONENT = 190


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


def _gimbal_profile_limits(profile: Any) -> tuple[bool, Mapping[str, Any]]:
    camera = getattr(profile, "camera", None)
    gimbal = getattr(camera, "gimbal", None)
    if isinstance(gimbal, Mapping) and bool(gimbal.get("enabled")):
        return True, gimbal.get("angle_range_deg", {})
    return False, {}


def _gimbal_angles_deg(
    status: Any,
    *,
    vehicle_yaw_deg: float | None = None,
    yaw_correction_deg: float = 0.0,
) -> dict[str, float] | None:
    q = status.get("q") if isinstance(status, Mapping) else getattr(status, "q", None)
    if not isinstance(q, (list, tuple)) or len(q) < 4:
        return None
    try:
        w, x, y, z = (float(value) for value in q[:4])
        norm = math.sqrt(w * w + x * x + y * y + z * z)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(norm) or norm <= 1e-9:
        return None
    w, x, y, z = (value / norm for value in (w, x, y, z))
    yaw_deg = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    flags = int(status.get("flags", 0) if isinstance(status, Mapping) else getattr(status, "flags", 0) or 0)
    if vehicle_yaw_deg is not None and flags & 64:
        yaw_deg = (yaw_deg - float(vehicle_yaw_deg) + 180.0) % 360.0 - 180.0
    yaw_deg = (yaw_deg + float(yaw_correction_deg) + 180.0) % 360.0 - 180.0
    return {
        "roll": math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))),
        "pitch": math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))),
        "yaw": yaw_deg,
    }


def execute_gimbal_action(
    vehicle: Any,
    action: ActionPrimitive,
    *,
    profile: Any,
    reader: Any,
    clock: callable = time.monotonic,
    timeout_s: float = 15.0,
    tolerance_deg: float = 4.0,
    yaw_correction_deg: float = 0.0,
) -> ExecutionResult:
    """Acquire the MAVLink gimbal-manager lease and require a fresh readback."""
    started = clock()
    if not isinstance(action, ActionPrimitive) or action.kind != "gimbal":
        return ExecutionResult("rejected", clock() - started, ack={"reason": "gimbal_action_required"})
    enabled, limits = _gimbal_profile_limits(profile)
    if not enabled:
        return ExecutionResult("rejected", clock() - started, ack={"reason": "gimbal_capability_required"})
    target = {
        "yaw": float(action.gimbal_yaw_deg),
        "roll": float(action.gimbal_roll_deg),
        "pitch": float(action.gimbal_pitch_deg),
    }
    for axis, value in target.items():
        bounds = limits.get(axis) if isinstance(limits, Mapping) else None
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            return ExecutionResult(
                "failed",
                clock() - started,
                ack={"reason": "gimbal_axis_range_missing", "axis": axis},
            )
        if value < float(bounds[0]) or value > float(bounds[1]):
            return ExecutionResult(
                "rejected",
                clock() - started,
                ack={"reason": "gimbal_angle_out_of_range", "axis": axis, "value_deg": value},
            )
    if reader is None:
        return ExecutionResult("failed", clock() - started, ack={"reason": "gimbal_reader_required"})
    mav = getattr(vehicle, "mav", None)
    if callable(mav):
        mav = mav()
    sender = getattr(mav, "command_long_send", None)
    if not callable(sender):
        return ExecutionResult("failed", clock() - started, ack={"reason": "gimbal_command_sender_missing"})
    target_system = int(getattr(vehicle, "target_system", 1) or 1)
    target_component = int(getattr(vehicle, "target_component", 1) or 1)
    command_started_ns = time.monotonic_ns()
    sender(
        target_system,
        target_component,
        MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE,
        0,
        float(GCS_SOURCE_SYSTEM),
        float(GCS_SOURCE_COMPONENT),
        -1.0,
        -1.0,
        0.0,
        0.0,
        float(GIMBAL_MANAGER_DEVICE_ID),
    )
    configure_deadline = time.monotonic() + min(max(0.1, float(timeout_s)), 5.0)
    configure_ack_ns = 0
    while time.monotonic() < configure_deadline:
        state = getattr(reader, "state", None)
        acks = getattr(state, "command_acks", {}) if state is not None else {}
        entry = acks.get(MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE) if isinstance(acks, Mapping) else None
        if entry is not None:
            message, configure_ack_ns = entry
            result = message.get("result", -1) if isinstance(message, Mapping) else getattr(message, "result", -1)
            if int(configure_ack_ns) >= command_started_ns:
                if int(result) not in {0, 5}:
                    return ExecutionResult("failed", clock() - started, ack={"reason": "gimbal_manager_configure_rejected", "ack_result": int(result)})
                if int(result) == 0:
                    break
        time.sleep(0.02)
    else:
        return ExecutionResult("timeout", clock() - started, ack={"reason": "gimbal_manager_configure_ack_timeout"})
    manager_deadline = time.monotonic() + GIMBAL_MANAGER_STATUS_WALL_TIMEOUT_S
    manager_status = None
    while time.monotonic() < manager_deadline:
        state = getattr(reader, "state", None)
        manager_status = state.get("GIMBAL_MANAGER_STATUS") if state is not None and callable(getattr(state, "get", None)) else None
        received = getattr(state, "received_monotonic_ns", {}) if state is not None else {}
        manager_ns = received.get("GIMBAL_MANAGER_STATUS", -1) if isinstance(received, Mapping) else -1
        primary = manager_status.get("primary_control_sysid", 0) if isinstance(manager_status, Mapping) else getattr(manager_status, "primary_control_sysid", 0)
        primary_component = manager_status.get("primary_control_compid", 0) if isinstance(manager_status, Mapping) else getattr(manager_status, "primary_control_compid", 0)
        if (int(manager_ns) >= int(configure_ack_ns)
                and int(primary or 0) == GCS_SOURCE_SYSTEM
                and int(primary_component or 0) == GCS_SOURCE_COMPONENT):
            break
        time.sleep(0.02)
    else:
        primary = manager_status.get("primary_control_sysid", 0) if isinstance(manager_status, Mapping) else getattr(manager_status, "primary_control_sysid", 0) if manager_status is not None else 0
        primary_component = manager_status.get("primary_control_compid", 0) if isinstance(manager_status, Mapping) else getattr(manager_status, "primary_control_compid", 0) if manager_status is not None else 0
        return ExecutionResult("failed", clock() - started, ack={"reason": "gimbal_manager_control_not_owned", "primary_control_sysid": int(primary or 0), "primary_control_compid": int(primary_component or 0)})
    command_started_ns = time.monotonic_ns()
    sender(
        target_system,
        target_component,
        MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
        0,
        target["pitch"],
        target["yaw"],
        math.nan,
        math.nan,
        float(GIMBAL_MANAGER_FLAGS_ROLL_LOCK | GIMBAL_MANAGER_FLAGS_PITCH_LOCK | GIMBAL_MANAGER_FLAGS_YAW_IN_VEHICLE_FRAME),
        0.0,
        float(GIMBAL_MANAGER_DEVICE_ID),
    )
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    last_error: dict[str, float] | None = None
    while time.monotonic() < deadline:
        state = getattr(reader, "state", None)
        if state is None:
            break
        acks = getattr(state, "command_acks", {})
        if isinstance(acks, Mapping) and MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW in acks:
            ack_message, received_ns = acks[MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW]
            if isinstance(ack_message, Mapping):
                ack_result = ack_message.get("result", -1)
            else:
                ack_result = getattr(ack_message, "result", -1)
            if int(received_ns) >= command_started_ns and int(ack_result) not in {0, 5}:
                return ExecutionResult("failed", clock() - started, ack={"reason": "gimbal_manager_pitchyaw_rejected"})
        status = state.get("GIMBAL_DEVICE_ATTITUDE_STATUS") if callable(getattr(state, "get", None)) else None
        received = getattr(state, "received_monotonic_ns", {})
        if status is not None and isinstance(received, Mapping) and received.get("GIMBAL_DEVICE_ATTITUDE_STATUS", -1) >= command_started_ns:
            attitude = state.get("ATTITUDE") if callable(getattr(state, "get", None)) else None
            raw_yaw = getattr(attitude, "yaw", attitude.get("yaw", 0.0) if isinstance(attitude, Mapping) else 0.0)
            actual = _gimbal_angles_deg(
                status,
                vehicle_yaw_deg=math.degrees(float(raw_yaw)),
                yaw_correction_deg=yaw_correction_deg,
            )
            if actual is not None:
                last_error = {
                    axis: abs((actual[axis] - target[axis] + 180.0) % 360.0 - 180.0)
                    for axis in ("yaw", "roll", "pitch")
                }
                if max(last_error.values()) <= tolerance_deg:
                    return ExecutionResult(
                        "success",
                        clock() - started,
                        ack={"command": "MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW", "command_id": MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW, "configure_command_id": MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE, "target_deg": target, "actual_deg": actual, "error_deg": last_error},
                    )
        time.sleep(0.02)
    return ExecutionResult("timeout", clock() - started, ack={"command": "MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW", "command_id": MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW, "configure_command_id": MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE, "target_deg": target, "error_deg": last_error})


class SetpointStreamer:
    """Continuously send a local-NED position/yaw target on one MAVLink link."""

    TYPE_MASK = 0b101111111000
    FRAME_LOCAL_NED = 1

    def __init__(
        self,
        vehicle: Any,
        *,
        rate_hz: float = 20.0,
        target_system: int | None = None,
        target_component: int | None = None,
        clock: callable = time.monotonic,
    ) -> None:
        if not math.isfinite(rate_hz) or rate_hz < 15.0:
            raise ValueError("rate_hz must be at least 15 Hz")
        self.vehicle = vehicle
        self.rate_hz = float(rate_hz)
        self._clock = clock
        self._period = 1.0 / self.rate_hz
        self._target_system = target_system
        self._target_component = target_component
        self._lock = threading.Lock()
        self._target: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.sent_count = 0

    def set_target(self, north_m: float, east_m: float, down_m: float, yaw_deg: float) -> None:
        target = tuple(float(value) for value in (north_m, east_m, down_m, yaw_deg))
        if not all(math.isfinite(value) for value in target):
            raise ValueError("setpoint target must contain finite values")
        with self._lock:
            self._target = target

    update_target = set_target

    @property
    def target(self) -> tuple[float, float, float, float]:
        with self._lock:
            return self._target

    def _mav(self) -> Any:
        mav = getattr(self.vehicle, "mav", None)
        if callable(mav):
            mav = mav()
        if not callable(getattr(mav, "set_position_target_local_ned_send", None)):
            raise RuntimeError("vehicle MAVLink sender lacks local NED setpoint method")
        return mav

    def send_once(self) -> None:
        """Send the current target immediately (used while waiting for ARM)."""
        self._send_once()

    def _send_once(self) -> None:
        north, east, down, yaw_deg = self.target
        target_system = self._target_system
        if target_system is None:
            target_system = int(getattr(self.vehicle, "target_system", 1) or 1)
        target_component = self._target_component
        if target_component is None:
            target_component = int(getattr(self.vehicle, "target_component", 1) or 1)
        self._mav().set_position_target_local_ned_send(
            int(self._clock() * 1000) & 0xFFFFFFFF,
            int(target_system),
            int(target_component),
            self.FRAME_LOCAL_NED,
            self.TYPE_MASK,
            north,
            east,
            down,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            math.radians(yaw_deg),
            0.0,
        )
        self.sent_count += 1

    def _run(self) -> None:
        next_tick = self._clock()
        try:
            while not self._stop.is_set():
                self._send_once()
                next_tick += self._period
                delay = next_tick - self._clock()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_tick = self._clock()
        except BaseException as exc:
            self.error = exc
            self._stop.set()

    def start(self) -> SetpointStreamer:
        if self._thread is not None and self._thread.is_alive():
            return self
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sim-teacher-setpoints", daemon=True)
        self._thread.start()
        return self

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise RuntimeError(f"setpoint streamer failed: {self.error}") from self.error

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None


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
        streamer: SetpointStreamer | None = None,
        wall_clock: callable = time.monotonic,
        max_wall_s: float | None = None,
        airframe_profile: Any = None,
        telemetry_reader: Any = None,
        gimbal_timeout_s: float = 15.0,
        gimbal_tolerance_deg: float = 4.0,
        gimbal_yaw_correction_deg: float = 0.0,
    ) -> None:
        validate_local_endpoint(endpoint)
        if stream_hz <= 2.0 or not math.isfinite(stream_hz):
            raise ValueError("stream_hz must be greater than 2 Hz")
        self.vehicle = vehicle
        self.criterion = criterion or CompletionCriterion()
        self._position = position or (lambda: (0.0, 0.0, 0.0))
        self._yaw = yaw_deg or (lambda: 0.0)
        self._speed = speed_mps or (lambda: 0.0)
        # ``clock`` measures primitive durations and timeouts.  A simulator's own
        # clock keeps them meaningful when the simulation runs slower than real
        # time; ``max_wall_s`` then bounds each primitive in wall time in case the
        # simulation stalls.
        self._clock = clock
        self._wall_clock = wall_clock
        if max_wall_s is not None and (not math.isfinite(max_wall_s) or max_wall_s <= 0):
            raise ValueError("max_wall_s must be finite and positive")
        self.max_wall_s = max_wall_s
        self._sleep = sleep
        self.stream_hz = float(stream_hz)
        self._armed = False
        self.streamer = streamer
        self._commanded_position: tuple[float, float, float] | None = None
        self._commanded_yaw: float | None = None
        self.airframe_profile = airframe_profile
        self.telemetry_reader = telemetry_reader
        self.gimbal_timeout_s = float(gimbal_timeout_s)
        self.gimbal_tolerance_deg = float(gimbal_tolerance_deg)
        self.gimbal_yaw_correction_deg = float(gimbal_yaw_correction_deg)

    def _mav(self) -> Any:
        mav = getattr(self.vehicle, "mav", None)
        if callable(mav):
            mav = mav()
        if mav is None:
            raise RuntimeError("vehicle has no MAVLink sender")
        return mav

    def _send_setpoint(self, n: float, e: float, d: float, yaw: float) -> None:
        if self.streamer is not None:
            self.streamer.set_target(n, e, d, yaw)
            return
        mav = self._mav()
        # Ignore velocity/acceleration/yaw-rate; keep position and yaw fields.
        type_mask = 0b101111111000
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
        yaw = float(self._yaw())
        target = (float(start[0]), float(start[1]), -float(altitude_m))
        self._commanded_position = target
        self._commanded_yaw = yaw
        self._send_setpoint(*target, yaw)

    def execute(self, action: ActionPrimitive) -> ExecutionResult:
        if action.kind == "gimbal":
            return execute_gimbal_action(
                self.vehicle,
                action,
                profile=self.airframe_profile,
                reader=self.telemetry_reader,
                clock=self._clock,
                timeout_s=self.gimbal_timeout_s,
                tolerance_deg=self.gimbal_tolerance_deg,
                yaw_correction_deg=self.gimbal_yaw_correction_deg,
            )
        start_time = self._clock()
        wall_start = self._wall_clock()
        last_clock = start_time
        last_progress_wall = wall_start
        wall_timeout_hit = False

        def wall_progress_ok(now: float) -> bool:
            """Bound only periods where the simulation clock stops advancing.

            Lockstep SITL can be much slower than real time on a loaded Linux
            host.  A total wall-clock budget would therefore cut a valid
            simulation-time primitive short.  The wall bound is still needed
            when telemetry/simulation stalls completely.
            """
            nonlocal last_clock, last_progress_wall, wall_timeout_hit
            if self.max_wall_s is None:
                return True
            wall_now = self._wall_clock()
            if now > last_clock:
                last_clock = now
                last_progress_wall = wall_now
            if wall_now - last_progress_wall >= self.max_wall_s:
                wall_timeout_hit = True
                return False
            return True

        measured_start = tuple(float(x) for x in self._position())
        measured_yaw = float(self._yaw())
        if self._commanded_position is None:
            self._commanded_position = measured_start
        if self._commanded_yaw is None:
            self._commanded_yaw = measured_yaw
        start_pos = self._commanded_position
        start_yaw = self._commanded_yaw
        if action.kind == "hold":
            while True:
                now = self._clock()
                if now - start_time >= action.seconds or not wall_progress_ok(now):
                    break
                self._send_setpoint(*start_pos, start_yaw)
                self._sleep(1.0 / self.stream_hz)
            held = self._clock() - start_time
            status = "success" if held >= action.seconds else "timeout"
            ack = {"stream_hz": self.stream_hz}
            if wall_timeout_hit:
                ack["wall_timeout"] = True
            return ExecutionResult(status, held, ack=ack)
        if action.kind == "stop":
            self._send_setpoint(*start_pos, start_yaw)
            return ExecutionResult("success", self._clock() - start_time)
        n, e, d, yaw_delta = primitive_to_ned(action, start_yaw)
        target = (start_pos[0] + n, start_pos[1] + e, start_pos[2] + d)
        target_yaw = start_yaw + yaw_delta
        self._commanded_position = target
        self._commanded_yaw = target_yaw
        result = "pending"
        current = measured_start
        while True:
            now = self._clock()
            if now - start_time >= self.criterion.timeout_s or not wall_progress_ok(now):
                break
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
        ack = {"stream_hz": self.stream_hz}
        if wall_timeout_hit:
            ack["wall_timeout"] = True
        return ExecutionResult(
            result,
            elapsed,
            tuple(current[i] - start_pos[i] for i in range(3)),
            float(self._yaw()) - start_yaw,
            ack,
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


__all__ = [
    "ExecutionResult",
    "PrimitiveExecutor",
    "SetpointStreamer",
    "PymavlinkPrimitiveExecutor",
    "execute_gimbal_action",
    "MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW",
    "MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE",
]

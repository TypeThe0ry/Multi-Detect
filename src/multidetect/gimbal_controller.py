"""Camera gimbal command boundary for SIM and PX4 runtimes."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from multidetect.camera_mount import CameraMount

MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW = 1000
MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE = 1001
MAVLINK_MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS = 285
GIMBAL_DEVICE_ATTITUDE_STATUS = MAVLINK_MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS
GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME = 1
GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME = 2


class GimbalManagerSession:
    """Nonblocking, serialized gimbal manager transaction on the telemetry reader.

    ACKs have no transaction identifier: after a timeout the session fails
    permanently instead of retrying and mistaking a late ACK for a new command.
    """

    def __init__(self, connection: Any, reader: Any, send: Callable[..., None], *,
                 device_id: int = 1, wall_timeout_s: float = 5.0,
                 vehicle_timeout_s: float = 2.0,
                 owner_vehicle_timeout_s: float = 4.0,
                 target: tuple[int, int] | None = None,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        mav = getattr(connection, "mav", None)
        self.identity = (getattr(mav, "srcSystem", None), getattr(mav, "srcComponent", None))
        if any(type(value) is not int or not 1 <= value <= 255 for value in self.identity):
            raise RuntimeError("MAVLink connection has no valid source identity")
        self.target = target or (int(getattr(connection, "target_system", 0) or 0),
                                 int(getattr(connection, "target_component", 0) or 0))
        if not all(self.target):
            raise RuntimeError("MAVLink manager target is unknown")
        self.reader, self.send = reader, send
        self.device_id, self.wall_timeout_s = device_id, wall_timeout_s
        self.vehicle_timeout_s, self.owner_vehicle_timeout_s, self.clock_ns = vehicle_timeout_s, owner_vehicle_timeout_s, clock_ns
        self.cursor = reader.gimbal_events_cursor()
        self.phase = "idle"
        self.pending = None
        self.error = None
        # Set once a land primitive is issued: PX4 then drops gimbal manager
        # control, so a DENIED ack for an in-flight pitch/yaw is expected.
        self.control_release_expected = False
        self.owned = False
        self.started_ns = 0
        self.started_vehicle_ms = None
        self.latest_attitude = None
        self._dispatch_ticket = None
        self._dispatch_phase = None
        self._dispatch_command = None
        self.last_command_started_ns = None

    def _start(self, phase: str, command: int, params: tuple[float, ...]) -> None:
        self.started_ns = self.clock_ns()
        self.started_vehicle_ms = self.reader.state.vehicle_time_boot_ms
        if command == MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW:
            self.last_command_started_ns = None
        ticket = self.send(command, *params)
        if ticket is not None and hasattr(ticket, "done"):
            self._dispatch_ticket = ticket
            self._dispatch_phase = phase
            self._dispatch_command = command
            self.phase = "dispatch"
        else:
            self.phase = phase
            if command == MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW:
                self.last_command_started_ns = self.started_ns

    def command(self, command: int, *params: float) -> None:
        if self.error:
            raise RuntimeError(self.error)
        if self.phase == "closed":
            raise RuntimeError("gimbal session is closed")
        if command != MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW:
            raise ValueError("unsupported gimbal command")
        self.pending = (command, params)
        self.poll()

    def _source_matches(self, message: Any) -> bool:
        def value(name: str):
            getter = getattr(message, name, None)
            return getter() if callable(getter) else None
        return (value("get_srcSystem"), value("get_srcComponent")) == self.target

    @staticmethod
    def _field(message: Any, name: str, default: Any = None) -> Any:
        return message.get(name, default) if isinstance(message, dict) else getattr(message, name, default)

    def _check_deadline(self, stamp_ns: int, vehicle_ms: int | None) -> None:
        if self.phase not in {"dispatch", "configure_ack", "owner", "command_ack"}:
            return
        elapsed_vehicle = (vehicle_ms - self.started_vehicle_ms) / 1000 if (
            vehicle_ms is not None and self.started_vehicle_ms is not None and vehicle_ms >= self.started_vehicle_ms
        ) else 0.0
        vehicle_limit = self.owner_vehicle_timeout_s if self.phase == "owner" else self.vehicle_timeout_s
        if ((stamp_ns - self.started_ns) / 1e9 >= self.wall_timeout_s or
            elapsed_vehicle >= vehicle_limit):
            raise TimeoutError(f"gimbal {self.phase} timeout")

    def poll(self) -> str:
        if self.error:
            raise RuntimeError(self.error)
        if self.phase == "closed":
            return self.phase
        self.reader.raise_if_failed()
        try:
            if self.phase == "dispatch":
                ticket = self._dispatch_ticket
                if not ticket.done.is_set():
                    self._check_deadline(self.clock_ns(), self.reader.state.vehicle_time_boot_ms)
                    return self.phase
                if ticket.error is not None:
                    raise RuntimeError(f"gimbal transport failed: {ticket.error}") from ticket.error
                if ticket.started_ns is None:
                    raise RuntimeError("gimbal transport completed without a send timestamp")
                self.started_ns = ticket.started_ns
                self.started_vehicle_ms = ticket.started_vehicle_ms
                self.phase = self._dispatch_phase
                if self._dispatch_command == MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW:
                    self.last_command_started_ns = self.started_ns
                self._dispatch_ticket = None
                self._dispatch_phase = None
                self._dispatch_command = None
            self.cursor, events = self.reader.gimbal_events_since(self.cursor)
            for name, msg, stamp, vehicle_ms in events:
                if stamp < self.started_ns or not self._source_matches(msg):
                    continue
                if name == "GIMBAL_DEVICE_ATTITUDE_STATUS":
                    if self._field(msg, "gimbal_device_id") == self.device_id:
                        self.latest_attitude = (msg, stamp)
                    continue
                if name == "COMMAND_ACK":
                    expected = 1001 if self.phase == "configure_ack" else 1000 if self.phase == "command_ack" else None
                    if (expected is None or self._field(msg, "command") != expected or
                        (self._field(msg, "target_system"), self._field(msg, "target_component")) != self.identity):
                        continue
                    self._check_deadline(stamp, vehicle_ms)
                    result = self._field(msg, "result", -1)
                    if result == 5:
                        continue
                    if result == 2 and expected == 1000 and self.control_release_expected:
                        logging.getLogger(__name__).warning(
                            "gimbal command 1000 denied after landing released control; ignoring")
                        self.phase = "ready"
                        continue
                    if result != 0:
                        raise RuntimeError(f"gimbal command {expected} rejected: MAV_RESULT={result}")
                    if expected == 1001:
                        self.phase = "owner"
                        # Manager status is periodic (2 s in PX4).  Its own
                        # budget starts at the ACK, not the configure send.
                        self.started_ns = stamp
                        self.started_vehicle_ms = vehicle_ms
                    else:
                        self.phase = "ready"
                elif name == "GIMBAL_MANAGER_STATUS" and self.phase == "owner":
                    if (self._field(msg, "gimbal_device_id") == self.device_id and
                        (self._field(msg, "primary_control_sysid"), self._field(msg, "primary_control_compid")) == self.identity):
                        self._check_deadline(stamp, vehicle_ms)
                        self.owned = True
                        self.phase = "ready"
                elif name == "GIMBAL_MANAGER_STATUS" and self.owned:
                    if (self._field(msg, "gimbal_device_id") == self.device_id and
                        (self._field(msg, "primary_control_sysid"), self._field(msg, "primary_control_compid")) != self.identity):
                        raise RuntimeError("gimbal manager ownership lost")
            self._check_deadline(self.clock_ns(), self.reader.state.vehicle_time_boot_ms)
            if self.phase == "idle" and self.pending:
                self._start("configure_ack", 1001, (*self.identity, -1.0, -1.0, 0.0, 0.0, float(self.device_id)))
            elif self.phase == "ready" and self.pending:
                command, params = self.pending
                self.pending = None
                self._start("command_ack", command, params)
        except Exception as exc:
            self.error = str(exc)
            self.phase = "failed"
            raise
        return self.phase

    def close(self) -> None:
        self.pending = None
        self.phase = "closed"


@dataclass(frozen=True, slots=True)
class GimbalState:
    """Feedback in the CameraMount convention: pitch positive toward NED down."""

    pitch_rad: float
    yaw_rad: float
    roll_rad: float = 0.0
    timestamp_us: int | None = None
    rotation: tuple[float, ...] | None = None
    yaw_frame: str = "unknown"


class GimbalController(Protocol):
    mount: CameraMount

    def command_pitch_yaw(
        self, pitch_rad: float, yaw_rad: float, *, mode: str = "lock-horizon", vehicle_yaw_rad: float = 0.0
    ) -> None: ...

    def update_feedback(self, message: Any) -> GimbalState | None: ...


class FixedGimbalController:
    """No-op controller for a fixed camera; executor legacy actions remain active."""

    mount = CameraMount.fixed()

    def command_pitch_yaw(
        self, pitch_rad: float, yaw_rad: float, *, mode: str = "follow-body", vehicle_yaw_rad: float = 0.0
    ) -> None:
        del pitch_rad, yaw_rad, mode, vehicle_yaw_rad

    def update_feedback(self, message: Any) -> None:
        del message
        return None


class SimGimbalController:
    """SIM adapter that forwards bounded radians to a simulator joint callback."""

    def __init__(self, mount: CameraMount, command: Callable[[float, float], None]) -> None:
        if mount.kind != "gimbal":
            raise ValueError("SIM gimbal controller requires a gimbal mount")
        self.mount = mount
        self._command = command
        self.state: GimbalState | None = None

    def command_pitch_yaw(
        self,
        pitch_rad: float,
        yaw_rad: float,
        *,
        mode: str = "lock-horizon",
        vehicle_yaw_rad: float = 0.0,
    ) -> None:
        pitch = min(self.mount.limits.pitch_max_rad, max(self.mount.limits.pitch_min_rad, float(pitch_rad)))
        relative_yaw = float(yaw_rad) if mode == "lock-horizon" else float(yaw_rad) - vehicle_yaw_rad
        yaw = min(self.mount.limits.yaw_max_rad, max(self.mount.limits.yaw_min_rad, relative_yaw))
        self._command(pitch, yaw)

    def update_feedback(self, message: Any) -> GimbalState | None:
        self.state = parse_gimbal_device_attitude_status(message)
        return self.state


class Px4GimbalController(SimGimbalController):
    """PX4 gimbal-manager adapter using COMMAND_LONG send semantics."""

    def __init__(
        self,
        mount: CameraMount,
        send_command_long: Callable[[int, tuple[float, ...]], None],
        *,
        gimbal_device_id: int = 1,
    ) -> None:
        if not 0 <= gimbal_device_id <= 6:
            raise ValueError("gimbal_device_id must be between 0 and 6")
        self.mount = mount
        self.state = None
        self._gimbal_device_id = gimbal_device_id
        self._send = send_command_long

    def command_pitch_yaw(
        self,
        pitch_rad: float,
        yaw_rad: float,
        *,
        mode: str | None = None,
        vehicle_yaw_rad: float = 0.0,
    ) -> None:
        pitch = min(self.mount.limits.pitch_max_rad, max(self.mount.limits.pitch_min_rad, float(pitch_rad)))
        selected_mode = self.mount.stabilization if mode is None else mode
        if selected_mode not in {"lock-horizon", "follow-yaw", "follow-body"}:
            raise ValueError("unsupported gimbal mode")
        relative_yaw = (float(yaw_rad) - vehicle_yaw_rad + math.pi) % (2 * math.pi) - math.pi
        yaw = min(self.mount.limits.yaw_max_rad, max(self.mount.limits.yaw_min_rad, relative_yaw))
        flags = {"lock-horizon": 44, "follow-yaw": 40, "follow-body": 32}[selected_mode]
        self._send(
            MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
            (
                -math.degrees(pitch),
                math.degrees(yaw),
                math.nan,
                math.nan,
                float(flags),
                0.0,
                float(self._gimbal_device_id),
            ),
        )


def parse_gimbal_device_attitude_status(message: Any) -> GimbalState | None:
    """Parse MAVLink feedback into CameraMount's positive-down convention.

    MAVLink's Euler pitch is positive up.  Keep the quaternion rotation matrix
    as the source of truth so a nadir crossing cannot lose orientation through
    the standard [-90, 90] Euler pitch representation.
    """
    getter = message.get if isinstance(message, dict) else lambda key, default=None: getattr(message, key, default)
    try:
        q = tuple(float(getter(f"q{index}")) for index in range(4))
    except (TypeError, ValueError):
        raw = getter("q")
        if raw is None:
            return None
        try:
            q = tuple(float(value) for value in raw)
        except (TypeError, ValueError):
            return None
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        return None
    norm = math.sqrt(sum(value * value for value in q))
    if norm <= 1e-9:
        return None
    w, x, y, z = (value / norm for value in q)
    rotation = (
        1.0 - 2.0 * (y * y + z * z),
        2.0 * (x * y - z * w),
        2.0 * (x * z + y * w),
        2.0 * (x * y + z * w),
        1.0 - 2.0 * (x * x + z * z),
        2.0 * (y * z - x * w),
        2.0 * (x * z - y * w),
        2.0 * (y * z + x * w),
        1.0 - 2.0 * (x * x + y * y),
    )
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch_mavlink = math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x))))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    stamp = getter("time_boot_ms")
    flags = getter("flags", getter("device_flags", 0))
    try:
        flags = int(flags or 0)
    except (TypeError, ValueError):
        flags = 0
    if flags & GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME:
        yaw_frame = "world"
    elif flags & GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME:
        yaw_frame = "body"
    else:
        yaw_frame = "unknown"
    return GimbalState(
        -pitch_mavlink,
        yaw,
        roll,
        int(stamp) * 1000 if isinstance(stamp, (int, float)) else None,
        rotation,
        yaw_frame,
    )


__all__ = [
    "FixedGimbalController",
    "GIMBAL_DEVICE_ATTITUDE_STATUS",
    "GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME",
    "GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME",
    "GimbalController",
    "GimbalState",
    "MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW",
    "Px4GimbalController",
    "SimGimbalController",
    "parse_gimbal_device_attitude_status",
]

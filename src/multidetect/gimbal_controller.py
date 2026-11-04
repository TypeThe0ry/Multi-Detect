"""Camera gimbal command boundary for SIM and PX4 runtimes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from multidetect.camera_mount import CameraMount

MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW = 1000
MAVLINK_MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS = 285
GIMBAL_DEVICE_ATTITUDE_STATUS = MAVLINK_MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS


@dataclass(frozen=True, slots=True)
class GimbalState:
    pitch_rad: float
    yaw_rad: float
    roll_rad: float = 0.0
    timestamp_us: int | None = None


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
        gimbal_device_id: int = 0,
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
        relative_yaw = float(yaw_rad) if selected_mode == "lock-horizon" else float(yaw_rad) - vehicle_yaw_rad
        yaw = min(self.mount.limits.yaw_max_rad, max(self.mount.limits.yaw_min_rad, relative_yaw))
        flags = {"lock-horizon": 24, "follow-yaw": 8, "follow-body": 0}[selected_mode]
        self._send(
            MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
            (
                -math.degrees(pitch),
                math.degrees(yaw),
                math.nan if self.mount.rate_rad_s is None else math.degrees(self.mount.rate_rad_s),
                math.nan if self.mount.rate_rad_s is None else math.degrees(self.mount.rate_rad_s),
                float(flags),
                float(self._gimbal_device_id),
                0.0,
            ),
        )


def parse_gimbal_device_attitude_status(message: Any) -> GimbalState | None:
    """Parse MAVLink GIMBAL_DEVICE_ATTITUDE_STATUS quaternion feedback."""
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
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x))))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    stamp = getter("time_boot_ms")
    return GimbalState(pitch, yaw, roll, int(stamp) * 1000 if isinstance(stamp, (int, float)) else None)


__all__ = [
    "FixedGimbalController",
    "GIMBAL_DEVICE_ATTITUDE_STATUS",
    "GimbalController",
    "GimbalState",
    "MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW",
    "Px4GimbalController",
    "SimGimbalController",
    "parse_gimbal_device_attitude_status",
]

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FixedWingTelemetryHilConfig:
    """Deterministic fixed-wing telemetry used only for software/HIL reception tests."""

    endpoint: str = "udpout:127.0.0.1:14550"
    system_id: int = 1
    component_id: int = 1
    rate_hz: float = 10.0
    latitude_deg: float = 31.123456
    longitude_deg: float = 121.654321
    altitude_agl_m: float = 42.5
    roll_deg: float = 1.2
    pitch_deg: float = -0.8
    heading_deg: float = 90.0
    ground_speed_mps: float = 17.0
    battery_remaining_pct: int = 81
    satellites_visible: int = 18
    mission_sequence: int = 3
    armed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("HIL telemetry endpoint cannot be empty")
        for name, value in (("system ID", self.system_id), ("component ID", self.component_id)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"HIL telemetry {name} must be in [1, 255]")
        if not math.isfinite(self.rate_hz) or self.rate_hz <= 0:
            raise ValueError("HIL telemetry rate must be finite and positive")
        if not math.isfinite(self.latitude_deg) or not -90 <= self.latitude_deg <= 90:
            raise ValueError("HIL telemetry latitude is invalid")
        if not math.isfinite(self.longitude_deg) or not -180 <= self.longitude_deg <= 180:
            raise ValueError("HIL telemetry longitude is invalid")
        for name, value in (
            ("altitude", self.altitude_agl_m),
            ("ground speed", self.ground_speed_mps),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"HIL telemetry {name} must be finite and non-negative")
        for name, value in (("roll", self.roll_deg), ("pitch", self.pitch_deg)):
            if not math.isfinite(value):
                raise ValueError(f"HIL telemetry {name} must be finite")
        if not math.isfinite(self.heading_deg) or not 0 <= self.heading_deg < 360:
            raise ValueError("HIL telemetry heading must be in [0, 360)")
        if (
            isinstance(self.battery_remaining_pct, bool)
            or not isinstance(self.battery_remaining_pct, int)
            or not 0 <= self.battery_remaining_pct <= 100
        ):
            raise ValueError("HIL telemetry battery percentage must be in [0, 100]")
        if (
            isinstance(self.satellites_visible, bool)
            or not isinstance(self.satellites_visible, int)
            or not 0 <= self.satellites_visible <= 254
        ):
            raise ValueError("HIL telemetry satellite count must be in [0, 254]")
        if (
            isinstance(self.mission_sequence, bool)
            or not isinstance(self.mission_sequence, int)
            or self.mission_sequence < 0
        ):
            raise ValueError("HIL telemetry mission sequence must be non-negative")
        if not isinstance(self.armed, bool):
            raise ValueError("HIL telemetry armed state must be boolean")


class FixedWingTelemetryHilEmitter:
    """Emits telemetry-only MAVLink frames to exercise the read-only receiver path."""

    messages_per_cycle = 6

    def __init__(
        self,
        config: FixedWingTelemetryHilConfig,
        *,
        connection: Any | None = None,
    ) -> None:
        self.config = config
        self._connection = connection
        self._owns_connection = connection is None
        self._cycle_count = 0

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def message_count(self) -> int:
        return self._cycle_count * self.messages_per_cycle

    def connect(self) -> None:
        if self._connection is not None:
            return
        try:
            from pymavlink import mavutil
        except ImportError as exc:  # pragma: no cover - optional dependency boundary.
            raise RuntimeError("Install the Pixhawk extra: pip install -e '.[pixhawk]'") from exc
        self._connection = mavutil.mavlink_connection(
            self.config.endpoint,
            source_system=self.config.system_id,
            source_component=self.config.component_id,
        )

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if self._owns_connection and connection is not None:
            connection.close()

    def emit_cycle(self, *, elapsed_s: float) -> None:
        if not math.isfinite(elapsed_s) or elapsed_s < 0:
            raise ValueError("HIL telemetry elapsed time must be finite and non-negative")
        self.connect()
        connection = self._connection
        if connection is None:
            raise RuntimeError("HIL telemetry connection failed to initialize")
        try:
            from pymavlink import mavutil
        except ImportError as exc:  # pragma: no cover - optional dependency boundary.
            raise RuntimeError("Install the Pixhawk extra: pip install -e '.[pixhawk]'") from exc

        boot_ms = min(int(elapsed_s * 1_000), 0xFFFFFFFF)
        epoch_us = time.time_ns() // 1_000
        base_mode = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if self.config.armed:
            base_mode |= mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        latitude = round(self.config.latitude_deg * 10_000_000)
        longitude = round(self.config.longitude_deg * 10_000_000)
        relative_altitude = round(self.config.altitude_agl_m * 1_000)
        velocity_x = round(self.config.ground_speed_mps * 100)
        heading = round(self.config.heading_deg * 100)
        mav = connection.mav
        mav.heartbeat_send(
            mavutil.mavlink.MAV_TYPE_FIXED_WING,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base_mode,
            10,  # ArduPlane custom mode 10 = AUTO, used only as incoming HIL telemetry.
            mavutil.mavlink.MAV_STATE_ACTIVE,
        )
        mav.attitude_send(
            boot_ms,
            math.radians(self.config.roll_deg),
            math.radians(self.config.pitch_deg),
            math.radians(self.config.heading_deg),
            0.0,
            0.0,
            0.0,
        )
        mav.global_position_int_send(
            boot_ms,
            latitude,
            longitude,
            relative_altitude,
            relative_altitude,
            velocity_x,
            0,
            0,
            heading,
        )
        mav.sys_status_send(
            0,
            0,
            0,
            250,
            16_000,
            -1,
            self.config.battery_remaining_pct,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        mav.gps_raw_int_send(
            epoch_us,
            3,
            latitude,
            longitude,
            relative_altitude,
            80,
            120,
            velocity_x,
            heading,
            self.config.satellites_visible,
        )
        mav.mission_current_send(self.config.mission_sequence)
        self._cycle_count += 1

    def run(self, *, duration_s: float) -> tuple[int, int]:
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("HIL telemetry duration must be finite and positive")
        started_s = time.monotonic()
        next_cycle_s = started_s
        interval_s = 1.0 / self.config.rate_hz
        try:
            while True:
                now_s = time.monotonic()
                if now_s - started_s >= duration_s:
                    break
                if now_s < next_cycle_s:
                    time.sleep(min(next_cycle_s - now_s, interval_s))
                    continue
                self.emit_cycle(elapsed_s=now_s - started_s)
                next_cycle_s += interval_s
        finally:
            self.close()
        return self.cycle_count, self.message_count


__all__ = ["FixedWingTelemetryHilConfig", "FixedWingTelemetryHilEmitter"]

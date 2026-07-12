from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .domain import VehicleTelemetry


class PixhawkDependencyError(RuntimeError):
    """Raised when optional pymavlink support is unavailable."""


@dataclass(frozen=True, slots=True)
class PixhawkReadOnlyConfig:
    """Read-only MAVLink connection settings for a Pixhawk telemetry link."""

    endpoint: str
    baud: int = 57_600
    stale_after_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise ValueError("Pixhawk endpoint cannot be empty")
        if self.baud <= 0 or self.stale_after_seconds <= 0:
            raise ValueError("Pixhawk baud and stale timeout must be positive")


class PixhawkReadOnlyTelemetryProvider:
    """Consumes MAVLink telemetry without transmitting commands or parameters.

    Unknown operational predicates intentionally remain ``None``. In particular,
    this class does not infer a deployment zone, geofence approval, flight-mode
    permission, or a clear release zone from generic MAVLink messages.
    """

    def __init__(self, config: PixhawkReadOnlyConfig) -> None:
        self.config = config
        self._connection: Any | None = None
        self._last_heartbeat_s: float | None = None
        self._last_position_s: float | None = None
        self._altitude_agl_m = float("nan")
        self._roll_deg = float("nan")
        self._pitch_deg = float("nan")
        self._ground_speed_mps = float("nan")
        self._latitude_deg = float("nan")
        self._longitude_deg = float("nan")
        self._heading_deg = float("nan")
        self._battery_remaining_pct = float("nan")
        self._satellites_visible: int | None = None
        self._armed: bool | None = None
        self._flight_mode: str | None = None
        self._mission_sequence: int | None = None

    @property
    def is_read_only(self) -> bool:
        return True

    def connect(self) -> None:
        if self._connection is not None:
            return
        try:
            from pymavlink import mavutil
        except ImportError as exc:  # pragma: no cover - exercised on dependency-free installs.
            raise PixhawkDependencyError(
                "Install the optional Pixhawk dependency: pip install -e '.[pixhawk]'"
            ) from exc
        # No heartbeat, parameter request, command, mission, actuator or stream-rate
        # message is sent here. The provider only opens the transport and receives data.
        self._connection = mavutil.mavlink_connection(
            self.config.endpoint,
            baud=self.config.baud,
            autoreconnect=True,
        )

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")
        self.connect()
        assert self._connection is not None
        for _ in range(64):
            message = self._connection.recv_match(blocking=False)
            if message is None:
                break
            self.ingest_message(message, received_at_s=now_s)
        connection_mode = getattr(self._connection, "flightmode", None)
        if isinstance(connection_mode, str) and connection_mode.strip():
            self._flight_mode = connection_mode.strip()
        return VehicleTelemetry(
            altitude_agl_m=self._altitude_agl_m,
            roll_deg=self._roll_deg,
            pitch_deg=self._pitch_deg,
            ground_speed_mps=self._ground_speed_mps,
            in_allowed_zone=None,
            geofence_healthy=None,
            position_healthy=self._is_fresh(self._last_position_s, now_s),
            link_healthy=self._is_fresh(self._last_heartbeat_s, now_s),
            flight_mode_allows_deploy=None,
            release_zone_clear=None,
            person_detector_healthy=None,
            latitude_deg=self._latitude_deg,
            longitude_deg=self._longitude_deg,
            heading_deg=self._heading_deg,
            battery_remaining_pct=self._battery_remaining_pct,
            satellites_visible=self._satellites_visible,
            armed=self._armed,
            flight_mode=self._flight_mode,
            mission_sequence=self._mission_sequence,
        )

    def ingest_message(self, message: Any, *, received_at_s: float | None = None) -> None:
        """Update the cache from a MAVLink-shaped message; useful for deterministic tests."""

        now_s = time.monotonic() if received_at_s is None else received_at_s
        message_type = (
            message.get_type() if hasattr(message, "get_type") else type(message).__name__
        )
        if message_type == "HEARTBEAT":
            self._last_heartbeat_s = now_s
            base_mode = getattr(message, "base_mode", None)
            if base_mode is not None:
                self._armed = bool(int(base_mode) & 128)
        elif message_type == "ATTITUDE":
            self._roll_deg = math.degrees(float(message.roll))
            self._pitch_deg = math.degrees(float(message.pitch))
        elif message_type == "GLOBAL_POSITION_INT":
            self._altitude_agl_m = float(message.relative_alt) / 1_000.0
            self._ground_speed_mps = math.hypot(float(message.vx), float(message.vy)) / 100.0
            latitude = getattr(message, "lat", None)
            longitude = getattr(message, "lon", None)
            heading = getattr(message, "hdg", None)
            if latitude is not None and longitude is not None:
                self._latitude_deg = float(latitude) / 10_000_000.0
                self._longitude_deg = float(longitude) / 10_000_000.0
            if heading is not None and int(heading) != 65_535:
                self._heading_deg = float(heading) / 100.0
            self._last_position_s = now_s
        elif message_type == "SYS_STATUS":
            remaining = getattr(message, "battery_remaining", None)
            if remaining is not None and int(remaining) >= 0:
                self._battery_remaining_pct = float(remaining)
        elif message_type == "GPS_RAW_INT":
            satellites = getattr(message, "satellites_visible", None)
            if satellites is not None and int(satellites) != 255:
                self._satellites_visible = int(satellites)
        elif message_type == "MISSION_CURRENT":
            sequence = getattr(message, "seq", None)
            if sequence is not None and int(sequence) >= 0:
                self._mission_sequence = int(sequence)

    def _is_fresh(self, timestamp_s: float | None, now_s: float) -> bool | None:
        if timestamp_s is None:
            return None
        return now_s - timestamp_s <= self.config.stale_after_seconds

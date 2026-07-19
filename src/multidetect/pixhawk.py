from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import VehicleTelemetry


class PixhawkDependencyError(RuntimeError):
    """Raised when optional pymavlink support is unavailable."""


class PixhawkDiscoveryError(RuntimeError):
    """Raised when an automatic serial selection would be absent or ambiguous."""


PIXHAWK_AUTOPILOT_IDS = {
    "ardupilot": 3,
    "px4": 12,
}
PIXHAWK_VEHICLE_TYPE_IDS = {
    "fixed_wing": 1,
}
_AUTOPILOT_NAMES = {
    0: "MAV_AUTOPILOT_GENERIC",
    3: "MAV_AUTOPILOT_ARDUPILOTMEGA",
    8: "MAV_AUTOPILOT_INVALID",
    12: "MAV_AUTOPILOT_PX4",
}
_VEHICLE_TYPE_NAMES = {
    0: "MAV_TYPE_GENERIC",
    1: "MAV_TYPE_FIXED_WING",
    2: "MAV_TYPE_QUADROTOR",
    6: "MAV_TYPE_GCS",
    18: "MAV_TYPE_ONBOARD_CONTROLLER",
}
_SYSTEM_STATUS_NAMES = {
    0: "MAV_STATE_UNINIT",
    1: "MAV_STATE_BOOT",
    2: "MAV_STATE_CALIBRATING",
    3: "MAV_STATE_STANDBY",
    4: "MAV_STATE_ACTIVE",
    5: "MAV_STATE_CRITICAL",
    6: "MAV_STATE_EMERGENCY",
    7: "MAV_STATE_POWEROFF",
    8: "MAV_STATE_FLIGHT_TERMINATION",
}
_OPERATIONAL_SYSTEM_STATUSES = frozenset({3, 4})


@dataclass(frozen=True, slots=True)
class PixhawkHeartbeatIdentity:
    source_system_id: int | None = None
    source_component_id: int | None = None
    autopilot_id: int | None = None
    vehicle_type_id: int | None = None
    system_status_id: int | None = None
    mavlink_version: int | None = None

    def to_document(self) -> dict[str, int | str | None]:
        return {
            "source_system_id": self.source_system_id,
            "source_component_id": self.source_component_id,
            "autopilot_id": self.autopilot_id,
            "autopilot_name": _enum_name(_AUTOPILOT_NAMES, self.autopilot_id, "MAV_AUTOPILOT"),
            "vehicle_type_id": self.vehicle_type_id,
            "vehicle_type_name": _enum_name(
                _VEHICLE_TYPE_NAMES,
                self.vehicle_type_id,
                "MAV_TYPE",
            ),
            "system_status_id": self.system_status_id,
            "system_status_name": _enum_name(
                _SYSTEM_STATUS_NAMES,
                self.system_status_id,
                "MAV_STATE",
            ),
            "mavlink_version": self.mavlink_version,
        }


@dataclass(frozen=True, slots=True)
class PixhawkQualification:
    required: bool
    passed: bool | None
    reasons: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "required": self.required,
            "passed": self.passed,
            "reasons": self.reasons,
        }


@dataclass(frozen=True, slots=True)
class PixhawkRcInputSnapshot:
    """One timestamped receiver input sample normalized to 18 PWM channels."""

    observed_at_s: float
    channels_pwm: tuple[int | None, ...]

    def __post_init__(self) -> None:
        if not math.isfinite(self.observed_at_s) or self.observed_at_s < 0.0:
            raise ValueError("RC input timestamp must be finite and non-negative")
        if len(self.channels_pwm) != 18:
            raise ValueError("RC input snapshot must contain exactly 18 channels")
        valid = tuple(value for value in self.channels_pwm if value is not None)
        if not valid:
            raise ValueError("RC input snapshot must contain at least one valid channel")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 800 <= value <= 2200
            for value in valid
        ):
            raise ValueError("RC input PWM values must be integers in [800, 2200]")

    @property
    def valid_channel_count(self) -> int:
        return sum(value is not None for value in self.channels_pwm)


def resolve_pixhawk_endpoint(
    endpoint: str,
    *,
    by_id_dir: Path = Path("/dev/serial/by-id"),
    device_dir: Path = Path("/dev"),
) -> str:
    """Resolve ``auto`` without guessing among multiple flight/serial devices."""

    if endpoint != "auto":
        return endpoint
    stable = sorted(path for path in by_id_dir.glob("*") if path.is_file() or path.is_symlink())
    identifiers = ("pixhawk", "px4", "ardupilot", "cube", "fmu")
    preferred = [
        path for path in stable if any(token in path.name.lower() for token in identifiers)
    ]
    if len(preferred) == 1:
        return str(preferred[0])
    if len(preferred) > 1:
        raise PixhawkDiscoveryError(
            "multiple Pixhawk-like /dev/serial/by-id devices found; specify one explicitly"
        )
    acm_devices = sorted(path for path in device_dir.glob("ttyACM*") if path.exists())
    if len(acm_devices) == 1:
        return str(acm_devices[0])
    if len(acm_devices) > 1:
        raise PixhawkDiscoveryError(
            "multiple /dev/ttyACM devices found; use a stable /dev/serial/by-id endpoint"
        )
    raise PixhawkDiscoveryError(
        "no Pixhawk serial device found; connect the V6X by USB or specify a TELEM endpoint"
    )


@dataclass(frozen=True, slots=True)
class PixhawkReadOnlyConfig:
    """Read-only MAVLink connection settings for a Pixhawk telemetry link."""

    endpoint: str
    baud: int = 57_600
    stale_after_seconds: float = 1.0
    expected_system_id: int | None = None
    expected_autopilot_id: int | None = None
    expected_vehicle_type_id: int | None = None
    require_operational_state: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("Pixhawk endpoint cannot be empty")
        if isinstance(self.baud, bool) or not isinstance(self.baud, int) or self.baud <= 0:
            raise ValueError("Pixhawk baud must be a positive integer")
        if (
            isinstance(self.stale_after_seconds, bool)
            or not math.isfinite(self.stale_after_seconds)
            or self.stale_after_seconds <= 0
        ):
            raise ValueError("Pixhawk stale timeout must be finite and positive")
        for name, value in (
            ("expected system ID", self.expected_system_id),
            ("expected autopilot ID", self.expected_autopilot_id),
            ("expected vehicle type ID", self.expected_vehicle_type_id),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
            ):
                raise ValueError(f"Pixhawk {name} must be an integer in [0, 255]")
        if self.expected_system_id == 0:
            raise ValueError("Pixhawk expected system ID must be in [1, 255]")
        if not isinstance(self.require_operational_state, bool):
            raise ValueError("Pixhawk operational-state requirement must be boolean")

    @property
    def qualification_required(self) -> bool:
        return (
            any(
                value is not None
                for value in (
                    self.expected_system_id,
                    self.expected_autopilot_id,
                    self.expected_vehicle_type_id,
                )
            )
            or self.require_operational_state
        )


class PixhawkReadOnlyTelemetryProvider:
    """Consumes MAVLink telemetry without transmitting commands or parameters.

    Unknown operational predicates intentionally remain ``None``. In particular,
    this class does not infer a deployment zone, geofence approval, flight-mode
    permission, or a clear release zone from generic MAVLink messages.
    """

    def __init__(self, config: PixhawkReadOnlyConfig) -> None:
        self.config = config
        self._connection: Any | None = None
        self._resolved_endpoint: str | None = None
        self._last_heartbeat_s: float | None = None
        self._last_attitude_s: float | None = None
        self._last_position_s: float | None = None
        self._last_velocity_s: float | None = None
        self._last_airspeed_s: float | None = None
        self._last_wind_s: float | None = None
        self._altitude_agl_m = float("nan")
        self._roll_deg = float("nan")
        self._pitch_deg = float("nan")
        self._ground_speed_mps = float("nan")
        self._velocity_north_mps = float("nan")
        self._velocity_east_mps = float("nan")
        self._airspeed_mps = float("nan")
        self._wind_north_mps = float("nan")
        self._wind_east_mps = float("nan")
        self._latitude_deg = float("nan")
        self._longitude_deg = float("nan")
        self._heading_deg = float("nan")
        self._battery_remaining_pct = float("nan")
        self._satellites_visible: int | None = None
        self._armed: bool | None = None
        self._flight_mode: str | None = None
        self._mission_sequence: int | None = None
        self._heartbeat_identity = PixhawkHeartbeatIdentity()
        self._autopilot_system_id: int | None = None
        self._received_message_count = 0
        self._rejected_system_message_count = 0
        self._ignored_non_autopilot_heartbeat_count = 0
        self._message_type_counts: Counter[str] = Counter()
        self._rc_input_snapshot: PixhawkRcInputSnapshot | None = None

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def messages_transmitted(self) -> int:
        """The provider has no send path; expose the invariant for integration evidence."""

        return 0

    @property
    def resolved_endpoint(self) -> str | None:
        return self._resolved_endpoint

    @property
    def heartbeat_identity(self) -> PixhawkHeartbeatIdentity:
        return self._heartbeat_identity

    @property
    def messages_received(self) -> int:
        return self._received_message_count

    @property
    def rejected_system_messages(self) -> int:
        return self._rejected_system_message_count

    @property
    def ignored_non_autopilot_heartbeats(self) -> int:
        return self._ignored_non_autopilot_heartbeat_count

    @property
    def message_type_counts(self) -> dict[str, int]:
        return dict(sorted(self._message_type_counts.items()))

    @property
    def rc_input_snapshot(self) -> PixhawkRcInputSnapshot | None:
        return self._rc_input_snapshot

    @property
    def qualification(self) -> PixhawkQualification:
        if not self.config.qualification_required:
            return PixhawkQualification(required=False, passed=None, reasons=())
        identity = self._heartbeat_identity
        failures: list[str] = []
        pending: list[str] = []
        for expected, actual, label in (
            (self.config.expected_system_id, identity.source_system_id, "system ID"),
            (self.config.expected_autopilot_id, identity.autopilot_id, "autopilot"),
            (self.config.expected_vehicle_type_id, identity.vehicle_type_id, "vehicle type"),
        ):
            if expected is None:
                continue
            if actual is None:
                pending.append(f"{label} has not been observed")
            elif actual != expected:
                failures.append(f"{label} mismatch: expected={expected}, actual={actual}")
        if self.config.require_operational_state:
            status = identity.system_status_id
            if status is None:
                pending.append("system status has not been observed")
            elif status not in _OPERATIONAL_SYSTEM_STATUSES:
                failures.append(
                    "system status is not operational: "
                    f"{_enum_name(_SYSTEM_STATUS_NAMES, status, 'MAV_STATE')}"
                )
        if failures:
            return PixhawkQualification(True, False, tuple((*failures, *pending)))
        if pending:
            return PixhawkQualification(True, None, tuple(pending))
        return PixhawkQualification(True, True, ())

    def transport_link_healthy(self, *, now_s: float) -> bool | None:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")
        return self._is_fresh(self._last_heartbeat_s, now_s)

    def diagnostics(self, *, now_s: float) -> dict[str, object]:
        """Return a cached, receive-only health report without opening the transport."""

        snapshot = self.cached_snapshot(now_s=now_s)
        rc_input = self.rc_input_snapshot
        return {
            "configured_endpoint": self.config.endpoint,
            "resolved_endpoint": self.resolved_endpoint,
            "read_only": self.is_read_only,
            "hardware_control_enabled": False,
            "messages_received": self.messages_received,
            "messages_transmitted": self.messages_transmitted,
            "rejected_system_messages": self.rejected_system_messages,
            "ignored_non_autopilot_heartbeats": self.ignored_non_autopilot_heartbeats,
            "message_type_counts": self.message_type_counts,
            "rc_input_observed": rc_input is not None,
            "rc_input_age_s": (
                max(0.0, now_s - rc_input.observed_at_s) if rc_input is not None else None
            ),
            "rc_valid_channel_count": (
                rc_input.valid_channel_count if rc_input is not None else 0
            ),
            "transport_link_healthy": self.transport_link_healthy(now_s=now_s),
            "qualified_link_healthy": snapshot.link_healthy,
            "position_healthy": snapshot.position_healthy,
            "heartbeat_identity": self.heartbeat_identity.to_document(),
            "qualification": self.qualification.to_document(),
        }

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
        endpoint = resolve_pixhawk_endpoint(self.config.endpoint)
        self._connection = mavutil.mavlink_connection(
            endpoint,
            baud=self.config.baud,
            autoreconnect=True,
        )
        if self.config.expected_system_id is not None:
            self._connection.target_system = self.config.expected_system_id
        self._resolved_endpoint = endpoint

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")
        self.connect()
        connection = self._connection
        if connection is None:  # Defensive guard for optimized Python and unusual subclasses.
            raise RuntimeError("Pixhawk connection failed to initialize")
        for _ in range(64):
            message = connection.recv_match(blocking=False)
            if message is None:
                break
            self.ingest_message(message, received_at_s=now_s)
        connection_mode = getattr(connection, "flightmode", None)
        if isinstance(connection_mode, str) and connection_mode.strip():
            self._flight_mode = connection_mode.strip()
        return self.cached_snapshot(now_s=now_s)

    def cached_snapshot(self, *, now_s: float) -> VehicleTelemetry:
        """Return cached telemetry without opening, reconnecting, receiving, or transmitting."""

        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")
        transport_link_healthy = self.transport_link_healthy(now_s=now_s)
        qualification = self.qualification
        link_healthy = transport_link_healthy
        if self.config.qualification_required and transport_link_healthy is True:
            link_healthy = qualification.passed is True
        return VehicleTelemetry(
            altitude_agl_m=self._altitude_agl_m,
            roll_deg=self._roll_deg,
            pitch_deg=self._pitch_deg,
            ground_speed_mps=self._ground_speed_mps,
            in_allowed_zone=None,
            geofence_healthy=None,
            position_healthy=self._is_fresh(self._last_position_s, now_s),
            link_healthy=link_healthy,
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
            attitude_observed_at_s=(
                self._last_attitude_s if self._last_attitude_s is not None else float("nan")
            ),
            position_observed_at_s=(
                self._last_position_s if self._last_position_s is not None else float("nan")
            ),
            velocity_north_mps=self._velocity_north_mps,
            velocity_east_mps=self._velocity_east_mps,
            airspeed_mps=self._airspeed_mps,
            wind_north_mps=self._wind_north_mps,
            wind_east_mps=self._wind_east_mps,
            velocity_observed_at_s=(
                self._last_velocity_s if self._last_velocity_s is not None else float("nan")
            ),
            airspeed_observed_at_s=(
                self._last_airspeed_s if self._last_airspeed_s is not None else float("nan")
            ),
            wind_observed_at_s=(
                self._last_wind_s if self._last_wind_s is not None else float("nan")
            ),
        )

    def ingest_message(self, message: Any, *, received_at_s: float | None = None) -> None:
        """Update the cache from a MAVLink-shaped message; useful for deterministic tests."""

        now_s = time.monotonic() if received_at_s is None else received_at_s
        message_type = (
            message.get_type() if hasattr(message, "get_type") else type(message).__name__
        )
        self._received_message_count += 1
        self._message_type_counts[message_type] += 1
        source_system_id = _message_source_id(message, "get_srcSystem", "srcSystem")
        source_component_id = _message_source_id(message, "get_srcComponent", "srcComponent")
        expected_system_id = self.config.expected_system_id or self._autopilot_system_id
        if (
            expected_system_id is not None
            and source_system_id is not None
            and source_system_id != expected_system_id
        ):
            self._rejected_system_message_count += 1
            return
        if message_type == "HEARTBEAT":
            autopilot_id = _optional_int(getattr(message, "autopilot", None))
            vehicle_type_id = _optional_int(getattr(message, "type", None))
            if autopilot_id == 8 or vehicle_type_id in {6, 18}:
                self._ignored_non_autopilot_heartbeat_count += 1
                return
            if source_system_id is not None:
                self._autopilot_system_id = source_system_id
            self._heartbeat_identity = PixhawkHeartbeatIdentity(
                source_system_id=source_system_id,
                source_component_id=source_component_id,
                autopilot_id=autopilot_id,
                vehicle_type_id=vehicle_type_id,
                system_status_id=_optional_int(getattr(message, "system_status", None)),
                mavlink_version=_optional_int(getattr(message, "mavlink_version", None)),
            )
            self._last_heartbeat_s = now_s
            base_mode = getattr(message, "base_mode", None)
            if base_mode is not None:
                self._armed = bool(int(base_mode) & 128)
        elif message_type == "ATTITUDE":
            self._roll_deg = math.degrees(float(message.roll))
            self._pitch_deg = math.degrees(float(message.pitch))
            yaw = getattr(message, "yaw", None)
            if yaw is not None and math.isfinite(float(yaw)):
                # GLOBAL_POSITION_INT.hdg is commonly unavailable indoors. The
                # attitude quaternion/Euler stream still carries the estimator's
                # yaw, which is the correct fallback for camera-ray projection.
                self._heading_deg = math.degrees(float(yaw)) % 360.0
            self._last_attitude_s = now_s
        elif message_type == "GLOBAL_POSITION_INT":
            self._altitude_agl_m = float(message.relative_alt) / 1_000.0
            self._ground_speed_mps = math.hypot(float(message.vx), float(message.vy)) / 100.0
            self._velocity_north_mps = float(message.vx) / 100.0
            self._velocity_east_mps = float(message.vy) / 100.0
            latitude = getattr(message, "lat", None)
            longitude = getattr(message, "lon", None)
            heading = getattr(message, "hdg", None)
            if latitude is not None and longitude is not None:
                self._latitude_deg = float(latitude) / 10_000_000.0
                self._longitude_deg = float(longitude) / 10_000_000.0
            if heading is not None and int(heading) != 65_535:
                self._heading_deg = float(heading) / 100.0
            self._last_position_s = now_s
            self._last_velocity_s = now_s
        elif message_type == "VFR_HUD":
            airspeed = getattr(message, "airspeed", None)
            if airspeed is not None and math.isfinite(float(airspeed)) and float(airspeed) >= 0.0:
                self._airspeed_mps = float(airspeed)
                self._last_airspeed_s = now_s
        elif message_type == "WIND_COV":
            wind_north = getattr(message, "wind_x", None)
            wind_east = getattr(message, "wind_y", None)
            if wind_north is not None and wind_east is not None:
                north = float(wind_north)
                east = float(wind_east)
                if math.isfinite(north) and math.isfinite(east):
                    self._wind_north_mps = north
                    self._wind_east_mps = east
                    self._last_wind_s = now_s
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
        elif message_type == "RC_CHANNELS":
            channel_count = _optional_int(getattr(message, "chancount", None))
            channels = tuple(
                _rc_pwm_value(getattr(message, f"chan{index}_raw", None))
                if channel_count is None or index <= channel_count
                else None
                for index in range(1, 19)
            )
            if any(value is not None for value in channels):
                self._rc_input_snapshot = PixhawkRcInputSnapshot(now_s, channels)
        elif message_type == "RC_CHANNELS_RAW":
            port = _optional_int(getattr(message, "port", None))
            if port is not None and 0 <= port <= 2:
                channels = list(
                    self._rc_input_snapshot.channels_pwm
                    if self._rc_input_snapshot is not None
                    else (None,) * 18
                )
                offset = port * 8
                for index in range(1, 9):
                    target_index = offset + index - 1
                    if target_index >= len(channels):
                        break
                    value = _rc_pwm_value(getattr(message, f"chan{index}_raw", None))
                    if value is not None:
                        channels[target_index] = value
                if any(value is not None for value in channels):
                    self._rc_input_snapshot = PixhawkRcInputSnapshot(now_s, tuple(channels))

    def _is_fresh(self, timestamp_s: float | None, now_s: float) -> bool | None:
        if timestamp_s is None:
            return None
        return now_s - timestamp_s <= self.config.stale_after_seconds


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _rc_pwm_value(value: object) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed is not None and 800 <= parsed <= 2200 else None


def _message_source_id(message: Any, method_name: str, header_name: str) -> int | None:
    method = getattr(message, method_name, None)
    if callable(method):
        try:
            return _optional_int(method())
        except (AttributeError, TypeError, ValueError):
            return None
    header = getattr(message, "_header", None)
    return _optional_int(getattr(header, header_name, None))


def _enum_name(mapping: dict[int, str], value: int | None, prefix: str) -> str | None:
    if value is None:
        return None
    return mapping.get(value, f"{prefix}_UNKNOWN_{value}")


__all__ = [
    "PIXHAWK_AUTOPILOT_IDS",
    "PIXHAWK_VEHICLE_TYPE_IDS",
    "PixhawkDependencyError",
    "PixhawkDiscoveryError",
    "PixhawkHeartbeatIdentity",
    "PixhawkQualification",
    "PixhawkRcInputSnapshot",
    "PixhawkReadOnlyConfig",
    "PixhawkReadOnlyTelemetryProvider",
    "resolve_pixhawk_endpoint",
]

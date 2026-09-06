from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import VehicleTelemetry
from .mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    normalize_gps_fix_type,
    normalize_satellites_visible,
)
from .px4_mission_feedback import (
    Px4MissionFeedbackSnapshot,
    Px4MissionFeedbackTracker,
)


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
_FIRMWARE_VERSION_TYPE_NAMES = {
    0: "development",
    64: "alpha",
    128: "beta",
    192: "release_candidate",
    255: "official",
}
_OPERATIONAL_SYSTEM_STATUSES = frozenset({3, 4})
_AUTOPILOT_VERSION_IDENTITY_DOMAIN = b"multidetect:autopilot-version-identity:v1\x00"
_DEVICE_UID_HASH_DOMAIN = b"multidetect:autopilot-device-uid:v1\x00"

# MAV_CMD_SET_MESSAGE_INTERVAL only changes this companion-computer link's
# telemetry cadence.  It never changes flight mode, parameters or actuators.
_MAV_CMD_SET_MESSAGE_INTERVAL = 511
_MAV_CMD_REQUEST_MESSAGE = 512
_AUTOPILOT_VERSION_MESSAGE_ID = 148
_RANGING_STREAM_INTERVALS_US: tuple[tuple[str, int, int], ...] = (
    ("ATTITUDE", 30, 50_000),
    ("ODOMETRY", 331, 50_000),
    ("SCALED_PRESSURE", 29, 100_000),
    ("LOCAL_POSITION_NED", 32, 66_667),
    ("GLOBAL_POSITION_INT", 33, 100_000),
    ("ALTITUDE", 141, 100_000),
    ("HOME_POSITION", 242, 1_000_000),
)

# Wire-domain sentinels for ``GLOBAL_POSITION_INT``.  pymavlink exposes these
# fields as Python numbers, so relying on the type alone would turn an unknown
# value (for example INT32_MAX) into a plausible-looking latitude, altitude or
# velocity.  Keep the bounds local to this adapter: they describe the MAVLink
# wire representation, not the wider ranges accepted by the domain models.
_MAVLINK_INT32_MIN = -(1 << 31)
_MAVLINK_INT32_MAX = (1 << 31) - 1
_MAVLINK_INT16_MIN = -(1 << 15)
_MAVLINK_INT16_MAX = (1 << 15) - 1
_MAVLINK_UINT16_MAX = (1 << 16) - 1
_MAVLINK_HEADING_MAX_CDEG = 35_999
_MAV_FRAME_LOCAL_NED = 1
_MAV_FRAME_BODY_FRD = 12
_ATTITUDE_UNCERTAINTY_MAX_ASSOCIATION_S = 0.250
_MAVLINK_ODOMETRY_ATTITUDE_UNCERTAINTY_SOURCE = (
    "mavlink_odometry_pose_covariance_local_ned_with_configured_floor"
)
_MINIMUM_ROLL_SIGMA_DEG = 0.3
_MINIMUM_PITCH_SIGMA_DEG = 0.3
_MINIMUM_HEADING_SIGMA_DEG = 1.0


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
class PixhawkAutopilotVersionIdentity:
    """Sanitized identity derived from one complete MAVLink AUTOPILOT_VERSION frame.

    The wire message can contain a board-unique UID.  This object deliberately
    retains only a domain-separated SHA-256 digest of that value, never the raw
    UID bytes or integer.
    """

    source_system_id: int
    source_component_id: int
    capabilities: int
    flight_sw_version: int
    middleware_sw_version: int
    os_sw_version: int
    board_version: int
    flight_custom_version_hex: str
    middleware_custom_version_hex: str
    os_custom_version_hex: str
    vendor_id: int
    product_id: int
    device_uid_source: str
    device_uid_hash: str | None

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("source_system_id", self.source_system_id, 1, 255),
            ("source_component_id", self.source_component_id, 0, 255),
            ("capabilities", self.capabilities, 0, (1 << 64) - 1),
            ("flight_sw_version", self.flight_sw_version, 0, (1 << 32) - 1),
            ("middleware_sw_version", self.middleware_sw_version, 0, (1 << 32) - 1),
            ("os_sw_version", self.os_sw_version, 0, (1 << 32) - 1),
            ("board_version", self.board_version, 0, (1 << 32) - 1),
            ("vendor_id", self.vendor_id, 0, (1 << 16) - 1),
            ("product_id", self.product_id, 0, (1 << 16) - 1),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
        for name, value in (
            ("flight_custom_version_hex", self.flight_custom_version_hex),
            ("middleware_custom_version_hex", self.middleware_custom_version_hex),
            ("os_custom_version_hex", self.os_custom_version_hex),
        ):
            if not _is_lower_hex(value, length=16):
                raise ValueError(f"{name} must contain exactly 8 lowercase hexadecimal bytes")
        if self.device_uid_source not in {"none", "uid", "uid2"}:
            raise ValueError("device_uid_source must be none, uid, or uid2")
        if self.device_uid_source == "none":
            if self.device_uid_hash is not None:
                raise ValueError("device_uid_hash must be absent when device_uid_source is none")
        elif not _is_lower_hex(self.device_uid_hash, length=64):
            raise ValueError("device_uid_hash must be a lowercase SHA-256 digest")

    def canonical_mapping(self) -> dict[str, object]:
        """Return the stable, privacy-preserving fields bound by ``identity_hash``."""

        return {
            "schema_version": 1,
            "source_system_id": self.source_system_id,
            "source_component_id": self.source_component_id,
            "capabilities": self.capabilities,
            "flight_sw_version": self.flight_sw_version,
            "middleware_sw_version": self.middleware_sw_version,
            "os_sw_version": self.os_sw_version,
            "board_version": self.board_version,
            "flight_custom_version_hex": self.flight_custom_version_hex,
            "middleware_custom_version_hex": self.middleware_custom_version_hex,
            "os_custom_version_hex": self.os_custom_version_hex,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "device_uid_source": self.device_uid_source,
            "device_uid_hash": self.device_uid_hash,
        }

    @property
    def identity_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_mapping(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(_AUTOPILOT_VERSION_IDENTITY_DOMAIN + encoded).hexdigest()

    def to_document(self) -> dict[str, object]:
        document = self.canonical_mapping()
        document.update(
            {
                "capabilities_hex": f"0x{self.capabilities:016x}",
                "flight_sw_version_text": _firmware_version_text(self.flight_sw_version),
                "flight_sw_version_type": _firmware_version_type(self.flight_sw_version),
                "middleware_sw_version_text": _firmware_version_text(self.middleware_sw_version),
                "middleware_sw_version_type": _firmware_version_type(self.middleware_sw_version),
                "os_sw_version_text": _firmware_version_text(self.os_sw_version),
                "os_sw_version_type": _firmware_version_type(self.os_sw_version),
                "raw_device_uid_serialized": False,
                "identity_hash": self.identity_hash,
            }
        )
        return document


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
    request_ranging_streams: bool = False
    request_autopilot_version: bool = False

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
        if not isinstance(self.request_ranging_streams, bool):
            raise ValueError("Pixhawk ranging-stream request setting must be boolean")
        if not isinstance(self.request_autopilot_version, bool):
            raise ValueError("Pixhawk autopilot-version request setting must be boolean")

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
    """Consumes MAVLink telemetry without flight-control or parameter writes.

    Unknown operational predicates intentionally remain ``None``. In particular,
    this class does not infer a deployment zone, geofence approval, flight-mode
    permission, or a clear release zone from generic MAVLink messages.
    """

    def __init__(
        self,
        config: PixhawkReadOnlyConfig,
        *,
        mission_feedback_tracker: Px4MissionFeedbackTracker | None = None,
    ) -> None:
        if not isinstance(config, PixhawkReadOnlyConfig):
            raise TypeError("config must be a PixhawkReadOnlyConfig")
        if mission_feedback_tracker is not None and not isinstance(
            mission_feedback_tracker, Px4MissionFeedbackTracker
        ):
            raise TypeError("mission_feedback_tracker must be a Px4MissionFeedbackTracker or None")
        self.config = config
        self._mission_feedback_tracker = mission_feedback_tracker
        self._connection: Any | None = None
        self._resolved_endpoint: str | None = None
        self._last_heartbeat_s: float | None = None
        self._last_attitude_s: float | None = None
        self._last_attitude_uncertainty_s: float | None = None
        self._last_attitude_uncertainty_message_s: float | None = None
        self._last_position_s: float | None = None
        # Keep GLOBAL_POSITION_INT's coordinate stream distinct from LOCAL
        # position.  ``_last_position_s`` remains the backwards-compatible
        # union timestamp used in evidence, while these fields let a malformed
        # global frame act as a hard GPS-quality barrier without invalidating a
        # healthy local-NED source.
        self._last_global_position_s: float | None = None
        self._last_global_position_invalid_s: float | None = None
        self._last_coordinate_s: float | None = None
        self._last_altitude_s: float | None = None
        self._last_local_position_s: float | None = None
        self._last_velocity_s: float | None = None
        self._last_airspeed_s: float | None = None
        self._last_differential_pressure_s: float | None = None
        self._last_wind_s: float | None = None
        self._last_heading_s: float | None = None
        self._last_gps_raw_s: float | None = None
        self._last_gps_raw_message_s: float | None = None
        self._last_home_position_s: float | None = None
        self._last_altitude_message_s: float | None = None
        self._raw_altitude_relative_m: float | None = None
        self._raw_altitude_local_m: float | None = None
        self._altitude_agl_m = float("nan")
        self._altitude_reference: str | None = None
        self._gps_absolute_altitude_m = float("nan")
        self._home_absolute_altitude_m = float("nan")
        # GPS-vs-home is a fallback, not a destructive replacement for an
        # estimator/local altitude sample.  Keep the last independent value so
        # a later degraded GPS_RAW_INT frame can restore it atomically.
        self._gps_home_fallback_previous: tuple[float, str | None, float | None] | None = None
        self._last_gps_fix_type: int | None = None
        self._gps_horizontal_accuracy_m = float("nan")
        self._gps_vertical_accuracy_m = float("nan")
        self._last_gps_altitude_m: float | None = None
        self._roll_deg = float("nan")
        self._pitch_deg = float("nan")
        self._attitude_roll_sigma_deg = float("nan")
        self._attitude_pitch_sigma_deg = float("nan")
        self._attitude_heading_sigma_deg = float("nan")
        self._attitude_uncertainty_source: str | None = None
        self._attitude_covariance_rad2: tuple[float, ...] | None = None
        self._attitude_uncertainty_reset_counter: int | None = None
        self._attitude_uncertainty_child_frame_id: int | None = None
        self._attitude_uncertainty_accepted_count = 0
        self._attitude_uncertainty_rejections: Counter[str] = Counter()
        self._ground_speed_mps = float("nan")
        self._velocity_north_mps = float("nan")
        self._velocity_east_mps = float("nan")
        self._airspeed_mps = float("nan")
        self._differential_pressure_hpa = float("nan")
        self._differential_pressure_temperature_c = float("nan")
        self._wind_north_mps = float("nan")
        self._wind_east_mps = float("nan")
        self._latitude_deg = float("nan")
        self._longitude_deg = float("nan")
        self._local_north_m = float("nan")
        self._local_east_m = float("nan")
        self._local_down_m = float("nan")
        self._local_ground_down_m: float | None = None
        self._heading_deg = float("nan")
        self._battery_remaining_pct = float("nan")
        self._satellites_visible: int | None = None
        self._armed: bool | None = None
        self._flight_mode: str | None = None
        self._mission_sequence: int | None = None
        self._last_mission_current_s: float | None = None
        self._mission_item_reached_sequence: int | None = None
        self._last_mission_item_reached_s: float | None = None
        self._heartbeat_identity = PixhawkHeartbeatIdentity()
        self._autopilot_version_identity: PixhawkAutopilotVersionIdentity | None = None
        self._last_autopilot_version_s: float | None = None
        self._last_autopilot_version_message_s: float | None = None
        self._autopilot_system_id: int | None = None
        self._received_message_count = 0
        self._rejected_system_message_count = 0
        self._ignored_non_autopilot_heartbeat_count = 0
        self._rejected_autopilot_version_message_count = 0
        self._message_type_counts: Counter[str] = Counter()
        self._rc_input_snapshot: PixhawkRcInputSnapshot | None = None
        self._messages_transmitted = 0
        self._stream_request_attempts = 0
        self._last_stream_request_s: float | None = None
        self._autopilot_version_request_attempts = 0
        self._last_autopilot_version_request_s: float | None = None
        self._autopilot_version_request_binding: tuple[int | None, ...] | None = None

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def messages_transmitted(self) -> int:
        """Count link-local stream-cadence requests; flight commands stay absent."""

        return self._messages_transmitted

    @property
    def resolved_endpoint(self) -> str | None:
        return self._resolved_endpoint

    @property
    def heartbeat_identity(self) -> PixhawkHeartbeatIdentity:
        return self._heartbeat_identity

    @property
    def autopilot_version_identity(self) -> PixhawkAutopilotVersionIdentity | None:
        return self._autopilot_version_identity

    @property
    def autopilot_version_observed_at_s(self) -> float | None:
        return self._last_autopilot_version_s

    @property
    def rejected_autopilot_version_messages(self) -> int:
        return self._rejected_autopilot_version_message_count

    def autopilot_version_age_s(self, *, now_s: float) -> float | None:
        if not math.isfinite(now_s) or now_s < 0:
            raise ValueError("now_s must be a finite non-negative number")
        return self._sample_age(self._last_autopilot_version_s, now_s)

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
    def mission_feedback_tracker(self) -> Px4MissionFeedbackTracker | None:
        """The explicitly supplied hash-bound feedback tracker, if any."""

        return self._mission_feedback_tracker

    def mission_feedback_snapshot(self, *, now_s: float) -> Px4MissionFeedbackSnapshot | None:
        """Return fresh/invalid Mission wire evidence without opening the link."""

        tracker = self._mission_feedback_tracker
        return None if tracker is None else tracker.snapshot(now_s=now_s)

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
        mission_feedback = self.mission_feedback_snapshot(now_s=now_s)
        return {
            "configured_endpoint": self.config.endpoint,
            "resolved_endpoint": self.resolved_endpoint,
            "read_only": self.is_read_only,
            "hardware_control_enabled": False,
            "telemetry_configuration_enabled": self.config.request_ranging_streams,
            "messages_received": self.messages_received,
            "messages_transmitted": self.messages_transmitted,
            "rejected_system_messages": self.rejected_system_messages,
            "ignored_non_autopilot_heartbeats": self.ignored_non_autopilot_heartbeats,
            "rejected_autopilot_version_messages": self.rejected_autopilot_version_messages,
            "message_type_counts": self.message_type_counts,
            "rc_input_observed": rc_input is not None,
            # Keep diagnostics fail-closed for clock-domain mismatches just
            # like telemetry freshness.  Clamping a future RC sample to zero
            # would report healthy input while the host clock is behind it.
            "rc_input_age_s": self._sample_age(
                rc_input.observed_at_s if rc_input is not None else None,
                now_s,
            ),
            "rc_valid_channel_count": (rc_input.valid_channel_count if rc_input is not None else 0),
            "transport_link_healthy": self.transport_link_healthy(now_s=now_s),
            "qualified_link_healthy": snapshot.link_healthy,
            "position_healthy": snapshot.position_healthy,
            "attitude_uncertainty": {
                "source": snapshot.attitude_uncertainty_source,
                "roll_sigma_deg": (
                    snapshot.attitude_roll_sigma_deg
                    if math.isfinite(snapshot.attitude_roll_sigma_deg)
                    else None
                ),
                "pitch_sigma_deg": (
                    snapshot.attitude_pitch_sigma_deg
                    if math.isfinite(snapshot.attitude_pitch_sigma_deg)
                    else None
                ),
                "heading_sigma_deg": (
                    snapshot.attitude_heading_sigma_deg
                    if math.isfinite(snapshot.attitude_heading_sigma_deg)
                    else None
                ),
                "observed_at_s": self._last_attitude_uncertainty_s,
                "age_s": self._sample_age(self._last_attitude_uncertainty_s, now_s),
                "fresh": self._is_fresh(self._last_attitude_uncertainty_s, now_s),
                "associated_with_attitude": self._attitude_uncertainty_available(now_s=now_s),
                "maximum_attitude_association_s": (_ATTITUDE_UNCERTAINTY_MAX_ASSOCIATION_S),
                "raw_angular_covariance_rad2": (
                    list(self._attitude_covariance_rad2)
                    if self._attitude_covariance_rad2 is not None
                    else None
                ),
                "configured_floor_deg": {
                    "roll": _MINIMUM_ROLL_SIGMA_DEG,
                    "pitch": _MINIMUM_PITCH_SIGMA_DEG,
                    "heading": _MINIMUM_HEADING_SIGMA_DEG,
                },
                "reset_counter": self._attitude_uncertainty_reset_counter,
                "child_frame_id": self._attitude_uncertainty_child_frame_id,
                "accepted_count": self._attitude_uncertainty_accepted_count,
                "rejections": dict(sorted(self._attitude_uncertainty_rejections.items())),
            },
            "global_position_age_s": self._sample_age(self._last_global_position_s, now_s),
            "global_position_invalid_age_s": self._sample_age(
                self._last_global_position_invalid_s,
                now_s,
            ),
            "motion": {
                "armed": snapshot.armed,
                "ground_speed_mps": (
                    snapshot.ground_speed_mps if math.isfinite(snapshot.ground_speed_mps) else None
                ),
                "velocity_north_mps": (
                    snapshot.velocity_north_mps
                    if math.isfinite(snapshot.velocity_north_mps)
                    else None
                ),
                "velocity_east_mps": (
                    snapshot.velocity_east_mps
                    if math.isfinite(snapshot.velocity_east_mps)
                    else None
                ),
                "velocity_age_s": self._sample_age(self._last_velocity_s, now_s),
                "airspeed_mps": (
                    snapshot.airspeed_mps if math.isfinite(snapshot.airspeed_mps) else None
                ),
                "airspeed_age_s": self._sample_age(self._last_airspeed_s, now_s),
                "differential_pressure_hpa": (
                    self._differential_pressure_hpa
                    if math.isfinite(self._differential_pressure_hpa)
                    else None
                ),
                "differential_pressure_temperature_c": (
                    self._differential_pressure_temperature_c
                    if math.isfinite(self._differential_pressure_temperature_c)
                    else None
                ),
                "differential_pressure_age_s": self._sample_age(
                    self._last_differential_pressure_s,
                    now_s,
                ),
            },
            "ranging_stream_requests": {
                "attempts": self._stream_request_attempts,
                "last_request_age_s": self._sample_age(self._last_stream_request_s, now_s),
                "requested_message_intervals_us": {
                    name: interval_us
                    for name, _message_id, interval_us in _RANGING_STREAM_INTERVALS_US
                },
            },
            "autopilot_version_requests": {
                "enabled": self.config.request_autopilot_version,
                "attempts": self._autopilot_version_request_attempts,
                "last_request_age_s": self._sample_age(
                    self._last_autopilot_version_request_s,
                    now_s,
                ),
                "command_id": _MAV_CMD_REQUEST_MESSAGE,
                "message_id": _AUTOPILOT_VERSION_MESSAGE_ID,
                "response_observed": self._autopilot_version_identity is not None,
            },
            "vertical": {
                "altitude_agl_m": (
                    snapshot.altitude_agl_m if math.isfinite(snapshot.altitude_agl_m) else None
                ),
                "altitude_reference": snapshot.altitude_reference,
                "altitude_fresh": self._is_fresh(self._last_altitude_s, now_s),
                "gps_raw_age_s": self._sample_age(self._last_gps_raw_s, now_s),
                "gps_raw_message_age_s": self._sample_age(self._last_gps_raw_message_s, now_s),
                "gps_fix_type": self._last_gps_fix_type,
                "gps_absolute_altitude_m": self._last_gps_altitude_m,
                "home_position_age_s": self._sample_age(self._last_home_position_s, now_s),
                "altitude_message_age_s": self._sample_age(self._last_altitude_message_s, now_s),
                "raw_altitude_relative_m": self._raw_altitude_relative_m,
                "raw_altitude_local_m": self._raw_altitude_local_m,
                "local_position_age_s": self._sample_age(self._last_local_position_s, now_s),
                "local_north_m": (
                    snapshot.local_north_m if math.isfinite(snapshot.local_north_m) else None
                ),
                "local_east_m": (
                    snapshot.local_east_m if math.isfinite(snapshot.local_east_m) else None
                ),
                "local_down_m": (
                    snapshot.local_down_m if math.isfinite(snapshot.local_down_m) else None
                ),
                "local_ground_down_m": self._local_ground_down_m,
            },
            "mission": {
                "current_sequence": snapshot.mission_sequence,
                "current_age_s": self._sample_age(self._last_mission_current_s, now_s),
                "item_reached_sequence": snapshot.mission_item_reached_sequence,
                "item_reached_age_s": self._sample_age(
                    self._last_mission_item_reached_s,
                    now_s,
                ),
                "feedback": (
                    None
                    if mission_feedback is None
                    else {
                        "document": mission_feedback.to_mapping(),
                        "sha256": mission_feedback.canonical_hash,
                    }
                ),
            },
            "heartbeat_identity": self.heartbeat_identity.to_document(),
            "autopilot_version_identity": (
                None
                if self.autopilot_version_identity is None
                else self.autopilot_version_identity.to_document()
            ),
            "autopilot_version_observed_at_s": self.autopilot_version_observed_at_s,
            "autopilot_version_age_s": self.autopilot_version_age_s(now_s=now_s),
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
        # Opening the transport itself sends nothing.  Optional message-interval
        # requests are emitted only after a matching autopilot heartbeat arrives.
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
        self._maybe_request_autopilot_version(now_s=now_s)
        self._maybe_request_ranging_streams(now_s=now_s)
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
        mission_current_fresh = self._is_fresh(self._last_mission_current_s, now_s) is True
        mission_reached_fresh = self._is_fresh(self._last_mission_item_reached_s, now_s) is True
        attitude_uncertainty_available = self._attitude_uncertainty_available(now_s=now_s)
        return VehicleTelemetry(
            altitude_agl_m=self._altitude_agl_m,
            roll_deg=self._roll_deg,
            pitch_deg=self._pitch_deg,
            ground_speed_mps=self._ground_speed_mps,
            in_allowed_zone=None,
            geofence_healthy=None,
            position_healthy=self._position_health(now_s=now_s),
            link_healthy=link_healthy,
            flight_mode_allows_deploy=None,
            release_zone_clear=None,
            person_detector_healthy=None,
            latitude_deg=self._latitude_deg,
            longitude_deg=self._longitude_deg,
            heading_deg=self._heading_deg,
            battery_remaining_pct=self._battery_remaining_pct,
            satellites_visible=self._satellites_visible,
            gps_fix_type=self._last_gps_fix_type,
            gps_horizontal_accuracy_m=self._gps_horizontal_accuracy_m,
            gps_vertical_accuracy_m=self._gps_vertical_accuracy_m,
            gps_observed_at_s=(
                self._last_gps_raw_s if self._last_gps_raw_s is not None else float("nan")
            ),
            armed=self._armed,
            flight_mode=self._flight_mode,
            mission_sequence=(self._mission_sequence if mission_current_fresh else None),
            mission_sequence_observed_at_s=(
                self._last_mission_current_s
                if self._last_mission_current_s is not None
                else float("nan")
            ),
            mission_item_reached_sequence=(
                self._mission_item_reached_sequence if mission_reached_fresh else None
            ),
            mission_item_reached_observed_at_s=(
                self._last_mission_item_reached_s
                if self._last_mission_item_reached_s is not None
                else float("nan")
            ),
            attitude_observed_at_s=(
                self._last_attitude_s if self._last_attitude_s is not None else float("nan")
            ),
            attitude_roll_sigma_deg=(
                self._attitude_roll_sigma_deg if attitude_uncertainty_available else float("nan")
            ),
            attitude_pitch_sigma_deg=(
                self._attitude_pitch_sigma_deg if attitude_uncertainty_available else float("nan")
            ),
            attitude_heading_sigma_deg=(
                self._attitude_heading_sigma_deg if attitude_uncertainty_available else float("nan")
            ),
            attitude_uncertainty_source=(
                self._attitude_uncertainty_source if attitude_uncertainty_available else None
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
            altitude_observed_at_s=(
                self._last_altitude_s if self._last_altitude_s is not None else float("nan")
            ),
            altitude_reference=self._altitude_reference,
            local_north_m=self._local_north_m,
            local_east_m=self._local_east_m,
            local_down_m=self._local_down_m,
            local_position_observed_at_s=(
                self._last_local_position_s
                if self._last_local_position_s is not None
                else float("nan")
            ),
            gps_absolute_altitude_m=self._gps_absolute_altitude_m,
        )

    def ingest_message(self, message: Any, *, received_at_s: float | None = None) -> None:
        """Update the cache from a MAVLink-shaped message; useful for deterministic tests."""

        now_s = _validated_received_timestamp(received_at_s)
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
            if message_type == "AUTOPILOT_VERSION":
                self._rejected_autopilot_version_message_count += 1
            return
        mission_feedback_accepted = True
        if message_type in {"MISSION_CURRENT", "MISSION_ITEM_REACHED"}:
            tracker = self._mission_feedback_tracker
            if tracker is not None:
                mission_feedback_accepted = tracker.ingest_message(
                    message,
                    observed_at_s=now_s,
                )
        if message_type == "HEARTBEAT":
            autopilot_id = _optional_int(getattr(message, "autopilot", None))
            vehicle_type_id = _optional_int(getattr(message, "type", None))
            if autopilot_id == 8 or vehicle_type_id in {6, 18}:
                self._ignored_non_autopilot_heartbeat_count += 1
                return
            if source_system_id is not None:
                self._autopilot_system_id = source_system_id
            next_identity = PixhawkHeartbeatIdentity(
                source_system_id=source_system_id,
                source_component_id=source_component_id,
                autopilot_id=autopilot_id,
                vehicle_type_id=vehicle_type_id,
                system_status_id=_optional_int(getattr(message, "system_status", None)),
                mavlink_version=_optional_int(getattr(message, "mavlink_version", None)),
            )
            if _heartbeat_binding(self._heartbeat_identity) != _heartbeat_binding(next_identity):
                self._clear_autopilot_version_identity()
            self._heartbeat_identity = next_identity
            self._last_heartbeat_s = now_s
            base_mode = getattr(message, "base_mode", None)
            if base_mode is not None:
                self._armed = bool(int(base_mode) & 128)
        elif message_type == "AUTOPILOT_VERSION":
            self._ingest_autopilot_version(
                message,
                source_system_id=source_system_id,
                source_component_id=source_component_id,
                received_at_s=now_s,
            )
        elif message_type == "ATTITUDE":
            self._roll_deg = math.degrees(float(message.roll))
            self._pitch_deg = math.degrees(float(message.pitch))
            yaw = getattr(message, "yaw", None)
            if yaw is not None and math.isfinite(float(yaw)):
                # GLOBAL_POSITION_INT.hdg is commonly unavailable indoors. The
                # attitude quaternion/Euler stream still carries the estimator's
                # yaw, which is the correct fallback for camera-ray projection.
                self._heading_deg = math.degrees(float(yaw)) % 360.0
                self._last_heading_s = now_s
            self._last_attitude_s = now_s
        elif message_type == "GLOBAL_POSITION_INT":
            # Parse each field independently, then commit only the fields that
            # form a valid MAVLink wire value.  GLOBAL_POSITION_INT is emitted
            # as one frame, but a sentinel in one field must not make its
            # siblings look fresh or overwrite an earlier good estimate.
            relative_alt_mm = _mavlink_int32(
                getattr(message, "relative_alt", None),
                unknown=_MAVLINK_INT32_MAX,
            )
            velocity_cm_s = tuple(
                _mavlink_int16(getattr(message, field, None), unknown=_MAVLINK_INT16_MAX)
                for field in ("vx", "vy")
            )
            heading_cdeg = _mavlink_heading_cdeg(getattr(message, "hdg", None))

            latitude_e7 = _mavlink_int32(
                getattr(message, "lat", None),
                unknown=_MAVLINK_INT32_MAX,
            )
            longitude_e7 = _mavlink_int32(
                getattr(message, "lon", None),
                unknown=_MAVLINK_INT32_MAX,
            )
            coordinates_valid = _valid_global_position_e7(latitude_e7, longitude_e7)

            if relative_alt_mm is not None and self._timestamp_is_current(
                now_s,
                self._last_altitude_s,
            ):
                self._forget_gps_home_fallback_previous()
                self._altitude_agl_m = relative_alt_mm / 1_000.0
                self._altitude_reference = "home_relative"
                self._last_altitude_s = now_s

            if all(value is not None for value in velocity_cm_s) and self._timestamp_is_current(
                now_s,
                self._last_velocity_s,
            ):
                velocity_north_cm_s, velocity_east_cm_s = velocity_cm_s  # type: ignore[misc]
                self._ground_speed_mps = (
                    math.hypot(
                        velocity_north_cm_s,
                        velocity_east_cm_s,
                    )
                    / 100.0
                )
                self._velocity_north_mps = velocity_north_cm_s / 100.0
                self._velocity_east_mps = velocity_east_cm_s / 100.0
                self._last_velocity_s = now_s

            if heading_cdeg is not None and self._timestamp_is_current(
                now_s,
                self._last_heading_s,
            ):
                self._heading_deg = heading_cdeg / 100.0
                self._last_heading_s = now_s

            global_coordinate_cursor = self._last_global_position_s
            if self._last_global_position_invalid_s is not None:
                global_coordinate_cursor = (
                    self._last_global_position_invalid_s
                    if global_coordinate_cursor is None
                    else max(global_coordinate_cursor, self._last_global_position_invalid_s)
                )
            if coordinates_valid:
                latitude_deg, longitude_deg = coordinates_valid
                global_is_current = self._timestamp_is_current(now_s, global_coordinate_cursor)
                coordinate_is_current = self._timestamp_is_current(now_s, self._last_coordinate_s)
                if global_is_current and coordinate_is_current:
                    self._latitude_deg = latitude_deg
                    self._longitude_deg = longitude_deg
                    self._last_global_position_s = now_s
                    self._last_global_position_invalid_s = None
                    self._last_coordinate_s = now_s
                    # ``_last_position_s`` is the historical union timestamp;
                    # never move it backwards when an out-of-order frame lands.
                    if self._last_position_s is None or now_s >= self._last_position_s:
                        self._last_position_s = now_s
            else:
                # Keep the last coordinate and the last valid position
                # timestamp for diagnostics, but create a hard barrier for GPS
                # qualification.  Otherwise a fresh GPS_RAW_INT quality tuple
                # could be paired with an old GLOBAL_POSITION_INT coordinate.
                if self._timestamp_is_current(now_s, global_coordinate_cursor):
                    if (
                        self._last_global_position_invalid_s is None
                        or now_s >= self._last_global_position_invalid_s
                    ):
                        self._last_global_position_invalid_s = now_s
                    # Clear only raw quality/freshness fields when this global
                    # frame is not older than the latest raw sample.  A newer
                    # raw sample is an independent complete source and must not
                    # be destroyed by an out-of-order estimator frame.
                    if self._last_gps_raw_s is None or now_s >= self._last_gps_raw_s:
                        self._invalidate_gps_raw_sample()
        elif message_type == "LOCAL_POSITION_NED":
            self._ingest_local_position(
                north=getattr(message, "x", None),
                east=getattr(message, "y", None),
                down=getattr(message, "z", None),
                north_velocity=getattr(message, "vx", None),
                east_velocity=getattr(message, "vy", None),
                received_at_s=now_s,
                reference="local_ned_relative",
            )
        elif message_type == "ODOMETRY":
            # ODOMETRY can use several frames.  Use only LOCAL_NED (MAV_FRAME=1)
            # values here; body/camera frames must first be transformed by their
            # producer and are never silently treated as a navigation position.
            frame_id = _optional_int(getattr(message, "frame_id", None))
            child_frame_id = _optional_int(getattr(message, "child_frame_id", None))
            if frame_id == _MAV_FRAME_LOCAL_NED:
                self._ingest_local_position(
                    north=getattr(message, "x", None),
                    east=getattr(message, "y", None),
                    down=getattr(message, "z", None),
                    north_velocity=(
                        getattr(message, "vx", None)
                        if child_frame_id == _MAV_FRAME_LOCAL_NED
                        else None
                    ),
                    east_velocity=(
                        getattr(message, "vy", None)
                        if child_frame_id == _MAV_FRAME_LOCAL_NED
                        else None
                    ),
                    received_at_s=now_s,
                    reference="odometry_local_ned_relative",
                )
            self._ingest_odometry_attitude_uncertainty(
                message,
                frame_id=frame_id,
                child_frame_id=child_frame_id,
                source_system_id=source_system_id,
                source_component_id=source_component_id,
                received_at_s=now_s,
            )
        elif message_type == "ALTITUDE":
            relative = _finite_float(getattr(message, "altitude_relative", None))
            local = _finite_float(getattr(message, "altitude_local", None))
            self._last_altitude_message_s = now_s
            self._raw_altitude_relative_m = relative
            self._raw_altitude_local_m = local
            if relative is not None and relative > 0.0:
                self._forget_gps_home_fallback_previous()
                self._altitude_agl_m = relative
                self._altitude_reference = "home_relative"
                self._last_altitude_s = now_s
        elif message_type == "ATTITUDE_QUATERNION":
            quaternion = tuple(
                _finite_float(getattr(message, name, None)) for name in ("q1", "q2", "q3", "q4")
            )
            if all(value is not None for value in quaternion):
                self._ingest_attitude_quaternion(
                    quaternion=quaternion,  # type: ignore[arg-type]
                    received_at_s=now_s,
                )
        elif message_type == "VFR_HUD":
            airspeed = getattr(message, "airspeed", None)
            if airspeed is not None and math.isfinite(float(airspeed)) and float(airspeed) >= 0.0:
                self._airspeed_mps = float(airspeed)
                self._last_airspeed_s = now_s
        elif message_type in {"SCALED_PRESSURE", "SCALED_PRESSURE2", "SCALED_PRESSURE3"}:
            differential_pressure = _finite_float(getattr(message, "press_diff", None))
            temperature_cdeg = _finite_float(getattr(message, "temperature", None))
            if differential_pressure is not None:
                self._differential_pressure_hpa = differential_pressure
                self._last_differential_pressure_s = now_s
            if temperature_cdeg is not None and temperature_cdeg != 32767:
                self._differential_pressure_temperature_c = temperature_cdeg / 100.0
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
            # MAVLink delivery can be reordered when a replay or UDP queue
            # drains.  Treat the per-source receive timestamp as a cursor:
            # an older raw frame must not overwrite newer fix/satellite/
            # accuracy diagnostics, clear a newer complete sample, or move
            # the GPS-home altitude fallback backwards.  The shared
            # coordinate cursor below still arbitrates RAW versus
            # GLOBAL_POSITION_INT independently.
            if not self._timestamp_is_current(now_s, self._last_gps_raw_message_s):
                return
            self._last_gps_raw_message_s = now_s
            satellites_wire = _optional_int(getattr(message, "satellites_visible", None))
            # GPS_RAW_INT.satellites_visible is a per-sample value.  Do not
            # retain the previous count when MAVLink reports 255 (unknown) or
            # a malformed byte; doing so can make a degraded sample pass the
            # outdoor GPS quality gate using stale metadata.
            satellites = normalize_satellites_visible(satellites_wire)
            self._satellites_visible = satellites
            raw_fix_type = _optional_int(getattr(message, "fix_type", None))
            # Keep only the legal MAVLink enum in the telemetry snapshot.  An
            # invalid/sentinel fix is represented as unavailable and therefore
            # cannot accidentally qualify a coordinate downstream.
            fix_type = normalize_gps_fix_type(raw_fix_type)
            # ``GPS_RAW_INT.alt`` is a signed int32 on the wire.  Parse it in
            # that domain so INT32_MAX (unknown) cannot turn into a plausible
            # but absurd GPS-vs-home height fallback.
            altitude_mm = _mavlink_int32(
                getattr(message, "alt", None),
                unknown=_MAVLINK_INT32_MAX,
            )
            self._last_gps_fix_type = fix_type
            eph_cm = _finite_float(getattr(message, "eph", None))
            epv_cm = _finite_float(getattr(message, "epv", None))
            self._gps_horizontal_accuracy_m = (
                eph_cm / 100.0 if eph_cm is not None and 0.0 <= eph_cm < 65_535.0 else float("nan")
            )
            self._gps_vertical_accuracy_m = (
                epv_cm / 100.0 if epv_cm is not None and 0.0 <= epv_cm < 65_535.0 else float("nan")
            )
            self._last_gps_altitude_m = altitude_mm / 1_000.0 if altitude_mm is not None else None
            # The raw freshness cursor represents one complete wire sample.
            # Satellite count and horizontal accuracy are part of that tuple;
            # a missing/unknown value may still be useful for diagnostics or
            # the independent height fallback, but it must not make an old
            # coordinate look like a newly qualified outdoor fix.
            raw_quality_fields_valid = (
                satellites is not None and eph_cm is not None and 0.0 <= eph_cm < 65_535.0
            )
            if (
                fix_type is not None
                and fix_type >= MAVLINK_QUALIFIED_FIX_TYPE_MIN
                and fix_type <= MAVLINK_GPS_FIX_TYPE_MAX
                and altitude_mm is not None
            ):
                latitude_e7 = _mavlink_int32(
                    getattr(message, "lat", None),
                    unknown=_MAVLINK_INT32_MAX,
                )
                longitude_e7 = _mavlink_int32(
                    getattr(message, "lon", None),
                    unknown=_MAVLINK_INT32_MAX,
                )
                if latitude_e7 is not None and longitude_e7 is not None:
                    coordinates = _valid_global_position_e7(latitude_e7, longitude_e7)
                    if coordinates is not None:
                        latitude_deg, longitude_deg = coordinates
                        if self._timestamp_is_current(now_s, self._last_coordinate_s):
                            self._latitude_deg = latitude_deg
                            self._longitude_deg = longitude_deg
                            self._last_coordinate_s = now_s
                        self._gps_absolute_altitude_m = altitude_mm / 1_000.0
                        self._last_gps_raw_s = now_s if raw_quality_fields_valid else None
                        self._update_gps_home_relative_altitude(now_s=now_s)
                    else:
                        # GPS_RAW_INT is a single atomic sample.  Do not
                        # refresh its quality timestamp when the coordinate
                        # payload is outside the WGS-84 domain: otherwise the
                        # previous coordinate could be paired with the new
                        # fix/satellite metadata and appear freshly qualified.
                        # GLOBAL_POSITION_INT owns the shared coordinate cache
                        # independently and remains untouched here.
                        self._invalidate_gps_raw_sample()
                        self._gps_absolute_altitude_m = altitude_mm / 1_000.0
                        self._update_gps_home_relative_altitude(now_s=now_s)
                else:
                    # A raw message can still carry a valid absolute altitude
                    # for the GPS-vs-home vertical fallback even when its
                    # positional fields are absent.  Keep that independent
                    # vertical datum while withholding the coordinate fix.
                    # Preserve the wire-level fix/satellite/accuracy fields
                    # for diagnostics, but clear any prior complete-sample
                    # timestamp so a GLOBAL_POSITION_INT coordinate cannot be
                    # paired with stale raw metadata.
                    self._last_gps_raw_s = None
                    self._gps_absolute_altitude_m = altitude_mm / 1_000.0
                    self._update_gps_home_relative_altitude(now_s=now_s)
            else:
                # A raw frame with an unqualified fix or without an absolute
                # altitude is not a complete navigation sample.  Clear the
                # previous raw position timestamp and GPS-home datum so an old
                # coordinate/height cannot survive behind the new degraded
                # quality metadata.  Keep a legal but degraded fix enum (0/1/2)
                # and its per-frame diagnostics visible; malformed enum values
                # are cleared entirely.  GLOBAL_POSITION_INT/LOCAL_POSITION_NED/
                # ALTITUDE remain independent sources and are left untouched.
                if fix_type is None:
                    self._invalidate_gps_raw_sample(clear_absolute_altitude=True)
                else:
                    self._clear_gps_raw_position(clear_absolute_altitude=True)
        elif message_type == "HOME_POSITION":
            altitude_mm = _finite_float(getattr(message, "altitude", None))
            if altitude_mm is not None:
                self._home_absolute_altitude_m = altitude_mm / 1_000.0
                self._last_home_position_s = now_s
                self._update_gps_home_relative_altitude(now_s=now_s)
        elif message_type == "MISSION_CURRENT":
            if not self._timestamp_is_current(now_s, self._last_mission_current_s):
                return
            self._last_mission_current_s = now_s
            self._mission_sequence = (
                _mavlink_integral(
                    getattr(message, "seq", None),
                    minimum=0,
                    maximum=_MAVLINK_UINT16_MAX,
                )
                if mission_feedback_accepted
                else None
            )
        elif message_type == "MISSION_ITEM_REACHED":
            if not self._timestamp_is_current(now_s, self._last_mission_item_reached_s):
                return
            self._last_mission_item_reached_s = now_s
            self._mission_item_reached_sequence = (
                _mavlink_integral(
                    getattr(message, "seq", None),
                    minimum=0,
                    maximum=_MAVLINK_UINT16_MAX,
                )
                if mission_feedback_accepted
                else None
            )
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

    def _maybe_request_ranging_streams(self, *, now_s: float) -> None:
        """Request dynamic pose/height streams with bounded retries.

        PX4 may start a companion UDP endpoint with only its default sparse
        stream.  Ranging needs estimator attitude and local/relative altitude at
        video-rate cadence, so the companion asks PX4 for those message rates.
        Three attempts cover link start-up races without adding persistent
        traffic.  This is telemetry configuration only.
        """

        if not self.config.request_ranging_streams or self._stream_request_attempts >= 3:
            return
        if self._heartbeat_identity.source_system_id is None:
            return
        if self._last_stream_request_s is not None and now_s - self._last_stream_request_s < 5.0:
            return
        connection = self._connection
        mav = getattr(connection, "mav", None)
        send = getattr(mav, "command_long_send", None)
        if not callable(send):
            return
        target_system = self._heartbeat_identity.source_system_id
        target_component = self._heartbeat_identity.source_component_id or 1
        sent = 0
        for _name, message_id, interval_us in _RANGING_STREAM_INTERVALS_US:
            send(
                target_system,
                target_component,
                _MAV_CMD_SET_MESSAGE_INTERVAL,
                0,
                float(message_id),
                float(interval_us),
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            sent += 1
        self._messages_transmitted += sent
        self._stream_request_attempts += 1
        self._last_stream_request_s = now_s

    def _maybe_request_autopilot_version(self, *, now_s: float) -> None:
        """Request one sanitized firmware identity with bounded retries.

        ``AUTOPILOT_VERSION`` is not guaranteed to be broadcast periodically.
        MAV_CMD_REQUEST_MESSAGE asks the already-qualified autopilot to return
        message 148.  The request changes no parameter, mode, mission, actuator,
        or stream rate.  At most three attempts are made for each distinct
        heartbeat identity, and requests stop immediately after a valid response.
        """

        if not self.config.request_autopilot_version:
            return
        binding = _heartbeat_binding(self._heartbeat_identity)
        if binding != self._autopilot_version_request_binding:
            self._autopilot_version_request_binding = binding
            self._autopilot_version_request_attempts = 0
            self._last_autopilot_version_request_s = None
        if self._autopilot_version_identity is not None:
            return
        if self._autopilot_version_request_attempts >= 3:
            return
        if self._heartbeat_identity.source_system_id is None:
            return
        if (
            self._last_autopilot_version_request_s is not None
            and now_s - self._last_autopilot_version_request_s < 5.0
        ):
            return
        connection = self._connection
        mav = getattr(connection, "mav", None)
        send = getattr(mav, "command_long_send", None)
        if not callable(send):
            return
        send(
            self._heartbeat_identity.source_system_id,
            self._heartbeat_identity.source_component_id or 1,
            _MAV_CMD_REQUEST_MESSAGE,
            0,
            float(_AUTOPILOT_VERSION_MESSAGE_ID),
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        self._messages_transmitted += 1
        self._autopilot_version_request_attempts += 1
        self._last_autopilot_version_request_s = now_s

    def _update_gps_home_relative_altitude(self, *, now_s: float) -> None:
        """Use GPS altitude only after PX4 supplies a matching home reference.

        GPS_RAW_INT.alt is an absolute ellipsoid/geoid-referenced value, so it is
        not directly useful for camera-ray ranging.  HOME_POSITION provides the
        corresponding PX4 home reference; their positive difference is a valid
        airborne height fallback when GLOBAL_POSITION_INT/ALTITUDE are absent.
        """

        if not (
            math.isfinite(self._gps_absolute_altitude_m)
            and math.isfinite(self._home_absolute_altitude_m)
        ):
            return
        relative_m = self._gps_absolute_altitude_m - self._home_absolute_altitude_m
        if relative_m <= 0.0:
            # A home update can make the GPS-vs-home delta non-positive (for
            # example after a reset or datum mismatch).  Retaining the prior
            # positive fallback would expose stale airborne height to ranging.
            self._restore_gps_home_fallback_previous()
            return
        if self._altitude_reference != "gps_home_relative":
            self._gps_home_fallback_previous = (
                self._altitude_agl_m,
                self._altitude_reference,
                self._last_altitude_s,
            )
        self._altitude_agl_m = relative_m
        self._altitude_reference = "gps_home_relative"
        self._last_altitude_s = now_s

    def _clear_gps_raw_position(self, *, clear_absolute_altitude: bool) -> None:
        """Invalidate only the complete raw-position/vertical contribution.

        ``GPS_RAW_INT`` quality fields are still useful diagnostics when PX4
        reports a legal but degraded fix (for example ``fix_type=2``) or omits
        ``alt``.  The timestamp and GPS-home datum must nevertheless be cleared
        so a previous complete sample cannot be paired with the new metadata.
        """

        self._last_gps_raw_s = None
        if clear_absolute_altitude:
            self._gps_absolute_altitude_m = float("nan")
            # Only invalidate the altitude that was actually derived from the
            # raw GPS/home pair.  An estimator, local-NED, or ALTITUDE sample
            # received earlier remains authoritative for its own source.
            if self._altitude_reference == "gps_home_relative":
                self._restore_gps_home_fallback_previous()

    def _forget_gps_home_fallback_previous(self) -> None:
        """Mark a newer independent altitude source as authoritative."""

        self._gps_home_fallback_previous = None

    def _restore_gps_home_fallback_previous(self) -> None:
        """Remove the GPS-home fallback and restore the independent source."""

        if self._altitude_reference != "gps_home_relative":
            self._gps_home_fallback_previous = None
            return
        previous = self._gps_home_fallback_previous
        if previous is None:
            self._altitude_agl_m = float("nan")
            self._altitude_reference = None
            self._last_altitude_s = None
        else:
            (
                self._altitude_agl_m,
                self._altitude_reference,
                self._last_altitude_s,
            ) = previous
        self._gps_home_fallback_previous = None

    def _invalidate_gps_raw_sample(self, *, clear_absolute_altitude: bool = False) -> None:
        """Clear the raw GPS qualification fields after a malformed sample.

        The latitude/longitude cache is shared with ``GLOBAL_POSITION_INT``;
        it is intentionally left intact so a valid estimator position remains
        usable as an independent source.  The raw source's own fix, accuracy,
        satellite and freshness fields are cleared atomically instead.
        """

        self._last_gps_fix_type = None
        self._satellites_visible = None
        self._gps_horizontal_accuracy_m = float("nan")
        self._gps_vertical_accuracy_m = float("nan")
        self._clear_gps_raw_position(clear_absolute_altitude=clear_absolute_altitude)

    def _ingest_autopilot_version(
        self,
        message: Any,
        *,
        source_system_id: int | None,
        source_component_id: int | None,
        received_at_s: float,
    ) -> None:
        heartbeat = self._heartbeat_identity
        if (
            heartbeat.source_system_id is None
            or heartbeat.source_component_id is None
            or source_system_id != heartbeat.source_system_id
            or source_component_id != heartbeat.source_component_id
        ):
            self._rejected_autopilot_version_message_count += 1
            return
        if (
            self._last_autopilot_version_message_s is not None
            and received_at_s < self._last_autopilot_version_message_s
        ):
            self._rejected_autopilot_version_message_count += 1
            return
        self._last_autopilot_version_message_s = received_at_s
        parsed = _parse_autopilot_version_identity(
            message,
            source_system_id=heartbeat.source_system_id,
            source_component_id=heartbeat.source_component_id,
        )
        if parsed is None:
            self._rejected_autopilot_version_message_count += 1
            self._clear_autopilot_version_identity(clear_message_cursor=False)
            return
        self._autopilot_version_identity = parsed
        self._last_autopilot_version_s = received_at_s

    def _clear_autopilot_version_identity(self, *, clear_message_cursor: bool = True) -> None:
        self._autopilot_version_identity = None
        self._last_autopilot_version_s = None
        if clear_message_cursor:
            self._last_autopilot_version_message_s = None

    def _ingest_local_position(
        self,
        *,
        north: object,
        east: object,
        down: object,
        north_velocity: object,
        east_velocity: object,
        received_at_s: float,
        reference: str,
    ) -> None:
        velocity = tuple(_finite_float(value) for value in (north_velocity, east_velocity))
        local_speed_mps = (
            math.hypot(*velocity) if all(value is not None for value in velocity) else None
        )
        coordinates = tuple(_finite_float(value) for value in (north, east, down))
        if all(value is not None for value in coordinates):
            north_m, east_m, down_m = coordinates  # type: ignore[misc]
            self._local_north_m = north_m
            self._local_east_m = east_m
            self._local_down_m = down_m
            self._last_local_position_s = received_at_s
            self._last_position_s = received_at_s
            # PX4 local NED z is positive down, but its estimator origin need
            # not coincide exactly with the physical takeoff surface.  Learn a
            # slowly varying zero while disarmed and stationary, then freeze it
            # when armed.  This removes barometer/EKF zero offsets without
            # requiring GPS or HOME_POSITION and preserves true climb dynamics.
            if self._armed is False and (local_speed_mps is None or local_speed_mps <= 1.0):
                if self._local_ground_down_m is None:
                    self._local_ground_down_m = down_m
                else:
                    self._local_ground_down_m = self._local_ground_down_m * 0.95 + down_m * 0.05
            if self._local_ground_down_m is not None:
                self._forget_gps_home_fallback_previous()
                self._altitude_agl_m = max(0.0, self._local_ground_down_m - down_m)
                self._altitude_reference = "local_ned_takeoff_relative"
                self._last_altitude_s = received_at_s
            elif down_m <= 0.0:
                self._forget_gps_home_fallback_previous()
                self._altitude_agl_m = -down_m
                self._altitude_reference = reference
                self._last_altitude_s = received_at_s
        if all(value is not None for value in velocity):
            north_mps, east_mps = velocity  # type: ignore[misc]
            self._velocity_north_mps = north_mps
            self._velocity_east_mps = east_mps
            self._ground_speed_mps = math.hypot(north_mps, east_mps)
            self._last_velocity_s = received_at_s

    def _ingest_attitude_quaternion(
        self,
        *,
        quaternion: tuple[float, float, float, float],
        received_at_s: float,
    ) -> None:
        q1, q2, q3, q4 = quaternion
        norm = math.sqrt(q1 * q1 + q2 * q2 + q3 * q3 + q4 * q4)
        if norm < 1e-6:
            return
        w, x, y, z = (value / norm for value in quaternion)
        roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
        pitch_term = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
        pitch = math.asin(pitch_term)
        yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        self._roll_deg = math.degrees(roll)
        self._pitch_deg = math.degrees(pitch)
        if self._timestamp_is_current(received_at_s, self._last_heading_s):
            self._heading_deg = math.degrees(yaw) % 360.0
            self._last_heading_s = received_at_s
        self._last_attitude_s = received_at_s

    def _ingest_odometry_attitude_uncertainty(
        self,
        message: Any,
        *,
        frame_id: int | None,
        child_frame_id: int | None,
        source_system_id: int | None,
        source_component_id: int | None,
        received_at_s: float,
    ) -> None:
        """Ingest one source-bound LOCAL_NED/BODY_FRD pose covariance atomically."""

        heartbeat = self._heartbeat_identity
        if heartbeat.source_system_id is None or heartbeat.source_component_id is None:
            self._attitude_uncertainty_rejections["autopilot_identity_unavailable"] += 1
            return
        if (
            source_system_id != heartbeat.source_system_id
            or source_component_id != heartbeat.source_component_id
        ):
            self._attitude_uncertainty_rejections["source_identity_mismatch"] += 1
            return
        if frame_id != _MAV_FRAME_LOCAL_NED or child_frame_id not in {
            _MAV_FRAME_LOCAL_NED,
            _MAV_FRAME_BODY_FRD,
        }:
            self._attitude_uncertainty_rejections["unsupported_coordinate_frame"] += 1
            return
        if not self._timestamp_is_current(
            received_at_s,
            self._last_attitude_uncertainty_message_s,
        ):
            self._attitude_uncertainty_rejections["timestamp_regression"] += 1
            return
        self._last_attitude_uncertainty_message_s = received_at_s

        quaternion = tuple(
            _finite_float(value)
            for value in _sequence_values(getattr(message, "q", None), expected=4)
        )
        if len(quaternion) != 4 or not all(value is not None for value in quaternion):
            self._reject_attitude_uncertainty("invalid_quaternion")
            return
        norm = math.sqrt(sum(float(value) ** 2 for value in quaternion))
        if norm < 1.0e-6:
            self._reject_attitude_uncertainty("invalid_quaternion")
            return
        try:
            covariance = mavlink_odometry_attitude_covariance_rad2(
                getattr(message, "pose_covariance", None)
            )
        except ValueError as exc:
            self._reject_attitude_uncertainty(str(exc))
            return

        self._ingest_attitude_quaternion(
            quaternion=tuple(float(value) for value in quaternion),  # type: ignore[arg-type]
            received_at_s=received_at_s,
        )
        roll_sigma = math.degrees(math.sqrt(covariance[0]))
        pitch_sigma = math.degrees(math.sqrt(covariance[4]))
        heading_sigma = math.degrees(math.sqrt(covariance[8]))
        self._attitude_roll_sigma_deg = max(roll_sigma, _MINIMUM_ROLL_SIGMA_DEG)
        self._attitude_pitch_sigma_deg = max(pitch_sigma, _MINIMUM_PITCH_SIGMA_DEG)
        self._attitude_heading_sigma_deg = max(
            heading_sigma,
            _MINIMUM_HEADING_SIGMA_DEG,
        )
        self._attitude_uncertainty_source = _MAVLINK_ODOMETRY_ATTITUDE_UNCERTAINTY_SOURCE
        self._attitude_covariance_rad2 = covariance
        self._attitude_uncertainty_reset_counter = _mavlink_integral(
            getattr(message, "reset_counter", None),
            minimum=0,
            maximum=255,
        )
        self._attitude_uncertainty_child_frame_id = child_frame_id
        self._last_attitude_uncertainty_s = received_at_s
        self._attitude_uncertainty_accepted_count += 1

    def _reject_attitude_uncertainty(self, reason: str) -> None:
        self._attitude_uncertainty_rejections[reason] += 1
        self._last_attitude_uncertainty_s = None
        self._attitude_roll_sigma_deg = float("nan")
        self._attitude_pitch_sigma_deg = float("nan")
        self._attitude_heading_sigma_deg = float("nan")
        self._attitude_uncertainty_source = None
        self._attitude_covariance_rad2 = None
        self._attitude_uncertainty_reset_counter = None
        self._attitude_uncertainty_child_frame_id = None

    def _attitude_uncertainty_available(self, *, now_s: float) -> bool:
        if (
            self._is_fresh(self._last_attitude_uncertainty_s, now_s) is not True
            or self._last_attitude_s is None
            or self._last_attitude_uncertainty_s is None
        ):
            return False
        return (
            abs(self._last_attitude_s - self._last_attitude_uncertainty_s)
            <= _ATTITUDE_UNCERTAINTY_MAX_ASSOCIATION_S
        )

    def _position_health(self, *, now_s: float) -> bool | None:
        """Return position health without letting a bad global frame masquerade as fresh.

        ``_last_position_s`` is retained as a compatibility union of global and
        local position streams.  A malformed ``GLOBAL_POSITION_INT`` frame is
        tracked separately so it can fail the global GPS gate immediately while
        a healthy LOCAL_POSITION_NED stream still reports generic position
        health for indoor ranging.
        """

        local_health = self._is_fresh(self._last_local_position_s, now_s)
        if local_health is True:
            return True

        global_health = self._is_fresh(self._last_global_position_s, now_s)
        if self._last_global_position_invalid_s is not None:
            # An invalid global sample remains a hard barrier until a newer
            # complete coordinate pair arrives.  Returning False even after the
            # invalid marker ages out preserves the existing fail-closed
            # ``False`` result for an observed-but-unusable position stream.
            return False
        if global_health is not None:
            return global_health
        if local_health is not None:
            return local_health
        return self._is_fresh(self._last_position_s, now_s)

    @staticmethod
    def _timestamp_is_current(now_s: float, previous_s: float | None) -> bool:
        """Accept only non-regressing observation timestamps."""

        return previous_s is None or now_s >= previous_s

    @staticmethod
    def _sample_age(timestamp_s: float | None, now_s: float) -> float | None:
        if timestamp_s is None or not math.isfinite(timestamp_s):
            return None
        age_s = now_s - timestamp_s
        # Keep evidence and freshness diagnostics aligned: a future sample is
        # not a zero-age sample.  Clamping it to zero made clock-domain errors
        # look healthy even though ``_is_fresh`` rejected them.
        return age_s if age_s >= 0.0 else None

    def _is_fresh(self, timestamp_s: float | None, now_s: float) -> bool | None:
        if timestamp_s is None:
            return None
        # A future sample is not fresh: accepting it would make a clock-domain
        # mismatch look like healthy telemetry and could release downstream
        # position/attitude gates before the corresponding sample exists.
        if not math.isfinite(timestamp_s):
            return None
        age_s = now_s - timestamp_s
        return 0.0 <= age_s <= self.config.stale_after_seconds


def _sequence_values(value: object, *, expected: int) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        return ()
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return ()
    return values if len(values) == expected else ()


def mavlink_odometry_attitude_covariance_rad2(value: object) -> tuple[float, ...]:
    """Expand the angular block of MAVLink ODOMETRY's 21-value upper triangle.

    MAVLink orders the 6-D pose state as x, y, z, roll, pitch, yaw.  The
    returned tuple is a symmetric row-major 3x3 roll/pitch/yaw covariance.
    Unknown, degenerate, non-finite, or non-PSD inputs fail closed.
    """

    packed = _sequence_values(value, expected=21)
    if not packed:
        raise ValueError("pose_covariance_length_invalid")
    if isinstance(packed[0], bool):
        raise ValueError("pose_covariance_non_numeric")
    try:
        first = float(packed[0])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("pose_covariance_non_numeric") from exc
    if math.isnan(first):
        raise ValueError("pose_covariance_unknown")
    if any(isinstance(item, bool) for item in packed):
        raise ValueError("pose_covariance_non_numeric")
    try:
        numeric = tuple(float(item) for item in packed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("pose_covariance_non_numeric") from exc
    if not all(math.isfinite(item) for item in numeric):
        raise ValueError("pose_covariance_non_finite")

    covariance = (
        numeric[15],
        numeric[16],
        numeric[17],
        numeric[16],
        numeric[18],
        numeric[19],
        numeric[17],
        numeric[19],
        numeric[20],
    )
    a, b, c, _d, e, f, _g, _h, i = covariance
    if a <= 0.0 or e <= 0.0 or i <= 0.0:
        raise ValueError("attitude_covariance_variance_not_positive")
    scale = max(abs(item) for item in covariance)
    minor_tolerance = 1.0e-10 * scale * scale
    determinant_tolerance = 1.0e-10 * scale * scale * scale
    minors = (a * e - b * b, a * i - c * c, e * i - f * f)
    determinant = a * (e * i - f * f) - b * (b * i - c * f) + c * (b * f - c * e)
    if any(item < -minor_tolerance for item in minors) or (determinant < -determinant_tolerance):
        raise ValueError("attitude_covariance_not_positive_semidefinite")
    return covariance


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_autopilot_version_identity(
    message: Any,
    *,
    source_system_id: int,
    source_component_id: int,
) -> PixhawkAutopilotVersionIdentity | None:
    capabilities = _mavlink_integral(
        getattr(message, "capabilities", None),
        minimum=0,
        maximum=(1 << 64) - 1,
    )
    uint32_fields = tuple(
        _mavlink_integral(
            getattr(message, name, None),
            minimum=0,
            maximum=(1 << 32) - 1,
        )
        for name in (
            "flight_sw_version",
            "middleware_sw_version",
            "os_sw_version",
            "board_version",
        )
    )
    custom_versions = tuple(
        _mavlink_byte_array(getattr(message, name, None), length=8)
        for name in (
            "flight_custom_version",
            "middleware_custom_version",
            "os_custom_version",
        )
    )
    vendor_id = _mavlink_integral(
        getattr(message, "vendor_id", None),
        minimum=0,
        maximum=(1 << 16) - 1,
    )
    product_id = _mavlink_integral(
        getattr(message, "product_id", None),
        minimum=0,
        maximum=(1 << 16) - 1,
    )
    uid = _mavlink_integral(
        getattr(message, "uid", None),
        minimum=0,
        maximum=(1 << 64) - 1,
    )
    uid2_value = getattr(message, "uid2", None)
    uid2 = bytes(18) if uid2_value is None else _mavlink_byte_array(uid2_value, length=18)
    if (
        capabilities is None
        or any(value is None for value in uint32_fields)
        or any(value is None for value in custom_versions)
        or vendor_id is None
        or product_id is None
        or uid is None
        or uid2 is None
    ):
        return None
    flight_sw_version, middleware_sw_version, os_sw_version, board_version = uint32_fields
    flight_custom, middleware_custom, os_custom = custom_versions
    assert flight_sw_version is not None
    assert middleware_sw_version is not None
    assert os_sw_version is not None
    assert board_version is not None
    assert flight_custom is not None
    assert middleware_custom is not None
    assert os_custom is not None
    if any(uid2):
        uid_source = "uid2"
        uid_hash = _hash_device_uid(uid_source, uid2)
    elif uid != 0:
        uid_source = "uid"
        uid_hash = _hash_device_uid(uid_source, uid.to_bytes(8, byteorder="big"))
    else:
        uid_source = "none"
        uid_hash = None
    return PixhawkAutopilotVersionIdentity(
        source_system_id=source_system_id,
        source_component_id=source_component_id,
        capabilities=capabilities,
        flight_sw_version=flight_sw_version,
        middleware_sw_version=middleware_sw_version,
        os_sw_version=os_sw_version,
        board_version=board_version,
        flight_custom_version_hex=flight_custom.hex(),
        middleware_custom_version_hex=middleware_custom.hex(),
        os_custom_version_hex=os_custom.hex(),
        vendor_id=vendor_id,
        product_id=product_id,
        device_uid_source=uid_source,
        device_uid_hash=uid_hash,
    )


def _mavlink_byte_array(value: object, *, length: int) -> bytes | None:
    if isinstance(value, (str, bool, int, float)) or value is None:
        return None
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if len(values) != length:
        return None
    parsed = tuple(_mavlink_integral(item, minimum=0, maximum=255) for item in values)
    if any(item is None for item in parsed):
        return None
    return bytes(item for item in parsed if item is not None)


def _hash_device_uid(source: str, value: bytes) -> str:
    payload = _DEVICE_UID_HASH_DOMAIN + source.encode("ascii") + b"\x00" + value
    return hashlib.sha256(payload).hexdigest()


def _heartbeat_binding(identity: PixhawkHeartbeatIdentity) -> tuple[int | None, ...]:
    return (
        identity.source_system_id,
        identity.source_component_id,
        identity.autopilot_id,
        identity.vehicle_type_id,
        identity.mavlink_version,
    )


def _firmware_version_text(value: int) -> str:
    return f"{(value >> 24) & 0xFF}.{(value >> 16) & 0xFF}.{(value >> 8) & 0xFF}"


def _firmware_version_type(value: int) -> str:
    version_type = value & 0xFF
    return _FIRMWARE_VERSION_TYPE_NAMES.get(version_type, f"unknown_{version_type}")


def _is_lower_hex(value: object, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_received_timestamp(value: float | None) -> float:
    """Normalize an ingest timestamp and reject clock-domain poison values.

    ``received_at_s`` is used as the freshness cursor for every field in a
    MAVLink frame.  Accepting a negative, NaN, or infinite value would either
    make a sample permanently fresh or make all subsequent samples appear
    out-of-order.  ``None`` deliberately uses the adapter's monotonic clock so
    production callers do not need to provide a timestamp.
    """

    if value is None:
        stamp = time.monotonic()
    else:
        if isinstance(value, bool):
            raise ValueError("received_at_s must be finite and non-negative")
        try:
            stamp = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("received_at_s must be finite and non-negative") from exc
    if not math.isfinite(stamp) or stamp < 0.0:
        raise ValueError("received_at_s must be finite and non-negative")
    return stamp


def _mavlink_integral(
    value: object,
    *,
    minimum: int,
    maximum: int,
    unknown: int | None = None,
) -> int | None:
    """Parse a MAVLink integer field without accepting sentinel/overflow data."""

    if value is None or isinstance(value, bool):
        return None
    # pymavlink exposes integer wire fields as Python ``int``.  Preserve that
    # representation before considering numeric adapters: routing uint64
    # values through IEEE-754 would silently erase low capability/UID bits.
    if isinstance(value, int):
        parsed = value
    else:
        if maximum > (1 << 53) - 1:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(numeric) or not numeric.is_integer():
            return None
        parsed = int(numeric)
    if parsed < minimum or parsed > maximum or (unknown is not None and parsed == unknown):
        return None
    return parsed


def _mavlink_int32(value: object, *, unknown: int | None = None) -> int | None:
    return _mavlink_integral(
        value,
        minimum=_MAVLINK_INT32_MIN,
        maximum=_MAVLINK_INT32_MAX,
        unknown=unknown,
    )


def _mavlink_int16(value: object, *, unknown: int | None = None) -> int | None:
    return _mavlink_integral(
        value,
        minimum=_MAVLINK_INT16_MIN,
        maximum=_MAVLINK_INT16_MAX,
        unknown=unknown,
    )


def _mavlink_heading_cdeg(value: object) -> int | None:
    """Parse GLOBAL_POSITION_INT.hdg (centidegrees, 65535 = unknown)."""

    parsed = _mavlink_integral(
        value,
        minimum=0,
        maximum=_MAVLINK_UINT16_MAX,
        unknown=_MAVLINK_UINT16_MAX,
    )
    if parsed is None or parsed > _MAVLINK_HEADING_MAX_CDEG:
        return None
    return parsed


def _valid_global_position_e7(
    latitude_e7: int | None,
    longitude_e7: int | None,
) -> tuple[float, float] | None:
    """Return a WGS-84 coordinate pair, preserving legal 0/0 coordinates."""

    if latitude_e7 is None or longitude_e7 is None:
        return None
    latitude_deg = latitude_e7 / 10_000_000.0
    longitude_deg = longitude_e7 / 10_000_000.0
    if not (
        math.isfinite(latitude_deg)
        and math.isfinite(longitude_deg)
        # Exact poles have no stable local longitude direction.  Keep the
        # telemetry edge aligned with target-geolocation and QGC map gates.
        and -90.0 < latitude_deg < 90.0
        and -180.0 <= longitude_deg <= 180.0
    ):
        return None
    return latitude_deg, longitude_deg


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


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
    "PixhawkAutopilotVersionIdentity",
    "PixhawkHeartbeatIdentity",
    "PixhawkQualification",
    "PixhawkRcInputSnapshot",
    "PixhawkReadOnlyConfig",
    "PixhawkReadOnlyTelemetryProvider",
    "mavlink_odometry_attitude_covariance_rad2",
    "resolve_pixhawk_endpoint",
]

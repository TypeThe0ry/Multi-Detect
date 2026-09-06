"""Truth-free MAVLink telemetry bridge for the SIM AI executor.

The interactive simulator has one perception/control contract.  This module
is the small adapter at the telemetry edge of that contract: a decoded
``pymavlink`` message (or an equivalent mapping / :class:`MavlinkFrame`) is
validated, folded into an observation-only
:class:`~multidetect.simulator.ai_executor.SimulatorTelemetrySnapshot`, and
then passed to the existing :class:`SimulatorAiExecutor`.  It never reads
Gazebo truth and it does not expose an alternate actuator transport.  A
heartbeat freshness gate is kept separately from the MAVLink control
watchdog: when PX4 heartbeats stop arriving, the observed link is marked
unhealthy and the shared executor is driven through its normal fail-safe
path exactly once.  This prevents a stream of GPS/position packets from
keeping an otherwise dead flight-controller link alive.

``SimulatorTelemetryBridge`` is intentionally usable as a callback::

    bridge = SimulatorTelemetryBridge(executor)
    bridge.enable_ai()
    decision = bridge(message)  # equivalent to bridge.ingest(message)

The class is independent of sockets and therefore can be attached to the
interactive router, a Docker probe, or a deterministic fixture.  Raw MAVLink
datagrams are supported through :meth:`ingest_datagram`; callers that already
decode frames should use :meth:`ingest` so the router can retain its normal
byte-for-byte forwarding path.

Perception observations enter through :meth:`ingest_perception`.  That seam is
deliberately narrow: it accepts a truth-free
:class:`~multidetect.simulator.ai_executor.SimulatorTelemetrySnapshot`, copies
only the target fields, and preserves all vehicle/navigation state learned
from PX4.  The same heartbeat, armed, authority, watchdog and bounded
``MANUAL_CONTROL`` path is then used.  A short target lease prevents one
synthetic or delayed frame from being reused indefinitely.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

from ..mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    normalize_gps_fix_type,
    normalize_satellites_visible,
)
from ..qgc_readonly_bridge import MavlinkFrame, MavlinkFrameDecoder
from .ai_executor import (
    SimulatorAiDecision,
    SimulatorAiExecutor,
    SimulatorTelemetrySnapshot,
)

# MAVLink common message ids used by PX4 telemetry.  Keeping these local avoids
# importing the optional pymavlink package in the production SIM control path.
MSG_HEARTBEAT = 0
MSG_SYS_STATUS = 1
MSG_GPS_RAW_INT = 24
MSG_ATTITUDE = 30
MSG_GLOBAL_POSITION_INT = 33
MSG_VFR_HUD = 74

# Perception and RANGE_STATUS use the same conservative freshness budget.  A
# caller can override it for deterministic replay, but the production default
# keeps a single target frame from turning into a persistent command source.
DEFAULT_PERCEPTION_TIMEOUT_S = 1.5

_ARMED_FLAG = 128  # MAV_MODE_FLAG_SAFETY_ARMED
# MAVLink wire-domain bounds/sentinels used by GLOBAL_POSITION_INT and
# GPS_RAW_INT.  A decoded ``pymavlink`` object exposes plain Python numbers, so
# the adapter must validate the original wire domain before converting them to
# engineering units.  Otherwise INT32_MAX/INT16_MAX/UINT16_MAX become plausible
# coordinates, velocities, or headings and can release a GPS quality gate.
_MAVLINK_INT32_MIN = -(1 << 31)
_MAVLINK_INT32_MAX = (1 << 31) - 1
_MAVLINK_INT16_MIN = -(1 << 15)
_MAVLINK_INT16_MAX = (1 << 15) - 1
_MAVLINK_UINT16_MAX = (1 << 16) - 1
_MAVLINK_HEADING_MAX_CDEG = 35_999
_KNOWN_MESSAGE_IDS = frozenset(
    {
        MSG_HEARTBEAT,
        MSG_SYS_STATUS,
        MSG_GPS_RAW_INT,
        MSG_ATTITUDE,
        MSG_GLOBAL_POSITION_INT,
        MSG_VFR_HUD,
    }
)


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _message_type(message: object) -> str:
    getter = getattr(message, "get_type", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = ""
        return str(value) if value is not None else ""
    if isinstance(message, Mapping):
        value = message.get("type") or message.get("message_type")
        return str(value) if value is not None else ""
    value = getattr(message, "_type", None)
    return str(value) if value is not None else ""


def _message_id(message: object) -> int | None:
    if isinstance(message, MavlinkFrame):
        return int(message.message_id)
    if isinstance(message, Mapping):
        value = message.get("message_id")
        if value is None:
            value = message.get("msgid")
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None
    for name in ("message_id", "msgid"):
        value = getattr(message, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _field(message: object, name: str, default: object = None) -> object:
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _int_field(message: object, name: str, default: int = 0) -> int:
    try:
        return int(_field(message, name, default))
    except (TypeError, ValueError):
        return default


def _float_field(message: object, name: str, default: float = math.nan) -> float:
    try:
        value = float(_field(message, name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _wire_integral(
    value: object,
    *,
    minimum: int,
    maximum: int,
    unknown: int | None = None,
) -> int | None:
    """Parse an integer MAVLink field and reject malformed/sentinel values."""

    if value is None or isinstance(value, bool):
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


def _wire_int32(value: object, *, unknown: int | None = None) -> int | None:
    return _wire_integral(
        value,
        minimum=_MAVLINK_INT32_MIN,
        maximum=_MAVLINK_INT32_MAX,
        unknown=unknown,
    )


def _wire_int16(value: object, *, unknown: int | None = None) -> int | None:
    return _wire_integral(
        value,
        minimum=_MAVLINK_INT16_MIN,
        maximum=_MAVLINK_INT16_MAX,
        unknown=unknown,
    )


def _wire_uint16(value: object, *, unknown: int | None = None) -> int | None:
    return _wire_integral(
        value,
        minimum=0,
        maximum=_MAVLINK_UINT16_MAX,
        unknown=unknown,
    )


def _wire_heading_cdeg(value: object) -> int | None:
    """Parse GLOBAL_POSITION_INT.hdg (0..35999 cdeg, 65535 = unknown)."""

    parsed = _wire_uint16(value, unknown=_MAVLINK_UINT16_MAX)
    if parsed is None or parsed > _MAVLINK_HEADING_MAX_CDEG:
        return None
    return parsed


def _valid_coordinate_pair(
    latitude_e7: int | None,
    longitude_e7: int | None,
) -> tuple[float, float] | None:
    """Convert a complete WGS-84 pair, preserving the legal 0/0 coordinate."""

    if latitude_e7 is None or longitude_e7 is None:
        return None
    latitude_deg = latitude_e7 / 10_000_000.0
    longitude_deg = longitude_e7 / 10_000_000.0
    if not (
        math.isfinite(latitude_deg)
        and math.isfinite(longitude_deg)
        # Exact geographic poles have no stable local longitude direction and
        # are rejected by the shared target-geolocation contract.  Keep the
        # telemetry edge equally strict so a pole cannot refresh a seemingly
        # valid position cursor and later be used for return/target projection.
        and -90.0 < latitude_deg < 90.0
        and -180.0 <= longitude_deg <= 180.0
    ):
        return None
    return latitude_deg, longitude_deg


def _source_ids(message: object) -> tuple[int | None, int | None]:
    """Return MAVLink source ids when a decoder exposes them.

    ``pymavlink`` has used both ``get_srcSystem``/``get_srcComponent`` and a
    private ``_header`` object over time.  Mapping fixtures can provide either
    spelling.  A missing identity is accepted for local deterministic fakes;
    an explicitly wrong identity is rejected by the bridge.
    """

    if isinstance(message, MavlinkFrame):
        return message.source_system_id, message.source_component_id
    if isinstance(message, Mapping):
        system = message.get("source_system_id", message.get("srcSystem"))
        component = message.get("source_component_id", message.get("srcComponent"))
    else:
        system = None
        component = None
        getter = getattr(message, "get_srcSystem", None)
        if callable(getter):
            try:
                system = getter()
            except Exception:
                system = None
        getter = getattr(message, "get_srcComponent", None)
        if callable(getter):
            try:
                component = getter()
            except Exception:
                component = None
        header = getattr(message, "_header", None)
        if header is not None:
            if system is None:
                system = getattr(header, "srcSystem", getattr(header, "src_system", None))
            if component is None:
                component = getattr(header, "srcComponent", getattr(header, "src_component", None))
    try:
        system = None if system is None else int(system)
    except (TypeError, ValueError):
        system = None
    try:
        component = None if component is None else int(component)
    except (TypeError, ValueError):
        component = None
    return system, component


def _frame_message(frame: MavlinkFrame) -> dict[str, object] | None:
    """Decode the small common-message subset from an already split frame."""

    payload = frame.payload
    msgid = frame.message_id
    if msgid == MSG_HEARTBEAT:
        if len(payload) < 9:
            return None
        custom_mode, vehicle_type, autopilot, base_mode, status, version = struct.unpack_from(
            "<IBBBBB", payload
        )
        return {
            "custom_mode": custom_mode,
            "type_value": vehicle_type,
            "type": "HEARTBEAT",
            "vehicle_type": vehicle_type,
            "autopilot": autopilot,
            "base_mode": base_mode,
            "system_status": status,
            "mavlink_version": version,
            "source_system_id": frame.source_system_id,
            "source_component_id": frame.source_component_id,
        }
    if msgid == MSG_SYS_STATUS:
        if len(payload) < 19:
            return None
        return {
            "type": "SYS_STATUS",
            "battery_remaining": struct.unpack_from("<b", payload, 18)[0],
            "source_system_id": frame.source_system_id,
            "source_component_id": frame.source_component_id,
        }
    if msgid == MSG_GPS_RAW_INT:
        if len(payload) < 30:
            return None
        lat, lon, alt = struct.unpack_from("<iii", payload, 8)
        eph, epv = struct.unpack_from("<HH", payload, 20)
        # GPS_RAW_INT.vel/cog follow eph/epv at offsets 24/26.  They are
        # centimetres per second and centidegrees respectively; retaining
        # both fields keeps the raw-frame path equivalent to pymavlink
        # mappings, while ``_apply`` converts ``vel`` into m/s.
        velocity, course = struct.unpack_from("<HH", payload, 24)
        fix_type = payload[28]
        satellites = payload[29]
        return {
            "type": "GPS_RAW_INT",
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "eph": eph,
            "epv": epv,
            "vel": velocity,
            "cog": course,
            "fix_type": fix_type,
            "satellites_visible": satellites,
            "source_system_id": frame.source_system_id,
            "source_component_id": frame.source_component_id,
        }
    if msgid == MSG_GLOBAL_POSITION_INT:
        if len(payload) < 28:
            return None
        lat, lon, alt, relative_alt = struct.unpack_from("<iiii", payload, 4)
        vx, vy, vz, heading = struct.unpack_from("<hhhH", payload, 20)
        return {
            "type": "GLOBAL_POSITION_INT",
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "relative_alt": relative_alt,
            "vx": vx,
            "vy": vy,
            "vz": vz,
            "hdg": heading,
            "source_system_id": frame.source_system_id,
            "source_component_id": frame.source_component_id,
        }
    if msgid == MSG_VFR_HUD:
        if len(payload) < 20:
            return None
        airspeed, groundspeed, altitude, climb, heading, throttle = struct.unpack_from(
            "<ffffhH", payload
        )
        return {
            "type": "VFR_HUD",
            "airspeed": airspeed,
            "groundspeed": groundspeed,
            "alt": altitude,
            "climb": climb,
            "heading": heading,
            "throttle": throttle,
            "source_system_id": frame.source_system_id,
            "source_component_id": frame.source_component_id,
        }
    if msgid == MSG_ATTITUDE:
        if len(payload) < 16:
            return None
        _, roll, pitch, yaw = struct.unpack_from("<Ifff", payload)
        return {
            "type": "ATTITUDE",
            "roll": roll,
            "pitch": pitch,
            "yaw": yaw,
            "source_system_id": frame.source_system_id,
            "source_component_id": frame.source_component_id,
        }
    return {
        "type": f"MSG_{msgid}",
        "message_id": msgid,
        "source_system_id": frame.source_system_id,
        "source_component_id": frame.source_component_id,
    }


class SimulatorTelemetryBridge:
    """Fold observed PX4 MAVLink telemetry into the shared AI executor."""

    def __init__(
        self,
        executor: SimulatorAiExecutor,
        *,
        clock: Any = time.monotonic,
        px4_system_id: int = 1,
        px4_component_ids: frozenset[int] | set[int] | None = None,
        minimum_gps_fix_type: int = MAVLINK_QUALIFIED_FIX_TYPE_MIN,
        maximum_history: int = 256,
        require_heartbeat: bool = True,
        heartbeat_timeout_s: float | None = 2.0,
        wait_for_armed_heartbeat: bool = True,
        perception_timeout_s: float | None = DEFAULT_PERCEPTION_TIMEOUT_S,
    ) -> None:
        if not isinstance(executor, SimulatorAiExecutor):
            raise TypeError("executor must be a SimulatorAiExecutor")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(px4_system_id, bool) or not 1 <= int(px4_system_id) <= 255:
            raise ValueError("px4_system_id must be in 1..255")
        if (
            isinstance(minimum_gps_fix_type, bool)
            or not MAVLINK_QUALIFIED_FIX_TYPE_MIN
            <= int(minimum_gps_fix_type)
            <= MAVLINK_GPS_FIX_TYPE_MAX
        ):
            raise ValueError(
                "minimum_gps_fix_type must be in "
                f"{MAVLINK_QUALIFIED_FIX_TYPE_MIN}..{MAVLINK_GPS_FIX_TYPE_MAX}"
            )
        if isinstance(maximum_history, bool) or not 1 <= int(maximum_history) <= 10_000:
            raise ValueError("maximum_history must be in 1..10000")
        if heartbeat_timeout_s is not None and (
            isinstance(heartbeat_timeout_s, bool)
            or not _finite(heartbeat_timeout_s)
            or float(heartbeat_timeout_s) <= 0.0
        ):
            raise ValueError("heartbeat_timeout_s must be None or a finite value > 0")
        if perception_timeout_s is not None and (
            isinstance(perception_timeout_s, bool)
            or not _finite(perception_timeout_s)
            or float(perception_timeout_s) <= 0.0
        ):
            raise ValueError("perception_timeout_s must be None or a finite value > 0")
        if px4_component_ids is not None:
            if not px4_component_ids or any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
                for value in px4_component_ids
            ):
                raise ValueError("px4_component_ids must contain MAVLink component ids")
            px4_component_ids = frozenset(px4_component_ids)
        self.executor = executor
        self.clock = clock
        self.px4_system_id = int(px4_system_id)
        self.px4_component_ids = px4_component_ids
        self.minimum_gps_fix_type = int(minimum_gps_fix_type)
        self.require_heartbeat = bool(require_heartbeat)
        if not isinstance(wait_for_armed_heartbeat, bool):
            raise TypeError("wait_for_armed_heartbeat must be bool")
        self.wait_for_armed_heartbeat = wait_for_armed_heartbeat
        # PX4 emits HEARTBEAT at roughly 1 Hz in normal operation.  A two
        # second default gives one lost sample of tolerance while still
        # failing closed well before an operator could mistake stale state for
        # live telemetry.  ``None`` is useful for deterministic replay fixtures
        # that intentionally omit heartbeat traffic; it does not weaken the
        # executor's own telemetry/control watchdog.
        self.heartbeat_timeout_s = (
            None if heartbeat_timeout_s is None else float(heartbeat_timeout_s)
        )
        self.perception_timeout_s = (
            None if perception_timeout_s is None else float(perception_timeout_s)
        )
        self._lock = threading.RLock()
        self._decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
        self._latest: dict[str, object] = {
            "altitude_agl_m": 0.0,
            "heading_deg": 0.0,
            "ground_speed_mps": 0.0,
            "latitude_deg": math.nan,
            "longitude_deg": math.nan,
            "gps_fix_type": None,
            "gps_horizontal_accuracy_m": math.nan,
            "gps_vertical_accuracy_m": math.nan,
            "satellites_visible": None,
            "link_healthy": None,
            "armed": None,
            "battery_remaining_pct": math.nan,
            "observed_at_s": 0.0,
            # Per-field freshness is kept separately from the frame cursor so
            # an incomplete/out-of-order MAVLink frame cannot make an older
            # coordinate look current.
            "position_observed_at_s": None,
            # GPS_RAW_INT freshness is intentionally independent from the
            # fused/global-position cursor.  REAL Pixhawk telemetry uses this
            # timestamp for the last complete raw GPS fix, while
            # ``position_observed_at_s`` may be refreshed by
            # GLOBAL_POSITION_INT.
            "gps_observed_at_s": None,
            "altitude_observed_at_s": None,
            # Target values are owned by the perception seam, never by a
            # MAVLink vehicle frame.  Keeping them explicitly in the latest
            # mapping makes the lease/expiry semantics visible and ensures a
            # normal telemetry frame cannot accidentally resurrect old data.
            "target_visible": False,
            "target_bearing_deg": None,
            "target_distance_m": None,
            "target_confidence": None,
        }
        self._coordinate_cursor_s: float | None = None
        self._coordinate_source: str | None = None
        self._global_position_cursor_s: float | None = None
        self._global_position_invalid_s: float | None = None
        self._gps_raw_cursor_s: float | None = None
        self._altitude_cursor_s: float | None = None
        self._velocity_cursor_s: float | None = None
        self._heading_cursor_s: float | None = None
        self._heartbeat_seen = False
        self._last_heartbeat_at_s: float | None = None
        self._heartbeat_stale = False
        self._heartbeat_stale_since_s: float | None = None
        self._heartbeat_last_age_s: float | None = None
        self._heartbeat_stale_events = 0
        self._heartbeat_recoveries = 0
        self._heartbeat_startup_stale_wait = 0
        self._heartbeat_last_decision: SimulatorAiDecision | None = None
        # PX4 commonly publishes a disarmed heartbeat during startup.  Do not
        # feed that transient state into the executor's terminal FAILSAFE
        # authority before the vehicle has ever been armed; wait for one
        # observed armed heartbeat first.  After the gate is released, a real
        # disarm remains a hard failsafe condition.
        self._armed_observed = False
        self._startup_disarmed_wait = 0
        self._target_observed_at_s: float | None = None
        self._perception_observations = 0
        self._perception_accepted = 0
        self._perception_rejected = 0
        self._perception_expired = 0
        self._perception_rejections: deque[dict[str, object]] = deque(maxlen=int(maximum_history))
        self._last_perception: dict[str, object] | None = None
        self._history: deque[dict[str, object]] = deque(maxlen=int(maximum_history))
        self._rejections: deque[dict[str, object]] = deque(maxlen=int(maximum_history))
        self._frames_observed = 0
        self._frames_accepted = 0
        self._frames_rejected = 0
        self._snapshots = 0
        self._lock = threading.RLock()

    def __call__(
        self, message: object, timestamp_s: float | None = None
    ) -> SimulatorAiDecision | None:
        return self.ingest(message, timestamp_s=timestamp_s)

    def enable_ai(self) -> None:
        """Explicitly opt the shared executor into AI authority."""

        self.executor.enable_ai()

    enable = enable_ai

    def release_manual_control(self) -> None:
        self.executor.release_manual_control()

    def ingest_datagram(
        self, datagram: bytes | bytearray, timestamp_s: float | None = None
    ) -> tuple[SimulatorAiDecision | None, ...]:
        """Split a raw MAVLink datagram and ingest each decoded frame."""

        if not isinstance(datagram, (bytes, bytearray)):
            raise TypeError("datagram must be bytes")
        decisions: list[SimulatorAiDecision | None] = []
        for frame in self._decoder.feed(bytes(datagram)):
            decisions.append(self.ingest(frame, timestamp_s=timestamp_s))
        return tuple(decisions)

    def ingest(
        self, message: object, timestamp_s: float | None = None
    ) -> SimulatorAiDecision | None:
        """Validate one observed message and run one AI step when ready.

        Unknown but well-formed telemetry is retained as an accepted frame and
        does not trigger a policy step.  Explicitly wrong PX4 identity or a
        truncated known frame is rejected and recorded instead of reaching the
        executor.
        """

        with self._lock:
            self._frames_observed += 1
            stamp = self._timestamp(timestamp_s)
            normalized = self._normalize_message(message)
            if normalized is None:
                self._reject("unsupported_or_truncated_message", stamp)
                return None
            source_system, source_component = _source_ids(normalized)
            if source_system is not None and source_system != self.px4_system_id:
                self._reject("source_system_mismatch", stamp, source_system, source_component)
                return None
            if (
                self.px4_component_ids is not None
                and source_component is not None
                and source_component not in self.px4_component_ids
            ):
                self._reject("source_component_mismatch", stamp, source_system, source_component)
                return None
            self._frames_accepted += 1
            kind = _message_type(normalized).upper()
            self._apply(kind, normalized, stamp)
            self._history.append(
                {
                    "type": kind,
                    "observed_at_s": stamp,
                    "source_system_id": source_system,
                    "source_component_id": source_component,
                }
            )
            # A real vehicle's armed bit is learned only from an accepted PX4
            # heartbeat.  Perception frames never get to manufacture it.
            if self._latest.get("armed") is True:
                self._armed_observed = True
            return self._step_if_ready(stamp, kind)

    def ingest_perception(
        self,
        observation: SimulatorTelemetrySnapshot | Mapping[str, object],
        timestamp_s: float | None = None,
    ) -> SimulatorAiDecision | None:
        """Merge one truth-free target observation and run the shared AI step.

        Only ``target_visible``, ``target_bearing_deg``,
        ``target_distance_m`` and ``target_confidence`` are copied.  GPS,
        armed/link state, altitude, heading, battery and return fields remain
        exclusively owned by PX4 telemetry already accepted by :meth:`ingest`.
        The observation timestamp must be current, monotonic and within the
        configured perception lease.  A rejected observation clears the
        cached target and never reaches the executor.

        A mapping may omit its timestamp when the caller supplies
        ``timestamp_s``; in that case the frame is stamped at the receive
        time.  This is useful for a live perception callback while still
        retaining deterministic replay behaviour for explicit timestamps.
        """

        with self._lock:
            stamp = self._timestamp(timestamp_s)
            self._perception_observations += 1
            try:
                if isinstance(observation, SimulatorTelemetrySnapshot):
                    sample = observation
                elif isinstance(observation, Mapping):
                    values = dict(observation)
                    if not any(key in values for key in ("observed_at_s", "timestamp_s", "time_s")):
                        values["observed_at_s"] = stamp
                    sample = SimulatorTelemetrySnapshot.from_mapping(values)
                else:
                    raise TypeError("perception observation must be a snapshot or mapping")
            except (TypeError, ValueError) as exc:
                self._reject_perception(f"invalid_observation:{type(exc).__name__}", stamp)
                return None

            observed_at = float(sample.observed_at_s)
            if observed_at > stamp:
                self._reject_perception("future_observation", stamp, observed_at)
                return None
            age = max(0.0, stamp - observed_at)
            if self.perception_timeout_s is not None and age > self.perception_timeout_s:
                self._reject_perception("stale_observation", stamp, observed_at, age=age)
                return None
            previous = self._target_observed_at_s
            if previous is not None and observed_at < previous:
                self._reject_perception("out_of_order_observation", stamp, observed_at)
                return None

            # Whitelist target fields.  A perception provider cannot spoof
            # navigation/GPS/armed values by passing a full snapshot mapping.
            visible = bool(sample.target_visible)
            self._latest["target_visible"] = visible
            if visible:
                self._latest["target_bearing_deg"] = sample.target_bearing_deg
                self._latest["target_distance_m"] = sample.target_distance_m
                self._latest["target_confidence"] = sample.target_confidence
                self._target_observed_at_s = observed_at
            else:
                self._clear_target_observation()
            self._perception_accepted += 1
            self._latest["observed_at_s"] = stamp
            self._last_perception = {
                "accepted": True,
                "observed_at_s": observed_at,
                "received_at_s": stamp,
                "age_s": age,
                "target_visible": visible,
                "target_bearing_deg": sample.target_bearing_deg if visible else None,
                "target_distance_m": sample.target_distance_m if visible else None,
                "target_confidence": sample.target_confidence if visible else None,
                "source": "truth_free_perception_observation",
            }
            self._history.append(
                {
                    "type": "PERCEPTION_TARGET",
                    "observed_at_s": stamp,
                    "source_observed_at_s": observed_at,
                    "target_visible": visible,
                }
            )
            return self._step_if_ready(stamp, "PERCEPTION_TARGET")

    def watchdog_tick(self, *, now_s: float | None = None) -> SimulatorAiDecision | None:
        """Poll the executor watchdog from a router/event-loop owner."""

        stamp = self._timestamp(now_s)
        with self._lock:
            self._expire_target_observation(stamp)
            heartbeat_decision = self._enforce_heartbeat_freshness(stamp)
            if heartbeat_decision is not None:
                return heartbeat_decision
            if self._heartbeat_stale:
                return None
        try:
            return self.executor.watchdog_tick(now_s=stamp)
        except (RuntimeError, TypeError, ValueError) as exc:
            with self._lock:
                self._reject(f"watchdog_error:{type(exc).__name__}", stamp)
            return None

    def reset(self) -> None:
        """Clear observation/callback counters while retaining executor state."""

        with self._lock:
            self._latest.update(
                {
                    "altitude_agl_m": 0.0,
                    "heading_deg": 0.0,
                    "ground_speed_mps": 0.0,
                    "latitude_deg": math.nan,
                    "longitude_deg": math.nan,
                    "gps_fix_type": None,
                    "gps_horizontal_accuracy_m": math.nan,
                    "gps_vertical_accuracy_m": math.nan,
                    "satellites_visible": None,
                    "link_healthy": None,
                    "armed": None,
                    "battery_remaining_pct": math.nan,
                    "observed_at_s": 0.0,
                    "position_observed_at_s": None,
                    "gps_observed_at_s": None,
                    "altitude_observed_at_s": None,
                    "target_visible": False,
                    "target_bearing_deg": None,
                    "target_distance_m": None,
                    "target_confidence": None,
                }
            )
            self._coordinate_cursor_s = None
            self._coordinate_source = None
            self._global_position_cursor_s = None
            self._global_position_invalid_s = None
            self._gps_raw_cursor_s = None
            self._altitude_cursor_s = None
            self._velocity_cursor_s = None
            self._heading_cursor_s = None
            self._heartbeat_seen = False
            self._last_heartbeat_at_s = None
            self._heartbeat_stale = False
            self._heartbeat_stale_since_s = None
            self._heartbeat_last_age_s = None
            self._heartbeat_stale_events = 0
            self._heartbeat_recoveries = 0
            self._heartbeat_startup_stale_wait = 0
            self._heartbeat_last_decision = None
            self._armed_observed = False
            self._startup_disarmed_wait = 0
            self._target_observed_at_s = None
            self._perception_observations = 0
            self._perception_accepted = 0
            self._perception_rejected = 0
            self._perception_expired = 0
            self._perception_rejections.clear()
            self._last_perception = None
            self._history.clear()
            self._rejections.clear()
            self._frames_observed = 0
            self._frames_accepted = 0
            self._frames_rejected = 0
            self._snapshots = 0
            self._decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)

    def evidence(self) -> dict[str, object]:
        with self._lock:
            executor_evidence = self.executor.evidence()
            return {
                "schema_version": 1,
                "event": "simulator_live_ai_telemetry_bridge",
                "px4_identity": {
                    "system_id": self.px4_system_id,
                    "component_ids": None
                    if self.px4_component_ids is None
                    else sorted(self.px4_component_ids),
                },
                "enabled": bool(getattr(self.executor, "enabled", False)),
                "minimum_gps_fix_type": self.minimum_gps_fix_type,
                "require_heartbeat": self.require_heartbeat,
                "heartbeat_timeout_s": self.heartbeat_timeout_s,
                "heartbeat_seen": self._heartbeat_seen,
                "last_heartbeat_at_s": self._last_heartbeat_at_s,
                "heartbeat_age_s": self._heartbeat_last_age_s,
                "heartbeat_fresh": (
                    None
                    if not self._heartbeat_seen or self.heartbeat_timeout_s is None
                    else not self._heartbeat_stale
                ),
                "heartbeat_stale": self._heartbeat_stale,
                "heartbeat_stale_since_s": self._heartbeat_stale_since_s,
                "heartbeat_stale_events": self._heartbeat_stale_events,
                "heartbeat_recoveries": self._heartbeat_recoveries,
                "heartbeat_startup_stale_wait": self._heartbeat_startup_stale_wait,
                "heartbeat_gate": self._heartbeat_gate_state(),
                "heartbeat_last_decision": (
                    None
                    if self._heartbeat_last_decision is None
                    else self._heartbeat_last_decision.to_mapping()
                ),
                "frames_observed": self._frames_observed,
                "frames_accepted": self._frames_accepted,
                "frames_rejected": self._frames_rejected,
                "rejections": list(self._rejections),
                "snapshots": self._snapshots,
                "armed_observed": self._armed_observed,
                "wait_for_armed_heartbeat": self.wait_for_armed_heartbeat,
                "startup_disarmed_wait": self._startup_disarmed_wait,
                "startup_gate": (
                    "released"
                    if self._armed_observed or not self.wait_for_armed_heartbeat
                    else "waiting_for_armed_heartbeat"
                ),
                "perception_timeout_s": self.perception_timeout_s,
                "perception_observations": self._perception_observations,
                "perception_accepted": self._perception_accepted,
                "perception_rejected": self._perception_rejected,
                "perception_expired": self._perception_expired,
                "target_observed_at_s": self._target_observed_at_s,
                "perception_rejections": list(self._perception_rejections),
                "last_perception": (
                    None if self._last_perception is None else dict(self._last_perception)
                ),
                "last_snapshot": self._snapshot_mapping(),
                "ai_steps": int(executor_evidence.get("decisions", 0)),
                "ai_accepted": int(executor_evidence.get("accepted", 0)),
                "ai_rejected": int(executor_evidence.get("rejected", 0)),
                "failsafe_steps": int(executor_evidence.get("failsafe", 0)),
                "watchdog_failsafe_steps": int(executor_evidence.get("watchdog_failsafe", 0)),
                "executor": executor_evidence,
                "truth_free": True,
                "truth_free_input_contract": True,
                "arbitrary_flight_commands": False,
                "output_transport": "shared SimulatorMavlinkControlAdapter MANUAL_CONTROL",
                "real_control_acceptance": "not_claimed",
            }

    def _timestamp(self, value: float | None) -> float:
        raw = self.clock() if value is None else value
        if isinstance(raw, bool):
            raise ValueError("telemetry timestamp must be finite and non-negative")
        try:
            stamp = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("telemetry timestamp must be finite and non-negative") from exc
        if not math.isfinite(stamp) or stamp < 0.0:
            raise ValueError("telemetry timestamp must be finite and non-negative")
        return stamp

    @staticmethod
    def _normalize_message(message: object) -> object | None:
        if isinstance(message, MavlinkFrame):
            return _frame_message(message)
        kind = _message_type(message).upper()
        if not kind:
            msgid = _message_id(message)
            if msgid is None:
                return None
            kind = {
                MSG_HEARTBEAT: "HEARTBEAT",
                MSG_SYS_STATUS: "SYS_STATUS",
                MSG_GPS_RAW_INT: "GPS_RAW_INT",
                MSG_GLOBAL_POSITION_INT: "GLOBAL_POSITION_INT",
                MSG_VFR_HUD: "VFR_HUD",
                MSG_ATTITUDE: "ATTITUDE",
            }.get(msgid, "")
            if not kind:
                return None
        if isinstance(message, Mapping):
            # A few lightweight decoders expose only ``message_id``.  Add the
            # inferred name without mutating the caller's mapping.
            if not _message_type(message):
                mapped = dict(message)
                mapped["type"] = kind
                return mapped
            return message
        return message

    def _apply(self, kind: str, message: object, stamp: float) -> None:
        latest = self._latest
        latest["observed_at_s"] = stamp
        if kind == "HEARTBEAT":
            latest["link_healthy"] = True
            base_mode = _int_field(message, "base_mode", 0)
            latest["armed"] = bool(base_mode & _ARMED_FLAG)
            self._heartbeat_seen = True
            # Do not let an out-of-order replayed heartbeat move the freshness
            # cursor backwards.  The current frame still contributes its
            # observed armed/link fields, but the newest timestamp remains the
            # source of truth for the age calculation.
            previous = self._last_heartbeat_at_s
            if previous is None or stamp >= previous:
                self._last_heartbeat_at_s = stamp
                self._heartbeat_last_age_s = 0.0
                if self._heartbeat_stale:
                    self._heartbeat_stale = False
                    self._heartbeat_recoveries += 1
                    self._heartbeat_stale_since_s = None
        elif kind == "SYS_STATUS":
            battery = _int_field(message, "battery_remaining", -1)
            if 0 <= battery <= 100:
                latest["battery_remaining_pct"] = float(battery)
        elif kind == "GPS_RAW_INT":
            self._apply_gps_raw(message, stamp)
        elif kind == "GLOBAL_POSITION_INT":
            self._apply_global_position(message, stamp)
        elif kind == "VFR_HUD":
            speed = _float_field(message, "groundspeed")
            if math.isfinite(speed) and speed >= 0.0:
                latest["ground_speed_mps"] = speed
            heading = _int_field(message, "heading", 0)
            latest["heading_deg"] = float(heading) % 360.0
        # ATTITUDE is intentionally observed but not converted into an action;
        # the shared AI contract only needs the bounded telemetry fields above.

    @staticmethod
    def _timestamp_is_current(stamp: float, previous: float | None) -> bool:
        return previous is None or stamp >= previous

    def _apply_gps_raw(self, message: object, stamp: float) -> None:
        """Apply one GPS_RAW_INT sample atomically.

        A GPS quality tuple is meaningful only when its coordinates, fix,
        satellite count and horizontal accuracy came from the same complete
        sample.  Invalid/incomplete frames therefore clear the coordinate used
        by the bridge when they are newer than the cached sample; they never
        leave an old coordinate paired with fresh metadata.
        """

        latest = self._latest
        if not self._timestamp_is_current(stamp, self._gps_raw_cursor_s):
            return

        latitude_e7 = _wire_int32(_field(message, "lat"), unknown=_MAVLINK_INT32_MAX)
        longitude_e7 = _wire_int32(_field(message, "lon"), unknown=_MAVLINK_INT32_MAX)
        coordinates = _valid_coordinate_pair(latitude_e7, longitude_e7)

        fix_wire = _wire_integral(_field(message, "fix_type"), minimum=0, maximum=255)
        fix_type = normalize_gps_fix_type(fix_wire)
        satellites_wire = _wire_integral(
            _field(message, "satellites_visible"), minimum=0, maximum=255
        )
        satellites = normalize_satellites_visible(satellites_wire)
        # GPS_RAW_INT.alt is part of the atomic raw sample.  A missing or
        # INT32_MAX sentinel must not refresh ``gps_observed_at_s``: otherwise
        # an old coordinate can appear to have a current GPS fix even though
        # the frame cannot provide a complete navigation observation.
        altitude_mm = _wire_int32(_field(message, "alt"), unknown=_MAVLINK_INT32_MAX)
        eph = _wire_uint16(_field(message, "eph"), unknown=_MAVLINK_UINT16_MAX)
        epv = _wire_uint16(_field(message, "epv"), unknown=_MAVLINK_UINT16_MAX)
        velocity = _wire_uint16(_field(message, "vel"), unknown=_MAVLINK_UINT16_MAX)

        # Preserve per-frame diagnostics, including a legal degraded fix.  A
        # missing/sentinel field is represented as unavailable rather than as a
        # previous value.
        latest["gps_fix_type"] = fix_type
        latest["satellites_visible"] = satellites
        latest["gps_horizontal_accuracy_m"] = eph / 100.0 if eph is not None else math.nan
        latest["gps_vertical_accuracy_m"] = epv / 100.0 if epv is not None else math.nan
        if velocity is not None and self._timestamp_is_current(stamp, self._velocity_cursor_s):
            latest["ground_speed_mps"] = velocity / 100.0
            self._velocity_cursor_s = stamp

        complete = (
            coordinates is not None
            and fix_type is not None
            and fix_type >= self.minimum_gps_fix_type
            and satellites is not None
            and altitude_mm is not None
            and eph is not None
        )
        self._gps_raw_cursor_s = stamp
        if complete:
            # Keep the dedicated GPS sample cursor independent from the
            # estimator/global-position cursor.  A complete raw sample still
            # counts even when an out-of-order GLOBAL_POSITION_INT frame owns
            # a newer fused coordinate.
            latest["gps_observed_at_s"] = stamp
            if self._timestamp_is_current(stamp, self._coordinate_cursor_s):
                latitude_deg, longitude_deg = coordinates  # type: ignore[misc]
                latest["latitude_deg"] = latitude_deg
                latest["longitude_deg"] = longitude_deg
                latest["position_observed_at_s"] = stamp
                self._coordinate_cursor_s = stamp
                self._coordinate_source = "GPS_RAW_INT"
                # A complete newer raw sample is allowed to recover a prior
                # malformed global-position barrier.
                if (
                    self._global_position_invalid_s is None
                    or stamp >= self._global_position_invalid_s
                ):
                    self._global_position_invalid_s = None
            return

        # Fail closed for a degraded/incomplete sample.  Keep the diagnostic
        # fields above, but remove the coordinate so the snapshot cannot be
        # mistaken for a locatable GPS fix.
        latest["gps_observed_at_s"] = None
        if self._timestamp_is_current(stamp, self._coordinate_cursor_s):
            latest["latitude_deg"] = math.nan
            latest["longitude_deg"] = math.nan
            latest["position_observed_at_s"] = None
            self._coordinate_cursor_s = stamp
            self._coordinate_source = None

    def _apply_global_position(self, message: object, stamp: float) -> None:
        """Apply GLOBAL_POSITION_INT with per-field sentinel validation."""

        latest = self._latest
        relative_alt_mm = _wire_int32(_field(message, "relative_alt"), unknown=_MAVLINK_INT32_MAX)
        if relative_alt_mm is not None and self._timestamp_is_current(
            stamp, self._altitude_cursor_s
        ):
            latest["altitude_agl_m"] = max(0.0, relative_alt_mm / 1000.0)
            latest["altitude_observed_at_s"] = stamp
            self._altitude_cursor_s = stamp

        velocity_x = _wire_int16(_field(message, "vx"), unknown=_MAVLINK_INT16_MAX)
        velocity_y = _wire_int16(_field(message, "vy"), unknown=_MAVLINK_INT16_MAX)
        if (
            velocity_x is not None
            and velocity_y is not None
            and self._timestamp_is_current(stamp, self._velocity_cursor_s)
        ):
            latest["ground_speed_mps"] = math.hypot(velocity_x, velocity_y) / 100.0
            self._velocity_cursor_s = stamp

        heading = _wire_heading_cdeg(_field(message, "hdg"))
        if heading is not None and self._timestamp_is_current(stamp, self._heading_cursor_s):
            latest["heading_deg"] = heading / 100.0
            self._heading_cursor_s = stamp

        latitude_e7 = _wire_int32(_field(message, "lat"), unknown=_MAVLINK_INT32_MAX)
        longitude_e7 = _wire_int32(_field(message, "lon"), unknown=_MAVLINK_INT32_MAX)
        coordinates = _valid_coordinate_pair(latitude_e7, longitude_e7)
        global_cursor = self._global_position_cursor_s
        if self._global_position_invalid_s is not None:
            global_cursor = (
                self._global_position_invalid_s
                if global_cursor is None
                else max(global_cursor, self._global_position_invalid_s)
            )
        if not self._timestamp_is_current(stamp, global_cursor):
            return

        self._global_position_cursor_s = stamp
        if coordinates is not None and self._timestamp_is_current(stamp, self._coordinate_cursor_s):
            latest["latitude_deg"], latest["longitude_deg"] = coordinates
            latest["position_observed_at_s"] = stamp
            self._coordinate_cursor_s = stamp
            self._coordinate_source = "GLOBAL_POSITION_INT"
            self._global_position_invalid_s = None
            return

        # Missing, sentinel, or out-of-domain lat/lon is a hard barrier.  Do
        # not let a previous GPI coordinate combine with a newer GPS_RAW fix
        # tuple.  A raw sample that is strictly newer remains authoritative.
        self._global_position_invalid_s = stamp
        raw_is_newer = self._gps_raw_cursor_s is not None and self._gps_raw_cursor_s > stamp
        if not raw_is_newer:
            latest["gps_fix_type"] = None
            latest["satellites_visible"] = None
            latest["gps_horizontal_accuracy_m"] = math.nan
            latest["gps_vertical_accuracy_m"] = math.nan
            latest["gps_observed_at_s"] = None
            self._gps_raw_cursor_s = stamp
            if self._timestamp_is_current(stamp, self._coordinate_cursor_s):
                latest["latitude_deg"] = math.nan
                latest["longitude_deg"] = math.nan
                latest["position_observed_at_s"] = None
                self._coordinate_cursor_s = stamp
                self._coordinate_source = None

    def _snapshot(self, stamp: float) -> SimulatorTelemetrySnapshot:
        self._expire_target_observation(stamp)
        values = dict(self._latest)
        values["observed_at_s"] = stamp
        return SimulatorTelemetrySnapshot(**values)  # type: ignore[arg-type]

    def _step_if_ready(self, stamp: float, kind: str) -> SimulatorAiDecision | None:
        """Run the common heartbeat/armed/authority/executor gate.

        The caller must hold ``self._lock``.  ``PERCEPTION_TARGET`` is an
        internal observation kind and intentionally shares every gate with a
        MAVLink frame; it is not a second command path.
        """

        if self.require_heartbeat and not self._heartbeat_seen:
            return None
        # Heartbeat freshness is checked after applying the current frame.
        # Thus a heartbeat received at the boundary immediately refreshes the
        # link, while GPS/position/perception traffic cannot mask a stale
        # flight-controller heartbeat.  Once stale, suppress ordinary AI
        # steps until a fresh heartbeat explicitly recovers the gate.
        heartbeat_decision = self._enforce_heartbeat_freshness(stamp)
        if heartbeat_decision is not None:
            return heartbeat_decision
        if self._heartbeat_stale:
            return None
        if kind not in {
            "HEARTBEAT",
            "SYS_STATUS",
            "GPS_RAW_INT",
            "GLOBAL_POSITION_INT",
            "VFR_HUD",
            "ATTITUDE",
            "PERCEPTION_TARGET",
        }:
            return None
        if not self._armed_observed and self.wait_for_armed_heartbeat:
            if kind == "HEARTBEAT":
                self._startup_disarmed_wait += 1
            return None
        snapshot = self._snapshot(stamp)
        self._snapshots += 1
        try:
            return self.executor.step(snapshot, now_s=stamp)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._reject(f"ai_step_error:{type(exc).__name__}", stamp)
            return None

    def _clear_target_observation(self) -> None:
        """Clear the cached target without touching vehicle telemetry."""

        self._latest["target_visible"] = False
        self._latest["target_bearing_deg"] = None
        self._latest["target_distance_m"] = None
        self._latest["target_confidence"] = None
        self._target_observed_at_s = None

    def _expire_target_observation(self, stamp: float) -> None:
        """Expire a target lease before any policy snapshot is constructed."""

        observed_at = self._target_observed_at_s
        timeout = self.perception_timeout_s
        if observed_at is None or timeout is None:
            return
        age = float(stamp) - observed_at
        if age <= timeout:
            return
        self._clear_target_observation()
        self._perception_expired += 1
        self._last_perception = {
            "accepted": False,
            "reason": "target_lease_expired",
            "observed_at_s": observed_at,
            "received_at_s": stamp,
            "age_s": max(0.0, age),
            "source": "truth_free_perception_observation",
        }

    def _reject_perception(
        self,
        reason: str,
        stamp: float,
        observed_at: float | None = None,
        *,
        age: float | None = None,
    ) -> None:
        # Every rejected perception sample is a safety boundary.  Do not let a
        # future, stale, out-of-order, or malformed frame leave the previous
        # target leased into a later policy step.  Vehicle telemetry remains
        # untouched; only the perception-owned target tuple is cleared.
        self._clear_target_observation()
        self._perception_rejected += 1
        row: dict[str, object] = {"reason": reason, "received_at_s": stamp}
        if observed_at is not None:
            row["observed_at_s"] = observed_at
        if age is not None:
            row["age_s"] = age
        self._perception_rejections.append(row)
        self._last_perception = {
            "accepted": False,
            "reason": reason,
            "received_at_s": stamp,
            **({"observed_at_s": observed_at} if observed_at is not None else {}),
            **({"age_s": age} if age is not None else {}),
            "source": "truth_free_perception_observation",
        }

    def _snapshot_mapping(self) -> dict[str, object]:
        try:
            return self._snapshot(float(self._latest["observed_at_s"])).to_mapping()
        except (TypeError, ValueError):
            return {"truth_free": True}

    def _heartbeat_gate_state(self) -> str:
        if self.heartbeat_timeout_s is None:
            return "disabled"
        if not self._heartbeat_seen:
            return "waiting_for_first_heartbeat"
        if self._heartbeat_stale:
            return "stale"
        return "fresh"

    def _heartbeat_age(self, stamp: float) -> float | None:
        if self._last_heartbeat_at_s is None:
            return None
        # A caller can feed a replay stream with an older wall-clock stamp.
        # Treat that sample as age zero for freshness purposes so a replay
        # cannot spuriously trip this gate; the newest heartbeat cursor is
        # retained separately in ``_apply``.
        return max(0.0, float(stamp) - self._last_heartbeat_at_s)

    def _enforce_heartbeat_freshness(self, stamp: float) -> SimulatorAiDecision | None:
        """Fail closed once when the PX4 heartbeat age crosses the timeout.

        The caller must hold ``self._lock``.  The returned decision is the
        shared executor's normal policy result, so the adapter emits the same
        canonical neutral frame and authority transition used by every other
        safety fault.  Repeated stale packets are observed and retained but do
        not spam neutral frames or increment executor decisions indefinitely.
        """

        age = self._heartbeat_age(stamp)
        self._heartbeat_last_age_s = age
        timeout = self.heartbeat_timeout_s
        if timeout is None or age is None or age < timeout:
            return None
        if self._heartbeat_stale:
            return None

        self._heartbeat_stale = True
        self._heartbeat_stale_since_s = stamp
        self._heartbeat_stale_events += 1
        self._latest["link_healthy"] = False

        # The startup disarmed gate intentionally suppresses all executor
        # failsafes until PX4 has reported one armed heartbeat.  Mark the stale
        # condition for evidence, but do not turn a boot-time absence into a
        # terminal authority state before the vehicle was ever armed.
        if not self._armed_observed:
            self._heartbeat_startup_stale_wait += 1
            return None

        snapshot = self._snapshot(stamp)
        self._snapshots += 1
        try:
            decision = self.executor.step(snapshot, now_s=stamp)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._reject(f"heartbeat_stale_ai_step_error:{type(exc).__name__}", stamp)
            return None
        self._heartbeat_last_decision = decision
        return decision

    def _reject(
        self,
        reason: str,
        stamp: float,
        source_system: int | None = None,
        source_component: int | None = None,
    ) -> None:
        self._frames_rejected += 1
        self._rejections.append(
            {
                "reason": reason,
                "timestamp_s": stamp,
                "source_system_id": source_system,
                "source_component_id": source_component,
            }
        )


__all__ = ["DEFAULT_PERCEPTION_TIMEOUT_S", "SimulatorTelemetryBridge"]

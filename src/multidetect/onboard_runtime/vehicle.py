"""Transport-independent flight state and a serial/UDP PX4 MAVLink adapter.

Local coordinates use metres in PX4's estimator NED frame. Angles exposed to
the autonomy boundary use degrees; only MAVLink encoding uses radians.
"""
# ruff: noqa: E501

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from multidetect.sim_teacher.executor import SetpointStreamer
from multidetect.sim_teacher.streams import MavlinkReader

from .fw_vtol_commands import (
    CommandSpec,
    do_change_speed,
    do_land_start,
    do_orbit,
    do_reposition,
    do_vtol_transition,
    nav_land,
    nav_takeoff,
    nav_vtol_land,
    nav_vtol_takeoff,
    parse_custom_mode,
)
from .fw_vtol_commands import (
    custom_mode as encode_custom_mode,
)

Vector3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class PositionSetpoint:
    north_m: float
    east_m: float
    down_m: float
    yaw_deg: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (
            self.north_m, self.east_m, self.down_m, self.yaw_deg
        )):
            raise ValueError("setpoint fields must be finite")


@dataclass(frozen=True, slots=True)
class VehicleState:
    position_ned: Vector3 | None = None
    attitude_deg: Vector3 | None = None
    velocity_ned: Vector3 | None = None
    home_ned: Vector3 | None = None
    landed: bool | None = None
    armed: bool | None = None
    mode: str | None = None
    battery_remaining: float | None = None
    pose_monotonic: float | None = None
    attitude_monotonic: float | None = None
    heartbeat_monotonic: float | None = None
    home_geodetic: Vector3 | None = None
    home_monotonic: float | None = None
    battery_monotonic: float | None = None
    battery_status_monotonic: float | None = None
    landed_monotonic: float | None = None
    sensor_present: int | None = None
    sensor_enabled: int | None = None
    sensor_health: int | None = None
    estimator_flags: int | None = None
    gps_fix_type: int | None = None
    gps_satellites: int | None = None
    gps_hdop: float | None = None
    sys_status_monotonic: float | None = None
    estimator_monotonic: float | None = None
    gps_monotonic: float | None = None
    battery_voltage_v: float | None = None
    battery_current_a: float | None = None
    battery_consumed_mah: float | None = None
    vtol_state: int | None = None
    landed_state: int | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Fail-closed result for a newly added fixed-wing/VTOL command."""

    status: str
    command: int
    ack_result: int | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True, slots=True)
class TakeoffPreflightReport:
    method: str
    missing: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    values: dict[str, float] | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched


@runtime_checkable
class VehicleInterface(Protocol):
    def start(self) -> Any: ...

    def snapshot(self) -> VehicleState: ...

    def send_setpoint(self, setpoint: PositionSetpoint) -> None: ...

    def arm(self) -> None: ...

    def set_mode(self, mode: str) -> None: ...

    def land(self) -> None: ...

    def rtl(self) -> None: ...

    def loiter_at(self, center: Any, radius: float, alt: float, **kwargs: Any) -> CommandResult: ...

    def reposition(self, point: Any, alt: float, ground_speed: float = -1.0,
                   **kwargs: Any) -> CommandResult: ...

    def orbit(self, center: Any, radius: float, speed: float, **kwargs: Any) -> CommandResult: ...

    def vtol_transition(self, to: str | int, **kwargs: Any) -> CommandResult: ...

    def fw_takeoff(self, method: str = "runway", **kwargs: Any) -> CommandResult: ...

    def fw_land(self, method: str = "runway", **kwargs: Any) -> CommandResult: ...

    def close(self) -> None: ...



@runtime_checkable
class PreflightVehicle(VehicleInterface, Protocol):
    """Optional PX4-only capabilities used by the real-aircraft preflight gate."""

    def read_parameter(self, name: str, *, timeout_s: float = 1.0, retries: int = 3) -> float: ...

    def write_parameter(self, name: str, value: float, *, timeout_s: float = 1.0, retries: int = 3) -> float: ...

    def request_home_position(self) -> None: ...


def _vector(message: Mapping[str, Any], names: tuple[str, str, str]) -> Vector3 | None:
    try:
        values = tuple(float(message[name]) for name in names)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def _mode_name(custom_mode: Any) -> str | None:
    if isinstance(custom_mode, str):
        return custom_mode
    if not isinstance(custom_mode, int):
        return None
    main, sub = (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF
    # HOLD is retained as the historical multirotor-facing alias for
    # AUTO.LOITER.  ``parse_custom_mode`` exposes the canonical PX4 name.
    names = {(6, 0): "OFFBOARD", (4, 2): "AUTO_TAKEOFF", (4, 3): "HOLD",
             (4, 4): "AUTO_MISSION", (4, 5): "RTL", (4, 6): "LAND", (3, 0): "POSCTL"}
    return names.get((main, sub), f"PX4({main},{sub})")


class _VehicleReader(MavlinkReader):
    @staticmethod
    def _mapping(message: Any) -> dict[str, Any]:
        row = MavlinkReader._mapping(message)
        for key, method in (("_source_system", "get_srcSystem"),
                            ("_source_component", "get_srcComponent")):
            getter = getattr(message, method, None)
            if callable(getter):
                row[key] = getter()
        return row


class PymavlinkVehicle:
    """One telemetry receiver with serialized MAVLink sends.

    ``connection`` permits CPU-only fake-link tests. Without it, pymavlink is
    imported lazily by ``start``. The first autopilot heartbeat selects the
    peer unless explicit target IDs were supplied. Commands request actions;
    callers must verify resulting heartbeat/landed state before proceeding.
    """

    _MODES = {"OFFBOARD": (6, 0), "HOLD": (4, 3), "AUTO_LOITER": (4, 3),
              "AUTO_TAKEOFF": (4, 2), "AUTO_MISSION": (4, 4), "RTL": (4, 5),
              "AUTO_RTL": (4, 5), "LAND": (4, 6), "AUTO_LAND": (4, 6), "POSCTL": (3, 0)}

    encode_custom_mode = staticmethod(encode_custom_mode)
    parse_custom_mode = staticmethod(parse_custom_mode)
    _INTERVALS = ((32, 50_000), (30, 50_000), (33, 200_000),
                  (242, 1_000_000), (245, 200_000), (1, 1_000_000), (147, 1_000_000))

    def __init__(
        self,
        endpoint: str,
        *,
        baud: int = 921600,
        connection: Any = None,
        target_system: int | None = None,
        target_component: int | None = None,
        write_timeout_s: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("MAVLink endpoint must be nonempty")
        if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
            raise ValueError("baud must be a positive integer")
        for value in (target_system, target_component):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255
            ):
                raise ValueError("target MAVLink IDs must be in 1..255")
        if not math.isfinite(write_timeout_s) or write_timeout_s <= 0:
            raise ValueError("write_timeout_s must be positive and finite")
        self.endpoint = endpoint
        self.baud = baud
        self.connection = connection
        self.target_system = target_system
        self.target_component = target_component
        self._clock = clock
        self._write_timeout_s = write_timeout_s
        self._state = VehicleState()
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._reader: MavlinkReader | None = None
        self._closed = False
        self._streams_requested = False
        self._heartbeat_sent_at = float("-inf")
        self._parameters: dict[str, tuple[float, int, float]] = {}
        self._parameter_events: dict[str, threading.Event] = {}
        self._parameter_pending_since: dict[str, float] = {}
        self._parameter_transactions: dict[str, dict[str, Any]] = {}
        self._parameter_locks: dict[str, threading.Lock] = {}
        self._ack_events: dict[int, threading.Event] = {}
        self._acks: dict[int, tuple[int, float]] = {}

    def snapshot(self) -> VehicleState:
        if self._reader is not None:
            self._reader.raise_if_failed()
        with self._state_lock:
            return self._state

    def ingest(self, message: Mapping[str, Any], received_monotonic: float | None = None) -> None:
        """Decode one message; replay can inject receive-time-aligned telemetry."""
        kind = str(message.get("message_type", message.get("mavpackettype", "")))
        if not kind:
            kind = str(message.get("type", ""))
        stamp = self._clock() if received_monotonic is None else float(received_monotonic)
        if not math.isfinite(stamp):
            return
        source_system = message.get("_source_system")
        source_component = message.get("_source_component")
        with self._state_lock:
            if kind != "HEARTBEAT" and source_system is not None and self.target_system is None:
                return
            if (source_system is not None and self.target_system is not None
                    and source_system != self.target_system):
                return
            if (source_component is not None and self.target_component is not None
                    and source_component != self.target_component):
                return
            updates: dict[str, Any] = {}
            state = self._state
            if kind == "HEARTBEAT":
                if message.get("autopilot") == 8:
                    return
                if self.target_system is None and source_system is not None:
                    self.target_system = int(source_system)
                if self.target_component is None and source_component is not None:
                    self.target_component = int(source_component)
                if state.heartbeat_monotonic is not None and stamp < state.heartbeat_monotonic:
                    return
                updates.update(armed=bool(int(message.get("base_mode", 0)) & 128),
                               mode=_mode_name(message.get("custom_mode")),
                               heartbeat_monotonic=stamp)
            elif kind == "LOCAL_POSITION_NED":
                position = _vector(message, ("x", "y", "z"))
                if position is not None and (
                    state.pose_monotonic is None or stamp >= state.pose_monotonic
                ):
                    updates.update(position_ned=position,
                                   velocity_ned=_vector(message, ("vx", "vy", "vz")),
                                   pose_monotonic=stamp)
            elif kind == "ATTITUDE":
                angles = _vector(message, ("roll", "pitch", "yaw"))
                if angles is not None and (
                    state.attitude_monotonic is None or stamp >= state.attitude_monotonic
                ):
                    updates.update(attitude_deg=tuple(math.degrees(x) for x in angles),
                                   attitude_monotonic=stamp)
            elif kind == "HOME_POSITION":
                home = _vector(message, ("latitude", "longitude", "altitude"))
                if state.home_monotonic is None or stamp >= state.home_monotonic:
                    updates.update(home_ned=_vector(message, ("x", "y", "z")),
                                   home_geodetic=None if home is None else (
                                       home[0] / 1e7, home[1] / 1e7, home[2] / 1e3),
                                   home_monotonic=stamp)
            elif kind == "EXTENDED_SYS_STATE":
                value = message.get("landed_state")
                if value in (0, 1, 2, 3, 4) and (
                    state.landed_monotonic is None or stamp >= state.landed_monotonic
                ):
                    updates.update(landed=None if value == 0 else value == 1,
                                   landed_state=int(value), landed_monotonic=stamp)
                vtol = message.get("vtol_state")
                if isinstance(vtol, (int, float)) and int(vtol) in range(0, 7):
                    updates["vtol_state"] = int(vtol)
            elif kind == "COMMAND_ACK":
                command = message.get("command")
                result = message.get("result")
                if isinstance(command, (int, float)) and isinstance(result, (int, float)):
                    command_i, result_i = int(command), int(result)
                    self._acks[command_i] = (result_i, stamp)
                    self._ack_events.setdefault(command_i, threading.Event()).set()
            elif kind in ("SYS_STATUS", "BATTERY_STATUS"):
                value = message.get("battery_remaining")
                if (isinstance(value, (int, float)) and -1 <= value <= 100 and (
                    state.battery_monotonic is None or stamp >= state.battery_monotonic
                )):
                    updates.update(battery_remaining=None if value < 0 else float(value),
                                   battery_monotonic=stamp)
                if kind == "SYS_STATUS":
                    updates.update(sensor_present=message.get("onboard_control_sensors_present"),
                                   sensor_enabled=message.get("onboard_control_sensors_enabled"),
                                   sensor_health=message.get("onboard_control_sensors_health"),
                                   sys_status_monotonic=stamp)
                else:
                    voltage = message.get("voltage_battery")
                    current = message.get("current_battery")
                    consumed = message.get("current_consumed")
                    updates.update(
                        battery_status_monotonic=stamp,
                        battery_voltage_v=(float(voltage) / 1000 if isinstance(voltage, (int, float)) and 0 < voltage < 65535 else None),
                        battery_current_a=(float(current) / 100 if isinstance(current, (int, float)) and current >= 0 else None),
                        battery_consumed_mah=(float(consumed) if isinstance(consumed, (int, float)) and consumed >= 0 else None),
                    )
            elif kind == "ESTIMATOR_STATUS":
                flags = message.get("flags")
                if isinstance(flags, (int, float)) and int(flags) >= 0:
                    updates.update(estimator_flags=int(flags), estimator_monotonic=stamp)
            elif kind == "GPS_RAW_INT":
                fix, sats, eph = message.get("fix_type"), message.get("satellites_visible"), message.get("eph")
                if isinstance(fix, (int, float)):
                    updates.update(gps_fix_type=int(fix), gps_satellites=int(sats) if isinstance(sats, (int, float)) else None,
                                   gps_hdop=(None if eph in (None, 65535) else float(eph) / 100.0
                                             if isinstance(eph, (int, float)) and eph >= 0 else None),
                                   gps_monotonic=stamp)
            elif kind == "PARAM_VALUE":
                name = message.get("param_id", message.get("param_name"))
                if isinstance(name, bytes):
                    name = name.decode("ascii", "ignore")
                if isinstance(name, str) and isinstance(message.get("param_value"), (int, float)):
                    clean = name.rstrip("\x00")
                    ptype = int(message.get("param_type", 9))
                    raw = float(message["param_value"])
                    value = (float(struct.unpack("<i", struct.pack("<f", raw))[0])
                             if ptype == 6 else raw)
                    transaction = self._parameter_transactions.get(clean)
                    if transaction is not None and transaction.get("state") == "arming":
                        return
                    pending = self._parameter_pending_since.get(clean)
                    if pending is not None and stamp <= pending:
                        # A response queued before the request/set must not
                        # satisfy or overwrite the pending transaction.
                        return
                    if pending is not None and stamp > pending:
                        transaction = self._parameter_transactions.get(clean, {})
                        expected = transaction.get("expected")
                        type_expected = transaction.get("param_type")
                        if type_expected is not None and ptype != type_expected:
                            return
                        if expected is not None and not math.isclose(value, expected, rel_tol=0, abs_tol=0):
                            return
                    self._parameters[clean] = (value, ptype, stamp)
                    if pending is not None and stamp > pending:
                        event = self._parameter_events.setdefault(clean, threading.Event())
                        event.set()
            if updates:
                self._state = replace(state, **updates)
        if kind == "HEARTBEAT" and self._reader is not None and not self._streams_requested:
            self._request_streams()

    def start(self) -> PymavlinkVehicle:
        if self._closed:
            raise RuntimeError("vehicle is closed")
        if self._reader is not None:
            return self
        if self.connection is None:
            from pymavlink import mavutil

            endpoint = self.endpoint
            if endpoint.startswith("udp://"):
                endpoint = "udpin:" + endpoint.removeprefix("udp://")
            self.connection = mavutil.mavlink_connection(
                endpoint, baud=self.baud, source_system=245, source_component=191
            )
        port = getattr(self.connection, "port", None)
        if port is not None and hasattr(port, "write_timeout"):
            # pyserial otherwise permits an unbounded hardware-flow-control wait.
            port.write_timeout = self._write_timeout_s
        self._reader = _VehicleReader(self.connection, on_message=self.ingest)
        self._reader.start()
        return self

    def _target(self) -> tuple[int, int]:
        if self._closed:
            raise RuntimeError("vehicle is closed")
        if self.connection is None:
            raise RuntimeError("vehicle is not started")
        system = self.target_system or int(getattr(self.connection, "target_system", 0))
        component = self.target_component or int(getattr(self.connection, "target_component", 0))
        if not system or not component:
            raise RuntimeError("autopilot heartbeat is required before sending commands")
        return system, component

    @contextmanager
    def _sending(self):
        if not self._send_lock.acquire(timeout=2 * self._write_timeout_s + 0.05):
            raise TimeoutError("MAVLink sender is busy beyond the transport write deadline")
        try:
            yield
        finally:
            self._send_lock.release()

    def _command(self, command: int, *params: float) -> None:
        with self._sending():
            system, component = self._target()
            values = (*params, *([0.0] * (7 - len(params))))
            self.connection.mav.command_long_send(system, component, command, 0, *values)

    def _command_with_ack(self, spec: CommandSpec, *, timeout_s: float = 0.5) -> CommandResult:
        """Send a command and wait for COMMAND_ACK without escaping to the state machine."""
        if timeout_s < 0 or not math.isfinite(timeout_s):
            return CommandResult("failed", spec.command, detail="invalid ACK timeout")
        try:
            event = self._ack_events.setdefault(spec.command, threading.Event())
            event.clear()
            with self._sending():
                system, component = self._target()
                self.connection.mav.command_long_send(system, component, spec.command, 0, *spec.params)
            if timeout_s == 0:
                return CommandResult("sent", spec.command)
            if not event.wait(timeout_s):
                return CommandResult("timeout", spec.command, detail="COMMAND_ACK timeout")
            result, _ = self._acks.get(spec.command, (-1, 0.0))
            if result in (0, 5):  # MAV_RESULT_ACCEPTED / IN_PROGRESS
                return CommandResult("accepted", spec.command, result)
            return CommandResult("rejected", spec.command, result, f"MAV_RESULT={result}")
        except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
            return CommandResult("failed", spec.command, detail=str(exc))

    def read_parameter(self, name: str, *, timeout_s: float = 1.0, retries: int = 3) -> float:
        """Read one parameter.

        The cache is cleared before each request and the first PARAM_VALUE
        ingested after the send is accepted. PX4 parameters only change through
        our own writes, so a queued older reply cannot represent a different
        value without an intervening write transaction.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("parameter name must be nonempty")
        if timeout_s <= 0 or retries < 1:
            raise ValueError("parameter timeout and retries must be positive")
        clean = name.strip()
        lock = self._parameter_locks.setdefault(clean, threading.Lock())
        with lock:
            for _ in range(retries):
                event = self._parameter_events.setdefault(clean, threading.Event())
                event.clear()
                self._parameters.pop(clean, None)
                self._parameter_transactions[clean] = {"state": "arming"}
                with self._sending():
                    system, component = self._target()
                    sender = getattr(self.connection.mav, "param_request_read_send", None)
                    if sender is None:
                        raise RuntimeError("MAVLink connection lacks PARAM_REQUEST_READ")
                    sender(system, component, clean.encode("ascii"), -1)
                sent = self._clock()
                self._parameter_pending_since[clean] = sent
                self._parameter_transactions[clean] = {"state": "awaiting"}
                if event.wait(timeout_s):
                    value, _, received = self._parameters[clean]
                    if received > sent:
                        self._parameter_pending_since.pop(clean, None)
                        self._parameter_transactions.pop(clean, None)
                        return value
            self._parameter_transactions.pop(clean, None)
            raise TimeoutError(f"PARAM_VALUE timeout for {clean}")

    def write_parameter(self, name: str, value: float, *, timeout_s: float = 1.0, retries: int = 3) -> float:
        """Set a PX4 parameter and verify its readback; callers must opt in."""
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("parameter value must be finite")
        lock = self._parameter_locks.setdefault(name, threading.Lock())
        with lock:
            for _ in range(retries):
                with self._sending():
                    system, component = self._target()
                    sender = getattr(self.connection.mav, "param_set_send", None)
                    if sender is None:
                        raise RuntimeError("MAVLink connection lacks PARAM_SET")
                    ptype = self._parameters.get(name, (0.0, 6 if name in {
                        "NAV_DLL_ACT", "COM_DL_LOSS_T", "NAV_RCL_ACT", "COM_RC_IN_MODE",
                        "COM_OBL_RC_ACT", "GF_ACTION", "COM_LOW_BAT_ACT"} else 9, 0.0))[1]
                    wire = (struct.unpack("<f", struct.pack("<i", int(round(value))))[0]
                            if ptype == 6 else float(value))
                    self._parameter_transactions[name] = {
                        "state": "arming", "expected": float(value),
                        "param_type": ptype,
                    }
                    self._parameter_events.setdefault(name, threading.Event()).clear()
                    sender(system, component, name.encode("ascii"), wire, ptype)
                sent = self._clock()
                self._parameter_pending_since[name] = sent
                self._parameter_transactions[name]["state"] = "awaiting"
                event = self._parameter_events.setdefault(name, threading.Event())
                if event.wait(timeout_s):
                    actual, _, received = self._parameters[name]
                    if received > sent and math.isclose(actual, float(value), rel_tol=0, abs_tol=0):
                        self._parameter_pending_since.pop(name, None)
                        self._parameter_transactions.pop(name, None)
                        return actual
                if getattr(self.read_parameter, "__func__", None) is not PymavlinkVehicle.read_parameter:
                    actual = self.read_parameter(name, timeout_s=timeout_s, retries=1)
                    if math.isclose(actual, float(value), rel_tol=0, abs_tol=0):
                        return actual
            self._parameter_transactions.pop(name, None)
            raise TimeoutError(f"parameter write/readback failed for {name}")

    def request_home_position(self) -> None:
        self._command(512, 242.0)

    def _request_streams(self) -> None:
        self._streams_requested = True
        for message_id, interval_us in self._INTERVALS:
            self._command(511, float(message_id), float(interval_us))

    def send_setpoint(self, setpoint: PositionSetpoint) -> None:
        with self._sending():
            system, component = self._target()
            now = self._clock()
            if now - self._heartbeat_sent_at >= 1.0:
                self.connection.mav.heartbeat_send(18, 8, 0, 0, 4)
                self._heartbeat_sent_at = now
            self.connection.mav.set_position_target_local_ned_send(
                int(now * 1000) & 0xFFFFFFFF, system, component,
                SetpointStreamer.FRAME_LOCAL_NED, SetpointStreamer.TYPE_MASK,
                setpoint.north_m, setpoint.east_m, setpoint.down_m,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, math.radians(setpoint.yaw_deg), 0.0,
            )

    def arm(self) -> None:
        self._command(400, 1.0)

    def set_mode(self, mode: str) -> None:
        try:
            main, sub = self._MODES[mode.upper()]
        except KeyError as exc:
            raise ValueError(f"unsupported PX4 mode: {mode}") from exc
        self._command(176, 1.0, float(main), float(sub))

    def land(self) -> None:
        self._command(21)

    def rtl(self) -> None:
        self._command(20)

    def loiter_at(self, center: Any, radius: float, alt: float, *,
                  speed: float = -1.0, timeout_s: float = 0.5) -> CommandResult:
        """Start an orbit around a WGS84 ``(lat, lon[, alt])`` center.

        Fixed-wing PX4 cannot hover.  Orbit/reposition is therefore preferred
        to repeatedly streaming a local position target, which PX4 interprets
        as fly-to-and-loiter for a plane.
        """
        try:
            return self._command_with_ack(
                do_orbit(center, radius=radius, speed=speed, alt=alt), timeout_s=timeout_s
            )
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 34, detail=str(exc))

    def reposition(self, point: Any, alt: float, ground_speed: float = -1.0, *,
                   radius: float = 0.0, yaw_deg: float = 0.0,
                   timeout_s: float = 0.5) -> CommandResult:
        """Fly to a WGS84 point and let PX4 maintain a fixed-wing loiter."""
        try:
            return self._command_with_ack(
                do_reposition(point, alt=alt, ground_speed=ground_speed, radius=radius,
                              yaw_deg=yaw_deg),
                timeout_s=timeout_s,
            )
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 192, detail=str(exc))

    def orbit(self, center: Any, radius: float, speed: float, *,
              yaw_behavior: float = 0.0, yaw_deg: float = 0.0,
              alt: float | None = None, timeout_s: float = 0.5) -> CommandResult:
        try:
            return self._command_with_ack(
                do_orbit(center, radius=radius, speed=speed, yaw_behavior=yaw_behavior,
                         yaw_deg=yaw_deg, alt=alt), timeout_s=timeout_s
            )
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 34, detail=str(exc))

    def vtol_transition(self, to: str | int, *, timeout_s: float = 0.5) -> CommandResult:
        try:
            return self._command_with_ack(do_vtol_transition(to), timeout_s=timeout_s)
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 3000, detail=str(exc))

    def fw_takeoff(self, method: str = "runway", *, alt: float, point: Any = (0.0, 0.0),
                   pitch_deg: float = 0.0, yaw_deg: float = 0.0,
                   timeout_s: float = 0.5) -> CommandResult:
        """Issue a fixed-wing/VTOL takeoff command.

        ``hand_launch`` and ``catapult`` rely on PX4 launch detection; ``runway``
        uses ``RWTO_TKOFF``; ``vtol`` emits NAV_VTOL_TAKEOFF.
        """
        normalized = method.strip().lower()
        try:
            if normalized == "vtol":
                spec = nav_vtol_takeoff(alt=alt, pitch_deg=pitch_deg, yaw_deg=yaw_deg, point=point)
            elif normalized in {"runway", "hand_launch", "catapult", "hand", "launch"}:
                spec = nav_takeoff(alt=alt, pitch_deg=pitch_deg, yaw_deg=yaw_deg, point=point)
            else:
                raise ValueError("takeoff method must be runway, hand_launch, catapult or vtol")
            return self._command_with_ack(spec, timeout_s=timeout_s)
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 84 if normalized == "vtol" else 22, detail=str(exc))

    def fw_land(self, method: str = "runway", *, point: Any = (0.0, 0.0),
                alt: float = 0.0, yaw_deg: float = 0.0,
                parachute_params: Mapping[str, float] | None = None,
                timeout_s: float = 0.5) -> CommandResult:
        normalized = method.strip().lower()
        try:
            if normalized == "vtol":
                return self._command_with_ack(
                    nav_vtol_land(point=point, alt=alt, yaw_deg=yaw_deg), timeout_s=timeout_s
                )
            if normalized == "parachute":
                values = parachute_params or {}
                if not values.get("COM_PARACHUTE", 0):
                    return CommandResult("failed", 21, detail="COM_PARACHUTE is not enabled")
                # PX4 parachute trigger/output parameters are airframe-specific;
                # keep this as an explicit interface until the pinned mapping is verified.
                return CommandResult("failed", 21, detail="parachute trigger mapping needs verification")
            if normalized not in {"runway", "belly", "mission"}:
                raise ValueError("land method must be runway, belly, parachute or vtol")
            start = self._command_with_ack(do_land_start(), timeout_s=timeout_s)
            if start.status not in {"accepted", "sent"}:
                return start
            return self._command_with_ack(
                nav_land(point=point, alt=alt, yaw_deg=yaw_deg), timeout_s=timeout_s
            )
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 85 if normalized == "vtol" else 21, detail=str(exc))

    def change_speed(self, speed_mps: float, *, speed_type: float = 1.0,
                     timeout_s: float = 0.5) -> CommandResult:
        try:
            return self._command_with_ack(
                do_change_speed(speed_mps, speed_type=speed_type), timeout_s=timeout_s
            )
        except (ValueError, TypeError) as exc:
            return CommandResult("failed", 178, detail=str(exc))

    def check_takeoff_preflight(self, method: str, parameters: Mapping[str, Any] | None = None,
                                *, reader: Callable[[str], float] | None = None) -> TakeoffPreflightReport:
        """Check only PX4 parameter names present in the pinned reference.

        ``parameters`` is convenient for deterministic tests; real vehicles may
        pass ``read_parameter`` as ``reader``. Missing reads are reported rather
        than treated as safe.
        """
        normalized = method.strip().lower()
        required: dict[str, Callable[[float], bool]]
        if normalized in {"hand", "hand_launch", "catapult", "launch"}:
            normalized = "hand_launch"
            required = {"FW_LAUN_DETCN_ON": lambda v: v == 1,
                        "FW_LAUN_AC_THLD": lambda v: v > 0,
                        "FW_LAUN_AC_T": lambda v: v > 0}
        elif normalized == "runway":
            required = {"RWTO_TKOFF": lambda v: v == 1}
        elif normalized == "vtol":
            required = {"VT_TYPE": lambda v: v in {0, 1, 2},
                        "VT_ARSP_TRANS": lambda v: v > 0,
                        "VT_FW_MIN_ALT": lambda v: v >= 0,
                        "VT_TRANS_TIMEOUT": lambda v: v > 0}
        else:
            return TakeoffPreflightReport(normalized, mismatched=("unsupported method",))
        values: dict[str, float] = {}
        missing: list[str] = []
        mismatched: list[str] = []
        for name, predicate in required.items():
            try:
                raw = parameters[name] if parameters is not None else (reader or self.read_parameter)(name)
                value = float(raw)
                values[name] = value
                if not math.isfinite(value) or not predicate(value):
                    mismatched.append(name)
            except (KeyError, TypeError, ValueError, TimeoutError, RuntimeError, OSError):
                missing.append(name)
        return TakeoffPreflightReport(normalized, tuple(missing), tuple(mismatched), values)

    preflight_takeoff = check_takeoff_preflight

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.stop()
        if self.connection is not None:
            acquired = self._send_lock.acquire(timeout=2 * self._write_timeout_s + 0.05)
            try:
                # Closing the link also interrupts a sender stalled in external I/O.
                self.connection.close()
            finally:
                if acquired:
                    self._send_lock.release()

    def __enter__(self) -> PymavlinkVehicle:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()


def check_takeoff_preflight(method: str, parameters: Mapping[str, Any]) -> TakeoffPreflightReport:
    """Pure convenience wrapper for preflight code that has a parameter snapshot."""
    return PymavlinkVehicle("preflight").check_takeoff_preflight(method, parameters)


preflight_takeoff = check_takeoff_preflight

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


@runtime_checkable
class VehicleInterface(Protocol):
    def start(self) -> Any: ...

    def snapshot(self) -> VehicleState: ...

    def send_setpoint(self, setpoint: PositionSetpoint) -> None: ...

    def arm(self) -> None: ...

    def set_mode(self, mode: str) -> None: ...

    def land(self) -> None: ...

    def rtl(self) -> None: ...

    def close(self) -> None: ...

    def read_parameter(self, name: str, *, timeout_s: float = 1.0, retries: int = 3) -> float: ...

    def write_parameter(self, name: str, value: float, *, timeout_s: float = 1.0, retries: int = 3) -> float: ...


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
    names = {(6, 0): "OFFBOARD", (4, 3): "HOLD", (4, 5): "RTL", (4, 6): "LAND"}
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

    _MODES = {"OFFBOARD": (6, 0), "HOLD": (4, 3), "RTL": (4, 5), "LAND": (4, 6)}
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
                                   landed_monotonic=stamp)
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
                    self._parameters[clean] = (value, ptype, stamp)
                    pending = self._parameter_pending_since.get(clean)
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

    def read_parameter(self, name: str, *, timeout_s: float = 1.0, retries: int = 3) -> float:
        """Request a PX4 parameter and return PARAM_VALUE, with bounded retries."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("parameter name must be nonempty")
        if timeout_s <= 0 or retries < 1:
            raise ValueError("parameter timeout and retries must be positive")
        clean = name.strip()
        for _ in range(retries):
            sent = self._clock()
            event = self._parameter_events.setdefault(clean, threading.Event())
            event.clear()
            self._parameter_pending_since[clean] = sent
            with self._sending():
                system, component = self._target()
                sender = getattr(self.connection.mav, "param_request_read_send", None)
                if sender is None:
                    raise RuntimeError("MAVLink connection lacks PARAM_REQUEST_READ")
                sender(system, component, clean.encode("ascii"), -1)
            if event.wait(timeout_s):
                value, _, received = self._parameters[clean]
                if received > sent:
                    self._parameter_pending_since.pop(clean, None)
                    return value
        raise TimeoutError(f"PARAM_VALUE timeout for {clean}")

    def write_parameter(self, name: str, value: float, *, timeout_s: float = 1.0, retries: int = 3) -> float:
        """Set a PX4 parameter and verify its readback; callers must opt in."""
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("parameter value must be finite")
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
                sender(system, component, name.encode("ascii"), wire, ptype)
            try:
                actual = self.read_parameter(name, timeout_s=timeout_s, retries=1)
            except TimeoutError:
                continue
            if math.isclose(actual, float(value), rel_tol=0, abs_tol=1e-3):
                return actual
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

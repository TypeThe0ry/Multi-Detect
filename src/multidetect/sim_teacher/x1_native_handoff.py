"""Live X1 native handoff: one route writer, one exact PX4 command, telemetry readback.

The collector's compiled route and the native PX4 mode must never write at the
same time. ``RouteWriteGate`` wraps the route executor's MAVLink link. Its
claim waits for an in-flight route send, then refuses every later route write
for the rest of the episode. ``NativeHandoffPort`` is the only writer after the
claim. It sends exactly the admitted action, never retries or substitutes one,
and reports PX4 ACK plus mode readback. An unconfirmed takeover stays
unconfirmed; PX4's own failsafe remains the floor.
"""
from __future__ import annotations

import math
import threading
import time

from multidetect.autonomy.x1_contingency_executor import CommandReceipt
from multidetect.onboard_runtime.fw_vtol_commands import (
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    PX4_CUSTOM_MAIN_MODE_AUTO,
    PX4_CUSTOM_SUB_MODE_AUTO_LAND,
    PX4_CUSTOM_SUB_MODE_AUTO_LOITER,
    PX4_CUSTOM_SUB_MODE_AUTO_RTL,
    CommandSpec,
    do_orbit,
)

MAV_CMD_DO_SET_MODE = 176
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
ACK_ACCEPTED = frozenset({0, 5})  # MAV_RESULT_ACCEPTED, MAV_RESULT_IN_PROGRESS
ACK_TIMEOUT_S = 3.0
CONFIRM_TIMEOUT_S = 10.0
ORBIT_TARGET_TOLERANCE_M = 2.0  # COMMAND_LONG carries float32 degrees (about 0.4 m).
AUTO_RTL = (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_RTL)
AUTO_LAND = (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_LAND)
AUTO_LOITER = (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_LOITER)
_READ_ONLY = frozenset({"wait_command_ack", "wait_message", "recv_match"})


class RouteWriteRevoked(RuntimeError):
    """A route write was attempted after the native handoff claim."""


def px4_mode(custom_mode):
    if type(custom_mode) is not int:
        return None
    return (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF


class RouteWriteGate:
    """Single owner of compiled-route MAVLink writes. A claim never reopens it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.owner = None
        self.blocked = []
        self._stops = []

    def add_stop(self, stop):
        """Register a route-side streamer stop (for example an OFFBOARD setpoint loop)."""
        self._stops.append(stop)

    def guard(self, name, function):
        def guarded(*args, **kwargs):
            with self._lock:
                if self.owner is not None:
                    self.blocked.append(name)
                    raise RouteWriteRevoked(f"route write {name} refused after native handoff to {self.owner}")
                return function(*args, **kwargs)
        return guarded

    def wrap(self, link):
        return _GatedLink(link, self)

    def claim(self, owner):
        # Taking the lock waits out a route send already on the wire.
        with self._lock:
            if self.owner is None:
                self.owner = owner
        for stop in self._stops:
            stop()
        return True


class _GatedMav:
    def __init__(self, mav, gate):
        self._mav, self._gate = mav, gate

    def __getattr__(self, name):
        value = getattr(self._mav, name)
        if callable(value) and (name == "send" or name.endswith("_send")):
            return self._gate.guard(f"mav.{name}", value)
        return value


class _GatedLink:
    """The route executor's view of the vehicle link after the gate is installed."""

    def __init__(self, link, gate):
        self._link, self._gate = link, gate

    def __getattr__(self, name):
        value = getattr(self._link, name)
        if name == "mav":
            return _GatedMav(value, self._gate)
        if callable(value) and name not in _READ_ONLY:
            return self._gate.guard(name, value)
        return value


def _set_mode(sub_mode, name):
    return CommandSpec(MAV_CMD_DO_SET_MODE, (float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
                                             float(PX4_CUSTOM_MAIN_MODE_AUTO), float(sub_mode), 0.0, 0.0, 0.0, 0.0),
                       name)


class NativeHandoffPort:
    """Send one admitted native action after the route claim and read PX4 back.

    ``wait_ack(command, timeout_s, after_ns)`` reads ACKs from the collector's
    MAVLink reader. Confirmation needs a target-peer HEARTBEAT received after
    the ACK in the expected PX4 mode. A loiter also needs a newer
    ``POSITION_TARGET_GLOBAL_INT`` at the commanded hold point. A mode that was
    already active before the command cannot confirm a takeover by itself.
    """

    def __init__(self, connection, reader, gate, record, *, wait_ack, peer, local_from_wgs84=None,
                 ack_timeout_s=ACK_TIMEOUT_S, confirm_timeout_s=CONFIRM_TIMEOUT_S,
                 monotonic_ns=time.monotonic_ns):
        self.connection, self.reader, self.gate, self.record = connection, reader, gate, record
        self.wait_ack, self.peer = wait_ack, peer
        self.local_from_wgs84 = local_from_wgs84
        self.ack_timeout_s, self.confirm_timeout_s = ack_timeout_s, confirm_timeout_s
        self.monotonic_ns = monotonic_ns
        self.pending = None
        self.history = []

    # Executor claim_native(): stop the ordinary writer before any native send.
    def claim(self):
        return self.gate.claim("native") is True

    @property
    def status(self):
        return None if self.pending is None else self.pending["status"]

    @property
    def action(self):
        return None if self.pending is None else self.pending["action"]

    def confirmed(self, *actions):
        return self.pending is not None and self.pending["status"] == "confirmed" and (
            not actions or self.pending["action"] in actions)

    def rtl(self):
        return self._command("return", CommandSpec(MAV_CMD_NAV_RETURN_TO_LAUNCH, (0.0,) * 7,
                                                   "MAV_CMD_NAV_RETURN_TO_LAUNCH"), AUTO_RTL)

    def land(self):
        return self._command("land-now", _set_mode(PX4_CUSTOM_SUB_MODE_AUTO_LAND, "MAV_CMD_DO_SET_MODE(AUTO.LAND)"),
                             AUTO_LAND)

    def set_mode(self, mode):
        if mode != "HOLD":
            raise ValueError(f"unsupported native mode: {mode}")
        return self._command("loiter", _set_mode(PX4_CUSTOM_SUB_MODE_AUTO_LOITER,
                                                 "MAV_CMD_DO_SET_MODE(AUTO.LOITER)"), AUTO_LOITER, hold="here")

    def loiter_at(self, centre, radius, altitude, *, speed):
        spec = do_orbit(tuple(centre), radius=radius, speed=speed, alt=altitude)
        return self._command("loiter", spec, AUTO_LOITER, hold=(float(centre[0]), float(centre[1])))

    def _peer_row(self, state, name):
        message = state.get(name)
        if message is None:
            return None, None
        row = dict(message) if isinstance(message, dict) else dict(message.to_dict())
        for key, getter in (("_source_system", "get_srcSystem"), ("_source_component", "get_srcComponent")):
            method = getattr(message, getter, None)
            if callable(method):
                row[key] = method()
        system, component = self.peer
        if row.get("_source_system") != system or (component and row.get("_source_component") != component):
            return None, None
        return row, state.received_monotonic_ns.get(name)

    def _state(self):
        return self.reader.latest_control_state()

    def _mode(self, state):
        row, _ = self._peer_row(state, "HEARTBEAT")
        return px4_mode(row.get("custom_mode")) if row is not None else None

    def _here(self, state):
        row, _ = self._peer_row(state, "GLOBAL_POSITION_INT")
        if row is None or type(row.get("lat")) is not int or type(row.get("lon")) is not int:
            return None
        return row["lat"] / 1e7, row["lon"] / 1e7

    def _command(self, action, spec, expected, *, hold=None):
        if self.gate.owner is None:
            raise RuntimeError("native command requires the route writer claim")
        if self.pending is not None and self.pending["status"] == "pending":
            raise RuntimeError("previous native command is still unconfirmed")
        state = self._state()
        target = self._here(state) if hold == "here" else hold
        mav = self.connection.mav
        system, component = self.peer
        sent_ns = self.monotonic_ns()
        mav.command_long_send(system, component, spec.command, 0, *spec.params)
        ack = self.wait_ack(spec.command, self.ack_timeout_s, after_ns=sent_ns)
        ack_ns = self.monotonic_ns()
        result = None if ack is None else (ack.get("result") if isinstance(ack, dict) else getattr(ack, "result", None))
        result = int(result) if type(result) is int else None
        pending = {"action": action, "command": spec.command, "command_name": spec.name,
                   "params": list(spec.params), "expected_mode": list(expected),
                   "mode_before": list(self._mode(state) or ()) or None, "hold_target": list(target) if target else None,
                   "sent_monotonic_ns": sent_ns, "ack_monotonic_ns": ack_ns, "ack_result": result,
                   "deadline_monotonic_ns": ack_ns + int(self.confirm_timeout_s * 1e9), "status": "pending"}
        self.pending = pending
        self.history.append(pending)
        self.record({"message_type": "scenario_native_command", "receive_monotonic_ns": ack_ns,
                     **{k: v for k, v in pending.items() if k != "status"}})
        if result is None:
            self._finish("unconfirmed", "ack_timeout")
        elif result not in ACK_ACCEPTED:
            self._finish("unconfirmed", "ack_denied")
        elif hold == "here" and target is None:
            self._finish("unconfirmed", "hold_point_unknown")
        return CommandReceipt(ok=result in ACK_ACCEPTED, ack_result=result)

    def _finish(self, status, reason, **fields):
        pending = self.pending
        pending["status"] = status
        self.record({"message_type": "scenario_native_handoff", "receive_monotonic_ns": self.monotonic_ns(),
                     "action": pending["action"], "status": status, "reason": reason,
                     "command": pending["command"], "ack_result": pending["ack_result"],
                     "expected_mode": pending["expected_mode"], "mode_before": pending["mode_before"], **fields})

    def _target_matches(self, state, after_ns, target, tolerance_m):
        row, received = self._peer_row(state, "POSITION_TARGET_GLOBAL_INT")
        if row is None or received is None or received <= after_ns:
            return None
        lat, lon = row.get("lat_int"), row.get("lon_int")
        if type(lat) is not int or type(lon) is not int:
            return None
        if self.local_from_wgs84 is not None:
            a = self.local_from_wgs84(lat / 1e7, lon / 1e7)
            b = self.local_from_wgs84(*target)
            distance = math.dist(a, b)
        else:
            # Small-angle WGS-84 sphere approximation, metres.
            north = math.radians(lat / 1e7 - target[0]) * 6_371_000
            east = math.radians(lon / 1e7 - target[1]) * 6_371_000 * math.cos(math.radians(target[0]))
            distance = math.hypot(north, east)
        return distance if distance <= tolerance_m else None

    def poll(self, *, hold_tolerance_m=ORBIT_TARGET_TOLERANCE_M):
        """Advance confirmation once; never sends anything."""
        pending = self.pending
        if pending is None or pending["status"] != "pending":
            return self.status
        state = self._state()
        row, received = self._peer_row(state, "HEARTBEAT")
        mode = px4_mode(row.get("custom_mode")) if row is not None else None
        fresh = received is not None and received > pending["ack_monotonic_ns"]
        expected = tuple(pending["expected_mode"])
        target_error = None
        if fresh and mode == expected:
            if pending["hold_target"] is not None:
                target_error = self._target_matches(state, pending["ack_monotonic_ns"], pending["hold_target"],
                                                    hold_tolerance_m)
                if target_error is not None:
                    self._finish("confirmed", "mode_and_hold_target_observed", observed_mode=list(mode),
                                 hold_target_error_m=target_error)
                    return self.status
            elif tuple(pending["mode_before"] or ()) != expected:
                self._finish("confirmed", "mode_observed", observed_mode=list(mode))
                return self.status
        if self.monotonic_ns() >= pending["deadline_monotonic_ns"]:
            if mode == expected and pending["hold_target"] is not None:
                reason = "hold_target_not_observed"
            elif mode == expected and fresh:
                reason = "mode_unchanged_unverifiable"
            else:
                reason = "mode_not_observed"
            self._finish("unconfirmed", reason, observed_mode=list(mode) if mode else None)
        return self.status

    def landed(self):
        state = self._state()
        row, received = self._peer_row(state, "EXTENDED_SYS_STATE")
        if row is None or self.pending is None or received is None or received <= self.pending["ack_monotonic_ns"]:
            return None
        if row.get("landed_state") != 1:
            return None
        return {"landed_state": 1, "vtol_state": row.get("vtol_state")}


def reader_ack_waiter(reader, *, sleep=time.sleep, monotonic=time.monotonic):
    """Read COMMAND_ACK from the collector reader's per-command cache (never recv_match)."""

    def wait(command, timeout_s, after_ns):
        deadline = monotonic() + max(0.0, float(timeout_s))
        while True:
            entry = getattr(reader.state, "command_acks", {}).get(int(command))
            if entry is not None and entry[1] >= after_ns:
                message = entry[0]
                result = message.get("result") if isinstance(message, dict) else getattr(message, "result", None)
                return {"command": int(command), "result": result}
            if monotonic() >= deadline:
                return None
            sleep(0.02)

    return wait

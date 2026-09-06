"""Loopback-only MAVLink control output for the PX4 SITL simulator.

This module is deliberately small and lives below both the QGC/manual bridge
and the AI mission executor.  It is the only SIM control transport: callers
submit normalized commands, the authority state machine arbitrates them, and
only then is a MAVLink ``MANUAL_CONTROL`` frame emitted to PX4 SITL.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from .control import (
    ControlAuthorityState,
    ControlSetpoint,
    SimulatorControlAuthority,
)
from .isolation import SimulationIsolationError


class SimulatorMavlinkControlError(RuntimeError):
    """Raised when a SIM-only MAVLink control request is unsafe or invalid."""


# Accept both pymavlink's historical unbracketed IPv6 spelling
# (``udpout:::1:<port>``) and the URI-style bracketed spelling
# (``udpout:[::1]:<port>``).  The endpoint is kept verbatim for evidence and
# for the connection factory; only the host/port are parsed for isolation.
_LOOPBACK_UDP_OUT = re.compile(
    r"^udpout:(?:127\.0\.0\.1:(?P<ipv4_port>\d{1,5})|(?:\[::1\]|::1):(?P<ipv6_port>\d{1,5}))$"
)


@dataclass(frozen=True, slots=True)
class AiControlEnvelope:
    """Conservative normalized bounds for AI actions in SIM.

    Human manual commands retain their full normalized range.  AI commands
    are bounded independently so a faulty decision cannot send an extreme
    control input while still preserving the same PX4/Gazebo dynamics path.
    """

    max_roll: float = 0.50
    max_pitch: float = 0.50
    max_yaw: float = 0.60
    min_throttle: float = 0.15
    max_throttle: float = 0.80

    def __post_init__(self) -> None:
        values = (
            self.max_roll,
            self.max_pitch,
            self.max_yaw,
            self.min_throttle,
            self.max_throttle,
        )
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in values
        ):
            raise ValueError("AI control envelope values must be finite numbers")
        if not 0 < self.max_roll <= 1 or not 0 < self.max_pitch <= 1 or not 0 < self.max_yaw <= 1:
            raise ValueError("AI roll, pitch and yaw limits must be in (0, 1]")
        if not 0 <= self.min_throttle <= self.max_throttle <= 1:
            raise ValueError("AI throttle envelope must be within [0, 1]")

    def validate(self, *, roll: float, pitch: float, yaw: float, throttle: float) -> None:
        _validate_normalized_command(roll=roll, pitch=pitch, yaw=yaw, throttle=throttle)
        if abs(roll) > self.max_roll:
            raise SimulatorMavlinkControlError("AI roll exceeds SIM envelope")
        if abs(pitch) > self.max_pitch:
            raise SimulatorMavlinkControlError("AI pitch exceeds SIM envelope")
        if abs(yaw) > self.max_yaw:
            raise SimulatorMavlinkControlError("AI yaw exceeds SIM envelope")
        if not self.min_throttle <= throttle <= self.max_throttle:
            raise SimulatorMavlinkControlError("AI throttle exceeds SIM envelope")


@dataclass(frozen=True, slots=True)
class SimulatorMavlinkControlConfig:
    """MAVLink target restricted to one literal loopback PX4 SITL endpoint."""

    endpoint: str = "udpout:127.0.0.1:14540"
    target_system: int = 1
    source_system: int = 245
    source_component: int = 190
    watchdog_s: float = 0.5
    ai_envelope: AiControlEnvelope = AiControlEnvelope()
    # QGroundControl emits thrust in the legacy [0, 1000] wire range even
    # though MAVLink also documents a signed [-1000, 1000] representation.
    # Keep QGC compatibility as the default and expose signed mode explicitly
    # for adapters talking to a controller that requires it.
    throttle_wire_mode: str = "qgc_legacy"
    # An interactive QGC session must keep sending MANUAL_CONTROL frames.  A
    # missing frame stream is fail-closed only when this optional deadline is
    # enabled; headless/deterministic callers retain the historical disabled
    # default for backwards compatibility.
    manual_watchdog_s: float | None = None

    def __post_init__(self) -> None:
        match = _LOOPBACK_UDP_OUT.fullmatch(self.endpoint)
        if match is None:
            raise SimulationIsolationError(
                "SIM MAVLink output must use literal udpout:127.0.0.1:<port> or udpout:::1:<port>"
            )
        port = int(match.group("ipv4_port") or match.group("ipv6_port"))
        if not 1 <= port <= 65535:
            raise SimulationIsolationError("SIM MAVLink port must be in [1, 65535]")
        for name, value in (
            ("target_system", self.target_system),
            ("source_system", self.source_system),
            ("source_component", self.source_component),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"{name} must be in [1, 255]")
        if (
            isinstance(self.watchdog_s, bool)
            or not isinstance(self.watchdog_s, (int, float))
            or not math.isfinite(float(self.watchdog_s))
        ):
            raise ValueError("watchdog_s must be finite")
        if not 0 < self.watchdog_s <= 10:
            raise ValueError("watchdog_s must be in (0, 10]")
        if self.throttle_wire_mode not in {"qgc_legacy", "signed"}:
            raise ValueError("throttle_wire_mode must be 'qgc_legacy' or 'signed'")
        if self.manual_watchdog_s is not None:
            if (
                isinstance(self.manual_watchdog_s, bool)
                or not isinstance(self.manual_watchdog_s, (int, float))
                or not math.isfinite(float(self.manual_watchdog_s))
            ):
                raise ValueError("manual_watchdog_s must be finite or None")
            if not 0 < float(self.manual_watchdog_s) <= 10:
                raise ValueError("manual_watchdog_s must be in (0, 10] or None")


@dataclass(frozen=True, slots=True)
class SimulatorControlReceipt:
    source: str
    state: str
    endpoint: str
    target_system: int
    x: int
    y: int
    z: int
    r: int
    timestamp_s: float


class SimulatorMavlinkControlAdapter:
    """Map manual/AI normalized commands to PX4 SITL ``MANUAL_CONTROL``.

    The adapter intentionally has no endpoint selection API.  Its config is
    validated at construction and only accepts a literal loopback ``udpout``
    URI, so SIM cannot inherit a serial, HIL, or REAL output route.
    """

    def __init__(
        self,
        authority: SimulatorControlAuthority,
        *,
        config: SimulatorMavlinkControlConfig | None = None,
        connection: Any | None = None,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(authority, SimulatorControlAuthority):
            raise TypeError("authority must be a SimulatorControlAuthority")
        self.authority = authority
        self.config = config or SimulatorMavlinkControlConfig()
        self._connection = connection
        self._connection_factory = connection_factory
        self._owns_connection = connection is None
        self._last_ai_output_s: float | None = None
        self._last_manual_output_s: float | None = None
        self._message_count = 0
        self._source_counts: Counter[str] = Counter()
        self._watchdog_count = 0
        self._manual_watchdog_count = 0
        self._last_receipt: SimulatorControlReceipt | None = None
        # The QGC gateway and AI executor run on different threads in an
        # interactive job.  Serialize authority arbitration and MAVLink
        # emission so a manual takeover cannot interleave with an AI frame.
        self._lock = threading.RLock()

    @property
    def message_count(self) -> int:
        with self._lock:
            return self._message_count

    @property
    def last_receipt(self) -> SimulatorControlReceipt | None:
        with self._lock:
            return self._last_receipt

    def submit_manual(
        self,
        *,
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        timestamp_s: float | None = None,
    ) -> SimulatorControlReceipt:
        """Forward a pilot command and immediately revoke AI authority."""
        with self._lock:
            _validate_normalized_command(roll=roll, pitch=pitch, yaw=yaw, throttle=throttle)
            setpoint = self.authority.submit_manual(
                roll=roll,
                pitch=pitch,
                yaw=yaw,
                throttle=throttle,
                timestamp_s=timestamp_s,
            )
            receipt = self._send(setpoint)
            self._last_ai_output_s = None
            self._last_manual_output_s = setpoint.timestamp_s
            return receipt

    def submit_ai(
        self,
        *,
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        timestamp_s: float | None = None,
    ) -> SimulatorControlReceipt:
        """Forward one bounded AI action after authority arbitration."""
        with self._lock:
            self.config.ai_envelope.validate(roll=roll, pitch=pitch, yaw=yaw, throttle=throttle)
            stamp = _timestamp_or_monotonic(timestamp_s, name="timestamp_s")
            setpoint = self.authority.submit_ai(
                roll=roll,
                pitch=pitch,
                yaw=yaw,
                throttle=throttle,
                timestamp_s=stamp,
            )
            receipt = self._send(setpoint)
            self._last_ai_output_s = stamp
            # The first accepted AI frame after a manual release proves that
            # the old pilot stream is no longer the active source.
            self._last_manual_output_s = None
            return receipt

    def check_watchdog(self, *, now_s: float | None = None) -> SimulatorControlReceipt | None:
        """Neutralize stale AI or (when enabled) manual output.

        Manual freshness is evaluated only while the authority remains in
        ``MANUAL``.  Releasing manual control therefore cannot inherit a
        stale pilot timestamp into a subsequent AI/paused state.
        """
        with self._lock:
            if self.authority.state is not ControlAuthorityState.MANUAL:
                self._last_manual_output_s = None
            manual_stamp = (
                self._last_manual_output_s
                if self.config.manual_watchdog_s is not None
                and self.authority.state is ControlAuthorityState.MANUAL
                else None
            )
            if self._last_ai_output_s is None and manual_stamp is None:
                return None
            current_s = _timestamp_or_monotonic(now_s, name="now_s")
            if self._last_ai_output_s is not None:
                if current_s - self._last_ai_output_s < self.config.watchdog_s:
                    return None
                self.authority.failsafe("AI MAVLink control watchdog expired")
                self._last_ai_output_s = None
                self._last_manual_output_s = None
                self._watchdog_count += 1
                return self._send_neutral(timestamp_s=current_s, source="watchdog")
            assert manual_stamp is not None
            assert self.config.manual_watchdog_s is not None
            if current_s - manual_stamp < self.config.manual_watchdog_s:
                return None
            self.authority.failsafe("Manual MAVLink control watchdog expired")
            self._last_manual_output_s = None
            self._watchdog_count += 1
            self._manual_watchdog_count += 1
            return self._send_neutral(timestamp_s=current_s, source="manual_watchdog")

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if self._owns_connection and connection is not None:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()

    def shutdown(self, *, timestamp_s: float | None = None) -> SimulatorControlReceipt | None:
        """Fail-safe an active AI/manual stream and emit one neutral setpoint."""
        with self._lock:
            manual_active = (
                self._last_manual_output_s is not None
                and self.authority.state is ControlAuthorityState.MANUAL
            )
            if self._last_ai_output_s is None and not manual_active:
                return None
            stamp = _timestamp_or_monotonic(timestamp_s, name="timestamp_s")
            self.authority.failsafe("SIM control adapter shutdown")
            self._last_ai_output_s = None
            self._last_manual_output_s = None
            self._watchdog_count += 1
            return self._send_neutral(timestamp_s=stamp, source="shutdown")

    def request_failsafe(
        self,
        reason: str,
        *,
        timestamp_s: float | None = None,
    ) -> SimulatorControlReceipt:
        """Enter failsafe and emit one canonical neutral MANUAL_CONTROL frame.

        AI policy code uses this explicit method for sensor/telemetry faults;
        it avoids reaching into private adapter state while guaranteeing the
        same neutral output and evidence path as watchdog/shutdown.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failsafe reason must not be empty")
        with self._lock:
            stamp = _timestamp_or_monotonic(timestamp_s, name="timestamp_s")
            self.authority.failsafe(reason.strip())
            self._last_ai_output_s = None
            self._last_manual_output_s = None
            self._watchdog_count += 1
            return self._send_neutral(timestamp_s=stamp, source="failsafe")

    def evidence(self) -> dict[str, object]:
        """Return non-secret control-route evidence for the simulator job."""
        with self._lock:
            return {
                "transport": "MAVLink MANUAL_CONTROL",
                "endpoint": self.config.endpoint,
                "literal_loopback_only": True,
                "target_system": self.config.target_system,
                "source_system": self.config.source_system,
                "source_component": self.config.source_component,
                "watchdog_s": self.config.watchdog_s,
                "manual_watchdog_s": self.config.manual_watchdog_s,
                "throttle_wire_mode": self.config.throttle_wire_mode,
                "authority_state": self.authority.state.value,
                "messages_sent": self._message_count,
                "source_counts": dict(sorted(self._source_counts.items())),
                "watchdog_neutral_count": self._watchdog_count,
                "manual_watchdog_neutral_count": self._manual_watchdog_count,
                "manual_control_active": bool(
                    self._last_manual_output_s is not None
                    and self.authority.state is ControlAuthorityState.MANUAL
                ),
                "last_source": None if self._last_receipt is None else self._last_receipt.source,
            }

    def _connect(self) -> Any:
        if self._connection is not None:
            return self._connection
        factory = self._connection_factory
        if factory is None:
            try:
                from pymavlink import mavutil
            except ImportError as exc:  # pragma: no cover - optional dependency boundary.
                raise SimulatorMavlinkControlError(
                    "Install MAVLink support with: pip install -e '.[pixhawk]'"
                ) from exc
            factory = mavutil.mavlink_connection
        self._connection = factory(
            self.config.endpoint,
            source_system=self.config.source_system,
            source_component=self.config.source_component,
        )
        return self._connection

    def _send(self, setpoint: ControlSetpoint) -> SimulatorControlReceipt:
        if setpoint.source not in {"manual", "ai"}:
            raise SimulatorMavlinkControlError("unsupported control setpoint source")
        x, y, z, r = _manual_control_axes_for_mode(setpoint, mode=self.config.throttle_wire_mode)
        return self._emit(
            source=setpoint.source,
            x=x,
            y=y,
            z=z,
            r=r,
            timestamp_s=setpoint.timestamp_s,
        )

    def _send_neutral(self, *, timestamp_s: float, source: str) -> SimulatorControlReceipt:
        # Neutral thrust is encoded differently by the two supported
        # MANUAL_CONTROL wire conventions: QGC's legacy range uses 500 for
        # midpoint, while signed MAVLink uses 0.  Emitting 500 in signed mode
        # would command half-positive thrust during a failsafe.
        neutral_z = 500 if self.config.throttle_wire_mode == "qgc_legacy" else 0
        return self._emit(source=source, x=0, y=0, z=neutral_z, r=0, timestamp_s=timestamp_s)

    def _emit(
        self, *, source: str, x: int, y: int, z: int, r: int, timestamp_s: float
    ) -> SimulatorControlReceipt:
        connection = self._connect()
        mav = getattr(connection, "mav", None)
        send = getattr(mav, "manual_control_send", None)
        if not callable(send):
            raise SimulatorMavlinkControlError("MAVLink connection has no manual_control_send")
        send(self.config.target_system, x, y, z, r, 0)
        self._message_count += 1
        self._source_counts[source] += 1
        receipt = SimulatorControlReceipt(
            source,
            self.authority.state.value,
            self.config.endpoint,
            self.config.target_system,
            x,
            y,
            z,
            r,
            timestamp_s,
        )
        self._last_receipt = receipt
        return receipt


def _validate_normalized_command(*, roll: float, pitch: float, yaw: float, throttle: float) -> None:
    values = {"roll": roll, "pitch": pitch, "yaw": yaw, "throttle": throttle}
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
        for value in values.values()
    ):
        raise SimulatorMavlinkControlError("SIM control values must be finite numbers")
    if any(abs(float(values[name])) > 1 for name in ("roll", "pitch", "yaw")):
        raise SimulatorMavlinkControlError("SIM roll, pitch and yaw must be within [-1, 1]")
    if not 0 <= float(throttle) <= 1:
        raise SimulatorMavlinkControlError("SIM throttle must be within [0, 1]")


def _timestamp_or_monotonic(value: float | None, *, name: str) -> float:
    """Validate an adapter timestamp without Python's ``bool``-as-int trap."""

    if value is None:
        return monotonic()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _manual_control_axes(setpoint: ControlSetpoint) -> tuple[int, int, int, int]:
    """Convert normalized roll/pitch/yaw/throttle to MAVLink MANUAL_CONTROL."""
    # The default adapter mode is QGC/PX4's historical thrust range [0,1000].
    # This helper remains intentionally pure; the mode-aware conversion lives
    # in ``_manual_control_axes_for_mode`` below.
    return _manual_control_axes_for_mode(setpoint, mode="qgc_legacy")


def _manual_control_axes_for_mode(
    setpoint: ControlSetpoint, *, mode: str
) -> tuple[int, int, int, int]:
    if mode not in {"qgc_legacy", "signed"}:
        raise ValueError("unsupported throttle wire mode")
    throttle = (
        round(setpoint.throttle * 1000)
        if mode == "qgc_legacy"
        else round(setpoint.throttle * 2000 - 1000)
    )
    return (
        round(setpoint.pitch * 1000),
        round(setpoint.roll * 1000),
        throttle,
        round(setpoint.yaw * 1000),
    )


__all__ = [
    "AiControlEnvelope",
    "SimulatorControlReceipt",
    "SimulatorMavlinkControlAdapter",
    "SimulatorMavlinkControlConfig",
    "SimulatorMavlinkControlError",
]

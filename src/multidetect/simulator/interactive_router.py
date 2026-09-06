"""Interactive loopback-only MAVLink proxy for the SIM runtime.

The acceptance router in :mod:`multidetect.px4_gazebo_qgc_router` deliberately
has a read-only QGC contract.  This module is the separate runtime path used
when a user actually flies a PX4 SITL vehicle from the native QGC Fly View.

There are two independent UDP legs:

``QGC -> router -> PX4``
    ``MANUAL_CONTROL`` is decoded and passed through the shared simulator
    authority/adapter.  A small read-only allowlist (heartbeats, parameter
    reads, timesync and protocol discovery) is forwarded byte-for-byte.  All
    other QGC frames are dropped.

``PX4 -> router -> QGC``
    Telemetry is accepted only from a loopback PX4 endpoint and is forwarded
    byte-for-byte to QGC's isolated local receive port.  No payload is
    rewritten and no network interface other than loopback can participate.

The class exposes ``process_qgc_datagram`` and ``process_px4_datagram`` so
tests, deterministic fixtures and an orchestrator can drive the exact same
parser without opening sockets.  ``run`` supplies the production UDP event
loop.  Send callbacks are injectable, which keeps the parser testable without
requiring pymavlink or a running PX4 process.
"""

from __future__ import annotations

import argparse
import json
import math
import select
import socket
import struct
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..qgc_readonly_bridge import (
    MSG_AUTOPILOT_VERSION_REQUEST,
    MSG_COMMAND_LONG,
    MSG_FILE_TRANSFER_PROTOCOL,
    MSG_HEARTBEAT,
    MSG_MISSION_ACK,
    MSG_MISSION_REQUEST,
    MSG_MISSION_REQUEST_INT,
    MSG_MISSION_REQUEST_LIST,
    MSG_PARAM_EXT_REQUEST_LIST,
    MSG_PARAM_EXT_REQUEST_READ,
    MSG_PARAM_REQUEST_LIST,
    MSG_PARAM_REQUEST_READ,
    MSG_PING,
    MSG_REQUEST_DATA_STREAM,
    MSG_TIMESYNC,
    READ_ONLY_FTP_OPCODES,
    MavlinkFrame,
    MavlinkFrameDecoder,
    qgc_frame_is_read_only,
)
from .control_gateway import MSG_MANUAL_CONTROL
from .isolation import SimulationIsolationError

LOOPBACK_HOST = "127.0.0.1"
IPV6_LOOPBACK_HOST = "::1"
PROTECTED_GROUND_STATION_PORT = 14550
QGC_SYSTEM_ID = 255
QGC_COMPONENT_ID = 190
PX4_SYSTEM_ID = 1

# QGC's isolated UDP link normally binds the local receive port and sends its
# outbound frames from that same socket.  The router defaults are intentionally
# different from PX4's ordinary 14540 endpoint and from protected ground
# station port 14550.
DEFAULT_QGC_INPUT_PORT = 14650
DEFAULT_QGC_OUTPUT_PORT = 14649
DEFAULT_PX4_INPUT_PORT = 14541
DEFAULT_PX4_OUTPUT_PORT = 14540

ThrottleWireMode = Literal["auto", "qgc_legacy", "signed"]
QgcControlMode = Literal["read_only", "sim_flight"]
SendCallback = Callable[[bytes, tuple[str, int]], Any]
# A telemetry sink observes an already validated PX4 frame and its local
# monotonic receive timestamp.  The sink is deliberately side-band: the
# router still forwards ``frame.encoded`` byte-for-byte to QGC and a sink
# failure never changes that transport decision.
TelemetrySink = Callable[[MavlinkFrame, float], Any]
# A watchdog sink receives the router's monotonic event-loop timestamp.  It is
# used by the live AI bridge to run both its HEARTBEAT freshness gate and the
# executor/adapter watchdog from the same poll cadence.  The callback is
# side-band and never changes QGC/PX4 datagram forwarding semantics.
WatchdogSink = Callable[[float], Any]
# Optional in-process lifecycle sink used by the deterministic closed-loop
# fixture.  Production PX4 jobs leave this unset and continue forwarding raw
# COMMAND_LONG/SET_MODE frames to PX4.
LifecycleSink = Any

# The default interactive path remains read-only apart from MANUAL_CONTROL.
# ``sim_flight`` is an explicit opt-in for the native QGC Fly View.  Keep this
# list deliberately small: every command below has a bounded, field-level
# validator in ``_sim_flight_command_rejection_reason``.
MSG_SET_MODE = 11
MSG_MISSION_COUNT = 44
MSG_MISSION_ITEM_INT = 73
MSG_MISSION_ITEM = 39

MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
MAV_CMD_MISSION_START = 300
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_DO_VTOL_TRANSITION = 3000

SIM_FLIGHT_COMMAND_ALLOWLIST = frozenset(
    {
        MAV_CMD_COMPONENT_ARM_DISARM,
        MAV_CMD_DO_VTOL_TRANSITION,
        MAV_CMD_NAV_RETURN_TO_LAUNCH,
        MAV_CMD_NAV_LAND,
        MAV_CMD_MISSION_START,
    }
)
SIM_FLIGHT_MISSION_COMMAND_ALLOWLIST = frozenset(
    {
        16,  # MAV_CMD_NAV_WAYPOINT
        19,  # MAV_CMD_NAV_LOITER_TIME
        MAV_CMD_NAV_RETURN_TO_LAUNCH,
        MAV_CMD_NAV_LAND,
        22,  # MAV_CMD_NAV_TAKEOFF
        31,  # MAV_CMD_NAV_LOITER_TO_ALT
        84,  # MAV_CMD_NAV_VTOL_TAKEOFF
        85,  # MAV_CMD_NAV_VTOL_LAND
        MAV_CMD_DO_VTOL_TRANSITION,
    }
)
SIM_FLIGHT_GLOBAL_INT_FRAMES = frozenset({5, 6, 11})
SIM_FLIGHT_MISSION_FRAME = 2
SIM_FLIGHT_MAX_MISSION_ITEMS = 64

# PX4 custom main modes.  OFFBOARD (6) and TERMINATED (10) are intentionally
# absent; both would hand authority to an unbounded external actuator path.
SIM_FLIGHT_MAIN_MODES = frozenset({1, 2, 3, 4, 5, 7, 8, 9})
SIM_FLIGHT_AUTO_SUB_MODES = frozenset(range(1, 12))


class SimulatorInteractiveRouterError(RuntimeError):
    """Raised when the interactive SIM proxy contract is invalid."""


@dataclass(frozen=True, slots=True)
class SimulatorInteractiveRouterConfig:
    """One isolated four-endpoint loopback topology.

    ``qgc_input`` is the target port QGC writes to.  ``qgc_output`` is the
    local QGC receive port to which PX4 telemetry is sent.  ``px4_input`` is
    the port on which the router receives PX4 telemetry; ``px4_output`` is the
    loopback SITL command endpoint.  Optional source-port checks can be set to
    make endpoint identity strict for a particular launch profile while the
    default remains compatible with QGC/PX4 builds that choose ephemeral
    source ports.
    """

    qgc_input_host: str = LOOPBACK_HOST
    qgc_input_port: int = DEFAULT_QGC_INPUT_PORT
    qgc_output_host: str = LOOPBACK_HOST
    qgc_output_port: int = DEFAULT_QGC_OUTPUT_PORT
    px4_input_host: str = LOOPBACK_HOST
    px4_input_port: int = DEFAULT_PX4_INPUT_PORT
    px4_output_endpoint: str = f"udpout:{LOOPBACK_HOST}:{DEFAULT_PX4_OUTPUT_PORT}"
    qgc_source_port: int | None = None
    px4_source_port: int | None = None
    qgc_system_id: int = QGC_SYSTEM_ID
    qgc_component_id: int = QGC_COMPONENT_ID
    target_system: int = PX4_SYSTEM_ID
    qgc_target_component_ids: frozenset[int] = frozenset({0, 1})
    # ``auto`` accepts QGC's historical [0, 1000] thrust wire representation
    # and the signed MAVLink [-1000, 1000] representation.  Negative values
    # are unambiguously signed; non-negative values retain QGC compatibility.
    throttle_wire_mode: ThrottleWireMode = "auto"
    maximum_datagram_bytes: int = 8192
    maximum_decoder_buffer_bytes: int = 32768
    px4_component_ids: frozenset[int] | None = None
    # Keep the historical read-only policy as the default.  Native QGC Fly
    # View can opt into the bounded SIM flight command/mission surface only by
    # selecting ``sim_flight`` explicitly.
    qgc_control_mode: QgcControlMode = "read_only"

    def __post_init__(self) -> None:
        for name, host in (
            ("qgc_input_host", self.qgc_input_host),
            ("qgc_output_host", self.qgc_output_host),
            ("px4_input_host", self.px4_input_host),
        ):
            if host not in {LOOPBACK_HOST, IPV6_LOOPBACK_HOST}:
                raise SimulationIsolationError(
                    f"SIM interactive router {name} must be literal loopback"
                )
        for name, value in (
            ("qgc_input_port", self.qgc_input_port),
            ("qgc_output_port", self.qgc_output_port),
            ("px4_input_port", self.px4_input_port),
        ):
            _validate_port(value, name)
            if int(value) == PROTECTED_GROUND_STATION_PORT:
                raise SimulationIsolationError(f"{name} cannot use protected UDP 14550")
        if len({self.qgc_input_port, self.qgc_output_port, self.px4_input_port}) != 3:
            raise SimulationIsolationError("interactive router UDP ports must be distinct")
        output_host, output_port = _parse_loopback_udpout(self.px4_output_endpoint)
        if output_host not in {LOOPBACK_HOST, IPV6_LOOPBACK_HOST}:
            raise SimulationIsolationError("PX4 output must use literal loopback")
        if output_port == PROTECTED_GROUND_STATION_PORT:
            raise SimulationIsolationError("PX4 output cannot use protected UDP 14550")
        if output_port in {self.qgc_input_port, self.qgc_output_port, self.px4_input_port}:
            raise SimulationIsolationError("interactive router UDP ports must be distinct")
        if self.throttle_wire_mode not in {"auto", "qgc_legacy", "signed"}:
            raise ValueError("throttle_wire_mode must be auto, qgc_legacy or signed")
        if not 64 <= int(self.maximum_datagram_bytes) <= 65535:
            raise ValueError("maximum_datagram_bytes must be in 64..65535")
        if int(self.maximum_decoder_buffer_bytes) < 280:
            raise ValueError("maximum_decoder_buffer_bytes must hold one MAVLink frame")
        for name, value in (
            ("qgc_system_id", self.qgc_system_id),
            ("qgc_component_id", self.qgc_component_id),
            ("target_system", self.target_system),
        ):
            if not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"{name} must be in 1..255")
        if not self.qgc_target_component_ids:
            raise ValueError("qgc_target_component_ids cannot be empty")
        if any(
            not isinstance(value, int) or not 0 <= value <= 255
            for value in self.qgc_target_component_ids
        ):
            raise ValueError("qgc_target_component_ids must contain MAVLink component IDs")
        for name, value in (
            ("qgc_source_port", self.qgc_source_port),
            ("px4_source_port", self.px4_source_port),
        ):
            if value is not None:
                _validate_port(value, name)
                if int(value) == PROTECTED_GROUND_STATION_PORT:
                    raise SimulationIsolationError(f"{name} cannot use protected UDP 14550")
        if self.px4_component_ids is not None:
            if not self.px4_component_ids:
                raise ValueError("px4_component_ids cannot be empty")
            if any(
                not isinstance(value, int) or not 0 <= value <= 255
                for value in self.px4_component_ids
            ):
                raise ValueError("px4_component_ids must contain MAVLink component IDs")
        if self.qgc_control_mode not in {"read_only", "sim_flight"}:
            raise ValueError("qgc_control_mode must be read_only or sim_flight")

    @property
    def qgc_input_endpoint(self) -> tuple[str, int]:
        return self.qgc_input_host, self.qgc_input_port

    @property
    def qgc_output_endpoint(self) -> tuple[str, int]:
        return self.qgc_output_host, self.qgc_output_port

    @property
    def px4_input_endpoint(self) -> tuple[str, int]:
        return self.px4_input_host, self.px4_input_port

    @property
    def px4_output_address(self) -> tuple[str, int]:
        return _parse_loopback_udpout(self.px4_output_endpoint)


@dataclass(slots=True)
class SimulatorInteractiveRouterSummary:
    """Counters emitted as ``interactive-router.json`` job evidence."""

    started_monotonic_s: float
    qgc_datagrams_received: int = 0
    qgc_frames_received: int = 0
    qgc_heartbeat_frames: int = 0
    qgc_manual_frames_accepted: int = 0
    qgc_read_only_frames_forwarded: int = 0
    qgc_frames_blocked: int = 0
    qgc_datagrams_blocked: int = 0
    qgc_non_loopback_datagrams_blocked: int = 0
    qgc_unexpected_source_datagrams_blocked: int = 0
    qgc_oversize_datagrams_blocked: int = 0
    qgc_fragment_source_resets: int = 0
    qgc_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_forwarded_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_blocked_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_rejected_reasons: Counter[str] = field(default_factory=Counter)
    qgc_frames_sent_to_px4: int = 0
    px4_datagrams_received: int = 0
    px4_frames_received: int = 0
    px4_heartbeat_frames: int = 0
    px4_frames_forwarded_to_qgc: int = 0
    px4_frames_blocked: int = 0
    px4_datagrams_blocked: int = 0
    px4_non_loopback_datagrams_blocked: int = 0
    px4_unexpected_source_datagrams_blocked: int = 0
    px4_oversize_datagrams_blocked: int = 0
    px4_fragment_source_resets: int = 0
    px4_message_ids: Counter[int] = field(default_factory=Counter)
    px4_forwarded_message_ids: Counter[int] = field(default_factory=Counter)
    px4_blocked_message_ids: Counter[int] = field(default_factory=Counter)
    px4_rejected_reasons: Counter[str] = field(default_factory=Counter)
    px4_frames_sent_to_qgc: int = 0
    # Optional observation hook used by the live SIM AI bridge.  These fields
    # describe only the side-band callback; they do not imply that a control
    # command was emitted.
    px4_telemetry_sink_frames: int = 0
    px4_telemetry_sink_errors: int = 0
    px4_telemetry_sink_message_ids: Counter[int] = field(default_factory=Counter)
    px4_telemetry_sink_last_error: str | None = None
    watchdog_ticks: int = 0
    watchdog_sink_calls: int = 0
    watchdog_sink_errors: int = 0
    watchdog_sink_last_error: str | None = None
    shutdown_neutral_count: int = 0
    stop_requested: bool = False
    qgc_sim_flight_frames_forwarded: int = 0
    qgc_flight_command_frames_forwarded: int = 0
    qgc_mission_write_frames_forwarded: int = 0
    qgc_mission_items_forwarded: int = 0
    qgc_mission_transactions: int = 0
    qgc_mission_expected_count: int | None = None
    qgc_mission_items_seen: int = 0
    lifecycle_sink_calls: int = 0
    lifecycle_sink_errors: int = 0
    lifecycle_sink_last_error: str | None = None

    def as_dict(
        self,
        *,
        config: SimulatorInteractiveRouterConfig,
        adapter_evidence: dict[str, object],
        qgc_discarded_bytes: int,
        px4_discarded_bytes: int,
        telemetry_sink_enabled: bool = False,
        watchdog_sink_enabled: bool = False,
        lifecycle_sink_enabled: bool = False,
        ready: bool,
        now_s: float | None = None,
    ) -> dict[str, object]:
        # ``now_s`` comes from the router-owned clock.  Keeping the fallback
        # preserves the legacy summary API, while deterministic SIM/REPLAY
        # callers can prove that duration/evidence never samples host wall
        # time by passing their shared ManualClock value.
        current_s = time.monotonic() if now_s is None else float(now_s)
        return {
            "schema_version": 1,
            "event": "simulator_interactive_router_finished",
            "started_monotonic_s": self.started_monotonic_s,
            "ended_monotonic_s": current_s,
            "duration_seconds": round(max(0.0, current_s - self.started_monotonic_s), 3),
            "endpoints": {
                "qgc_input": _endpoint_text(config.qgc_input_endpoint),
                "qgc_output": _endpoint_text(config.qgc_output_endpoint),
                "px4_telemetry_input": _endpoint_text(config.px4_input_endpoint),
                "px4_command_output": config.px4_output_endpoint,
            },
            "qgc_identity": {
                "system_id": config.qgc_system_id,
                "component_id": config.qgc_component_id,
                "source_port": config.qgc_source_port,
                "target_system": config.target_system,
                "target_component_ids": sorted(config.qgc_target_component_ids),
            },
            "px4_identity": {
                "system_id": config.target_system,
                "component_ids": None
                if config.px4_component_ids is None
                else sorted(config.px4_component_ids),
                "source_port": config.px4_source_port,
            },
            "qgc_datagrams_received": self.qgc_datagrams_received,
            "qgc_frames_received": self.qgc_frames_received,
            "qgc_heartbeat_frames": self.qgc_heartbeat_frames,
            "qgc_manual_frames_accepted": self.qgc_manual_frames_accepted,
            "qgc_read_only_frames_forwarded": self.qgc_read_only_frames_forwarded,
            "qgc_frames_blocked": self.qgc_frames_blocked,
            "qgc_datagrams_blocked": self.qgc_datagrams_blocked,
            "qgc_non_loopback_datagrams_blocked": self.qgc_non_loopback_datagrams_blocked,
            "qgc_unexpected_source_datagrams_blocked": self.qgc_unexpected_source_datagrams_blocked,
            "qgc_oversize_datagrams_blocked": self.qgc_oversize_datagrams_blocked,
            "qgc_fragment_source_resets": self.qgc_fragment_source_resets,
            "qgc_message_ids": _counter_mapping(self.qgc_message_ids),
            "qgc_forwarded_message_ids": _counter_mapping(self.qgc_forwarded_message_ids),
            "qgc_blocked_message_ids": _counter_mapping(self.qgc_blocked_message_ids),
            "qgc_rejected_reasons": _counter_mapping(self.qgc_rejected_reasons),
            "qgc_frames_sent_to_px4": self.qgc_frames_sent_to_px4,
            "px4_datagrams_received": self.px4_datagrams_received,
            "px4_frames_received": self.px4_frames_received,
            "px4_heartbeat_frames": self.px4_heartbeat_frames,
            "px4_frames_forwarded_to_qgc": self.px4_frames_forwarded_to_qgc,
            "px4_frames_blocked": self.px4_frames_blocked,
            "px4_datagrams_blocked": self.px4_datagrams_blocked,
            "px4_non_loopback_datagrams_blocked": self.px4_non_loopback_datagrams_blocked,
            "px4_unexpected_source_datagrams_blocked": self.px4_unexpected_source_datagrams_blocked,
            "px4_oversize_datagrams_blocked": self.px4_oversize_datagrams_blocked,
            "px4_fragment_source_resets": self.px4_fragment_source_resets,
            "px4_message_ids": _counter_mapping(self.px4_message_ids),
            "px4_forwarded_message_ids": _counter_mapping(self.px4_forwarded_message_ids),
            "px4_blocked_message_ids": _counter_mapping(self.px4_blocked_message_ids),
            "px4_rejected_reasons": _counter_mapping(self.px4_rejected_reasons),
            "px4_frames_sent_to_qgc": self.px4_frames_sent_to_qgc,
            "telemetry_sink_enabled": bool(telemetry_sink_enabled),
            "px4_telemetry_sink_frames": self.px4_telemetry_sink_frames,
            "px4_telemetry_sink_errors": self.px4_telemetry_sink_errors,
            "px4_telemetry_sink_message_ids": _counter_mapping(self.px4_telemetry_sink_message_ids),
            "px4_telemetry_sink_last_error": self.px4_telemetry_sink_last_error,
            "watchdog_ticks": self.watchdog_ticks,
            "watchdog_sink_enabled": bool(watchdog_sink_enabled),
            "watchdog_sink_calls": self.watchdog_sink_calls,
            "watchdog_sink_errors": self.watchdog_sink_errors,
            "watchdog_sink_last_error": self.watchdog_sink_last_error,
            "manual_watchdog_s": adapter_evidence.get("manual_watchdog_s"),
            "manual_watchdog_neutral_count": adapter_evidence.get(
                "manual_watchdog_neutral_count", 0
            ),
            "shutdown_neutral_count": self.shutdown_neutral_count,
            "stop_requested": self.stop_requested,
            "qgc_control_mode": config.qgc_control_mode,
            "qgc_sim_flight_frames_forwarded": self.qgc_sim_flight_frames_forwarded,
            "qgc_flight_command_frames_forwarded": self.qgc_flight_command_frames_forwarded,
            "qgc_mission_write_frames_forwarded": self.qgc_mission_write_frames_forwarded,
            "qgc_mission_items_forwarded": self.qgc_mission_items_forwarded,
            "qgc_mission_transactions": self.qgc_mission_transactions,
            "qgc_mission_expected_count": self.qgc_mission_expected_count,
            "qgc_mission_items_seen": self.qgc_mission_items_seen,
            "lifecycle_sink_enabled": bool(lifecycle_sink_enabled),
            "lifecycle_sink_calls": self.lifecycle_sink_calls,
            "lifecycle_sink_errors": self.lifecycle_sink_errors,
            "lifecycle_sink_last_error": self.lifecycle_sink_last_error,
            "sim_flight_command_allowlist": sorted(SIM_FLIGHT_COMMAND_ALLOWLIST),
            "sim_flight_mission_command_allowlist": sorted(SIM_FLIGHT_MISSION_COMMAND_ALLOWLIST),
            "ready": ready,
            "throttle_wire_mode": config.throttle_wire_mode,
            "literal_loopback_only": True,
            "telemetry_frames_rewritten": False,
            # These flags describe policy availability, not an unconditional
            # pass-through.  ``sim_flight`` still enforces the allowlists and
            # field validators above; read_only retains the historical false
            # values for compatibility.
            "qgc_flight_commands_allowed": config.qgc_control_mode == "sim_flight",
            "qgc_mission_writes_allowed": config.qgc_control_mode == "sim_flight",
            "qgc_parameter_writes_allowed": False,
            "qgc_manual_control_via_shared_adapter": True,
            "adapter": adapter_evidence,
        }


class SimulatorInteractiveRouter:
    """Proxy the native QGC UDP link to one PX4 SITL instance."""

    def __init__(
        self,
        adapter: Any,
        *,
        config: SimulatorInteractiveRouterConfig | None = None,
        qgc_sender: SendCallback | None = None,
        px4_sender: SendCallback | None = None,
        telemetry_sink: TelemetrySink | None = None,
        watchdog_sink: WatchdogSink | None = None,
        lifecycle_sink: LifecycleSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_adapter(adapter)
        self.adapter = adapter
        self.config = config or SimulatorInteractiveRouterConfig()
        self.clock = clock
        self._qgc_sender = qgc_sender
        self._px4_sender = px4_sender
        if telemetry_sink is not None and not callable(telemetry_sink):
            raise TypeError("telemetry_sink must be callable or None")
        if watchdog_sink is not None and not callable(watchdog_sink):
            raise TypeError("watchdog_sink must be callable or None")
        self._telemetry_sink = telemetry_sink
        self._watchdog_sink = watchdog_sink
        if lifecycle_sink is not None and not any(
            callable(getattr(lifecycle_sink, name, None))
            for name in ("command_long_send", "set_mode_send")
        ):
            raise TypeError("lifecycle_sink must expose command_long_send or set_mode_send")
        self._lifecycle_sink = lifecycle_sink
        self.qgc_decoder = MavlinkFrameDecoder(
            maximum_buffer_bytes=self.config.maximum_decoder_buffer_bytes
        )
        self.px4_decoder = MavlinkFrameDecoder(
            maximum_buffer_bytes=self.config.maximum_decoder_buffer_bytes
        )
        # MAVLink frames may span UDP datagrams.  Keep the source endpoint
        # associated with each decoder so bytes from two loopback senders can
        # never be stitched into one accepted frame.
        self._qgc_fragment_source: tuple[str, int] | None = None
        self._px4_fragment_source: tuple[str, int] | None = None
        self.summary = SimulatorInteractiveRouterSummary(started_monotonic_s=float(clock()))
        self.ready_event = threading.Event()
        self.bound_endpoints: dict[str, tuple[str, int]] = {}
        self._stop_event = threading.Event()
        self._shutdown_done = False
        self._run_lock = threading.RLock()
        self._qgc_socket: socket.socket | None = None
        self._px4_socket: socket.socket | None = None
        # Direct ``process_*`` calls are intentionally usable by deterministic
        # fixtures before ``run`` binds sockets.  ``stop``/run-finally revoke
        # that capability so a stale caller cannot inject a post-shutdown
        # control frame into a still-live adapter connection.
        self._accepting_input = True
        self._mission_expected_count: int | None = None
        self._mission_items_seen: set[int] = set()
        # These lists are useful for deterministic fixtures and are also a
        # compact audit trail when the callback is backed by a fake transport.
        self.sent_to_qgc: list[tuple[bytes, tuple[str, int]]] = []
        self.sent_to_px4: list[tuple[bytes, tuple[str, int]]] = []

    @property
    def lifecycle_sink(self) -> LifecycleSink | None:
        return self._lifecycle_sink

    def set_lifecycle_sink(self, sink: LifecycleSink | None) -> None:
        """Install/clear the optional deterministic lifecycle command sink."""

        if sink is not None and not any(
            callable(getattr(sink, name, None)) for name in ("command_long_send", "set_mode_send")
        ):
            raise TypeError("lifecycle_sink must expose command_long_send or set_mode_send")
        with self._run_lock:
            self._lifecycle_sink = sink

    @property
    def is_running(self) -> bool:
        return self.ready_event.is_set() and not self._stop_event.is_set()

    @property
    def telemetry_sink(self) -> TelemetrySink | None:
        """Return the optional side-band PX4 telemetry observer."""

        return self._telemetry_sink

    @property
    def watchdog_sink(self) -> WatchdogSink | None:
        """Return the optional event-loop watchdog observer."""

        return self._watchdog_sink

    def set_watchdog_sink(self, sink: WatchdogSink | None) -> None:
        """Install or clear the side-band watchdog observer."""

        if sink is not None and not callable(sink):
            raise TypeError("watchdog sink must be callable or None")
        with self._run_lock:
            self._watchdog_sink = sink

    def set_telemetry_sink(self, sink: TelemetrySink | None) -> None:
        """Install or clear the side-band telemetry observer.

        The callback is intentionally configured outside the datagram path so
        an orchestrator can wire the live AI bridge before binding sockets.
        Reconfiguration while the router is running is allowed only as an
        atomic pointer swap; no frame bytes or transport state are changed.
        """

        if sink is not None and not callable(sink):
            raise TypeError("telemetry sink must be callable or None")
        with self._run_lock:
            self._telemetry_sink = sink

    def process_qgc_datagram(
        self,
        datagram: bytes,
        *,
        source: tuple[str, int] = (LOOPBACK_HOST, 0),
        timestamp_s: float | None = None,
    ) -> tuple[Any, ...]:
        """Validate and route one QGC datagram.

        Accepted manual frames are deliberately *not* copied raw to PX4: the
        shared adapter emits the canonical MANUAL_CONTROL frame after the
        authority state machine has accepted it.  Read-only frames are copied
        byte-for-byte to PX4.
        """

        self.summary.qgc_datagrams_received += 1
        if not self._accepting_input:
            self._reject_qgc("router_not_accepting", frame=False)
            return ()
        source_reason = self._source_reason(source, expected_port=self.config.qgc_source_port)
        if source_reason is not None:
            self._discard_qgc_fragment()
            self._reject_qgc(source_reason, frame=False)
            if source_reason == "non_loopback_source":
                self.summary.qgc_non_loopback_datagrams_blocked += 1
            else:
                self.summary.qgc_unexpected_source_datagrams_blocked += 1
            return ()
        if not isinstance(datagram, (bytes, bytearray)):
            raise TypeError("datagram must be bytes")
        if len(datagram) > self.config.maximum_datagram_bytes:
            self._discard_qgc_fragment()
            self.summary.qgc_oversize_datagrams_blocked += 1
            self._reject_qgc("datagram_too_large", frame=False)
            return ()
        self._prepare_qgc_decoder_source(source)
        receipts: list[Any] = []
        for frame in self.qgc_decoder.feed(bytes(datagram)):
            self.summary.qgc_frames_received += 1
            self.summary.qgc_message_ids[frame.message_id] += 1
            reason = self._qgc_frame_rejection_reason(frame)
            if reason is not None:
                self._reject_qgc(reason, message_id=frame.message_id)
                continue
            if frame.message_id == MSG_HEARTBEAT:
                self.summary.qgc_heartbeat_frames += 1
            if frame.message_id == MSG_MANUAL_CONTROL:
                pitch, roll, yaw, throttle = _decode_qgc_manual_control(
                    frame.payload,
                    version=frame.version,
                    throttle_wire_mode=self.config.throttle_wire_mode,
                )
                receipt = self.adapter.submit_manual(
                    roll=roll,
                    pitch=pitch,
                    yaw=yaw,
                    throttle=throttle,
                    timestamp_s=timestamp_s,
                )
                self.summary.qgc_manual_frames_accepted += 1
                receipts.append(receipt)
                continue
            if self._is_sim_flight_forwardable(frame):
                self._record_sim_flight_forward(frame)
                self._notify_lifecycle_sink(frame)
                self._emit_px4(frame.encoded)
                self.summary.qgc_frames_sent_to_px4 += 1
                continue
            self._emit_px4(frame.encoded)
            self.summary.qgc_read_only_frames_forwarded += 1
            self.summary.qgc_frames_sent_to_px4 += 1
            self.summary.qgc_forwarded_message_ids[frame.message_id] += 1
        return tuple(receipts)

    def _notify_lifecycle_sink(self, frame: MavlinkFrame) -> None:
        """Mirror validated QGC lifecycle messages into a local SIM model.

        The sink is side-band: PX4 forwarding remains byte-for-byte and a
        sink exception is recorded rather than taking down the router.
        """

        sink = self._lifecycle_sink
        if sink is None:
            return
        try:
            if frame.message_id == MSG_COMMAND_LONG:
                method = getattr(sink, "command_long_send", None)
                if not callable(method):
                    return
                params = struct.unpack_from("<7f", frame.payload, 0)
                command = int.from_bytes(frame.payload[28:30], "little")
                target_system = frame.payload[30]
                target_component = frame.payload[31] if len(frame.payload) > 31 else 0
                confirmation = frame.payload[32] if len(frame.payload) > 32 else 0
                method(
                    target_system,
                    target_component,
                    command,
                    confirmation,
                    *params,
                )
            elif frame.message_id == MSG_SET_MODE:
                method = getattr(sink, "set_mode_send", None)
                if not callable(method):
                    return
                custom_mode = int.from_bytes(frame.payload[0:4], "little")
                target_system = frame.payload[4]
                base_mode = frame.payload[5] if len(frame.payload) > 5 else 0
                method(target_system, base_mode, custom_mode)
            else:
                return
        except Exception as exc:  # pragma: no cover - defensive transport edge.
            self.summary.lifecycle_sink_errors += 1
            self.summary.lifecycle_sink_last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        else:
            self.summary.lifecycle_sink_calls += 1

    def process_px4_datagram(
        self,
        datagram: bytes,
        *,
        source: tuple[str, int] = (LOOPBACK_HOST, DEFAULT_PX4_OUTPUT_PORT),
        timestamp_s: float | None = None,
    ) -> int:
        """Validate and forward one PX4 telemetry datagram to local QGC."""

        self.summary.px4_datagrams_received += 1
        if not self._accepting_input:
            self._reject_px4("router_not_accepting", frame=False)
            return 0
        source_reason = self._source_reason(source, expected_port=self.config.px4_source_port)
        if source_reason is not None:
            self._discard_px4_fragment()
            self._reject_px4(source_reason, frame=False)
            if source_reason == "non_loopback_source":
                self.summary.px4_non_loopback_datagrams_blocked += 1
            else:
                self.summary.px4_unexpected_source_datagrams_blocked += 1
            return 0
        if not isinstance(datagram, (bytes, bytearray)):
            raise TypeError("datagram must be bytes")
        if len(datagram) > self.config.maximum_datagram_bytes:
            self._discard_px4_fragment()
            self.summary.px4_oversize_datagrams_blocked += 1
            self._reject_px4("datagram_too_large", frame=False)
            return 0
        self._prepare_px4_decoder_source(source)
        forwarded = 0
        for frame in self.px4_decoder.feed(bytes(datagram)):
            self.summary.px4_frames_received += 1
            self.summary.px4_message_ids[frame.message_id] += 1
            reason = self._px4_frame_rejection_reason(frame)
            if reason is not None:
                self._reject_px4(reason, message_id=frame.message_id)
                continue
            if frame.message_id == MSG_HEARTBEAT:
                self.summary.px4_heartbeat_frames += 1
            self._emit_qgc(frame.encoded)
            self.summary.px4_frames_forwarded_to_qgc += 1
            self.summary.px4_frames_sent_to_qgc += 1
            self.summary.px4_forwarded_message_ids[frame.message_id] += 1
            # Observe only after source/identity validation and after the raw
            # frame has entered the QGC forwarding path.  This guarantees that
            # an AI/telemetry parser cannot rewrite or block native Fly View
            # telemetry, and parser exceptions are recorded rather than
            # taking down the router thread.
            self._notify_telemetry_sink(
                frame,
                timestamp_s=self.clock() if timestamp_s is None else timestamp_s,
            )
            forwarded += 1
        return forwarded

    def poll_watchdog(self, *, now_s: float | None = None) -> Any:
        """Tick the shared watchdog from the router event-loop owner.

        A live-AI watchdog sink owns both heartbeat freshness and the adapter
        watchdog.  Avoid calling the adapter twice, which would duplicate
        neutral frames; fall back to the direct adapter check only if the sink
        raises.
        """

        self.summary.watchdog_ticks += 1
        stamp = self.clock() if now_s is None else now_s
        sink = self._watchdog_sink
        if sink is not None:
            try:
                result = sink(float(stamp))
            except Exception as exc:
                self.summary.watchdog_sink_errors += 1
                self.summary.watchdog_sink_last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            else:
                self.summary.watchdog_sink_calls += 1
                return result
        check = getattr(self.adapter, "check_watchdog", None)
        if not callable(check):
            return None
        return check(now_s=stamp)

    def run(self, *, duration_s: float = 0.0) -> dict[str, object]:
        """Bind both loopback sockets and proxy frames until stopped."""

        if not isinstance(duration_s, (int, float)) or not math.isfinite(duration_s):
            raise ValueError("duration_s must be a finite number")
        if duration_s < 0:
            raise ValueError("duration_s cannot be negative")
        with self._run_lock:
            if self.ready_event.is_set():
                raise SimulatorInteractiveRouterError("interactive router is already running")
            self._stop_event.clear()
            self.summary.stop_requested = False
            self._shutdown_done = False
            self._accepting_input = True
            self._mission_expected_count = None
            self._mission_items_seen.clear()
            self.summary.qgc_mission_expected_count = None
            self.summary.qgc_mission_items_seen = 0
            qgc_socket = _make_udp_socket(self.config.qgc_input_endpoint)
            try:
                px4_socket = _make_udp_socket(self.config.px4_input_endpoint)
            except BaseException:
                qgc_socket.close()
                raise
            self._qgc_socket = qgc_socket
            self._px4_socket = px4_socket
            # Install socket-backed senders only when the caller did not
            # provide an injected transport.  Keeping the callbacks on the
            # instance makes ``process_*_datagram`` and the real event loop
            # exercise the exact same forwarding path.
            installed_qgc_sender = self._qgc_sender is None
            installed_px4_sender = self._px4_sender is None
            if installed_qgc_sender:
                self._qgc_sender = lambda data, destination: _socket_send(
                    px4_socket, data, destination
                )
            if installed_px4_sender:
                self._px4_sender = lambda data, destination: _socket_send(
                    qgc_socket, data, destination
                )
            self.bound_endpoints = {
                "qgc_input": self.config.qgc_input_endpoint,
                "px4_telemetry_input": self.config.px4_input_endpoint,
            }
            self.ready_event.set()
        deadline = self.clock() + float(duration_s) if duration_s > 0 else None
        try:
            while not self._stop_event.is_set() and (deadline is None or self.clock() < deadline):
                self.poll_watchdog()
                timeout = 0.25
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - self.clock()))
                readable, _, _ = select.select(
                    [self._qgc_socket, self._px4_socket], [], [], timeout
                )
                if self._qgc_socket in readable:
                    self._receive_qgc_socket()
                if self._px4_socket in readable:
                    self._receive_px4_socket()
        finally:
            self._shutdown_adapter()
            with self._run_lock:
                self._accepting_input = False
                for channel in (self._qgc_socket, self._px4_socket):
                    if channel is not None:
                        channel.close()
                self._qgc_socket = None
                self._px4_socket = None
                self.bound_endpoints.clear()
                self.ready_event.clear()
                # Do not retain closed socket closures across a later run.
                # Explicitly injected callbacks remain untouched.
                if installed_qgc_sender:
                    self._qgc_sender = None
                if installed_px4_sender:
                    self._px4_sender = None
        return self.evidence()

    def stop(self) -> None:
        """Request shutdown and neutralize an active AI stream once."""

        self.summary.stop_requested = True
        self._accepting_input = False
        self._stop_event.set()
        self._shutdown_adapter()

    def evidence(self) -> dict[str, object]:
        adapter_evidence = self.adapter.evidence()
        return self.summary.as_dict(
            config=self.config,
            adapter_evidence=adapter_evidence,
            qgc_discarded_bytes=self.qgc_decoder.discarded_bytes,
            px4_discarded_bytes=self.px4_decoder.discarded_bytes,
            telemetry_sink_enabled=self._telemetry_sink is not None,
            watchdog_sink_enabled=self._watchdog_sink is not None,
            lifecycle_sink_enabled=self._lifecycle_sink is not None,
            ready=self.ready_event.is_set(),
            now_s=self.clock(),
        ) | {
            "qgc_discarded_bytes": self.qgc_decoder.discarded_bytes,
            "px4_discarded_bytes": self.px4_decoder.discarded_bytes,
        }

    def _receive_qgc_socket(self) -> None:
        channel = self._qgc_socket
        if channel is None:
            return
        try:
            datagram, source = channel.recvfrom(self.config.maximum_datagram_bytes + 1)
        except (BlockingIOError, ConnectionResetError):
            return
        self.process_qgc_datagram(datagram, source=source, timestamp_s=self.clock())

    def _receive_px4_socket(self) -> None:
        channel = self._px4_socket
        if channel is None:
            return
        try:
            datagram, source = channel.recvfrom(self.config.maximum_datagram_bytes + 1)
        except (BlockingIOError, ConnectionResetError):
            return
        self.process_px4_datagram(datagram, source=source, timestamp_s=self.clock())

    def _source_reason(self, source: tuple[str, int], *, expected_port: int | None) -> str | None:
        if (
            not isinstance(source, tuple)
            or not source
            or source[0]
            not in {
                LOOPBACK_HOST,
                IPV6_LOOPBACK_HOST,
            }
        ):
            return "non_loopback_source"
        if expected_port is not None:
            if len(source) < 2:
                return "unexpected_source_port"
            try:
                source_port = int(source[1])
            except (TypeError, ValueError):
                return "unexpected_source_port"
            if source_port != expected_port:
                return "unexpected_source_port"
        return None

    def _prepare_qgc_decoder_source(self, source: tuple[str, int]) -> None:
        key = _source_key(source)
        if self._qgc_fragment_source is not None and self._qgc_fragment_source != key:
            self._reset_qgc_decoder(reason="fragment_source_changed")
        self._qgc_fragment_source = key

    def _prepare_px4_decoder_source(self, source: tuple[str, int]) -> None:
        key = _source_key(source)
        if self._px4_fragment_source is not None and self._px4_fragment_source != key:
            self._reset_px4_decoder(reason="fragment_source_changed")
        self._px4_fragment_source = key

    def _discard_qgc_fragment(self) -> None:
        """Drop a partial QGC frame before handling an untrusted datagram."""

        if self._qgc_fragment_source is not None:
            self._reset_qgc_decoder(reason="fragment_source_changed")
        self._qgc_fragment_source = None

    def _discard_px4_fragment(self) -> None:
        """Drop a partial PX4 frame before handling an untrusted datagram."""

        if self._px4_fragment_source is not None:
            self._reset_px4_decoder(reason="fragment_source_changed")
        self._px4_fragment_source = None

    def _reset_qgc_decoder(self, *, reason: str) -> None:
        previous = self.qgc_decoder
        pending = bool(getattr(previous, "_buffer", ()))
        discarded = previous.discarded_bytes
        self.qgc_decoder = MavlinkFrameDecoder(
            maximum_buffer_bytes=self.config.maximum_decoder_buffer_bytes
        )
        # Preserve cumulative parser-discard evidence across a source-boundary
        # reset; only the incomplete fragment itself is dropped.
        self.qgc_decoder.discarded_bytes = discarded
        self.summary.qgc_fragment_source_resets += 1
        if pending:
            self.summary.qgc_rejected_reasons[reason] += 1

    def _reset_px4_decoder(self, *, reason: str) -> None:
        previous = self.px4_decoder
        pending = bool(getattr(previous, "_buffer", ()))
        discarded = previous.discarded_bytes
        self.px4_decoder = MavlinkFrameDecoder(
            maximum_buffer_bytes=self.config.maximum_decoder_buffer_bytes
        )
        self.px4_decoder.discarded_bytes = discarded
        self.summary.px4_fragment_source_resets += 1
        if pending:
            self.summary.px4_rejected_reasons[reason] += 1

    def _qgc_frame_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        if frame.source_system_id != self.config.qgc_system_id:
            return "qgc_source_system_mismatch"
        if frame.source_component_id != self.config.qgc_component_id:
            return "qgc_source_component_mismatch"
        if frame.message_id == MSG_MANUAL_CONTROL:
            try:
                _decode_qgc_manual_control(
                    frame.payload,
                    version=frame.version,
                    throttle_wire_mode=self.config.throttle_wire_mode,
                )
            except ValueError as exc:
                return str(exc)
            if frame.payload[10] != self.config.target_system:
                return "manual_control_target_mismatch"
            return None
        # COMMAND_LONG is a mixed class: the existing read-only command IDs
        # remain read-only, while the explicit sim_flight mode may forward a
        # tiny, field-validated lifecycle subset.
        if frame.message_id == MSG_COMMAND_LONG:
            if self.config.qgc_control_mode == "sim_flight" and not qgc_frame_is_read_only(frame):
                return self._sim_flight_command_rejection_reason(frame)
            if not self._qgc_frame_is_read_only_for_config(frame):
                return "qgc_message_not_allowed"
        elif frame.message_id in {
            MSG_SET_MODE,
            MSG_MISSION_COUNT,
            MSG_MISSION_ITEM_INT,
            MSG_MISSION_ITEM,
        }:
            if self.config.qgc_control_mode != "sim_flight":
                return "qgc_message_not_allowed"
            return self._sim_flight_mission_or_mode_rejection_reason(frame)
        if self._qgc_frame_is_read_only_for_config(frame):
            if not _qgc_read_only_target_matches(
                frame,
                target_system=self.config.target_system,
                target_component_ids=self.config.qgc_target_component_ids,
            ):
                return "qgc_read_only_target_mismatch"
            return None
        return "qgc_message_not_allowed"

    def _qgc_frame_is_read_only_for_config(self, frame: MavlinkFrame) -> bool:
        """Apply the shared read-only classifier with configurable PX4 target.

        The legacy classifier intentionally hard-codes the acceptance fixture's
        ``(system, component) == (1, 1)`` FTP target.  Interactive SIM profiles
        may use another system ID, so FTP must be classified locally while all
        other message classes continue to use the shared policy.
        """

        if frame.message_id != MSG_FILE_TRANSFER_PROTOCOL:
            return qgc_frame_is_read_only(frame)
        target = frame.file_transfer_protocol_target
        return (
            target is not None
            and target[0] == self.config.target_system
            and target[1] in self.config.qgc_target_component_ids
            and frame.file_transfer_protocol_opcode in READ_ONLY_FTP_OPCODES
        )

    def _sim_flight_mission_or_mode_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        if frame.message_id == MSG_SET_MODE:
            return self._set_mode_rejection_reason(frame)
        if frame.message_id == MSG_MISSION_COUNT:
            return self._mission_count_rejection_reason(frame)
        if frame.message_id == MSG_MISSION_ITEM_INT:
            return self._mission_item_int_rejection_reason(frame)
        # Legacy MISSION_ITEM has a larger floating-point coordinate surface and
        # is intentionally excluded.  QGC's PX4 path uses MISSION_ITEM_INT; an
        # explicit rejection avoids silently accepting an alternate wire form.
        if frame.message_id == MSG_MISSION_ITEM:
            return "legacy_mission_item_not_allowed"
        return "qgc_message_not_allowed"

    def _sim_flight_command_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        payload = frame.payload
        if frame.version == 1 and len(payload) != 33:
            return "command_long_payload_length"
        if frame.version == 2 and not 31 <= len(payload) <= 33:
            return "command_long_payload_length"
        if frame.version not in (1, 2) or len(payload) < 31:
            return "command_long_payload_truncated"
        target_system = payload[30]
        target_component = payload[31] if len(payload) > 31 else 0
        if target_system != self.config.target_system:
            return "sim_flight_target_system_mismatch"
        if target_component not in self.config.qgc_target_component_ids:
            return "sim_flight_target_component_mismatch"
        confirmation = payload[32] if len(payload) > 32 else 0
        if confirmation != 0:
            return "sim_flight_confirmation_not_zero"
        command = int.from_bytes(payload[28:30], "little")
        if command not in SIM_FLIGHT_COMMAND_ALLOWLIST:
            return "sim_flight_command_not_allowed"
        parameters = struct.unpack_from("<7f", payload, 0)
        if any(math.isinf(value) for value in parameters):
            return "sim_flight_command_infinite_parameter"
        if command == MAV_CMD_COMPONENT_ARM_DISARM:
            if not _float_is_one_of(parameters[0], (0.0, 1.0)):
                return "arm_disarm_parameter_invalid"
            if not _float_is_one_of(parameters[1], (0.0, 21196.0)):
                return "arm_disarm_force_parameter_invalid"
            if not _zero_or_nan(parameters[2:]):
                return "arm_disarm_tail_parameter_invalid"
        elif command == MAV_CMD_DO_VTOL_TRANSITION:
            if not _float_is_one_of(parameters[0], (3.0, 4.0)):
                return "vtol_transition_parameter_invalid"
            if not _zero_or_nan(parameters[1:]):
                return "vtol_transition_tail_parameter_invalid"
        elif command in {
            MAV_CMD_NAV_RETURN_TO_LAUNCH,
            MAV_CMD_NAV_LAND,
            MAV_CMD_MISSION_START,
        } and not _zero_or_nan(parameters):
            return "sim_flight_command_parameter_invalid"
        return None

    def _set_mode_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        payload = frame.payload
        if frame.version == 1 and len(payload) != 6:
            return "set_mode_payload_length"
        if frame.version == 2 and len(payload) not in {5, 6}:
            return "set_mode_payload_length"
        if frame.version not in (1, 2) or len(payload) < 5:
            return "set_mode_payload_truncated"
        custom_mode = int.from_bytes(payload[0:4], "little")
        target_system = payload[4]
        base_mode = payload[5] if len(payload) > 5 else 0
        if target_system != self.config.target_system:
            return "set_mode_target_system_mismatch"
        if not base_mode & 1:  # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            return "set_mode_custom_flag_required"
        main_mode = (custom_mode >> 16) & 0xFF
        if main_mode not in SIM_FLIGHT_MAIN_MODES:
            return "set_mode_main_mode_not_allowed"
        if main_mode == 4:  # PX4 AUTO
            sub_mode = (custom_mode >> 24) & 0xFF
            if sub_mode not in SIM_FLIGHT_AUTO_SUB_MODES:
                return "set_mode_auto_sub_mode_not_allowed"
        return None

    def _mission_count_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        payload = frame.payload
        if frame.version == 1 and len(payload) != 4:
            return "mission_count_payload_length"
        if frame.version == 2 and len(payload) not in {4, 5}:
            return "mission_count_payload_length"
        if frame.version not in (1, 2) or len(payload) < 4:
            return "mission_count_payload_truncated"
        count = int.from_bytes(payload[0:2], "little")
        target_system, target_component = payload[2], payload[3]
        mission_type = payload[4] if len(payload) > 4 else 0
        if target_system != self.config.target_system:
            return "mission_count_target_system_mismatch"
        if target_component not in self.config.qgc_target_component_ids:
            return "mission_count_target_component_mismatch"
        if mission_type != 0:
            return "mission_type_not_mission"
        if not 1 <= count <= SIM_FLIGHT_MAX_MISSION_ITEMS:
            return "mission_count_out_of_range"
        return None

    def _mission_item_int_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        payload = frame.payload
        if frame.version == 1 and len(payload) != 37:
            return "mission_item_int_payload_length"
        if frame.version == 2 and len(payload) not in {37, 38}:
            return "mission_item_int_payload_length"
        if frame.version not in (1, 2) or len(payload) < 37:
            return "mission_item_int_payload_truncated"
        if self._mission_expected_count is None:
            return "mission_item_without_count"
        seq = int.from_bytes(payload[28:30], "little")
        if seq >= self._mission_expected_count:
            return "mission_item_sequence_out_of_range"
        command = int.from_bytes(payload[30:32], "little")
        target_system, target_component = payload[32], payload[33]
        frame_id, current, autocontinue = payload[34], payload[35], payload[36]
        mission_type = payload[37] if len(payload) > 37 else 0
        if target_system != self.config.target_system:
            return "mission_item_target_system_mismatch"
        if target_component not in self.config.qgc_target_component_ids:
            return "mission_item_target_component_mismatch"
        if mission_type != 0:
            return "mission_type_not_mission"
        if command not in SIM_FLIGHT_MISSION_COMMAND_ALLOWLIST:
            return "mission_command_not_allowed"
        if current != (1 if seq == 0 else 0) or autocontinue != 1:
            return "mission_item_lifecycle_flags_invalid"
        parameters = struct.unpack_from("<4f", payload, 0)
        if any(math.isinf(value) for value in parameters):
            return "mission_item_infinite_parameter"
        latitude_e7 = int.from_bytes(payload[16:20], "little", signed=True)
        longitude_e7 = int.from_bytes(payload[20:24], "little", signed=True)
        latitude = latitude_e7 / 10_000_000.0
        longitude = longitude_e7 / 10_000_000.0
        altitude = struct.unpack_from("<f", payload, 24)[0]
        if not math.isfinite(altitude) or not -50.0 <= altitude <= 1000.0:
            return "mission_item_altitude_invalid"
        coordinate_command = command not in {
            MAV_CMD_NAV_RETURN_TO_LAUNCH,
            MAV_CMD_DO_VTOL_TRANSITION,
        }
        if coordinate_command:
            if frame_id not in SIM_FLIGHT_GLOBAL_INT_FRAMES:
                return "mission_item_frame_not_global"
            if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
                return "mission_item_coordinate_invalid"
        else:
            if frame_id != SIM_FLIGHT_MISSION_FRAME:
                return "mission_item_control_frame_invalid"
            if latitude_e7 != 0 or longitude_e7 != 0 or altitude != 0.0:
                return "mission_item_control_coordinate_invalid"
            if not _zero_or_nan(parameters):
                return "mission_item_control_parameter_invalid"
        return None

    def _is_sim_flight_forwardable(self, frame: MavlinkFrame) -> bool:
        if self.config.qgc_control_mode != "sim_flight":
            return False
        if frame.message_id == MSG_COMMAND_LONG:
            return not qgc_frame_is_read_only(frame)
        return frame.message_id in {MSG_SET_MODE, MSG_MISSION_COUNT, MSG_MISSION_ITEM_INT}

    def _record_sim_flight_forward(self, frame: MavlinkFrame) -> None:
        self.summary.qgc_sim_flight_frames_forwarded += 1
        self.summary.qgc_forwarded_message_ids[frame.message_id] += 1
        if frame.message_id == MSG_COMMAND_LONG or frame.message_id == MSG_SET_MODE:
            self.summary.qgc_flight_command_frames_forwarded += 1
        else:
            self.summary.qgc_mission_write_frames_forwarded += 1
            if frame.message_id == MSG_MISSION_COUNT:
                count = int.from_bytes(frame.payload[0:2], "little")
                transaction_complete = (
                    self._mission_expected_count is not None
                    and len(self._mission_items_seen) >= self._mission_expected_count
                )
                if (
                    self._mission_expected_count is None
                    or count != self._mission_expected_count
                    or transaction_complete
                ):
                    self._mission_items_seen.clear()
                    self.summary.qgc_mission_transactions += 1
                self._mission_expected_count = count
            elif frame.message_id == MSG_MISSION_ITEM_INT:
                self._mission_items_seen.add(int.from_bytes(frame.payload[28:30], "little"))
                self.summary.qgc_mission_items_forwarded += 1
            self.summary.qgc_mission_expected_count = self._mission_expected_count
            self.summary.qgc_mission_items_seen = len(self._mission_items_seen)

    def _px4_frame_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        if frame.source_system_id != self.config.target_system:
            return "px4_source_system_mismatch"
        if (
            self.config.px4_component_ids is not None
            and frame.source_component_id not in self.config.px4_component_ids
        ):
            return "px4_source_component_mismatch"
        return None

    def _emit_px4(self, encoded: bytes) -> None:
        destination = self.config.px4_output_address
        self.sent_to_px4.append((bytes(encoded), destination))
        if self._px4_sender is not None:
            self._px4_sender(bytes(encoded), destination)

    def _emit_qgc(self, encoded: bytes) -> None:
        destination = self.config.qgc_output_endpoint
        self.sent_to_qgc.append((bytes(encoded), destination))
        if self._qgc_sender is not None:
            self._qgc_sender(bytes(encoded), destination)

    def _notify_telemetry_sink(self, frame: MavlinkFrame, *, timestamp_s: float) -> None:
        sink = self._telemetry_sink
        if sink is None:
            return
        try:
            stamp = float(timestamp_s)
            if not math.isfinite(stamp) or stamp < 0.0:
                raise ValueError("telemetry sink timestamp must be finite and non-negative")
            sink(frame, stamp)
        except Exception as exc:
            self.summary.px4_telemetry_sink_errors += 1
            self.summary.px4_telemetry_sink_last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            return
        self.summary.px4_telemetry_sink_frames += 1
        self.summary.px4_telemetry_sink_message_ids[frame.message_id] += 1

    def _shutdown_adapter(self) -> None:
        if self._shutdown_done:
            return
        shutdown = getattr(self.adapter, "shutdown", None)
        if not callable(shutdown):
            self._shutdown_done = True
            return
        receipt = shutdown(timestamp_s=self.clock())
        if receipt is not None:
            self.summary.shutdown_neutral_count += 1
        self._shutdown_done = True

    def _reject_qgc(
        self,
        reason: str,
        *,
        message_id: int | None = None,
        frame: bool = True,
    ) -> None:
        if frame:
            self.summary.qgc_frames_blocked += 1
        else:
            self.summary.qgc_datagrams_blocked += 1
        self.summary.qgc_rejected_reasons[reason] += 1
        if message_id is not None:
            self.summary.qgc_blocked_message_ids[message_id] += 1

    def _reject_px4(
        self,
        reason: str,
        *,
        message_id: int | None = None,
        frame: bool = True,
    ) -> None:
        if frame:
            self.summary.px4_frames_blocked += 1
        else:
            self.summary.px4_datagrams_blocked += 1
        self.summary.px4_rejected_reasons[reason] += 1
        if message_id is not None:
            self.summary.px4_blocked_message_ids[message_id] += 1


def _validate_adapter(adapter: Any) -> None:
    if adapter is None:
        raise TypeError("adapter is required")
    for name in ("submit_manual", "evidence", "shutdown"):
        if not callable(getattr(adapter, name, None)):
            raise TypeError(f"adapter must provide {name}()")


def _float_is_one_of(value: float, allowed: tuple[float, ...]) -> bool:
    return math.isfinite(value) and any(
        math.isclose(value, candidate, rel_tol=0.0, abs_tol=1e-6) for candidate in allowed
    )


def _zero_or_nan(values: tuple[float, ...]) -> bool:
    """Accept protocol's zero or NaN ('unspecified') padding, never infinity."""

    return all(
        math.isnan(value) or math.isclose(value, 0.0, rel_tol=0.0, abs_tol=1e-6) for value in values
    )


def _validate_port(value: int, name: str) -> None:
    if not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ValueError(f"{name} must be in 1024..65535")


def _source_key(source: tuple[str, int]) -> tuple[str, int]:
    """Normalize a recvfrom endpoint for decoder-fragment ownership."""

    host = str(source[0])
    try:
        port = int(source[1])
    except (TypeError, ValueError):
        # Invalid ports are rejected before this helper is called in the
        # normal path.  Keeping a deterministic sentinel makes the helper
        # robust for direct fixture calls as well.
        port = -1
    return host, port


def _parse_loopback_udpout(endpoint: str) -> tuple[str, int]:
    if not isinstance(endpoint, str) or not endpoint.startswith("udpout:"):
        raise SimulationIsolationError(
            "PX4 output must use udpout:127.0.0.1:<port> or udpout:::1:<port>"
        )
    value = endpoint[len("udpout:") :]
    if value.startswith("127.0.0.1:"):
        host, port_text = LOOPBACK_HOST, value[len("127.0.0.1:") :]
    elif value.startswith("[::1]:"):
        host, port_text = IPV6_LOOPBACK_HOST, value[len("[::1]:") :]
    elif value.startswith("::1:"):
        host, port_text = IPV6_LOOPBACK_HOST, value[len("::1:") :]
    else:
        raise SimulationIsolationError(
            "PX4 output must use udpout:127.0.0.1:<port> or udpout:::1:<port>"
        )
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise SimulationIsolationError("PX4 output port is invalid") from exc
    _validate_port(port, "px4_output_port")
    return host, port


def _endpoint_text(endpoint: tuple[str, int]) -> str:
    host, port = endpoint
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _make_udp_socket(endpoint: tuple[str, int]) -> socket.socket:
    host, port = endpoint
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    channel = socket.socket(family, socket.SOCK_DGRAM)
    channel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        channel.bind(endpoint)
        channel.setblocking(False)
    except BaseException:
        channel.close()
        raise
    return channel


def _socket_send(channel: socket.socket, data: bytes, destination: tuple[str, int]) -> None:
    """Send one frame, handling an IPv4/IPv6 destination family mismatch.

    The normal topology uses IPv4 loopback throughout.  Supporting ``::1`` in
    the config is cheap, though, and avoids silently turning an explicitly
    selected IPv6 loopback profile into a failed send when the receive socket
    was created in the other family.
    """

    host, port = destination
    expected_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    if channel.family == expected_family:
        channel.sendto(data, destination)
        return
    with socket.socket(expected_family, socket.SOCK_DGRAM) as transient:
        transient.sendto(data, destination)


def _counter_mapping(counter: Counter[int] | Counter[str]) -> dict[str, int]:
    return {str(key): value for key, value in sorted(counter.items(), key=lambda item: item[0])}


def _payload_target_matches(
    payload: bytes,
    *,
    system_offset: int,
    component_offset: int,
    target_system: int,
    target_component_ids: frozenset[int],
    broadcast_system_allowed: bool = False,
) -> bool:
    """Validate target bytes while respecting MAVLink 2 zero-tail truncation."""

    if len(payload) <= system_offset:
        return False
    system_id = payload[system_offset]
    if system_id != target_system and not (broadcast_system_allowed and system_id == 0):
        return False
    component_id = payload[component_offset] if len(payload) > component_offset else 0
    return component_id in target_component_ids


def _qgc_read_only_target_matches(
    frame: MavlinkFrame,
    *,
    target_system: int,
    target_component_ids: frozenset[int],
) -> bool:
    """Fail closed on malformed or cross-vehicle QGC discovery requests.

    ``qgc_frame_is_read_only`` classifies operation type, not its vehicle
    target.  The interactive proxy has only one disposable SITL vehicle, so
    every targeted read must address that system (and component 0/1 by
    default).  Heartbeat and TIMESYNC carry no target.  PING may legitimately
    use broadcast system 0 during link discovery.
    """

    if frame.message_id == MSG_HEARTBEAT:
        return len(frame.payload) >= 9
    if frame.message_id == MSG_TIMESYNC:
        return len(frame.payload) >= 8
    if frame.message_id == MSG_PING:
        return _payload_target_matches(
            frame.payload,
            system_offset=12,
            component_offset=13,
            target_system=target_system,
            target_component_ids=target_component_ids,
            broadcast_system_allowed=True,
        )
    if frame.message_id in {
        MSG_PARAM_REQUEST_LIST,
        MSG_MISSION_REQUEST_LIST,
        MSG_MISSION_ACK,
        MSG_AUTOPILOT_VERSION_REQUEST,
        MSG_PARAM_EXT_REQUEST_READ,
        MSG_PARAM_EXT_REQUEST_LIST,
    }:
        return _payload_target_matches(
            frame.payload,
            system_offset=0,
            component_offset=1,
            target_system=target_system,
            target_component_ids=target_component_ids,
        )
    if frame.message_id in {
        MSG_PARAM_REQUEST_READ,
        MSG_MISSION_REQUEST,
        MSG_MISSION_REQUEST_INT,
        MSG_REQUEST_DATA_STREAM,
    }:
        return _payload_target_matches(
            frame.payload,
            system_offset=2,
            component_offset=3,
            target_system=target_system,
            target_component_ids=target_component_ids,
        )
    if frame.message_id == MSG_COMMAND_LONG:
        return _payload_target_matches(
            frame.payload,
            system_offset=30,
            component_offset=31,
            target_system=target_system,
            target_component_ids=target_component_ids,
        )
    if frame.message_id == MSG_FILE_TRANSFER_PROTOCOL:
        target = frame.file_transfer_protocol_target
        return (
            target is not None
            and target[0] == target_system
            and target[1] in target_component_ids
            and frame.file_transfer_protocol_opcode in READ_ONLY_FTP_OPCODES
        )
    return False


def _decode_qgc_manual_control(
    payload: bytes,
    *,
    version: int,
    throttle_wire_mode: ThrottleWireMode,
) -> tuple[float, float, float, float]:
    """Decode strict MANUAL_CONTROL and normalize QGC/PX4 thrust semantics.

    QGC's established wire format uses ``z`` in ``[0, 1000]`` for thrust,
    while signed MAVLink senders may use ``[-1000, 1000]``.  ``auto`` keeps
    both forms interoperable: negative values are signed and non-negative
    values use QGC's legacy scale.  Axis and extension validation intentionally
    mirrors the SIM control gateway.
    """

    if version not in (1, 2):
        raise ValueError("unsupported_mavlink_version")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 11:
        raise ValueError("manual_control_payload_truncated")
    if version == 1 and len(payload) != 11:
        raise ValueError("v1_manual_control_payload_length")
    if version == 2 and (len(payload) > 30 or 11 < len(payload) < 14):
        raise ValueError("v2_manual_control_payload_length")
    x, y, z, r = struct.unpack_from("<hhhh", payload, 0)
    if any(value == 32767 for value in (x, y, z, r)):
        raise ValueError("manual_control_axis_invalid_sentinel")
    if any(abs(value) > 1000 for value in (x, y, z, r)):
        raise ValueError("manual_control_axis_out_of_range")
    if version == 2 and len(payload) >= 14:
        enabled = payload[13]
        required = 14 + 2 * enabled.bit_length() if enabled else 14
        if len(payload) < required:
            raise ValueError("v2_manual_control_extension_truncated")
        for index in range(8):
            if enabled & (1 << index):
                value = struct.unpack_from("<h", payload, 14 + 2 * index)[0]
                if value == 32767 or abs(value) > 1000:
                    raise ValueError("manual_control_extension_out_of_range")
    if throttle_wire_mode == "signed":
        throttle = (z + 1000) / 2000.0
    else:  # qgc_legacy/auto: retain QGC [0,1000], signed fallback for negatives.
        throttle = z / 1000.0 if z >= 0 else (z + 1000) / 2000.0
    return x / 1000.0, y / 1000.0, r / 1000.0, throttle


def _build_frame_for_cli(
    *,
    pitch: float,
    roll: float,
    yaw: float,
    throttle: float,
    throttle_wire_mode: ThrottleWireMode = "qgc_legacy",
) -> bytes:
    """Build a tiny v1 fixture without depending on pymavlink.

    ``auto`` uses the legacy non-negative representation for generated frames;
    it is an inbound decoder mode and has no single canonical outbound scale.
    """

    if throttle_wire_mode not in {"auto", "qgc_legacy", "signed"}:
        raise ValueError("throttle_wire_mode must be auto, qgc_legacy or signed")
    wire_throttle = (
        round(throttle * 2000 - 1000) if throttle_wire_mode == "signed" else round(throttle * 1000)
    )
    values = (round(pitch * 1000), round(roll * 1000), wire_throttle, round(yaw * 1000))
    if not all(-1000 <= value <= 1000 for value in values):
        raise ValueError("axis outside MAVLink range")
    payload = struct.pack("<hhhhHB", values[0], values[1], values[2], values[3], 0, PX4_SYSTEM_ID)
    return (
        bytes([0xFE, len(payload), 0, QGC_SYSTEM_ID, QGC_COMPONENT_ID, MSG_MANUAL_CONTROL])
        + payload
        + b"\x00\x00"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the loopback-only SIM QGC/PX4 interactive router"
    )
    parser.add_argument("--qgc-input-port", type=int, default=DEFAULT_QGC_INPUT_PORT)
    parser.add_argument("--qgc-output-port", type=int, default=DEFAULT_QGC_OUTPUT_PORT)
    parser.add_argument("--px4-input-port", type=int, default=DEFAULT_PX4_INPUT_PORT)
    parser.add_argument(
        "--px4-output-endpoint",
        default=f"udpout:{LOOPBACK_HOST}:{DEFAULT_PX4_OUTPUT_PORT}",
    )
    parser.add_argument(
        "--throttle-wire-mode",
        choices=("auto", "qgc_legacy", "signed"),
        default="auto",
        help=(
            "MANUAL_CONTROL z decoding mode: auto accepts QGC's legacy "
            "[0,1000] scale and signed negative values"
        ),
    )
    parser.add_argument(
        "--manual-watchdog-seconds",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="fail-safe when QGC MANUAL_CONTROL stops (default: 0.5)",
    )
    parser.add_argument(
        "--qgc-control-mode",
        choices=("read_only", "sim_flight"),
        default="read_only",
        help="QGC command policy; sim_flight enables the bounded native Fly View path",
    )
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # CLI startup intentionally fails before opening anything without an
    # adapter/connection.  The orchestrator supplies the real adapter; this
    # command is primarily a configuration probe and JSON evidence endpoint.
    try:
        from .control import SimulatorControlAuthority
        from .mavlink_control import SimulatorMavlinkControlAdapter, SimulatorMavlinkControlConfig

        authority = SimulatorControlAuthority()
        # The adapter emits one canonical wire representation and therefore
        # intentionally accepts only the two concrete encodings.  ``auto`` is
        # a parser-side compatibility mode: retain the legacy QGC output for
        # adapter/AI frames while allowing signed negative ``z`` on inbound
        # QGC frames through the router decoder.
        adapter_wire_mode = (
            "qgc_legacy" if args.throttle_wire_mode == "auto" else args.throttle_wire_mode
        )
        adapter = SimulatorMavlinkControlAdapter(
            authority,
            config=SimulatorMavlinkControlConfig(
                endpoint=args.px4_output_endpoint,
                throttle_wire_mode=adapter_wire_mode,
                manual_watchdog_s=args.manual_watchdog_seconds,
            ),
        )
        router = SimulatorInteractiveRouter(
            adapter,
            config=SimulatorInteractiveRouterConfig(
                qgc_input_port=args.qgc_input_port,
                qgc_output_port=args.qgc_output_port,
                px4_input_port=args.px4_input_port,
                px4_output_endpoint=args.px4_output_endpoint,
                throttle_wire_mode=args.throttle_wire_mode,
                qgc_control_mode=args.qgc_control_mode,
            ),
        )
        print(json.dumps(router.run(duration_s=args.duration_seconds), sort_keys=True))
        return 0
    except (OSError, SimulationIsolationError, SimulatorInteractiveRouterError, ValueError) as exc:
        print(json.dumps({"event": "simulator_interactive_router_failed", "error": str(exc)}))
        return 2


__all__ = [
    "DEFAULT_PX4_INPUT_PORT",
    "DEFAULT_PX4_OUTPUT_PORT",
    "DEFAULT_QGC_INPUT_PORT",
    "DEFAULT_QGC_OUTPUT_PORT",
    "IPV6_LOOPBACK_HOST",
    "LOOPBACK_HOST",
    "MSG_MANUAL_CONTROL",
    "MSG_MISSION_COUNT",
    "MSG_MISSION_ITEM",
    "MSG_MISSION_ITEM_INT",
    "MSG_SET_MODE",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_DO_VTOL_TRANSITION",
    "MAV_CMD_MISSION_START",
    "MAV_CMD_NAV_LAND",
    "MAV_CMD_NAV_RETURN_TO_LAUNCH",
    "QgcControlMode",
    "SIM_FLIGHT_COMMAND_ALLOWLIST",
    "SIM_FLIGHT_MISSION_COMMAND_ALLOWLIST",
    "PX4_SYSTEM_ID",
    "QGC_COMPONENT_ID",
    "QGC_SYSTEM_ID",
    "SimulatorInteractiveRouter",
    "SimulatorInteractiveRouterConfig",
    "SimulatorInteractiveRouterError",
    "SimulatorInteractiveRouterSummary",
    "ThrottleWireMode",
    "_build_frame_for_cli",
    "_decode_qgc_manual_control",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

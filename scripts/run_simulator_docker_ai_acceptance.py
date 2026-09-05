#!/usr/bin/env python3
"""Run the truth-free AI path against PX4/Gazebo Docker telemetry.

The normal Docker acceptance helper is deliberately passive: it observes a
heartbeat/GPS fix and sends only a GCS heartbeat.  This companion keeps that
runtime/asset gate, then attaches :class:`SimulatorTelemetryBridge` to the
same PX4 telemetry stream.  Observations are converted into
``SimulatorTelemetrySnapshot`` values and evaluated by the production
``SimulatorAiExecutor``.  Any actuator output is emitted only by the existing
loopback ``SimulatorMavlinkControlAdapter`` (``MANUAL_CONTROL``); no QGC,
serial, Pixhawk, Gazebo-truth, or alternate flight-command path is created.

The default invocation is a no-I/O preflight plus a deterministic in-process
fixture.  ``--execute`` is required to start the pinned Docker image and open
the two loopback MAVLink legs.  ``--exercise-arm`` is an explicit, disposable
SITL-only opt-in: it sends one ordinary ``MAV_CMD_COMPONENT_ARM_DISARM`` arm
request, waits for an armed heartbeat, and sends a matching disarm request
before cleanup.  Runtime evidence keeps policy evaluation and command
emission separate from PX4 vehicle/actuator response and from visual,
hardware, and real-flight acceptance.

``--exercise-target`` is a second explicit, disposable SITL fixture.  After
the armed-heartbeat gate opens it injects one truth-free perception snapshot
through ``SimulatorTelemetryBridge.ingest_perception`` and requires the
production policy to emit ``TRACK``.  It never reads Gazebo truth or treats
synthetic target data as camera/visual accuracy evidence.

``--exercise-flight-seconds`` extends that target exercise into a bounded,
sustained control-response gate.  The same perception seam refreshes the
target, the same production AI executor emits ``MANUAL_CONTROL``, and actual
PX4 ``GLOBAL_POSITION_INT`` samples must show the configured WGS-84 horizontal
displacement.  No Gazebo pose service, truth topic, teleport, or alternate
flight-command path is used.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.geolocation_accuracy import wgs84_distance_m
from multidetect.product_version import PRODUCT_VERSION
from multidetect.qgc_readonly_bridge import MavlinkFrame
from multidetect.simulator import (
    SimulatorAiExecutor,
    SimulatorControlAuthority,
    SimulatorInteractiveRouter,
    SimulatorInteractiveRouterConfig,
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
    SimulatorTelemetrySnapshot,
)
from multidetect.simulator.control_gateway import build_manual_control_frame
from multidetect.simulator.live_ai_bridge import SimulatorTelemetryBridge

try:  # pytest imports ``scripts`` as a namespace package
    from scripts.run_simulator_docker import (
        DEFAULT_SCENARIO,
        PINNED_IMAGE,
        DockerSimulatorPlanError,
    )
    from scripts.run_simulator_docker_acceptance import (
        DEFAULT_CONTAINER_NAME,
        DEFAULT_TELEMETRY_PORT,
        DockerSimulatorAcceptanceError,
    )
    from scripts.run_simulator_docker_acceptance import (
        run_acceptance as run_docker_acceptance,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from run_simulator_docker import (  # type: ignore[no-redef]
        DEFAULT_SCENARIO,
        PINNED_IMAGE,
        DockerSimulatorPlanError,
    )
    from run_simulator_docker_acceptance import (  # type: ignore[no-redef]
        DEFAULT_CONTAINER_NAME,
        DEFAULT_TELEMETRY_PORT,
        DockerSimulatorAcceptanceError,
    )
    from run_simulator_docker_acceptance import (
        run_acceptance as run_docker_acceptance,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "artifacts" / "evaluation" / "simulator-docker-ai-acceptance.json"
DEFAULT_AI_CONTAINER_NAME = f"{DEFAULT_CONTAINER_NAME}-ai"
DEFAULT_AI_CONTROL_PORT = 24540
DEFAULT_AI_TELEMETRY_PORT = DEFAULT_TELEMETRY_PORT
GCS_SOURCE_SYSTEM = 245
GCS_SOURCE_COMPONENT = 190
PX4_SYSTEM_ID = 1
PX4_AUTOPILOT_COMPONENT = 1
DEFAULT_PX4_COMPONENT_IDS = frozenset({PX4_AUTOPILOT_COMPONENT})
MAV_CMD_COMPONENT_ARM_DISARM = 400
MSG_COMMAND_ACK = 77
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
PX4_CUSTOM_MAIN_MODE_POSCTL = 3
PX4_POSCTL_CUSTOM_MODE = PX4_CUSTOM_MAIN_MODE_POSCTL << 16
PX4_CUSTOM_MAIN_MODE_STABILIZED = 7
PX4_STABILIZED_CUSTOM_MODE = PX4_CUSTOM_MAIN_MODE_STABILIZED << 16
ARM_RETRY_INTERVAL_S = 1.0
ARM_MAX_ATTEMPTS = 6
FLIGHT_MODE_RETRY_INTERVAL_S = 1.0
FLIGHT_MODE_MAX_ATTEMPTS = 3
FLIGHT_TARGET_REFRESH_S = 0.1
MAX_EXERCISE_FLIGHT_DURATION_S = 30.0
DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M = 0.5
MAX_POSITION_SAMPLES = 4096
DEFAULT_MAXIMUM_TELEMETRY_FRAMES = 100_000
DOCKER_FLIGHT_HEARTBEAT_TIMEOUT_S = 3.0
FLIGHT_FAULTS = frozenset(
    {"none", "target_refresh_dropout", "heartbeat_dropout", "flight_mode_loss"}
)
RELEASE_STATUS = (
    f"v{PRODUCT_VERSION.release_label}; final v{PRODUCT_VERSION.numeric} remains blocked"
)


# Keep the Docker AI probe's transport contract local and explicit.  The base
# Docker helper publishes host ports on 127.0.0.1, but a caller can still
# provide a custom probe factory or construct ``DockerAiTelemetryProbe``
# directly.  Do not let those seams turn this acceptance path into a REAL
# network, hostname, serial, or wildcard endpoint.  Both spellings emitted by
# pymavlink for IPv6 loopback are accepted (bracketed URI style and the
# historical ``udpin:::1:<port>`` form).
_LOOPBACK_UDP_ENDPOINT = re.compile(
    r"^(?P<scheme>udpin|udpout):(?:"
    r"127\.0\.0\.1:(?P<ipv4_port>[0-9]{1,5})|"
    r"(?:\[::1\]|::1):(?P<ipv6_port>[0-9]{1,5})"
    r")\Z"
)


def parse_literal_loopback_udp_endpoint(
    endpoint: object,
    *,
    scheme: str,
) -> tuple[str, int]:
    """Return ``(host, port)`` for one exact loopback MAVLink URI.

    The Docker acceptance path intentionally does not resolve names.  A
    hostname such as ``localhost`` is ambiguous and a wildcard/non-loopback
    address would weaken the isolation claim, so neither is accepted.  The
    ``scheme`` argument is kept separate from the URI text to prevent an
    ``udpout`` control URI being accidentally accepted as telemetry input (or
    vice versa).
    """

    if scheme not in {"udpin", "udpout"}:
        raise ValueError("scheme must be udpin or udpout")
    if not isinstance(endpoint, str):
        raise DockerAiAcceptanceError(
            f"{scheme} endpoint must be a literal loopback UDP URI"
        )
    match = _LOOPBACK_UDP_ENDPOINT.fullmatch(endpoint)
    if match is None or match.group("scheme") != scheme:
        raise DockerAiAcceptanceError(
            f"{scheme} endpoint must be literal loopback 127.0.0.1 or ::1"
        )
    port_text = match.group("ipv4_port") or match.group("ipv6_port")
    # The regex guarantees digits, but keep the conversion defensive because
    # this helper is part of the public test seam.
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:  # pragma: no cover - regex guards it
        raise DockerAiAcceptanceError(f"{scheme} endpoint port is invalid") from exc
    if not 1 <= port <= 65535:
        raise DockerAiAcceptanceError(
            f"{scheme} endpoint port must be in 1..65535 "
            "for a literal loopback UDP URI"
        )
    host = "127.0.0.1" if match.group("ipv4_port") is not None else "::1"
    return host, port


def _validate_telemetry_endpoint(endpoint: object) -> tuple[str, int]:
    return parse_literal_loopback_udp_endpoint(endpoint, scheme="udpin")


def _validate_control_endpoint(endpoint: object) -> tuple[str, int]:
    return parse_literal_loopback_udp_endpoint(endpoint, scheme="udpout")


def _normalize_component_ids(
    component_ids: Iterable[int] | None,
) -> frozenset[int] | None:
    """Validate and freeze the expected PX4 MAVLink source components.

    ``None`` intentionally means "accept any component" for replay/fuzz
    fixtures.  The live Docker probe defaults to
    :data:`DEFAULT_PX4_COMPONENT_IDS`, so a companion, camera, or simulator
    bridge cannot satisfy the armed-heartbeat/COMMAND_ACK gate by accident.
    """

    if component_ids is None:
        return None
    if isinstance(component_ids, (str, bytes, bytearray)):
        raise DockerAiAcceptanceError(
            "px4_component_ids must contain integer MAVLink component ids"
        )
    try:
        selected = frozenset(component_ids)
    except TypeError as exc:
        raise DockerAiAcceptanceError(
            "px4_component_ids must contain integer MAVLink component ids"
        ) from exc
    if not selected or any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
        for value in selected
    ):
        raise DockerAiAcceptanceError(
            "px4_component_ids must contain integer MAVLink component ids in 0..255"
        )
    return selected


def _validate_minimum_gps_fix_type(value: object) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        selected = int(value)
    except (TypeError, ValueError) as exc:
        raise DockerAiAcceptanceError(
            "minimum_gps_fix_type must be an integer in 0..6"
        ) from exc
    if selected != value and isinstance(value, float):
        # Avoid silently accepting fractional thresholds such as 3.5.
        raise DockerAiAcceptanceError(
            "minimum_gps_fix_type must be an integer in 0..6"
        )
    if not 0 <= selected <= 6:
        raise DockerAiAcceptanceError(
            "minimum_gps_fix_type must be an integer in 0..6"
        )
    return selected


def _validate_exercise_flight_duration(value: object) -> float:
    if isinstance(value, bool):
        raise DockerAiAcceptanceError(
            "exercise_flight_duration_s must be 0 or a finite value in 1..30 seconds"
        )
    try:
        selected = float(value)
    except (TypeError, ValueError) as exc:
        raise DockerAiAcceptanceError(
            "exercise_flight_duration_s must be 0 or a finite value in 1..30 seconds"
        ) from exc
    if not math.isfinite(selected) or selected < 0.0 or (
        selected != 0.0 and not 1.0 <= selected <= MAX_EXERCISE_FLIGHT_DURATION_S
    ):
        raise DockerAiAcceptanceError(
            "exercise_flight_duration_s must be 0 or a finite value in 1..30 seconds"
        )
    return selected


def _validate_minimum_flight_displacement(value: object) -> float:
    if isinstance(value, bool):
        raise DockerAiAcceptanceError(
            "minimum_flight_displacement_m must be a finite value in (0, 100]"
        )
    try:
        selected = float(value)
    except (TypeError, ValueError) as exc:
        raise DockerAiAcceptanceError(
            "minimum_flight_displacement_m must be a finite value in (0, 100]"
        ) from exc
    if not math.isfinite(selected) or not 0.0 < selected <= 100.0:
        raise DockerAiAcceptanceError(
            "minimum_flight_displacement_m must be a finite value in (0, 100]"
        )
    return selected


def _validate_flight_fault(value: object) -> str:
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized not in FLIGHT_FAULTS:
        raise DockerAiAcceptanceError(
            f"flight fault must be one of {sorted(FLIGHT_FAULTS)!r}"
        )
    return normalized


class DockerAiAcceptanceError(DockerSimulatorAcceptanceError):
    """A bounded Docker/telemetry/AI bridge gate failed."""


SimulatorDockerAiAcceptanceError = DockerAiAcceptanceError


def _resolve_docker_executable(docker: str | Path | None) -> str:
    """Resolve the same Docker executable candidates as the base runner."""

    if docker is not None:
        return str(Path(docker).expanduser())
    found = shutil.which("docker")
    if found:
        return found
    return r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def _prepare_disposable_sitl_for_arm(
    docker: str,
    container_name: str,
    *,
    runner: Callable[..., object] | None,
    enable_joystick_control: bool = False,
) -> dict[str, object]:
    """Disable only disposable PX4 pre-arm checks before an opt-in arm.

    The parameters match the existing VTOL SITL acceptance harness.  They are
    applied with ``docker exec`` inside the newly-created, hash-pinned
    container and are retained as evidence; no host/Pixhawk configuration is
    touched.
    """

    overrides: tuple[tuple[str, str], ...] = (
        ("COM_ARM_HFLT_CHK", "0"),
        ("COM_MODE_ARM_CHK", "0"),
        ("CBRK_VTOLARMING", "22027"),
        ("NAV_DLL_ACT", "0"),
    )
    if enable_joystick_control:
        # MANUAL_CONTROL is PX4's joystick input.  This opt-in is scoped to
        # the hash-pinned disposable container and is required before POSCTL
        # will consume the shared AI/manual axes as pilot input.
        overrides += (("COM_RC_IN_MODE", "1"),)
    applied: list[dict[str, object]] = []
    for parameter, value in overrides:
        argv = [
            docker,
            "exec",
            container_name,
            "/opt/px4-gazebo/bin/px4-param",
            "set",
            parameter,
            value,
        ]
        if runner is not None:
            result = runner(argv, timeout_s=10.0, check=True)
        else:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
        returncode = getattr(result, "returncode", 0)
        stdout = str(getattr(result, "stdout", "") or "").strip()
        stderr = str(getattr(result, "stderr", "") or "").strip()
        row = {
            "parameter": parameter,
            "value": value,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "scope": "owned disposable PX4/Gazebo SITL only",
        }
        applied.append(row)
        if returncode != 0:
            raise DockerAiAcceptanceError(
                f"disposable SITL arm preflight failed for {parameter}: "
                f"{stderr or stdout or f'returncode={returncode}'}"
            )
    return {
        "requested": True,
        "applied": True,
        "parameters": applied,
        "error": None,
    }


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _message_type(message: object) -> str:
    if isinstance(message, MavlinkFrame):
        return {
            0: "HEARTBEAT",
            1: "SYS_STATUS",
            24: "GPS_RAW_INT",
            30: "ATTITUDE",
            33: "GLOBAL_POSITION_INT",
            74: "VFR_HUD",
            MSG_COMMAND_ACK: "COMMAND_ACK",
        }.get(message.message_id, f"MSG_{message.message_id}")
    getter = getattr(message, "get_type", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = ""
        return str(value).upper() if value is not None else ""
    if isinstance(message, Mapping):
        value = message.get("type") or message.get("message_type")
        return str(value).upper() if value is not None else ""
    value = getattr(message, "_type", None)
    return str(value).upper() if value is not None else ""


def _field(message: object, name: str, default: object = None) -> object:
    if isinstance(message, MavlinkFrame) and message.message_id == MSG_COMMAND_ACK:
        # MAVLink common COMMAND_ACK payload (v1/v2 compatible prefix):
        # command:uint16, result:uint8, progress:uint8,
        # result_param2:int32, target_system:uint8, target_component:uint8.
        payload = message.payload
        offsets: dict[str, tuple[int, str]] = {
            "command": (0, "H"),
            "result": (2, "B"),
            "progress": (3, "B"),
            "result_param2": (4, "i"),
            "target_system": (8, "B"),
            "target_component": (9, "B"),
        }
        spec = offsets.get(name)
        if spec is not None:
            offset, fmt = spec
            width = struct.calcsize(fmt)
            if len(payload) >= offset + width:
                return struct.unpack_from(f"<{fmt}", payload, offset)[0]
        return default
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


def _source_ids(message: object) -> tuple[int | None, int | None]:
    if isinstance(message, MavlinkFrame):
        return message.source_system_id, message.source_component_id
    if isinstance(message, Mapping):
        system = message.get("source_system_id", message.get("srcSystem"))
        component = message.get("source_component_id", message.get("srcComponent"))
    else:
        system = component = None
        for name, target in (("get_srcSystem", "system"), ("get_srcComponent", "component")):
            getter = getattr(message, name, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    value = None
                if target == "system":
                    system = value
                else:
                    component = value
        header = getattr(message, "_header", None)
        if header is not None:
            if system is None:
                system = getattr(header, "srcSystem", getattr(header, "src_system", None))
            if component is None:
                component = getattr(
                    header, "srcComponent", getattr(header, "src_component", None)
                )
    try:
        system = None if system is None else int(system)
    except (TypeError, ValueError):
        system = None
    try:
        component = None if component is None else int(component)
    except (TypeError, ValueError):
        component = None
    return system, component


def _result_text(result: object, field: str) -> str:
    value = getattr(result, field, "")
    return value if isinstance(value, str) else str(value or "")


class DockerAiTelemetryProbe:
    """Observe PX4 telemetry and run the shared SIM AI executor.

    The class deliberately accepts injectable connection factories.  Unit
    tests can therefore feed deterministic message objects without importing
    ``pymavlink``; production ``--execute`` lazily imports it only at the
    actual Docker boundary.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        control_endpoint: str,
        connection_factory: Callable[..., Any] | None = None,
        telemetry_connection_factory: Callable[..., Any] | None = None,
        control_connection_factory: Callable[..., Any] | None = None,
        mavutil_module: object | None = None,
        source_system: int = GCS_SOURCE_SYSTEM,
        source_component: int = GCS_SOURCE_COMPONENT,
        px4_system_id: int = PX4_SYSTEM_ID,
        px4_component_ids: Iterable[int] | None = DEFAULT_PX4_COMPONENT_IDS,
        minimum_gps_fix_type: int = 3,
        clock: Callable[[], float] = time.monotonic,
        minimum_ai_steps: int = 1,
        maximum_frames: int = DEFAULT_MAXIMUM_TELEMETRY_FRAMES,
        wait_for_armed_heartbeat: bool = True,
        exercise_arm: bool = False,
        exercise_target: bool = False,
        exercise_flight_duration_s: float = 0.0,
        minimum_flight_displacement_m: float = DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M,
        inject_flight_fault: str = "none",
    ) -> None:
        # Validate both legs before constructing any connection factory.  This
        # keeps dry-run fixtures and live ``--execute`` runs under the same
        # isolation contract, and prevents a custom factory from smuggling in
        # a hostname, serial URI, wildcard, or non-loopback destination.
        telemetry_host, telemetry_port = _validate_telemetry_endpoint(endpoint)
        control_host, control_port = _validate_control_endpoint(control_endpoint)
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(minimum_ai_steps, bool) or not 1 <= int(minimum_ai_steps) <= 10_000:
            raise ValueError("minimum_ai_steps must be in 1..10000")
        if isinstance(maximum_frames, bool) or not 1 <= int(maximum_frames) <= 100_000:
            raise ValueError("maximum_frames must be in 1..100000")
        if not isinstance(wait_for_armed_heartbeat, bool):
            raise TypeError("wait_for_armed_heartbeat must be bool")
        if not isinstance(exercise_arm, bool):
            raise TypeError("exercise_arm must be bool")
        if exercise_arm and not wait_for_armed_heartbeat:
            raise ValueError("exercise_arm requires wait_for_armed_heartbeat")
        if not isinstance(exercise_target, bool):
            raise TypeError("exercise_target must be bool")
        if exercise_target and not exercise_arm:
            raise ValueError("exercise_target requires exercise_arm")
        try:
            normalized_flight_duration = _validate_exercise_flight_duration(
                exercise_flight_duration_s
            )
            normalized_minimum_displacement = _validate_minimum_flight_displacement(
                minimum_flight_displacement_m
            )
            normalized_flight_fault = _validate_flight_fault(inject_flight_fault)
        except DockerAiAcceptanceError as exc:
            raise ValueError(str(exc)) from exc
        if normalized_flight_duration > 0.0 and not exercise_target:
            raise ValueError("exercise_flight_duration_s requires exercise_target")
        if normalized_flight_fault != "none" and normalized_flight_duration <= 0.0:
            raise ValueError("inject_flight_fault requires exercise_flight_duration_s")
        try:
            normalized_component_ids = _normalize_component_ids(px4_component_ids)
        except DockerAiAcceptanceError as exc:
            # Keep constructor validation errors consistent with the existing
            # probe API (ValueError for bad numeric/configuration arguments),
            # while retaining the detailed message for callers/evidence.
            raise ValueError(str(exc)) from exc
        try:
            normalized_minimum_fix = _validate_minimum_gps_fix_type(
                minimum_gps_fix_type
            )
        except DockerAiAcceptanceError as exc:
            raise ValueError(str(exc)) from exc
        # Preserve the caller's exact URI for evidence (including bracketed
        # IPv6 spelling), while retaining parsed values for validation and
        # endpoint-family-aware fallback handling.
        self.endpoint = endpoint
        self.requested_endpoint = endpoint
        self.control_endpoint = control_endpoint
        self._telemetry_host = telemetry_host
        self._telemetry_port = telemetry_port
        self._control_host = control_host
        self._control_port = control_port
        self._internal_wildcard_fallback = False
        self.connection_factory = telemetry_connection_factory or connection_factory
        self.control_connection_factory = control_connection_factory
        self.mavutil = mavutil_module
        self.source_system = int(source_system)
        self.source_component = int(source_component)
        self.px4_system_id = int(px4_system_id)
        self.px4_component_ids = normalized_component_ids
        self.minimum_gps_fix_type = normalized_minimum_fix
        self.clock = clock
        self.minimum_ai_steps = int(minimum_ai_steps)
        self.maximum_frames = int(maximum_frames)
        self.poll_iterations = 0
        self.frame_limit_reached = False
        self.wait_for_armed_heartbeat = wait_for_armed_heartbeat
        self.exercise_arm = exercise_arm
        self.exercise_target = exercise_target
        self.exercise_flight_duration_s = normalized_flight_duration
        self.minimum_flight_displacement_m = normalized_minimum_displacement
        self.inject_flight_fault = normalized_flight_fault
        self.flight_fault_trigger_after_s = min(1.0, normalized_flight_duration / 2.0)
        self.flight_fault_activated_at_s: float | None = None
        self.flight_fault_mode_command_sent = False
        self.flight_fault_mode_command_error: str | None = None
        self.flight_fault_suppressed_heartbeats = 0
        self.frames_observed = 0
        self.messages: list[dict[str, object]] = []
        self.rejections: list[dict[str, object]] = []
        self.heartbeat: dict[str, object] | None = None
        self.gps: dict[str, object] | None = None
        self.position: dict[str, object] | None = None
        self.position_samples: list[dict[str, object]] = []
        self.heartbeat_samples: list[dict[str, object]] = []
        self.gcs_heartbeat_sent = 0
        self.wildcard_bind_fallback = False
        self.bind_endpoint = endpoint
        self.error: str | None = None
        self.error_code: int | None = None
        self.observe_exit_reason = "not_started"
        self.observe_result: dict[str, object] | None = None
        self._telemetry_connection: Any | None = None
        self._control_connection: Any | None = None
        # Arm/disarm is deliberately opt-in and is retained as a separate
        # evidence channel from MANUAL_CONTROL policy output.  This makes a
        # passive telemetry run impossible to misread as an arm exercise.
        self.arm_requested = False
        self.arm_attempts = 0
        self.arm_last_attempt_at_s: float | None = None
        self.arm_command_sent = False
        self.arm_command_error: str | None = None
        self.disarm_requested = False
        self.disarm_command_sent = False
        self.disarm_command_error: str | None = None
        self.target_observation_sent = False
        self.target_observation: dict[str, object] | None = None
        self.target_decision: dict[str, object] | None = None
        self.target_exercise_error: str | None = None
        self.flight_started_at_s: float | None = None
        self.flight_completed_at_s: float | None = None
        self.flight_ended_at_s: float | None = None
        self.flight_initial_position: dict[str, object] | None = None
        self.flight_position_samples: list[dict[str, object]] = []
        self.flight_maximum_horizontal_displacement_m = 0.0
        self.flight_target_updates = 0
        self.flight_target_rejections = 0
        self.flight_target_update_times_s: list[float] = []
        self._last_flight_target_at_s: float | None = None
        self.flight_mode_requested = False
        self.flight_mode_attempts = 0
        self.flight_mode_last_attempt_at_s: float | None = None
        self.flight_mode_command_sent = False
        self.flight_mode_command_error: str | None = None
        self.flight_mode_observed = False
        self.flight_mode_observed_at_s: float | None = None
        self.flight_mode_loss_events = 0
        self.command_results: list[dict[str, object]] = []
        self.command_acks: list[dict[str, object]] = []
        self.command_acks_observed = 0
        self.command_acks_accepted = 0
        self.command_acks_rejected = 0
        self.arm_preflight: dict[str, object] = {
            "requested": self.exercise_arm,
            "applied": False,
            "parameters": [],
            "error": None,
        }
        authority = SimulatorControlAuthority()
        # Construction validates the endpoint as literal loopback and keeps
        # AI's conservative envelope in force for every runtime command.
        adapter = SimulatorMavlinkControlAdapter(
            authority,
            config=SimulatorMavlinkControlConfig(
                endpoint=control_endpoint,
                source_system=self.source_system,
                source_component=self.source_component,
                watchdog_s=0.5,
            ),
            connection_factory=self._connect_control,
        )
        self.adapter = adapter
        self.executor = SimulatorAiExecutor(adapter, clock=self.clock)
        # Windows Docker Desktop can deliver the nominal 1 Hz PX4 heartbeat
        # with slightly more than two seconds of host scheduling jitter.  A
        # bounded live flight exercise uses a still-fail-closed 3 s timeout;
        # all other probe behavior retains the production bridge default.
        heartbeat_timeout_s = (
            DOCKER_FLIGHT_HEARTBEAT_TIMEOUT_S
            if normalized_flight_duration > 0.0
            else 2.0
        )
        self.bridge = SimulatorTelemetryBridge(
            self.executor,
            clock=self.clock,
            px4_system_id=self.px4_system_id,
            px4_component_ids=self.px4_component_ids,
            minimum_gps_fix_type=self.minimum_gps_fix_type,
            heartbeat_timeout_s=heartbeat_timeout_s,
            # Keep the production live path behind the startup arm gate.
            # Disarmed PX4 telemetry is recorded, but no AI policy step or
            # control output is claimed until an armed heartbeat is observed.
            wait_for_armed_heartbeat=self.wait_for_armed_heartbeat,
        )

    def _connect_telemetry(self, endpoint: str | None = None) -> Any:
        target = self.endpoint if endpoint is None else endpoint
        if target != self.endpoint and not self._internal_wildcard_fallback:
            # The only alternate target this class may open is the narrowly
            # scoped Windows Docker UDP-proxy fallback created by ``observe``;
            # arbitrary caller-supplied targets remain rejected.
            _validate_telemetry_endpoint(target)
        factory = self.connection_factory
        if factory is not None:
            try:
                return factory(
                    target,
                    source_system=self.source_system,
                    source_component=self.source_component,
                    autoreconnect=False,
                )
            except TypeError:
                return factory(target)
        module = self.mavutil
        if module is None:
            try:
                from pymavlink import mavutil as module  # type: ignore[import-not-found]
            except ImportError as exc:
                raise DockerAiAcceptanceError(
                    "pymavlink is unavailable; install the pixhawk extra for --execute"
                ) from exc
        self.mavutil = module
        connector = getattr(module, "mavlink_connection", None)
        if not callable(connector):
            raise DockerAiAcceptanceError("MAVLink module has no mavlink_connection")
        return connector(
            target,
            source_system=self.source_system,
            source_component=self.source_component,
            autoreconnect=False,
        )

    def _connect_control(self, endpoint: str, **_kwargs: object) -> Any:
        _validate_control_endpoint(endpoint)
        # The optional arm command and the shared MANUAL_CONTROL adapter must
        # use one loopback socket.  Reusing the connection prevents a second
        # ephemeral source port from making the arm/disarm evidence look like
        # an alternate control path.
        if self._control_connection is not None:
            return self._control_connection
        factory = self.control_connection_factory
        if factory is not None:
            try:
                connection = factory(
                    endpoint,
                    source_system=self.source_system,
                    source_component=self.source_component,
                    autoreconnect=False,
                )
            except TypeError:
                connection = factory(endpoint)
            self._control_connection = connection
            return connection
        module = self.mavutil
        if module is None:
            try:
                from pymavlink import mavutil as module  # type: ignore[import-not-found]
            except ImportError as exc:
                raise DockerAiAcceptanceError(
                    "pymavlink is unavailable; install the pixhawk extra for --execute"
                ) from exc
        self.mavutil = module
        connector = getattr(module, "mavlink_connection", None)
        if not callable(connector):
            raise DockerAiAcceptanceError("MAVLink module has no mavlink_connection")
        connection = connector(
            endpoint,
            source_system=self.source_system,
            source_component=self.source_component,
            autoreconnect=False,
        )
        self._control_connection = connection
        return connection

    def _mavlink_constants(self) -> object | None:
        module = self.mavutil
        if module is None:
            return None
        return getattr(module, "mavlink", None)

    def _send_command_long(
        self,
        *,
        action: str,
        param1: float,
    ) -> bool:
        """Send one normal arm/disarm command over the shared SITL socket.

        The method is intentionally narrow: only
        ``MAV_CMD_COMPONENT_ARM_DISARM`` is available, the optional PX4
        force-arm sentinel is used for the disposable arm exercise, and the
        target is the validated PX4 system/autopilot component.  It returns
        ``False`` when a minimal test double does not expose
        ``command_long_send``; production callers get a structured error in
        the resulting evidence instead of a silent arm claim.
        """

        if action not in {"arm", "disarm"}:
            raise ValueError("action must be arm or disarm")
        connection = self._connect_control(self.control_endpoint)
        # PX4 only treats a command source as an active GCS after seeing its
        # heartbeat.  Send that liveness frame on the same published control
        # socket before the arm request; the telemetry leg remains receive/
        # outbound-only and no alternate command transport is introduced.
        self._send_heartbeat(connection)
        mav = getattr(connection, "mav", None)
        sender = getattr(mav, "command_long_send", None)
        if not callable(sender):
            raise DockerAiAcceptanceError(
                "MAVLink control connection has no command_long_send"
            )
        constants = self._mavlink_constants()
        command_id = int(
            getattr(constants, "MAV_CMD_COMPONENT_ARM_DISARM", MAV_CMD_COMPONENT_ARM_DISARM)
            if constants is not None
            else MAV_CMD_COMPONENT_ARM_DISARM
        )
        target_component = int(
            getattr(constants, "MAV_COMP_ID_AUTOPILOT1", PX4_AUTOPILOT_COMPONENT)
            if constants is not None
            else PX4_AUTOPILOT_COMPONENT
        )
        # PX4's documented force-arm sentinel is used only for the disposable
        # SITL exercise after the preflight parameters above have been applied.
        # It is recorded explicitly so the run cannot be mistaken for a
        # physical-aircraft safety-check result.
        force_param2 = 21196.0 if action == "arm" else 0.0
        sender(
            int(self.adapter.config.target_system),
            target_component,
            command_id,
            0,
            float(param1),
            force_param2,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        self.command_results.append(
            {
                "action": action,
                "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                "command_id": command_id,
                "target_system": int(self.adapter.config.target_system),
                "target_component": target_component,
                "param1": float(param1),
                "param2": force_param2,
                "force_arm_sentinel": force_param2 == 21196.0,
                "sent": True,
            }
        )
        return True

    @staticmethod
    def _command_result_name(result: int) -> str:
        return {
            0: "accepted",
            1: "temporarily_rejected",
            2: "denied",
            3: "unsupported",
            4: "failed",
            5: "in_progress",
            6: "cancelled",
        }.get(result, "unknown")

    def _record_command_ack(self, message: object, stamp: float) -> None:
        source_system, source_component = _source_ids(message)
        command = _int_field(message, "command", -1)
        result = _int_field(message, "result", -1)
        self.command_acks_observed += 1
        rejection_reason: str | None = None
        # COMMAND_ACK is the only arm/disarm acknowledgement accepted by this
        # probe.  Require the source to be the configured PX4 system and (for
        # the live default) an autopilot component.  A companion or camera
        # sending an ACK must never make the armed-heartbeat gate pass.
        if source_system != self.px4_system_id:
            rejection_reason = "command_ack_source_system_mismatch"
        elif (
            self.px4_component_ids is not None
            and source_component not in self.px4_component_ids
        ):
            rejection_reason = "command_ack_source_component_mismatch"
        elif command != MAV_CMD_COMPONENT_ARM_DISARM:
            rejection_reason = "command_ack_command_mismatch"
        row: dict[str, object] = {
            "type": "COMMAND_ACK",
            "observed_at_s": stamp,
            "source_system_id": source_system,
            "source_component_id": source_component,
            "command_id": command,
            "result": result,
            "result_name": self._command_result_name(result),
            "result_param2": _int_field(message, "result_param2", 0),
            "progress": _int_field(message, "progress", 0),
            "identity_valid": rejection_reason is None,
            "accepted_for_arm_exercise": rejection_reason is None,
        }
        if rejection_reason is not None:
            row["rejection_reason"] = rejection_reason
        self.command_acks.append(row)
        if len(self.command_acks) > 64:
            del self.command_acks[:-64]
        if rejection_reason is not None:
            self.command_acks_rejected += 1
            self._reject(
                rejection_reason,
                stamp,
                source_system=source_system,
                source_component=source_component,
                command_id=command,
            )
            return
        self.command_acks_accepted += 1
        # Attach the first matching acknowledgement to the most recent arm or
        # disarm request.  Keeping the raw ACK list above preserves duplicate
        # or delayed PX4 responses for diagnosis.
        for request in reversed(self.command_results):
            if request.get("command_id") == command and "ack_result" not in request:
                request["ack_result"] = result
                request["ack_result_name"] = self._command_result_name(result)
                break

    def _request_arm(self) -> bool:
        self.arm_requested = True
        self.arm_attempts += 1
        self.arm_last_attempt_at_s = self.clock()
        try:
            self.arm_command_sent = self._send_command_long(action="arm", param1=1.0)
        except (DockerAiAcceptanceError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self.arm_command_error = str(exc)
            self.command_results.append(
                {
                    "action": "arm",
                    "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                    "command_id": MAV_CMD_COMPONENT_ARM_DISARM,
                    "sent": False,
                    "error": str(exc),
                }
            )
        return self.arm_command_sent

    def _request_disarm(self) -> bool:
        self.disarm_requested = True
        try:
            self.disarm_command_sent = self._send_command_long(
                action="disarm", param1=0.0
            )
        except (DockerAiAcceptanceError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self.disarm_command_error = str(exc)
            self.command_results.append(
                {
                    "action": "disarm",
                    "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                    "command_id": MAV_CMD_COMPONENT_ARM_DISARM,
                    "sent": False,
                    "error": str(exc),
                }
            )
        return self.disarm_command_sent

    def _request_flight_mode(self) -> bool:
        """Request PX4 POSCTL on the existing loopback control socket."""

        self.flight_mode_requested = True
        self.flight_mode_attempts += 1
        self.flight_mode_last_attempt_at_s = self.clock()
        try:
            connection = self._connect_control(self.control_endpoint)
            self._send_heartbeat(connection)
            mav = getattr(connection, "mav", None)
            sender = getattr(mav, "set_mode_send", None)
            if not callable(sender):
                raise DockerAiAcceptanceError(
                    "MAVLink control connection has no set_mode_send"
                )
            sender(
                int(self.adapter.config.target_system),
                MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                PX4_POSCTL_CUSTOM_MODE,
            )
            self.flight_mode_command_sent = True
            self.flight_mode_command_error = None
        except (
            DockerAiAcceptanceError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self.flight_mode_command_sent = False
            self.flight_mode_command_error = str(exc)
        return self.flight_mode_command_sent

    def _activate_flight_fault(self, stamp: float) -> bool:
        """Activate one explicit SITL-only negative-path fault after flight starts."""

        if (
            self.inject_flight_fault == "none"
            or self.flight_fault_activated_at_s is not None
            or self.flight_started_at_s is None
            or stamp - self.flight_started_at_s < self.flight_fault_trigger_after_s
        ):
            return False
        self.flight_fault_activated_at_s = stamp
        if self.inject_flight_fault != "flight_mode_loss":
            return True
        try:
            connection = self._connect_control(self.control_endpoint)
            mav = getattr(connection, "mav", None)
            sender = getattr(mav, "set_mode_send", None)
            if not callable(sender):
                raise DockerAiAcceptanceError(
                    "MAVLink control connection has no set_mode_send"
                )
            sender(
                int(self.adapter.config.target_system),
                MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                PX4_STABILIZED_CUSTOM_MODE,
            )
            self.flight_fault_mode_command_sent = True
            self.flight_fault_mode_command_error = None
        except (
            DockerAiAcceptanceError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self.flight_fault_mode_command_sent = False
            self.flight_fault_mode_command_error = str(exc)
        return True

    def _flight_fault_evidence(self, status: str) -> dict[str, object]:
        requested = self.inject_flight_fault != "none"
        activated = self.flight_fault_activated_at_s is not None
        if self.inject_flight_fault == "target_refresh_dropout":
            observed = activated and status == "target_refresh_not_sustained"
        elif self.inject_flight_fault == "heartbeat_dropout":
            observed = (
                activated
                and self.flight_fault_suppressed_heartbeats > 0
                and status == "heartbeat_continuity_failed"
            )
        elif self.inject_flight_fault == "flight_mode_loss":
            observed = (
                activated
                and self.flight_fault_mode_command_sent
                and status == "flight_mode_loss_observed"
            )
        else:
            observed = False
        return {
            "requested": requested,
            "name": self.inject_flight_fault,
            "trigger_after_s": self.flight_fault_trigger_after_s,
            "activated": activated,
            "activated_at_s": self.flight_fault_activated_at_s,
            "observed": observed,
            "fail_closed": bool(requested and observed),
            "suppressed_heartbeats": self.flight_fault_suppressed_heartbeats,
            "mode_command_sent": self.flight_fault_mode_command_sent,
            "mode_command_error": self.flight_fault_mode_command_error,
            "scope": "disposable_loopback_px4_sitl_only",
        }

    def _arm_evidence(self) -> dict[str, object]:
        return {
            "enabled": self.exercise_arm,
            "autopilot_system_id": self.px4_system_id,
            "autopilot_component_ids": (
                None
                if self.px4_component_ids is None
                else sorted(self.px4_component_ids)
            ),
            "command_id": MAV_CMD_COMPONENT_ARM_DISARM,
            "arm_requested": self.arm_requested,
            "arm_attempts": self.arm_attempts,
            "arm_last_attempt_at_s": self.arm_last_attempt_at_s,
            "arm_command_sent": self.arm_command_sent,
            "arm_command_error": self.arm_command_error,
            "disarm_requested": self.disarm_requested,
            "disarm_command_sent": self.disarm_command_sent,
            "disarm_command_error": self.disarm_command_error,
            "commands": list(self.command_results),
            "acks": list(self.command_acks),
            "command_acks_observed": self.command_acks_observed,
            "command_acks_accepted": self.command_acks_accepted,
            "command_acks_rejected": self.command_acks_rejected,
            "preflight": dict(self.arm_preflight),
        }

    def _flight_mode_evidence(self) -> dict[str, object]:
        current_base_mode = (
            None if self.heartbeat is None else int(self.heartbeat.get("base_mode", 0))
        )
        current_custom_mode = (
            None if self.heartbeat is None else int(self.heartbeat.get("custom_mode", 0))
        )
        return {
            "requested": self.flight_mode_requested,
            "attempts": self.flight_mode_attempts,
            "last_attempt_at_s": self.flight_mode_last_attempt_at_s,
            "command": "SET_MODE",
            "base_mode": MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            "custom_mode": PX4_POSCTL_CUSTOM_MODE,
            "px4_main_mode": "POSCTL",
            "command_sent": self.flight_mode_command_sent,
            "command_error": self.flight_mode_command_error,
            "observed": self.flight_mode_observed,
            "observed_at_s": self.flight_mode_observed_at_s,
            "current_heartbeat_base_mode": current_base_mode,
            "current_heartbeat_custom_mode": current_custom_mode,
            "current_heartbeat_armed": bool(
                current_base_mode is not None and current_base_mode & 128
            ),
            "current_heartbeat_custom_mode_enabled": bool(
                current_base_mode is not None
                and current_base_mode & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            ),
            "loss_events": self.flight_mode_loss_events,
            "clean_throughout": self.flight_mode_loss_events == 0,
            "transport": "shared loopback MAVLink control socket",
        }

    def _send_heartbeat(self, connection: Any) -> None:
        mav = getattr(connection, "mav", None)
        sender = getattr(mav, "heartbeat_send", None)
        module = self.mavutil
        constants = getattr(module, "mavlink", None) if module is not None else None
        if not callable(sender) or constants is None:
            return
        try:
            sender(
                getattr(constants, "MAV_TYPE_GCS", 6),
                getattr(constants, "MAV_AUTOPILOT_INVALID", 8),
                0,
                0,
                0,
            )
            self.gcs_heartbeat_sent += 1
        except Exception:
            return

    def _record(self, message: object, stamp: float) -> None:
        self.frames_observed += 1
        if self.frames_observed >= self.maximum_frames:
            self.frame_limit_reached = True
        if self.frames_observed > self.maximum_frames:
            self._reject("maximum_frames_exceeded", stamp)
            return
        kind = _message_type(message)
        if kind.upper() == "COMMAND_ACK":
            self._record_command_ack(message, stamp)
            return
        if (
            kind == "HEARTBEAT"
            and self.inject_flight_fault == "heartbeat_dropout"
            and self.flight_fault_activated_at_s is not None
        ):
            self.flight_fault_suppressed_heartbeats += 1
            self.messages.append(
                {
                    "type": kind,
                    "observed_at_s": stamp,
                    "fault_injected": "heartbeat_dropout",
                    "suppressed_before_bridge": True,
                }
            )
            return
        before = int(self.bridge.evidence().get("frames_rejected", 0))
        decision = self.bridge.ingest(message, stamp)
        bridge_evidence = self.bridge.evidence()
        after = int(bridge_evidence.get("frames_rejected", 0))
        if after > before:
            reason = "bridge_rejected"
            rejection_list = bridge_evidence.get("rejections")
            if isinstance(rejection_list, list) and rejection_list:
                last = rejection_list[-1]
                if isinstance(last, Mapping):
                    reason = str(last.get("reason") or reason)
            self._reject(reason, stamp)
            return
        source_system, source_component = _source_ids(message)
        row: dict[str, object] = {
            "type": kind,
            "observed_at_s": stamp,
            "source_system_id": source_system,
            "source_component_id": source_component,
            "ai_step": decision is not None,
        }
        if kind == "HEARTBEAT" and not isinstance(message, MavlinkFrame):
            custom_mode = _int_field(message, "custom_mode", 0)
            self.heartbeat = {
                "vehicle_type": _int_field(message, "type_value", _int_field(message, "type", 0)),
                "autopilot": _int_field(message, "autopilot", 0),
                "base_mode": _int_field(message, "base_mode", 0),
                "custom_mode": custom_mode,
                "system_status": _int_field(message, "system_status", 0),
            }
            row.update(self.heartbeat)
            self.heartbeat_samples.append({"observed_at_s": stamp, **self.heartbeat})
            if len(self.heartbeat_samples) > 256:
                del self.heartbeat_samples[:-256]
            previously_observed = self.flight_mode_observed
            base_mode = int(self.heartbeat["base_mode"])
            self.flight_mode_observed = bool(
                self.flight_mode_command_sent
                and base_mode & 128
                and base_mode & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                and ((custom_mode >> 16) & 0xFF) == PX4_CUSTOM_MAIN_MODE_POSCTL
            )
            if self.flight_mode_observed:
                self.flight_mode_observed_at_s = stamp
            elif previously_observed:
                self.flight_mode_loss_events += 1
        elif kind == "GPS_RAW_INT" and not isinstance(message, MavlinkFrame):
            self.gps = {
                "fix_type": _int_field(message, "fix_type", 0),
                "satellites_visible": _int_field(message, "satellites_visible", 0),
                "lat": _int_field(message, "lat", 0),
                "lon": _int_field(message, "lon", 0),
                "alt_mm": _int_field(message, "alt", 0),
                "eph_cm": _int_field(message, "eph", 0),
                "epv_cm": _int_field(message, "epv", 0),
            }
            row.update(self.gps)
        elif kind == "GLOBAL_POSITION_INT" and not isinstance(message, MavlinkFrame):
            lat = _int_field(message, "lat", 0) / 10_000_000.0
            lon = _int_field(message, "lon", 0) / 10_000_000.0
            relative_alt = _int_field(message, "relative_alt", 0) / 1000.0
            self.position = {
                "latitude_deg": lat,
                "longitude_deg": lon,
                "relative_altitude_m": relative_alt,
                "heading_cdeg": _int_field(message, "hdg", 65535),
            }
            row.update(self.position)
        elif isinstance(message, MavlinkFrame):
            # The live pymavlink path supplies named fields.  A router
            # telemetry sink may instead hand us an already split frame; the
            # bridge has decoded its observation, so mirror the normalized
            # snapshot into the compact probe fields for qualification.
            snapshot = self.bridge.evidence().get("last_snapshot")
            if isinstance(snapshot, Mapping):
                if kind == "HEARTBEAT":
                    armed = snapshot.get("armed") is True
                    self.heartbeat = {
                        "vehicle_type": 0,
                        "autopilot": 0,
                        "base_mode": 128 if armed else 0,
                        "custom_mode": 0,
                        "system_status": 0,
                    }
                    row.update(self.heartbeat)
                elif kind == "GPS_RAW_INT":
                    latitude = float(snapshot.get("latitude_deg") or 0.0)
                    longitude = float(snapshot.get("longitude_deg") or 0.0)
                    self.gps = {
                        "fix_type": int(snapshot.get("gps_fix_type") or 0),
                        "satellites_visible": int(snapshot.get("satellites_visible") or 0),
                        "lat": round(latitude * 10_000_000),
                        "lon": round(longitude * 10_000_000),
                        "alt_mm": 0,
                        "eph_cm": round(
                            float(snapshot.get("gps_horizontal_accuracy_m") or 0.0) * 100
                        ),
                        "epv_cm": round(
                            float(snapshot.get("gps_vertical_accuracy_m") or 0.0) * 100
                        ),
                    }
                    row.update(self.gps)
                elif kind == "GLOBAL_POSITION_INT":
                    self.position = {
                        "latitude_deg": snapshot.get("latitude_deg"),
                        "longitude_deg": snapshot.get("longitude_deg"),
                        "relative_altitude_m": snapshot.get("altitude_agl_m"),
                        "heading_cdeg": round(float(snapshot.get("heading_deg") or 0.0) * 100),
                    }
                    row.update(self.position)
        if kind == "GLOBAL_POSITION_INT" and self.position is not None:
            try:
                latitude = float(self.position["latitude_deg"])
                longitude = float(self.position["longitude_deg"])
                relative_altitude = float(self.position["relative_altitude_m"])
                heading_cdeg = int(self.position["heading_cdeg"])
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            else:
                if (
                    math.isfinite(latitude)
                    and math.isfinite(longitude)
                    and math.isfinite(relative_altitude)
                    and -90.0 <= latitude <= 90.0
                    and -180.0 <= longitude <= 180.0
                ):
                    sample = {
                        "observed_at_s": stamp,
                        "latitude_deg": latitude,
                        "longitude_deg": longitude,
                        "relative_altitude_m": relative_altitude,
                        "heading_cdeg": heading_cdeg,
                    }
                    self.position_samples.append(sample)
                    if len(self.position_samples) > MAX_POSITION_SAMPLES:
                        del self.position_samples[:-MAX_POSITION_SAMPLES]
                    if (
                        self.flight_started_at_s is not None
                        and self.flight_initial_position is not None
                    ):
                        self.flight_position_samples.append(dict(sample))
                        if len(self.flight_position_samples) > MAX_POSITION_SAMPLES:
                            del self.flight_position_samples[:-MAX_POSITION_SAMPLES]
                        try:
                            displacement = wgs84_distance_m(
                                latitude1_deg=float(
                                    self.flight_initial_position["latitude_deg"]
                                ),
                                longitude1_deg=float(
                                    self.flight_initial_position["longitude_deg"]
                                ),
                                latitude2_deg=latitude,
                                longitude2_deg=longitude,
                            )
                        except (KeyError, TypeError, ValueError, OverflowError):
                            displacement = 0.0
                        self.flight_maximum_horizontal_displacement_m = max(
                            self.flight_maximum_horizontal_displacement_m,
                            displacement,
                        )
        self.messages.append(row)
        if len(self.messages) > 1000:
            del self.messages[:-1000]

    def _exercise_target_observation(self) -> object | None:
        """Inject or refresh a synthetic target through the public seam.

        A normal target exercise remains a single observation.  A requested
        flight exercise refreshes the same observation at a bounded cadence
        so the adapter watchdog receives a real sustained ``MANUAL_CONTROL``
        stream.  The input stays explicitly synthetic and never writes a
        Gazebo pose or consumes a simulator truth topic.
        """

        stamp = self.clock()
        flight_requested = self.exercise_flight_duration_s > 0.0
        if not flight_requested and self.target_observation_sent:
            return self.target_decision
        if (
            flight_requested
            and self._last_flight_target_at_s is not None
            and stamp - self._last_flight_target_at_s < FLIGHT_TARGET_REFRESH_S
        ):
            return self.target_decision
        if flight_requested and not self.position_samples:
            self.target_exercise_error = (
                "flight exercise is waiting for a valid GLOBAL_POSITION_INT baseline"
            )
            return None

        source = (
            "synthetic_perception_flight_fixture"
            if flight_requested
            else "synthetic_perception_fixture"
        )
        bearing_deg = 0.0 if flight_requested else 18.0
        distance_m = 120.0 if flight_requested else 90.0
        confidence = 0.90 if flight_requested else 0.86
        self.target_observation_sent = True
        if flight_requested:
            self._last_flight_target_at_s = stamp
        observation = SimulatorTelemetrySnapshot(
            observed_at_s=stamp,
            target_visible=True,
            target_bearing_deg=bearing_deg,
            target_distance_m=distance_m,
            target_confidence=confidence,
        )
        previous_observation = self.target_observation
        self.target_observation = {
            "source": source,
            "truth_free": True,
            "observed_at_s": stamp,
            "target_visible": True,
            "target_bearing_deg": bearing_deg,
            "target_distance_m": distance_m,
            "target_confidence": confidence,
        }
        try:
            decision = self.bridge.ingest_perception(observation, timestamp_s=stamp)
        except (RuntimeError, TypeError, ValueError) as exc:
            self.target_exercise_error = str(exc)
            self.target_observation["accepted"] = False
            self.target_observation["error"] = str(exc)
            if flight_requested:
                self.flight_target_rejections += 1
            if previous_observation is not None and self.target_decision is not None:
                self.target_observation = previous_observation
            return None
        if decision is None:
            self.target_observation["accepted"] = False
            self.target_observation["decision"] = None
            self.target_exercise_error = "perception observation did not reach the AI gate"
            if flight_requested:
                self.flight_target_rejections += 1
            if previous_observation is not None and self.target_decision is not None:
                self.target_observation = previous_observation
            return None
        decision_mapping = decision.to_mapping()
        accepted_track = bool(
            decision_mapping.get("mode") == "track"
            and decision_mapping.get("accepted") is True
        )
        self.target_observation["accepted"] = accepted_track
        self.target_observation["decision"] = decision_mapping
        if not accepted_track:
            if flight_requested:
                self.flight_target_rejections += 1
            if self.target_decision is None:
                self.target_exercise_error = (
                    "perception observation did not produce an accepted TRACK decision"
                )
            elif previous_observation is not None:
                self.target_observation = previous_observation
            return decision
        self.target_decision = decision_mapping
        self.target_exercise_error = None
        if flight_requested:
            if self.flight_started_at_s is None:
                self.flight_initial_position = dict(self.position_samples[-1])
                self.flight_started_at_s = stamp
                self.flight_position_samples = [dict(self.flight_initial_position)]
            self.flight_target_updates += 1
            self.flight_target_update_times_s.append(stamp)
        return decision

    def _flight_evidence(
        self,
        *,
        now_s: float | None = None,
        include_samples: bool = True,
    ) -> dict[str, object]:
        requested = self.exercise_flight_duration_s > 0.0
        stamp = self.clock() if now_s is None else float(now_s)
        started = self.flight_started_at_s
        finished = self.flight_completed_at_s
        ended = self.flight_ended_at_s
        end_stamp = finished if finished is not None else ended if ended is not None else stamp
        elapsed = 0.0 if started is None else max(0.0, end_stamp - started)

        initial = self.flight_initial_position
        samples = [dict(sample) for sample in self.flight_position_samples]
        final_position = samples[-1] if samples else None
        final_displacement = 0.0
        if initial is not None and final_position is not None:
            try:
                final_displacement = wgs84_distance_m(
                    latitude1_deg=float(initial["latitude_deg"]),
                    longitude1_deg=float(initial["longitude_deg"]),
                    latitude2_deg=float(final_position["latitude_deg"]),
                    longitude2_deg=float(final_position["longitude_deg"]),
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                final_displacement = 0.0
        maximum_displacement = self.flight_maximum_horizontal_displacement_m
        altitude_delta = 0.0
        if initial is not None and final_position is not None:
            try:
                altitude_delta = float(final_position["relative_altitude_m"]) - float(
                    initial["relative_altitude_m"]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                altitude_delta = 0.0

        update_times = list(self.flight_target_update_times_s)
        refresh_coverage = (
            max(0.0, update_times[-1] - update_times[0])
            if len(update_times) >= 2
            else 0.0
        )
        update_gaps = [
            later - earlier
            for earlier, later in zip(update_times, update_times[1:], strict=False)
        ]
        maximum_refresh_gap = max(update_gaps, default=None)
        heartbeat_rows = [dict(sample) for sample in self.heartbeat_samples]
        if started is not None:
            heartbeat_rows = [
                sample
                for sample in heartbeat_rows
                if started <= float(sample["observed_at_s"]) <= end_stamp
            ]
        heartbeat_times = [float(sample["observed_at_s"]) for sample in heartbeat_rows]
        heartbeat_boundaries = list(heartbeat_times)
        if started is not None:
            prior_heartbeat = next(
                (
                    float(sample["observed_at_s"])
                    for sample in reversed(self.heartbeat_samples)
                    if float(sample["observed_at_s"]) < started
                ),
                None,
            )
            if prior_heartbeat is not None:
                heartbeat_boundaries.insert(0, prior_heartbeat)
            elif heartbeat_boundaries:
                heartbeat_boundaries.insert(0, started)
            if heartbeat_boundaries:
                heartbeat_boundaries.append(end_stamp)
        heartbeat_gaps = [
            later - earlier
            for earlier, later in zip(
                heartbeat_boundaries,
                heartbeat_boundaries[1:],
                strict=False,
            )
        ]
        maximum_heartbeat_gap = max(heartbeat_gaps, default=None)
        bridge_evidence = self.bridge.evidence()
        heartbeat_timeout = bridge_evidence.get("heartbeat_timeout_s")
        heartbeat_continuity_gate = bool(
            not requested
            or (
                maximum_heartbeat_gap is not None
                and _finite(heartbeat_timeout)
                and maximum_heartbeat_gap < float(heartbeat_timeout)
                and bridge_evidence.get("heartbeat_stale") is not True
            )
        )
        refresh_gate = bool(
            len(update_times) >= 2
            and refresh_coverage >= self.exercise_flight_duration_s
            and maximum_refresh_gap is not None
            and maximum_refresh_gap <= 0.5
        )
        gate_passed = bool(
            not requested
            or (
                started is not None
                and elapsed >= self.exercise_flight_duration_s
                and self.flight_mode_observed
                and self.flight_mode_loss_events == 0
                and refresh_gate
                and heartbeat_continuity_gate
                and self.flight_target_rejections == 0
                and int(bridge_evidence.get("ai_rejected", 0)) == 0
                and int(bridge_evidence.get("failsafe_steps", 0)) == 0
                and int(bridge_evidence.get("watchdog_failsafe_steps", 0)) == 0
                and maximum_displacement >= self.minimum_flight_displacement_m
            )
        )
        if not requested:
            status = "not_requested"
        elif gate_passed:
            status = "passed"
        elif started is None:
            status = "target_stream_not_started"
        elif self.flight_mode_loss_events > 0:
            status = "flight_mode_loss_observed"
        elif not self.flight_mode_observed:
            status = "flight_mode_not_observed"
        elif (
            self.inject_flight_fault == "heartbeat_dropout"
            and not heartbeat_continuity_gate
        ):
            status = "heartbeat_continuity_failed"
        elif not refresh_gate:
            status = "target_refresh_not_sustained"
        elif not heartbeat_continuity_gate:
            status = "heartbeat_continuity_failed"
        elif self.flight_target_rejections > 0:
            status = "target_refresh_rejected"
        elif (
            int(bridge_evidence.get("ai_rejected", 0)) > 0
            or int(bridge_evidence.get("failsafe_steps", 0)) > 0
            or int(bridge_evidence.get("watchdog_failsafe_steps", 0)) > 0
        ):
            status = "failsafe_or_rejection_observed"
        elif elapsed < self.exercise_flight_duration_s:
            status = "flight_duration_not_reached"
        else:
            status = "position_response_not_observed"
        result: dict[str, object] = {
            "requested": requested,
            "gate_passed": gate_passed,
            "status": status,
            "requested_duration_s": self.exercise_flight_duration_s,
            "elapsed_duration_s": round(elapsed, 6),
            "completed": finished is not None,
            "minimum_horizontal_displacement_m": self.minimum_flight_displacement_m,
            "maximum_horizontal_displacement_m": round(maximum_displacement, 6),
            "final_horizontal_displacement_m": round(final_displacement, 6),
            "final_relative_altitude_delta_m": round(altitude_delta, 6),
            "target_updates_accepted": self.flight_target_updates,
            "target_updates_rejected": self.flight_target_rejections,
            "target_refresh_interval_s": FLIGHT_TARGET_REFRESH_S,
            "target_refresh_coverage_s": round(refresh_coverage, 6),
            "maximum_target_refresh_gap_s": (
                None if maximum_refresh_gap is None else round(maximum_refresh_gap, 6)
            ),
            "target_refresh_gate_passed": refresh_gate if requested else True,
            "flight_mode": self._flight_mode_evidence(),
            "flight_mode_gate_passed": (
                self.flight_mode_observed and self.flight_mode_loss_events == 0
                if requested
                else True
            ),
            "heartbeat_timeout_s": heartbeat_timeout,
            "heartbeat_sample_count": len(heartbeat_times),
            "maximum_heartbeat_gap_s": (
                None if maximum_heartbeat_gap is None else round(maximum_heartbeat_gap, 6)
            ),
            "heartbeat_continuity_gate_passed": heartbeat_continuity_gate,
            "position_sample_count": len(samples),
            "initial_position": None if initial is None else dict(initial),
            "final_position": None if final_position is None else dict(final_position),
            "position_source": "MAVLink GLOBAL_POSITION_INT",
            "control_path": (
                "SimulatorTelemetryBridge.ingest_perception -> "
                "SimulatorAiExecutor -> SimulatorMavlinkControlAdapter -> "
                "MAVLink MANUAL_CONTROL -> PX4 SITL/Gazebo"
            ),
            "synthetic_perception_input": True,
            "gazebo_truth_consumed": False,
            "gazebo_pose_injection_used": False,
            "px4_actuator_response": "not_claimed",
            "visual_accuracy": "not_claimed",
            "hardware_acceptance": "not_claimed",
            "hil_acceptance": "not_claimed",
            "real_flight_acceptance": "not_claimed",
        }
        result["fault_injection"] = self._flight_fault_evidence(status)
        if include_samples:
            result["heartbeat_samples"] = heartbeat_rows
            result["position_samples"] = samples
        return result

    def _reject(
        self,
        reason: str,
        stamp: float,
        *,
        source_system: int | None = None,
        source_component: int | None = None,
        command_id: int | None = None,
    ) -> None:
        row: dict[str, object] = {
            "reason": reason,
            "timestamp_s": stamp,
        }
        if source_system is not None:
            row["source_system_id"] = source_system
        if source_component is not None:
            row["source_component_id"] = source_component
        if command_id is not None:
            row["command_id"] = command_id
        self.rejections.append(row)
        if len(self.rejections) > 256:
            del self.rejections[:-256]

    def _qualified(self) -> bool:
        if self.heartbeat is None:
            return False
        if self.gps is not None:
            try:
                latitude = int(self.gps.get("lat", 0)) / 10_000_000.0
                longitude = int(self.gps.get("lon", 0)) / 10_000_000.0
                return bool(
                    int(self.gps.get("fix_type", 0)) >= self.minimum_gps_fix_type
                    and _finite(latitude)
                    and _finite(longitude)
                    and -90.0 <= latitude <= 90.0
                    and -180.0 <= longitude <= 180.0
                    and latitude != 0.0
                    and longitude != 0.0
                )
            except (TypeError, ValueError, OverflowError):
                return False
        snapshot = self.bridge.evidence().get("last_snapshot")
        if not isinstance(snapshot, Mapping):
            return False
        try:
            return bool(
                int(snapshot.get("gps_fix_type") or 0) >= self.minimum_gps_fix_type
                and _finite(snapshot.get("latitude_deg"))
                and _finite(snapshot.get("longitude_deg"))
                and -90.0 <= float(snapshot.get("latitude_deg")) <= 90.0
                and -180.0 <= float(snapshot.get("longitude_deg")) <= 180.0
                and float(snapshot.get("latitude_deg") or 0.0) != 0.0
                and float(snapshot.get("longitude_deg") or 0.0) != 0.0
            )
        except (TypeError, ValueError):
            return False

    def observe(self, timeout_s: float) -> dict[str, object]:
        if not _finite(timeout_s) or float(timeout_s) <= 0.0:
            raise DockerAiAcceptanceError("telemetry timeout must be finite and positive")
        started = self.clock()
        deadline = started + float(timeout_s)
        telemetry: Any | None = None
        self.observe_exit_reason = "running"
        try:
            try:
                telemetry = self._connect_telemetry()
            except OSError as exc:
                code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
                if code != 10013 or not self.endpoint.startswith("udpin:127.0.0.1:"):
                    raise
                fallback = "udpin:0.0.0.0:" + self.endpoint.rsplit(":", 1)[-1]
                # Docker Desktop's Windows UDP proxy can reserve the
                # published 127.0.0.1 socket exclusively.  Permit exactly
                # this internally generated wildcard *receive* bind after the
                # literal loopback attempt fails; it is never accepted as a
                # caller endpoint and the host publication remains loopback.
                self._internal_wildcard_fallback = True
                try:
                    telemetry = self._connect_telemetry(fallback)
                finally:
                    self._internal_wildcard_fallback = False
                self.bind_endpoint = fallback
                self.wildcard_bind_fallback = True
            self.bridge.enable_ai()
            # For a real PX4 startup the first socket connection can precede
            # commander/GPS readiness by several seconds.  Keep the arm
            # request pending until one qualified heartbeat+GPS sample has
            # arrived; sending it earlier can be silently dropped while the
            # same command succeeds once PX4 is ready.
            next_heartbeat = started
            while self.clock() < deadline and self.frames_observed < self.maximum_frames:
                self.poll_iterations += 1
                now = self.clock()
                self._activate_flight_fault(now)
                if now >= next_heartbeat:
                    self._send_heartbeat(telemetry)
                    next_heartbeat = now + 0.5
                receiver = getattr(telemetry, "recv_match", None)
                if not callable(receiver):
                    raise DockerAiAcceptanceError("MAVLink telemetry connection has no recv_match")
                remaining = max(0.01, min(0.25, deadline - now))
                try:
                    message = receiver(blocking=True, timeout=remaining)
                except TypeError:
                    message = receiver(timeout=remaining)
                if message is not None:
                    self._record(message, self.clock())
                if self.exercise_arm and not self.arm_requested and self._qualified():
                    # The bridge still requires the armed heartbeat before it
                    # can emit a policy step, so this command cannot bypass
                    # the live startup gate.
                    self._request_arm()
                elif (
                    self.exercise_arm
                    and self.arm_requested
                    and not self.bridge.evidence().get("armed_observed")
                    and self.arm_attempts < ARM_MAX_ATTEMPTS
                    and self.arm_last_attempt_at_s is not None
                    and self.clock() - self.arm_last_attempt_at_s >= ARM_RETRY_INTERVAL_S
                ):
                    # PX4 may report a temporary pre-arm rejection while the
                    # estimator is still settling even after GPS is valid.
                    # Retry a bounded number of times, recording every ACK,
                    # instead of treating the first transient response as a
                    # terminal policy failure.
                    self._request_arm()
                if (
                    self.exercise_flight_duration_s > 0.0
                    and self.bridge.evidence().get("armed_observed") is True
                    and not self.flight_mode_observed
                    and (
                        not self.flight_mode_requested
                        or (
                            self.flight_mode_attempts < FLIGHT_MODE_MAX_ATTEMPTS
                            and self.flight_mode_last_attempt_at_s is not None
                            and self.clock() - self.flight_mode_last_attempt_at_s
                            >= FLIGHT_MODE_RETRY_INTERVAL_S
                        )
                    )
                ):
                    self._request_flight_mode()
                if (
                    self.exercise_target
                    and self.bridge.evidence().get("armed_observed") is True
                    and (
                        self.exercise_flight_duration_s == 0.0
                        or self.flight_mode_observed
                    )
                    and (
                        self.exercise_flight_duration_s > 0.0
                        or not self.target_observation_sent
                    )
                    and not (
                        self.inject_flight_fault == "target_refresh_dropout"
                        and self.flight_fault_activated_at_s is not None
                    )
                ):
                    self._exercise_target_observation()
                bridge_data = self.bridge.evidence()
                target_ready = (
                    not self.exercise_target
                    or (
                        isinstance(self.target_decision, Mapping)
                        and self.target_decision.get("mode") == "track"
                        and self.target_decision.get("accepted") is True
                    )
                )
                flight_data = self._flight_evidence(
                    now_s=self.clock(), include_samples=False
                )
                flight_ready = bool(flight_data.get("gate_passed"))
                fault_data = flight_data.get("fault_injection")
                if (
                    isinstance(fault_data, Mapping)
                    and fault_data.get("requested") is True
                    and fault_data.get("observed") is True
                    and flight_ready is False
                ):
                    self.observe_exit_reason = "injected_fault_observed"
                    break
                if (
                    self._qualified()
                    and int(bridge_data.get("ai_steps", 0)) >= self.minimum_ai_steps
                    and target_ready
                    and flight_ready
                ):
                    if self.exercise_flight_duration_s > 0.0:
                        self.flight_completed_at_s = self.clock()
                    self.observe_exit_reason = "accepted"
                    break
            if (
                self.frames_observed >= self.maximum_frames
                and self.observe_exit_reason != "accepted"
            ):
                self.frame_limit_reached = True
                self.observe_exit_reason = "frame_limit"
                capped_bridge_data = self.bridge.evidence()
                capped_target_ready = bool(
                    not self.exercise_target
                    or (
                        isinstance(self.target_decision, Mapping)
                        and self.target_decision.get("mode") == "track"
                        and self.target_decision.get("accepted") is True
                    )
                )
                capped_acceptance_ready = bool(
                    self._qualified()
                    and int(capped_bridge_data.get("ai_steps", 0))
                    >= self.minimum_ai_steps
                    and capped_target_ready
                    and self._flight_evidence(include_samples=False).get("gate_passed")
                    is True
                )
                if not capped_acceptance_ready:
                    self.error = (
                        f"maximum telemetry frame limit {self.maximum_frames} reached "
                        "before the requested acceptance gates completed"
                    )
            elif self.observe_exit_reason == "running" and self.clock() >= deadline:
                self.observe_exit_reason = "timeout"
        except (DockerAiAcceptanceError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self.error = str(exc)
            self.error_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            self.observe_exit_reason = "error"
        finally:
            # Freeze the observation interval before disarm, neutral-frame
            # emission, socket close, or container teardown.  Cleanup time is
            # not flight-response evidence.
            if self.flight_started_at_s is not None and self.flight_ended_at_s is None:
                self.flight_ended_at_s = (
                    self.flight_completed_at_s
                    if self.flight_completed_at_s is not None
                    else self.clock()
                )
            # A requested arm is always paired with a best-effort disarm before
            # the shared neutral/failsafe frame and socket teardown.  This is
            # intentionally bounded and is never counted as actuator-response
            # acceptance.
            if self.exercise_arm and (
                self.arm_command_sent
                or self.bridge.evidence().get("armed_observed") is True
            ):
                self._request_disarm()
            # If AI emitted a setpoint, stop() sends the canonical neutral frame
            # before closing the control connection.  This is a SIM failsafe,
            # not evidence of real-aircraft control.
            try:
                self.executor.stop()
            except (RuntimeError, TypeError, ValueError) as exc:
                self._reject(f"executor_stop:{type(exc).__name__}", self.clock())
            try:
                self.adapter.close()
            except Exception:
                pass
            for connection in (telemetry, self._control_connection):
                if connection is None:
                    continue
                try:
                    connection.close()
                except Exception:
                    pass
            self._telemetry_connection = None
            self._control_connection = None
        bridge_data = self.evidence()
        qualified = self._qualified()
        flight_data = self._flight_evidence()
        target_ready = bool(
            not self.exercise_target
            or (
                isinstance(self.target_decision, Mapping)
                and self.target_decision.get("mode") == "track"
                and self.target_decision.get("accepted") is True
            )
        )
        acceptance_ready = bool(
            qualified
            and int(bridge_data.get("ai_steps", 0)) >= self.minimum_ai_steps
            and target_ready
            and flight_data.get("gate_passed") is True
        )
        result: dict[str, object] = {
            "status": "passed" if acceptance_ready else "failed",
            "endpoint": self.bind_endpoint,
            "requested_endpoint": self.requested_endpoint,
            "bind_endpoint": self.bind_endpoint,
            "control_endpoint": self.control_endpoint,
            "px4_identity": {
                "system_id": self.px4_system_id,
                "component_ids": (
                    None
                    if self.px4_component_ids is None
                    else sorted(self.px4_component_ids)
                ),
            },
            "minimum_gps_fix_type": self.minimum_gps_fix_type,
            "loopback_publication": self.requested_endpoint.startswith("udpin:127.0.0.1:"),
            "wildcard_bind_fallback": self.wildcard_bind_fallback,
            "heartbeat_observed": self.heartbeat is not None,
            "heartbeat": self.heartbeat,
            "gps_observed": self.gps is not None,
            "gps": self.gps,
            "position": self.position,
            "qualified": qualified,
            "message_count": len(self.messages),
            "frames_observed": self.frames_observed,
            "maximum_frames": self.maximum_frames,
            "poll_iterations": self.poll_iterations,
            "frame_limit_reached": self.frame_limit_reached,
            "observe_exit_reason": self.observe_exit_reason,
            "frames_rejected": len(self.rejections),
            "rejections": list(self.rejections),
            "gcs_heartbeat_sent": self.gcs_heartbeat_sent,
            "duration_s": round(max(0.0, self.clock() - started), 6),
            "truth_free": True,
            "ai": bridge_data,
            "ai_steps": bridge_data.get("ai_steps", 0),
            "ai_accepted": bridge_data.get("ai_accepted", 0),
            "ai_rejected": bridge_data.get("ai_rejected", 0),
            "failsafe": bridge_data.get("failsafe_steps", 0),
            "watchdog_failsafe": bridge_data.get("watchdog_failsafe_steps", 0),
            "control_messages_sent": self.adapter.evidence().get("messages_sent", 0),
            "exercise_arm": self.exercise_arm,
            "exercise_target": self.exercise_target,
            "exercise_flight": self.exercise_flight_duration_s > 0.0,
            "flight_exercise": flight_data,
            "target_exercise": {
                "requested": self.exercise_target,
                "sent": self.target_observation_sent,
                "accepted": bool(
                    isinstance(self.target_decision, Mapping)
                    and self.target_decision.get("mode") == "track"
                    and self.target_decision.get("accepted") is True
                ),
                "observation": (
                    None
                    if self.target_observation is None
                    else dict(self.target_observation)
                ),
                "decision": (
                    None if self.target_decision is None else dict(self.target_decision)
                ),
                "error": self.target_exercise_error,
                "source": (
                    "synthetic_perception_flight_fixture"
                    if self.exercise_flight_duration_s > 0.0
                    else "synthetic_perception_fixture"
                ),
                "truth_free": True,
                "visual_accuracy": "not_claimed",
            },
            "arm_exercise": self._arm_evidence(),
            "real_control_acceptance": "not_claimed",
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.error:
            result["error"] = self.error
        if not qualified and "error" not in result:
            result["error"] = "qualified PX4 heartbeat and GPS fix did not arrive before timeout"
        elif (
            self.exercise_flight_duration_s > 0.0
            and flight_data.get("gate_passed") is not True
            and "error" not in result
        ):
            result["error"] = (
                "sustained MANUAL_CONTROL did not produce the required PX4 "
                "GLOBAL_POSITION_INT displacement before timeout"
            )
        result["messages_tail"] = self.messages[-100:]
        self.observe_result = result
        return result

    def evidence(self) -> dict[str, object]:
        evidence = self.bridge.evidence()
        evidence["exercise_arm"] = self.exercise_arm
        evidence["exercise_target"] = self.exercise_target
        evidence["exercise_flight"] = self.exercise_flight_duration_s > 0.0
        evidence["flight_exercise"] = self._flight_evidence(include_samples=False)
        evidence["target_exercise"] = {
            "requested": self.exercise_target,
            "sent": self.target_observation_sent,
            "accepted": bool(
                isinstance(self.target_decision, Mapping)
                and self.target_decision.get("mode") == "track"
                and self.target_decision.get("accepted") is True
            ),
            "observation": (
                None if self.target_observation is None else dict(self.target_observation)
            ),
            "decision": None if self.target_decision is None else dict(self.target_decision),
            "error": self.target_exercise_error,
            "source": (
                "synthetic_perception_flight_fixture"
                if self.exercise_flight_duration_s > 0.0
                else "synthetic_perception_fixture"
            ),
            "truth_free": True,
            "visual_accuracy": "not_claimed",
        }
        evidence["arm_exercise"] = self._arm_evidence()
        return evidence


class _FixtureMav:
    def __init__(self) -> None:
        self.manual_control_messages: list[tuple[int, ...]] = []
        self.heartbeats = 0

    def manual_control_send(self, *args: int) -> None:
        self.manual_control_messages.append(tuple(int(value) for value in args))

    def heartbeat_send(self, *_args: object) -> None:
        self.heartbeats += 1


class _FixtureConnection:
    def __init__(self) -> None:
        self.mav = _FixtureMav()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fixture_message_sequence() -> tuple[dict[str, object], ...]:
    return (
        {
            "type": "HEARTBEAT",
            "source_system_id": PX4_SYSTEM_ID,
            "source_component_id": 1,
            "type_value": 2,
            "autopilot": 3,
            "base_mode": 128,
            "system_status": 4,
        },
        {
            "type": "GPS_RAW_INT",
            "source_system_id": PX4_SYSTEM_ID,
            "source_component_id": 1,
            "fix_type": 3,
            "satellites_visible": 14,
            "lat": 13_000_000,
            "lon": 1_038_005_000,
            "alt": 5000,
            "eph": 120,
            "epv": 180,
        },
        {
            "type": "GLOBAL_POSITION_INT",
            "source_system_id": PX4_SYSTEM_ID,
            "source_component_id": 1,
            "lat": 13_000_000,
            "lon": 1_038_005_000,
            "relative_alt": 5000,
            "hdg": 9000,
        },
    )


def _run_dry_fixture(
    *,
    px4_component_ids: Iterable[int] | None = DEFAULT_PX4_COMPONENT_IDS,
    minimum_gps_fix_type: int = 3,
) -> dict[str, object]:
    """Exercise AI, QGC takeover, release, and failsafe without any sockets."""

    telemetry_connection = _FixtureConnection()
    control_connection = _FixtureConnection()
    probe = DockerAiTelemetryProbe(
        "udpin:127.0.0.1:24541",
        control_endpoint="udpout:127.0.0.1:24540",
        connection_factory=lambda *_args, **_kwargs: telemetry_connection,
        control_connection_factory=lambda *_args, **_kwargs: control_connection,
        mavutil_module=SimpleNamespace(
            mavlink=SimpleNamespace(MAV_TYPE_GCS=6, MAV_AUTOPILOT_INVALID=8)
        ),
        # This deterministic fixture intentionally exercises the complete AI,
        # manual takeover, release, and failsafe path without a live arm event.
        wait_for_armed_heartbeat=False,
        px4_component_ids=px4_component_ids,
        minimum_gps_fix_type=minimum_gps_fix_type,
    )
    # Feed the same bridge callback that a live router would call.
    probe.bridge.enable_ai()
    stamps = (1.0, 1.1, 1.2)
    for message, stamp in zip(_fixture_message_sequence(), stamps, strict=True):
        probe._record(message, stamp)
    before_manual = int(probe.bridge.evidence().get("ai_steps", 0))
    router = SimulatorInteractiveRouter(
        probe.adapter,
        config=SimulatorInteractiveRouterConfig(
            qgc_input_port=24550,
            qgc_output_port=24551,
            px4_input_port=24552,
            px4_output_endpoint="udpout:127.0.0.1:24540",
        ),
    )
    router.process_qgc_datagram(
        build_manual_control_frame(pitch=0.05, roll=-0.10, yaw=0.02, throttle=0.60),
        source=("127.0.0.1", 50123),
        timestamp_s=1.3,
    )
    suppressed = probe.bridge.ingest(
        {
            "type": "GLOBAL_POSITION_INT",
            "source_system_id": PX4_SYSTEM_ID,
            "lat": 13_000_000,
            "lon": 103_800_500,
            "relative_alt": 5000,
            "hdg": 9000,
        },
        1.3,
    )
    probe.bridge.release_manual_control()
    resumed = probe.bridge.ingest(
        {
            "type": "VFR_HUD",
            "source_system_id": PX4_SYSTEM_ID,
            "groundspeed": 8.0,
            "heading": 90,
        },
        1.4,
    )
    failsafe = probe.bridge.ingest(
        {
            "type": "HEARTBEAT",
            "source_system_id": PX4_SYSTEM_ID,
            "base_mode": 0,
        },
        1.5,
    )
    bridge_data = probe.bridge.evidence()
    fixture_gate = bool(
        before_manual >= 1
        and suppressed is not None
        and getattr(suppressed, "mode", None).value == "suppressed"
        and resumed is not None
        and getattr(resumed, "accepted", False)
        and failsafe is not None
        and getattr(failsafe, "mode", None).value == "failsafe"
        and bridge_data.get("truth_free") is True
        and int(bridge_data.get("frames_rejected", 0)) == 0
    )
    try:
        probe.executor.stop()
    except (RuntimeError, TypeError, ValueError):
        pass
    probe.adapter.close()
    return {
        "gate_passed": fixture_gate,
        "fixture": True,
        "bridge": bridge_data,
        "router": router.evidence(),
        "control": probe.adapter.evidence(),
        "mavlink_messages": len(control_connection.mav.manual_control_messages),
        "last_mavlink_message": (
            control_connection.mav.manual_control_messages[-1]
            if control_connection.mav.manual_control_messages
            else None
        ),
        "suppressed_mode": None if suppressed is None else suppressed.mode.value,
        "resumed_mode": None if resumed is None else resumed.mode.value,
        "failsafe_mode": None if failsafe is None else failsafe.mode.value,
        "claims": {
            "reasoned_policy_used": True,
            "shared_manual_ai_authority": True,
            "truth_free_input": True,
            "qgc_is_sole_operator_ui": True,
            "sim_control_frame_emission": "fixture_only",
            "px4_actuator_response": "not_claimed",
            "visual_acceptance": "not_claimed",
            "pixhawk_hardware_acceptance": "not_claimed",
        },
    }


def _empty_ai_evidence(
    *,
    px4_system_id: int = PX4_SYSTEM_ID,
    px4_component_ids: Iterable[int] | None = DEFAULT_PX4_COMPONENT_IDS,
    minimum_gps_fix_type: int = 3,
    exercise_flight_duration_s: float = 0.0,
    minimum_flight_displacement_m: float = DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M,
    inject_flight_fault: str = "none",
) -> dict[str, object]:
    normalized_component_ids = _normalize_component_ids(px4_component_ids)
    normalized_flight_duration = _validate_exercise_flight_duration(
        exercise_flight_duration_s
    )
    normalized_minimum_displacement = _validate_minimum_flight_displacement(
        minimum_flight_displacement_m
    )
    normalized_flight_fault = _validate_flight_fault(inject_flight_fault)
    return {
        "schema_version": 1,
        "event": "simulator_live_ai_telemetry_bridge",
        "frames_observed": 0,
        "frames_accepted": 0,
        "frames_rejected": 0,
        "rejections": [],
        "px4_identity": {
            "system_id": int(px4_system_id),
            "component_ids": (
                None
                if normalized_component_ids is None
                else sorted(normalized_component_ids)
            ),
        },
        "minimum_gps_fix_type": _validate_minimum_gps_fix_type(minimum_gps_fix_type),
        "snapshots": 0,
        "ai_steps": 0,
        "ai_accepted": 0,
        "ai_rejected": 0,
        "failsafe_steps": 0,
        "watchdog_failsafe_steps": 0,
        "exercise_arm": False,
        "exercise_target": False,
        "exercise_flight": normalized_flight_duration > 0.0,
        "flight_exercise": {
            "requested": normalized_flight_duration > 0.0,
            "gate_passed": normalized_flight_duration == 0.0,
            "status": (
                "not_requested"
                if normalized_flight_duration == 0.0
                else "position_response_not_observed"
            ),
            "requested_duration_s": normalized_flight_duration,
            "elapsed_duration_s": 0.0,
            "completed": False,
            "minimum_horizontal_displacement_m": normalized_minimum_displacement,
            "maximum_horizontal_displacement_m": 0.0,
            "final_horizontal_displacement_m": 0.0,
            "final_relative_altitude_delta_m": 0.0,
            "target_updates_accepted": 0,
            "target_updates_rejected": 0,
            "target_refresh_interval_s": FLIGHT_TARGET_REFRESH_S,
            "target_refresh_coverage_s": 0.0,
            "maximum_target_refresh_gap_s": None,
            "target_refresh_gate_passed": normalized_flight_duration == 0.0,
            "flight_mode": {
                "requested": False,
                "attempts": 0,
                "last_attempt_at_s": None,
                "command": "SET_MODE",
                "base_mode": MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                "custom_mode": PX4_POSCTL_CUSTOM_MODE,
                "px4_main_mode": "POSCTL",
                "command_sent": False,
                "command_error": None,
                "observed": False,
                "observed_at_s": None,
                "current_heartbeat_base_mode": None,
                "current_heartbeat_custom_mode": None,
                "current_heartbeat_armed": False,
                "current_heartbeat_custom_mode_enabled": False,
                "loss_events": 0,
                "clean_throughout": True,
                "transport": "shared loopback MAVLink control socket",
            },
            "flight_mode_gate_passed": normalized_flight_duration == 0.0,
            "heartbeat_timeout_s": (
                DOCKER_FLIGHT_HEARTBEAT_TIMEOUT_S
                if normalized_flight_duration > 0.0
                else 2.0
            ),
            "heartbeat_sample_count": 0,
            "maximum_heartbeat_gap_s": None,
            "heartbeat_continuity_gate_passed": normalized_flight_duration == 0.0,
            "heartbeat_samples": [],
            "position_sample_count": 0,
            "initial_position": None,
            "final_position": None,
            "position_samples": [],
            "position_source": "MAVLink GLOBAL_POSITION_INT",
            "control_path": (
                "SimulatorTelemetryBridge.ingest_perception -> "
                "SimulatorAiExecutor -> SimulatorMavlinkControlAdapter -> "
                "MAVLink MANUAL_CONTROL -> PX4 SITL/Gazebo"
            ),
            "synthetic_perception_input": True,
            "gazebo_truth_consumed": False,
            "gazebo_pose_injection_used": False,
            "px4_actuator_response": "not_claimed",
            "visual_accuracy": "not_claimed",
            "hardware_acceptance": "not_claimed",
            "hil_acceptance": "not_claimed",
            "real_flight_acceptance": "not_claimed",
            "fault_injection": {
                "requested": normalized_flight_fault != "none",
                "name": normalized_flight_fault,
                "trigger_after_s": min(1.0, normalized_flight_duration / 2.0),
                "activated": False,
                "activated_at_s": None,
                "observed": False,
                "fail_closed": False,
                "suppressed_heartbeats": 0,
                "mode_command_sent": False,
                "mode_command_error": None,
                "scope": "disposable_loopback_px4_sitl_only",
            },
        },
        "target_exercise": {
            "requested": False,
            "sent": False,
            "accepted": False,
            "observation": None,
            "decision": None,
            "error": None,
            "source": "synthetic_perception_fixture",
            "truth_free": True,
            "visual_accuracy": "not_claimed",
        },
        "arm_exercise": {
            "enabled": False,
            "autopilot_system_id": int(px4_system_id),
            "autopilot_component_ids": (
                None
                if normalized_component_ids is None
                else sorted(normalized_component_ids)
            ),
            "command_id": MAV_CMD_COMPONENT_ARM_DISARM,
            "arm_requested": False,
            "arm_attempts": 0,
            "arm_last_attempt_at_s": None,
            "arm_command_sent": False,
            "arm_command_error": None,
            "disarm_requested": False,
            "disarm_command_sent": False,
            "disarm_command_error": None,
            "commands": [],
            "acks": [],
            "command_acks_observed": 0,
            "command_acks_accepted": 0,
            "command_acks_rejected": 0,
            "preflight": {
                "requested": False,
                "applied": False,
                "parameters": [],
                "error": None,
            },
        },
        "truth_free": True,
        "real_control_acceptance": "not_claimed",
    }


def _augment_payload(
    payload: dict[str, object],
    *,
    ai_evidence: dict[str, object],
    fixture: dict[str, object] | None,
    execution_requested: bool,
) -> dict[str, object]:
    ai_steps = int(ai_evidence.get("ai_steps", 0))
    ai_accepted = int(ai_evidence.get("ai_accepted", 0))
    ai_rejected = int(ai_evidence.get("ai_rejected", 0))
    failsafe = int(ai_evidence.get("failsafe_steps", 0))
    frames = {
        "observed": int(ai_evidence.get("frames_observed", 0)),
        "accepted": int(ai_evidence.get("frames_accepted", 0)),
        "rejected": int(ai_evidence.get("frames_rejected", 0)),
    }
    payload["ai"] = ai_evidence
    payload["ai_steps"] = ai_steps
    payload["frames"] = frames
    payload["rejections"] = ai_evidence.get("rejections", [])
    payload["failsafe"] = failsafe
    payload["ai_rejected"] = ai_rejected
    payload["execution_requested"] = execution_requested
    payload["fixture"] = fixture
    # Keep the two gates separate.  The base Docker gate is the runtime and
    # qualified-telemetry gate; the AI gate only becomes pass/fail after the
    # shared policy path has actually been exercised.  A live run that is
    # safely disarmed is therefore *not exercised*, not an algorithm failure.
    runtime_gate_passed = bool(
        payload.get("runtime_gate_passed")
        if execution_requested
        else payload.get("gate_passed")
    )
    truth_free = ai_evidence.get("truth_free") is True
    watchdog_failsafe = int(ai_evidence.get("watchdog_failsafe_steps", 0))
    # ``ai_steps`` includes safety decisions (notably the expected disarmed
    # failsafe).  Count a policy exercise only when the shared executor
    # accepted at least one non-safety command; this keeps runtime safety
    # evidence from being reported as an AI-control pass.
    # Executor watchdog failsafes are already included in ``failsafe_steps``;
    # subtracting them twice would under-report non-safety policy decisions.
    ai_policy_steps = max(0, ai_steps - failsafe)
    ai_gate = bool(ai_policy_steps > 0 and ai_accepted > 0 and truth_free)
    if fixture is not None:
        ai_gate = bool(fixture.get("gate_passed") and ai_gate)

    target_requested = ai_evidence.get("exercise_target") is True
    target_exercise = ai_evidence.get("target_exercise")
    target_exercise = target_exercise if isinstance(target_exercise, Mapping) else {}
    target_decision = target_exercise.get("decision")
    target_policy_gate = bool(
        not target_requested
        or (
            target_exercise.get("accepted") is True
            and isinstance(target_decision, Mapping)
            and target_decision.get("mode") == "track"
            and target_decision.get("accepted") is True
        )
    )
    if target_requested:
        ai_gate = bool(ai_gate and target_policy_gate)

    flight_requested = ai_evidence.get("exercise_flight") is True
    flight_exercise = ai_evidence.get("flight_exercise")
    flight_exercise = flight_exercise if isinstance(flight_exercise, Mapping) else {}
    flight_clean_run = bool(
        not flight_requested
        or (
            ai_rejected == 0
            and failsafe == 0
            and watchdog_failsafe == 0
            and int(flight_exercise.get("target_updates_accepted", 0)) > 0
            and int(flight_exercise.get("target_updates_rejected", 0)) == 0
            and flight_exercise.get("target_refresh_gate_passed") is True
            and flight_exercise.get("flight_mode_gate_passed") is True
            and flight_exercise.get("heartbeat_continuity_gate_passed") is True
            and flight_exercise.get("completed") is True
        )
    )
    flight_response_gate = bool(
        not flight_requested
        or (
            flight_exercise.get("gate_passed") is True
            and flight_clean_run
            and flight_exercise.get("gazebo_pose_injection_used") is False
            and flight_exercise.get("gazebo_truth_consumed") is False
            and flight_exercise.get("position_source") == "MAVLink GLOBAL_POSITION_INT"
        )
    )

    armed_observed = ai_evidence.get("armed_observed") is True
    startup_gate = ai_evidence.get("startup_gate")
    if ai_gate:
        ai_policy_gate_status = "passed"
    elif target_requested and not target_policy_gate:
        ai_policy_gate_status = "target_observation_not_exercised"
    elif (
        execution_requested
        and runtime_gate_passed
        and not armed_observed
        and ai_accepted == 0
        and (
            startup_gate == "waiting_for_armed_heartbeat"
            or ai_steps > 0
        )
    ):
        ai_policy_gate_status = "not_exercised_waiting_for_armed_heartbeat"
    elif ai_steps > 0:
        ai_policy_gate_status = "failed"
    else:
        ai_policy_gate_status = "not_exercised"

    payload["runtime_gate_passed"] = runtime_gate_passed
    payload["ai_policy_steps"] = ai_policy_steps
    payload["ai_policy_gate_passed"] = ai_gate
    payload["ai_policy_gate_status"] = ai_policy_gate_status
    payload["target_policy_gate_passed"] = target_policy_gate
    payload["target_policy_gate_status"] = (
        "passed" if target_policy_gate else "not_exercised"
    )
    payload["target_exercise"] = dict(target_exercise)
    payload["flight_response_gate_passed"] = flight_response_gate
    payload["flight_response_gate_status"] = (
        "not_requested"
        if not flight_requested
        else "passed"
        if flight_response_gate
        else "safety_fault_observed"
        if not flight_clean_run
        else str(flight_exercise.get("status") or "position_response_not_observed")
    )
    payload["flight_exercise"] = dict(flight_exercise)
    payload["gate_passed"] = bool(
        runtime_gate_passed and ai_gate and flight_response_gate
    )
    claims = payload.get("claims")
    claims = dict(claims) if isinstance(claims, Mapping) else {}
    claims.update(
        {
            "sim_ai_policy": ai_policy_gate_status,
            "sim_ai_target_policy": (
                "passed" if target_policy_gate else "not_exercised"
            ),
            "px4_sim_vehicle_motion": (
                "not_exercised"
                if not flight_requested
                else "passed" if flight_response_gate else "not_observed"
            ),
            "sim_ai_control_output": (
                "emitted_by_shared_loopback_adapter" if ai_accepted > 0 else "none"
            ),
            "px4_actuator_response": "not_claimed",
            "real_control_acceptance": "not_claimed",
            "visual": "not_claimed",
            "hardware": "not_claimed",
        }
    )
    payload["claims"] = claims
    safety = payload.get("safety")
    safety = dict(safety) if isinstance(safety, Mapping) else {}
    safety.update(
        {
            "truth_free": True,
            "real_pixhawk_contacted": False,
            "hardware_control_enabled": False,
            "sim_ai_policy_steps": ai_steps,
            "sim_ai_control_commands_sent": ai_accepted,
            "sim_ai_failsafe_steps": failsafe,
            "flight_control_commands_sent": ai_accepted,
            "flight_control_output_enabled": bool(ai_accepted > 0),
            "gcs_heartbeat_only": bool(ai_accepted == 0),
            "arm_exercise": ai_evidence.get("arm_exercise", {
                "enabled": False,
                "arm_requested": False,
                "arm_attempts": 0,
                "arm_last_attempt_at_s": None,
                "arm_command_sent": False,
                "arm_command_error": None,
                "disarm_requested": False,
                "disarm_command_sent": False,
                "disarm_command_error": None,
                "commands": [],
                "acks": [],
                "preflight": {
                    "requested": False,
                    "applied": False,
                    "parameters": [],
                    "error": None,
                },
            }),
            "flight_exercise": dict(flight_exercise),
        }
    )
    payload["safety"] = safety
    payload["release_status"] = RELEASE_STATUS
    return payload


def run_acceptance(
    manifest_path: str | Path = DEFAULT_SCENARIO,
    evidence_path: str | Path = DEFAULT_EVIDENCE,
    *,
    execute: bool = False,
    timeout_s: float = 90.0,
    docker: str | Path | None = None,
    container_name: str = DEFAULT_AI_CONTAINER_NAME,
    control_port: int = DEFAULT_AI_CONTROL_PORT,
    telemetry_port: int = DEFAULT_AI_TELEMETRY_PORT,
    image: str = PINNED_IMAGE,
    px4_component_ids: Iterable[int] | None = DEFAULT_PX4_COMPONENT_IDS,
    minimum_gps_fix_type: int = 3,
    exercise_arm: bool = False,
    exercise_target: bool = False,
    exercise_flight_duration_s: float = 0.0,
    minimum_flight_displacement_m: float = DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M,
    inject_flight_fault: str = "none",
    runner: Callable[..., object] | None = None,
    telemetry_probe_factory: Callable[[str], DockerAiTelemetryProbe] | None = None,
) -> dict[str, object]:
    """Run a no-I/O fixture or the pinned Docker telemetry/AI gate."""

    if not isinstance(exercise_arm, bool):
        raise TypeError("exercise_arm must be bool")
    if exercise_arm and not execute:
        raise DockerAiAcceptanceError("exercise_arm requires --execute")
    if not isinstance(exercise_target, bool):
        raise TypeError("exercise_target must be bool")
    if exercise_target and not exercise_arm:
        raise DockerAiAcceptanceError("exercise_target requires --exercise-arm")
    if exercise_target and not execute:
        raise DockerAiAcceptanceError("exercise_target requires --execute")
    normalized_flight_duration = _validate_exercise_flight_duration(
        exercise_flight_duration_s
    )
    normalized_minimum_displacement = _validate_minimum_flight_displacement(
        minimum_flight_displacement_m
    )
    normalized_flight_fault = _validate_flight_fault(inject_flight_fault)
    if normalized_flight_duration > 0.0 and not exercise_target:
        raise DockerAiAcceptanceError(
            "exercise_flight_duration_s requires --exercise-target"
        )
    if normalized_flight_fault != "none" and normalized_flight_duration <= 0.0:
        raise DockerAiAcceptanceError(
            "inject_flight_fault requires --exercise-flight-seconds"
        )
    try:
        normalized_component_ids = _normalize_component_ids(px4_component_ids)
    except DockerAiAcceptanceError:
        raise
    normalized_minimum_fix = _validate_minimum_gps_fix_type(minimum_gps_fix_type)

    # Validate the two endpoint strings before the base helper is entered.  A
    # dry-run therefore exercises the same literal-loopback contract as an
    # execute run and cannot be used to hide an unsafe transport selection.
    _validate_control_endpoint(f"udpout:127.0.0.1:{control_port}")
    _validate_telemetry_endpoint(f"udpin:127.0.0.1:{telemetry_port}")

    fixture: dict[str, object] | None = None
    probes: list[DockerAiTelemetryProbe] = []
    arm_preflight_result: dict[str, object] | None = None
    if execute:
        def factory(endpoint: str) -> DockerAiTelemetryProbe:
            nonlocal arm_preflight_result
            _validate_telemetry_endpoint(endpoint)
            if exercise_arm:
                docker_executable = _resolve_docker_executable(docker)
                try:
                    arm_preflight_result = _prepare_disposable_sitl_for_arm(
                        docker_executable,
                        container_name,
                        runner=runner,
                        enable_joystick_control=normalized_flight_duration > 0.0,
                    )
                except (
                    DockerAiAcceptanceError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    arm_preflight_result = {
                        "requested": True,
                        "applied": False,
                        "parameters": [],
                        "error": str(exc),
                    }
                    raise
            probe = (
                telemetry_probe_factory(endpoint)
                if telemetry_probe_factory is not None
                else DockerAiTelemetryProbe(
                    endpoint,
                    control_endpoint=f"udpout:127.0.0.1:{control_port}",
                    px4_component_ids=normalized_component_ids,
                    minimum_gps_fix_type=normalized_minimum_fix,
                    exercise_arm=exercise_arm,
                    exercise_target=exercise_target,
                    exercise_flight_duration_s=normalized_flight_duration,
                    minimum_flight_displacement_m=normalized_minimum_displacement,
                    inject_flight_fault=normalized_flight_fault,
                )
            )
            # Keep custom test/production factories inside the same boundary:
            # if they expose endpoint attributes, validate those exact values
            # rather than trusting the factory's type annotation.  The
            # built-in probe always has both attributes.
            probe_endpoint = getattr(probe, "requested_endpoint", None)
            if probe_endpoint is None:
                probe_endpoint = getattr(probe, "endpoint", endpoint)
            _validate_telemetry_endpoint(probe_endpoint)
            control_endpoint = getattr(
                probe, "control_endpoint", f"udpout:127.0.0.1:{control_port}"
            )
            _validate_control_endpoint(control_endpoint)
            probe_component_ids = getattr(probe, "px4_component_ids", None)
            if probe_component_ids is not None:
                probe_component_ids = _normalize_component_ids(probe_component_ids)
            if probe_component_ids != normalized_component_ids:
                raise DockerAiAcceptanceError(
                    "telemetry probe PX4 component identity does not match acceptance config"
                )
            probe_minimum_fix = getattr(probe, "minimum_gps_fix_type", None)
            if probe_minimum_fix is not None and int(probe_minimum_fix) != normalized_minimum_fix:
                raise DockerAiAcceptanceError(
                    "telemetry probe GPS fix threshold does not match acceptance config"
                )
            if exercise_arm and getattr(probe, "exercise_arm", False) is not True:
                raise DockerAiAcceptanceError(
                    "exercise_arm requested but telemetry probe did not enable it"
                )
            if exercise_target and getattr(probe, "exercise_target", False) is not True:
                raise DockerAiAcceptanceError(
                    "exercise_target requested but telemetry probe did not enable it"
                )
            probe_flight_duration = getattr(
                probe, "exercise_flight_duration_s", 0.0
            )
            if float(probe_flight_duration) != normalized_flight_duration:
                raise DockerAiAcceptanceError(
                    "telemetry probe flight duration does not match acceptance config"
                )
            probe_minimum_displacement = getattr(
                probe,
                "minimum_flight_displacement_m",
                DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M,
            )
            if (
                normalized_flight_duration > 0.0
                and float(probe_minimum_displacement)
                != normalized_minimum_displacement
            ):
                raise DockerAiAcceptanceError(
                    "telemetry probe flight displacement threshold does not match "
                    "acceptance config"
                )
            probe_flight_fault = getattr(probe, "inject_flight_fault", "none")
            if str(probe_flight_fault) != normalized_flight_fault:
                raise DockerAiAcceptanceError(
                    "telemetry probe flight fault does not match acceptance config"
                )
            probes.append(probe)
            if exercise_arm and arm_preflight_result is not None:
                probe.arm_preflight = dict(arm_preflight_result)
            return probe

        payload = run_docker_acceptance(
            manifest_path,
            evidence_path,
            execute=True,
            timeout_s=timeout_s,
            docker=docker,
            container_name=container_name,
            control_port=control_port,
            telemetry_port=telemetry_port,
            image=image,
            runner=runner,
            telemetry_probe_factory=factory,
        )
        if probes:
            ai_evidence = probes[-1].evidence()
            # Preserve the richer probe telemetry while retaining the base
            # container/asset evidence and cleanup record.
            if probes[-1].observe_result is not None:
                base_telemetry = payload.get("telemetry")
                base_telemetry = base_telemetry if isinstance(base_telemetry, Mapping) else {}
                payload["telemetry"] = {
                    **dict(base_telemetry),
                    **probes[-1].observe_result,
                }
        else:
            ai_evidence = _empty_ai_evidence(
                px4_system_id=PX4_SYSTEM_ID,
                px4_component_ids=normalized_component_ids,
                minimum_gps_fix_type=normalized_minimum_fix,
                exercise_flight_duration_s=normalized_flight_duration,
                minimum_flight_displacement_m=normalized_minimum_displacement,
                inject_flight_fault=normalized_flight_fault,
            )
        runtime = payload.get("container_runtime")
        if isinstance(runtime, Mapping) and arm_preflight_result is not None:
            runtime = dict(runtime)
            runtime["arm_preflight"] = arm_preflight_result
            payload["container_runtime"] = runtime
    else:
        payload = run_docker_acceptance(
            manifest_path,
            evidence_path,
            execute=False,
            timeout_s=timeout_s,
            docker=docker,
            container_name=container_name,
            control_port=control_port,
            telemetry_port=telemetry_port,
            image=image,
            runner=runner,
        )
        fixture = _run_dry_fixture(
            px4_component_ids=normalized_component_ids,
            minimum_gps_fix_type=normalized_minimum_fix,
        )
        ai_evidence = (
            fixture["bridge"]
            if isinstance(fixture.get("bridge"), dict)
            else _empty_ai_evidence(
                px4_system_id=PX4_SYSTEM_ID,
                px4_component_ids=normalized_component_ids,
                minimum_gps_fix_type=normalized_minimum_fix,
            )
        )
    result = _augment_payload(
        payload,
        ai_evidence=ai_evidence,
        fixture=fixture,
        execution_requested=execute,
    )
    # Rewrite the evidence after augmentation; the base helper writes before
    # this bridge-specific layer is known.
    evidence = Path(evidence_path).expanduser().resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    temporary = evidence.with_name(f".{evidence.name}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(evidence)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", "--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--evidence", "--out", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--container-name", default=DEFAULT_AI_CONTAINER_NAME)
    parser.add_argument("--control-port", type=int, default=DEFAULT_AI_CONTROL_PORT)
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_AI_TELEMETRY_PORT)
    parser.add_argument(
        "--minimum-gps-fix-type",
        type=int,
        default=3,
        help="minimum MAVLink GPS fix type required by the live qualification gate",
    )
    parser.add_argument(
        "--px4-component-id",
        dest="px4_component_ids",
        type=int,
        action="append",
        default=None,
        help=(
            "expected PX4 MAVLink source component (repeatable; defaults to "
            "autopilot component 1)"
        ),
    )
    parser.add_argument("--image", default=PINNED_IMAGE)
    parser.add_argument("--docker", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start Docker and run the live AI bridge",
    )
    parser.add_argument(
        "--exercise-arm",
        action="store_true",
        help=(
            "explicit disposable SITL arm/disarm exercise; sends one normal "
            "MAV_CMD_COMPONENT_ARM_DISARM and waits for an armed heartbeat"
        ),
    )
    parser.add_argument(
        "--exercise-target",
        action="store_true",
        help=(
            "explicit disposable SITL perception fixture; after an armed "
            "heartbeat inject one truth-free target and require a TRACK decision "
            "(requires --execute --exercise-arm)"
        ),
    )
    parser.add_argument(
        "--exercise-flight-seconds",
        dest="exercise_flight_duration_s",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "sustain the synthetic target through the shared MANUAL_CONTROL "
            "path for 1..30 seconds and require actual PX4 GLOBAL_POSITION_INT "
            "movement (requires --execute --exercise-arm --exercise-target)"
        ),
    )
    parser.add_argument(
        "--minimum-flight-displacement-m",
        type=float,
        default=DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M,
        metavar="METRES",
        help=(
            "minimum WGS-84 horizontal displacement required by the optional "
            "flight-response gate (default: 0.5 m)"
        ),
    )
    parser.add_argument(
        "--inject-flight-fault",
        choices=sorted(FLIGHT_FAULTS - {"none"}),
        default="none",
        help=(
            "explicit disposable SITL-only negative-path fault; the acceptance "
            "gate must reject the run after observing the selected fault"
        ),
    )
    parser.add_argument(
        "--timeout",
        "--timeout-seconds",
        dest="timeout_s",
        type=float,
        default=90.0,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = run_acceptance(
            args.scenario_manifest,
            args.evidence,
            execute=args.execute,
            timeout_s=args.timeout_s,
            docker=args.docker,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            image=args.image,
            px4_component_ids=(
                DEFAULT_PX4_COMPONENT_IDS
                if args.px4_component_ids is None
                else args.px4_component_ids
            ),
            minimum_gps_fix_type=args.minimum_gps_fix_type,
            exercise_arm=args.exercise_arm,
            exercise_target=args.exercise_target,
            exercise_flight_duration_s=args.exercise_flight_duration_s,
            minimum_flight_displacement_m=args.minimum_flight_displacement_m,
            inject_flight_fault=args.inject_flight_fault,
        )
    except (
        DockerAiAcceptanceError,
        DockerSimulatorAcceptanceError,
        DockerSimulatorPlanError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"simulator docker-ai-acceptance: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("gate_passed") else 1


__all__ = [
    "DockerAiAcceptanceError",
    "DockerAiTelemetryProbe",
    "DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M",
    "DEFAULT_PX4_COMPONENT_IDS",
    "FLIGHT_TARGET_REFRESH_S",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MSG_COMMAND_ACK",
    "SimulatorDockerAiAcceptanceError",
    "main",
    "run_acceptance",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

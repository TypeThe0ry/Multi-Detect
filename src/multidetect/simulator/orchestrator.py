"""Lifecycle manager for an isolated PX4/Gazebo/QGC simulator job."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..autonomy.serialization import canonical_sha256
from ..runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile
from ..simulation import ScenarioRuntimeInput, load_runtime_scenario
from .ai_executor import (
    SimulatorAiExecutor,
    SimulatorAiExecutorConfig,
    SimulatorTelemetrySnapshot,
)
from .control import ControlAuthorityState, SimulatorControlAuthority
from .control_gateway import SimulatorControlGateway, SimulatorControlGatewayConfig
from .interactive_router import (
    DEFAULT_PX4_INPUT_PORT,
    DEFAULT_QGC_INPUT_PORT,
    DEFAULT_QGC_OUTPUT_PORT,
    QgcControlMode,
    SimulatorInteractiveRouter,
    SimulatorInteractiveRouterConfig,
)
from .isolation import (
    discover_simulator_tools,
    ensure_port_available,
    validate_simulation_endpoint,
)
from .live_ai_bridge import SimulatorTelemetryBridge
from .mavlink_control import SimulatorMavlinkControlAdapter, SimulatorMavlinkControlConfig
from .px4_launch import Px4LaunchError, Px4LaunchPlan, build_px4_launch_plan
from .readiness import (
    SimulatorHeartbeatObservation,
    SimulatorReadinessError,
    SimulatorReadinessProbe,
    SimulatorReadinessResult,
)
from .stack import (
    DEFAULT_METADATA_PORT,
    DEFAULT_RTSP_PORT,
    SimulatorStackError,
    SimulatorStackPlan,
    SimulatorStackPorts,
    build_stack_plan,
)


class SimulatorOrchestratorError(RuntimeError):
    """Raised when a simulator job cannot be started or validated."""


class _RecordingMavlink:
    """Minimal in-process MAVLink sink used by deterministic dry-runs.

    A dry-run must validate policy and evidence without opening a UDP socket or
    importing a real ``pymavlink`` connection.  Keeping the sink structurally
    identical to the ``mav`` object used by the adapter lets the exact control
    path execute while making accidental external I/O impossible.
    """

    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(
        self, target: int, x: int, y: int, z: int, r: int, buttons: int
    ) -> None:
        self.messages.append((target, x, y, z, r, buttons))


class _RecordingMavlinkConnection:
    def __init__(self) -> None:
        self.mav = _RecordingMavlink()
        self.closed = False

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True, slots=True)
class SimulatorProcessSpec:
    name: str
    command: tuple[str, ...]
    required: bool = True
    # Optional process environment supplied by the repository-owned stack
    # planner.  These fields are appended to preserve the original three
    # positional constructor arguments used by older fake factories/tests.
    env: Mapping[str, str] | None = None
    cwd: str | Path | None = None


@dataclass(slots=True)
class _ManagedProcess:
    spec: SimulatorProcessSpec
    process: subprocess.Popen[Any]
    log_path: Path


@dataclass(frozen=True, slots=True)
class SimulatorRunResult:
    status: str
    scenario_id: str
    scenario_manifest_hash: str
    seed: int
    job_dir: Path
    runtime_descriptor: RuntimeDescriptor
    tools: dict[str, Any]
    authority_state: str
    control: dict[str, object]
    control_gateway: dict[str, object]
    processes: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    # The fields below were appended after the original result contract so
    # callers constructing ``SimulatorRunResult`` positionally remain
    # compatible with pre-interactive releases.
    interactive_router: dict[str, object] = field(default_factory=dict)
    ai: dict[str, object] = field(default_factory=dict)
    # Appended for source compatibility with callers constructing the result
    # positionally before stack planning was introduced.
    stack_plan: dict[str, object] = field(default_factory=dict)
    stack_plan_enabled: bool = False
    # Job-local PX4 startup wrapper/provenance.  Appended for positional
    # compatibility with pre-launch-plan callers.
    px4_launch: dict[str, object] = field(default_factory=dict)
    # Runtime process/heartbeat readiness evidence.  Appended for positional
    # compatibility with callers that construct results directly.
    readiness: dict[str, object] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scenario_id": self.scenario_id,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "seed": self.seed,
            "job_dir": str(self.job_dir),
            "runtime_descriptor": self.runtime_descriptor.to_mapping(),
            "tools": self.tools,
            "authority_state": self.authority_state,
            "control": self.control,
            "control_gateway": self.control_gateway,
            "qgc_control_mode": self.interactive_router.get("qgc_control_mode", "read_only"),
            "processes": list(self.processes),
            "error": self.error,
            "interactive_router": self.interactive_router,
            "ai": self.ai,
            "stack_plan": self.stack_plan,
            "stack_plan_enabled": self.stack_plan_enabled,
            "stack_plan_sha256": self.stack_plan.get("sha256"),
            "px4_launch": self.px4_launch,
            "px4_launch_sha256": self.px4_launch.get("sha256"),
            "readiness": self.readiness,
            "readiness_sha256": self.readiness.get("sha256"),
        }


class SimulatorOrchestrator:
    """Prepare and run one deterministic, loopback-only simulator job.

    ``dry_run`` validates every contract and writes evidence without starting
    external programs.  This is useful on machines where PX4/Gazebo are not
    installed yet and intentionally does not claim a rendered 3-D result.
    """

    def __init__(
        self,
        scenario: str | Path,
        *,
        qgc: str | Path | None = None,
        seed: int | None = None,
        frames: int = 600,
        realtime: bool = False,
        headless: bool = False,
        dry_run: bool = False,
        job_root: str | Path | None = None,
        pause_at: float | None = None,
        fault: str | None = None,
        process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        process_specs: Sequence[SimulatorProcessSpec] | None = None,
        qgc_control_port: int = 14650,
        px4_control_endpoint: str | None = None,
        throttle_wire_mode: str = "qgc_legacy",
        manual_watchdog_s: float | None = None,
        gateway_start_timeout_s: float = 2.0,
        initial_authority: ControlAuthorityState | str | None = None,
        interactive: bool | None = None,
        interactive_router_config: SimulatorInteractiveRouterConfig | None = None,
        qgc_control_mode: QgcControlMode | None = None,
        qgc_output_port: int = DEFAULT_QGC_OUTPUT_PORT,
        px4_telemetry_port: int = DEFAULT_PX4_INPUT_PORT,
        ai_config: SimulatorAiExecutorConfig | None = None,
        control_connection: Any | None = None,
        control_connection_factory: Callable[..., Any] | None = None,
        stack_plan_enabled: bool = True,
        stack_repository_root: str | Path | None = None,
        stack_ports: SimulatorStackPorts | Mapping[str, object] | None = None,
        stack_plan: SimulatorStackPlan | None = None,
        stack_gazebo_executable: str | Path | None = None,
        stack_px4_executable: str | Path | None = None,
        stack_gazebo_flavor: str | None = None,
        stack_px4_workdir: str | Path | None = None,
        px4_launch_enabled: bool | None = None,
        px4_launch_strict: bool | None = None,
        readiness_probe: SimulatorReadinessProbe | None = None,
        readiness_timeout_s: float = 0.0,
        readiness_require_heartbeat: bool = False,
        readiness_strict: bool = False,
        live_ai: bool = False,
    ) -> None:
        self.scenario_path = Path(scenario).resolve()
        if not self.scenario_path.is_file():
            raise SimulatorOrchestratorError(f"scenario manifest not found: {self.scenario_path}")
        try:
            self.runtime_input: ScenarioRuntimeInput = load_runtime_scenario(
                self.scenario_path, mode=RuntimeMode.SIM
            )
        except Exception as exc:
            raise SimulatorOrchestratorError(f"invalid SIM scenario: {exc}") from exc
        if seed is not None and int(seed) != self.runtime_input.seed:
            raise SimulatorOrchestratorError(
                f"seed {seed} does not match scenario seed {self.runtime_input.seed}"
            )
        if frames < 1:
            raise SimulatorOrchestratorError("frames must be positive")
        if pause_at is not None and pause_at < 0:
            raise SimulatorOrchestratorError("pause_at must be non-negative")
        if not isinstance(gateway_start_timeout_s, (int, float)) or gateway_start_timeout_s <= 0:
            raise SimulatorOrchestratorError("gateway_start_timeout_s must be positive")
        if fault is not None and fault not in {
            event.event_id for event in self.runtime_input.faults
        }:
            raise SimulatorOrchestratorError(f"unknown fault event: {fault}")
        self.seed = self.runtime_input.seed if seed is None else int(seed)
        self.frames = int(frames)
        self.realtime = bool(realtime)
        self.headless = bool(headless)
        self.dry_run = bool(dry_run)
        self.qgc_path = None if qgc is None else Path(qgc).resolve()
        self.pause_at = pause_at
        self.fault = fault
        self.gateway_start_timeout_s = float(gateway_start_timeout_s)
        if throttle_wire_mode not in {"qgc_legacy", "signed"}:
            raise SimulatorOrchestratorError("throttle_wire_mode must be 'qgc_legacy' or 'signed'")
        self.throttle_wire_mode = throttle_wire_mode
        # ``None`` preserves the old control-gateway path unless the caller
        # supplied a visible QGC executable.  An explicit bool is honoured so
        # an external QGC instance can opt into the full telemetry proxy even
        # when this orchestrator is headless.
        self.interactive = (
            self.qgc_path is not None and not self.headless
            if interactive is None
            else bool(interactive)
        )
        # Native QGC Fly View emits a continuous MANUAL_CONTROL stream.  Make
        # that stream fail closed by default in the interactive path while
        # preserving the historical disabled manual watchdog for deterministic
        # headless fixtures.  Callers can override the deadline explicitly.
        if manual_watchdog_s is not None:
            if (
                isinstance(manual_watchdog_s, bool)
                or not isinstance(manual_watchdog_s, (int, float))
                or not math.isfinite(float(manual_watchdog_s))
                or not 0 < float(manual_watchdog_s) <= 10
            ):
                raise SimulatorOrchestratorError("manual_watchdog_s must be in (0, 10] or None")
        self.manual_watchdog_s = (
            0.5 if self.interactive and manual_watchdog_s is None else manual_watchdog_s
        )
        if interactive_router_config is not None and not isinstance(
            interactive_router_config, SimulatorInteractiveRouterConfig
        ):
            raise SimulatorOrchestratorError(
                "interactive_router_config must be SimulatorInteractiveRouterConfig"
            )
        self._interactive_router_config_override = interactive_router_config
        if qgc_control_mode is not None and qgc_control_mode not in {
            "read_only",
            "sim_flight",
        }:
            raise SimulatorOrchestratorError("qgc_control_mode must be 'read_only' or 'sim_flight'")
        self._qgc_control_mode_override = qgc_control_mode
        try:
            requested_qgc_control_port = int(qgc_control_port)
            requested_qgc_output_port = int(qgc_output_port)
            requested_px4_telemetry_port = int(px4_telemetry_port)
        except (TypeError, ValueError) as exc:
            raise SimulatorOrchestratorError("interactive SIM ports must be integers") from exc
        self.qgc_output_port = requested_qgc_output_port
        self.px4_telemetry_port = requested_px4_telemetry_port
        if interactive_router_config is not None:
            # A concrete router throttle mode must agree with the adapter that
            # emits AI/manual frames.  ``auto`` is intentionally the one
            # compatibility exception: it accepts both inbound QGC wire
            # conventions while the adapter still emits its selected concrete
            # representation.
            router_mode = interactive_router_config.throttle_wire_mode
            if router_mode != "auto" and router_mode != self.throttle_wire_mode:
                raise SimulatorOrchestratorError(
                    "interactive router throttle_wire_mode must match "
                    "throttle_wire_mode (or be 'auto')"
                )
            # When a complete config object is supplied, it is the source of
            # truth for the interactive endpoints.  Non-default CLI values
            # are treated as explicit conflicting requests instead of being
            # silently ignored; omitted/default values remain compatible with
            # callers that only provide the custom config.
            if (
                requested_qgc_control_port != DEFAULT_QGC_INPUT_PORT
                and requested_qgc_control_port != interactive_router_config.qgc_input_port
            ):
                raise SimulatorOrchestratorError(
                    "interactive router qgc_input_port conflicts with qgc_control_port"
                )
            if (
                requested_qgc_output_port != DEFAULT_QGC_OUTPUT_PORT
                and requested_qgc_output_port != interactive_router_config.qgc_output_port
            ):
                raise SimulatorOrchestratorError(
                    "interactive router qgc_output_port conflicts with qgc_output_port"
                )
            if (
                requested_px4_telemetry_port != DEFAULT_PX4_INPUT_PORT
                and requested_px4_telemetry_port != interactive_router_config.px4_input_port
            ):
                raise SimulatorOrchestratorError(
                    "interactive router px4_input_port conflicts with px4_telemetry_port"
                )
            self.qgc_output_port = interactive_router_config.qgc_output_port
            self.px4_telemetry_port = interactive_router_config.px4_input_port
        self._process_factory = process_factory
        self._managed: list[_ManagedProcess] = []
        # ``_managed`` is intentionally cleared during shutdown so process
        # handles cannot be reused after the job ends.  Keep an immutable
        # evidence snapshot separately; otherwise the final ``stopped`` (or
        # startup-failure) result would serialize an empty ``processes`` list.
        self._retained_processes: tuple[dict[str, Any], ...] = ()
        self._lifecycle_lock = threading.RLock()
        # ``live_ai`` receives telemetry on the interactive-router thread,
        # while lifecycle/result snapshots can be written by the caller (or
        # by an out-of-band ``step_ai``).  Keep evidence publication ordered
        # independently from the lifecycle lock and make each individual
        # document replacement atomic; otherwise concurrent ``ai.json`` and
        # ``result.json`` writes can expose a truncated JSON document to a
        # tailer or evaluator.
        self._evidence_write_lock = threading.RLock()
        self._last_result: SimulatorRunResult | None = None
        self._started = False
        self._stopped = False
        self._custom_process_specs = process_specs is not None
        if not isinstance(stack_plan_enabled, bool):
            raise SimulatorOrchestratorError("stack_plan_enabled must be a bool")
        if stack_plan is not None and not isinstance(stack_plan, SimulatorStackPlan):
            raise SimulatorOrchestratorError(
                "stack_plan must be a SimulatorStackPlan when supplied"
            )
        self.stack_plan_enabled = stack_plan_enabled
        self._stack_plan_input = stack_plan
        self._stack_repository_root = (
            None if stack_repository_root is None else Path(stack_repository_root).expanduser()
        )
        self._stack_ports_input = stack_ports
        self._stack_gazebo_executable = stack_gazebo_executable
        self._stack_px4_executable = stack_px4_executable
        self._stack_gazebo_flavor = stack_gazebo_flavor
        self._stack_px4_workdir = stack_px4_workdir
        # The wrapper is generated only after the disposable job directory is
        # allocated.  ``None`` selects the repository-stack default; callers
        # can explicitly disable it for legacy/custom process supervision.
        if px4_launch_enabled is not None and not isinstance(px4_launch_enabled, bool):
            raise SimulatorOrchestratorError("px4_launch_enabled must be a bool or None")
        if px4_launch_strict is not None and not isinstance(px4_launch_strict, bool):
            raise SimulatorOrchestratorError("px4_launch_strict must be a bool or None")
        self.px4_launch_enabled = (
            bool(px4_launch_enabled)
            if px4_launch_enabled is not None
            else bool(stack_plan_enabled and not self._custom_process_specs)
        )
        self.px4_launch_strict = (
            bool(px4_launch_strict) if px4_launch_strict is not None else not self.dry_run
        )
        self.px4_launch_plan: Px4LaunchPlan | None = None
        self._px4_launch_error: str | None = None
        if readiness_probe is not None and not isinstance(readiness_probe, SimulatorReadinessProbe):
            raise SimulatorOrchestratorError(
                "readiness_probe must be a SimulatorReadinessProbe or None"
            )
        if isinstance(readiness_require_heartbeat, bool) is False:
            raise SimulatorOrchestratorError("readiness_require_heartbeat must be bool")
        if isinstance(readiness_strict, bool) is False:
            raise SimulatorOrchestratorError("readiness_strict must be bool")
        if isinstance(readiness_timeout_s, bool):
            raise SimulatorOrchestratorError(
                "readiness_timeout_s must be a finite non-negative number"
            )
        try:
            self.readiness_timeout_s = float(readiness_timeout_s)
        except (TypeError, ValueError) as exc:
            raise SimulatorOrchestratorError(
                "readiness_timeout_s must be a finite non-negative number"
            ) from exc
        if not math.isfinite(self.readiness_timeout_s) or self.readiness_timeout_s < 0:
            raise SimulatorOrchestratorError(
                "readiness_timeout_s must be a finite non-negative number"
            )
        self.readiness_probe = readiness_probe or SimulatorReadinessProbe()
        self.readiness_require_heartbeat = readiness_require_heartbeat
        self.readiness_strict = readiness_strict
        if not isinstance(live_ai, bool):
            raise SimulatorOrchestratorError("live_ai must be a bool")
        self.live_ai = live_ai
        if self.live_ai and not self.interactive:
            raise SimulatorOrchestratorError(
                "live_ai requires the interactive QGC/PX4 telemetry router"
            )
        self._readiness_result: SimulatorReadinessResult | None = None
        self._readiness_error: str | None = None
        self.stack_plan: SimulatorStackPlan | None = None
        self._stack_plan_error: str | None = None
        self._stack_plan_compatibility = bool(
            self._custom_process_specs
            or interactive_router_config is not None
            or (
                px4_control_endpoint is not None
                and px4_control_endpoint != "udpout:127.0.0.1:14540"
            )
            or qgc_control_port != DEFAULT_QGC_INPUT_PORT
            or qgc_output_port != DEFAULT_QGC_OUTPUT_PORT
            or px4_telemetry_port != DEFAULT_PX4_INPUT_PORT
            or stack_ports is not None
        )
        self.authority = SimulatorControlAuthority()
        if initial_authority is not None:
            try:
                selected_authority = (
                    initial_authority
                    if isinstance(initial_authority, ControlAuthorityState)
                    else ControlAuthorityState(str(initial_authority).lower())
                )
            except ValueError as exc:
                raise SimulatorOrchestratorError(
                    f"invalid initial authority: {initial_authority}"
                ) from exc
            if selected_authority is ControlAuthorityState.MANUAL:
                self.authority.resume_manual()
            elif selected_authority is ControlAuthorityState.AI:
                self.authority.enable_ai()
                self.authority.release_manual_control()
            elif selected_authority is ControlAuthorityState.PAUSED:
                self.authority.pause()
            else:
                self.authority.failsafe("initial authority")
        adapter_endpoint = px4_control_endpoint
        if adapter_endpoint is None and interactive_router_config is not None:
            adapter_endpoint = interactive_router_config.px4_output_endpoint
        # A dry-run executes the same authority/policy/adapter code but must
        # never open a real pymavlink socket.  Use an in-process MAVLink sink
        # unless the caller supplied an explicit fake/recorder.
        if self.dry_run and control_connection is None and control_connection_factory is None:
            control_connection = _RecordingMavlinkConnection()
        self.control_adapter = SimulatorMavlinkControlAdapter(
            self.authority,
            config=(
                SimulatorMavlinkControlConfig(
                    throttle_wire_mode=self.throttle_wire_mode,
                    manual_watchdog_s=self.manual_watchdog_s,
                )
                if adapter_endpoint is None
                else SimulatorMavlinkControlConfig(
                    endpoint=adapter_endpoint,
                    throttle_wire_mode=self.throttle_wire_mode,
                    manual_watchdog_s=self.manual_watchdog_s,
                )
            ),
            connection=control_connection,
            connection_factory=control_connection_factory,
        )
        self._dry_run_connection = control_connection if self.dry_run else None
        if interactive_router_config is None:
            # A real interactive QGC session must be able to arm, change mode,
            # take off/land and upload a bounded mission.  Keep the legacy
            # read-only policy for headless/dry-run callers, while selecting
            # the explicit bounded ``sim_flight`` policy whenever the native
            # Fly View router is actually enabled.  Callers can still force
            # read-only with ``qgc_control_mode='read_only'``.
            selected_qgc_control_mode: QgcControlMode = (
                qgc_control_mode
                if qgc_control_mode is not None
                else ("sim_flight" if self.interactive else "read_only")
            )
            try:
                self.interactive_router_config = SimulatorInteractiveRouterConfig(
                    qgc_input_port=int(qgc_control_port),
                    qgc_output_port=self.qgc_output_port,
                    px4_input_port=self.px4_telemetry_port,
                    px4_output_endpoint=self.control_adapter.config.endpoint,
                    throttle_wire_mode=self.control_adapter.config.throttle_wire_mode,
                    qgc_control_mode=selected_qgc_control_mode,
                )
            except Exception as exc:
                raise SimulatorOrchestratorError(
                    f"invalid interactive SIM router configuration: {exc}"
                ) from exc
        else:
            self.interactive_router_config = interactive_router_config
            if (
                qgc_control_mode is not None
                and interactive_router_config.qgc_control_mode != qgc_control_mode
            ):
                raise SimulatorOrchestratorError(
                    "qgc_control_mode conflicts with interactive_router_config"
                )
            # A custom router still has to feed the exact adapter endpoint;
            # otherwise manual and AI output could silently diverge.
            if (
                interactive_router_config.px4_output_endpoint
                != self.control_adapter.config.endpoint
            ):
                raise SimulatorOrchestratorError(
                    "interactive router PX4 endpoint must match px4_control_endpoint"
                )
        # Public, immutable-by-convention snapshot used by launchers and
        # evidence consumers; the router config remains the source of truth.
        self.qgc_control_mode: QgcControlMode = self.interactive_router_config.qgc_control_mode
        gateway_qgc_input_port = (
            self.interactive_router_config.qgc_input_port
            if interactive_router_config is not None
            else int(qgc_control_port)
        )
        self.control_gateway = SimulatorControlGateway(
            self.control_adapter,
            config=SimulatorControlGatewayConfig(
                qgc_input_port=gateway_qgc_input_port,
                px4_output_endpoint=self.control_adapter.config.endpoint,
                # The legacy gateway is inactive while interactive mode is
                # selected, but its evidence/config must still describe the
                # same concrete adapter wire mode.  ``auto`` belongs only to
                # the interactive inbound parser and is not a gateway mode.
                throttle_wire_mode=self.control_adapter.config.throttle_wire_mode,
            ),
        )
        self.interactive_router: SimulatorInteractiveRouter | None = (
            SimulatorInteractiveRouter(
                self.control_adapter,
                config=self.interactive_router_config,
            )
            if self.interactive
            else None
        )
        # The executor is always present (and disabled by default), which
        # gives every job a stable ``ai.json`` schema and lets callers inject
        # real observed telemetry without adding another actuator path.
        self.ai_executor = SimulatorAiExecutor(
            self.control_adapter,
            config=ai_config,
        )
        # The bridge is an observation-only adapter from validated PX4
        # telemetry frames to the same truth-free AI executor used by the
        # deterministic closed loop.  It never owns a second transport.
        self.live_ai_bridge: SimulatorTelemetryBridge | None = (
            SimulatorTelemetryBridge(self.ai_executor) if self.live_ai else None
        )
        if self.interactive_router is not None and self.live_ai_bridge is not None:
            self.interactive_router.set_telemetry_sink(self._consume_live_telemetry)
            # Keep heartbeat freshness and the shared executor watchdog on the
            # same interactive-router event-loop cadence.  This avoids a
            # second timer/transport path and ensures a stale live bridge
            # emits the canonical adapter neutral/failsafe exactly once.
            self.interactive_router.set_watchdog_sink(self._consume_live_watchdog)
        if self.authority.state is ControlAuthorityState.AI:
            # Keep the executor's explicit-enable bit in sync with the
            # authority state selected above.
            self.ai_executor.enable_ai()
        self._gateway_thread: threading.Thread | None = None
        self._gateway_started = threading.Event()
        self._gateway_finished = threading.Event()
        self._gateway_error: str | None = None
        self._interactive_thread: threading.Thread | None = None
        self._interactive_started = threading.Event()
        self._interactive_finished = threading.Event()
        self._interactive_error: str | None = None
        if job_root is None:
            job_root = self.scenario_path.parents[2] / "runs" / "simulator"
        self.job_root = Path(job_root).resolve()
        self.job_dir: Path | None = None
        self.tools = discover_simulator_tools()
        self._initialize_stack_plan()
        self.runtime_descriptor = RuntimeDescriptor(
            mode=RuntimeMode.SIM,
            profile=RuntimeProfile.sim(),
            backend_id="multidetect.simulator",
            airframe_profile_hash=canonical_sha256(self.runtime_input.vehicle.to_mapping()),
            scenario_id=self.runtime_input.scenario_id,
            scenario_manifest_hash=self.runtime_input.scenario_manifest_hash,
            scenario_seed=self.seed,
            deterministic=not self.realtime,
            fault_injection_enabled=bool(self.runtime_input.faults),
        )
        self.process_specs = (
            tuple(process_specs) if process_specs is not None else self._default_process_specs()
        )
        self._validate_isolation()

    @property
    def last_result(self) -> SimulatorRunResult | None:
        """Most recently published lifecycle result, if a job was started."""
        return self._last_result

    def _initialize_stack_plan(self) -> None:
        """Resolve the repository-owned PX4/Gazebo plan once per job.

        Planning is intentionally side-effect free.  A missing executable is
        recorded as an advisory capability by ``build_stack_plan`` and does
        not prevent deterministic dry-runs.  Legacy/custom process specs keep
        their old launch behavior if a custom route cannot satisfy the stricter
        repository asset checks; the error is persisted in job evidence.
        """

        if not self.stack_plan_enabled:
            return
        if self._stack_plan_input is not None:
            try:
                self._validate_stack_plan_alignment(self._stack_plan_input)
            except (SimulatorStackError, ValueError, TypeError) as exc:
                raise SimulatorOrchestratorError(
                    f"supplied stack plan conflicts with SIM endpoints: {exc}"
                ) from exc
            self.stack_plan = self._stack_plan_input
            return
        try:
            ports = self._stack_ports_for_plan()
            repository_root = self._stack_repository_root or _infer_repository_root(
                self.scenario_path
            )
            gazebo_executable = self._stack_gazebo_executable or (
                self.tools["gazebo"].executable or "gz"
            )
            px4_executable = self._stack_px4_executable or (self.tools["px4"].executable or "px4")
            capabilities = {
                "gazebo": self.tools["gazebo"].available,
                "px4": self.tools["px4"].available,
            }
            plan = build_stack_plan(
                self.scenario_path,
                repository_root=repository_root,
                ports=ports,
                gazebo_executable=gazebo_executable,
                px4_executable=px4_executable,
                gazebo_flavor=self._stack_gazebo_flavor,
                headless=self.headless,
                realtime=self.realtime,
                px4_workdir=self._stack_px4_workdir,
                tool_capabilities=capabilities,
            )
            self._validate_stack_plan_alignment(plan)
            self.stack_plan = plan
        except (SimulatorStackError, OSError, TypeError, ValueError) as exc:
            self._stack_plan_error = str(exc)

    def _prepare_px4_launch_plan(self) -> None:
        """Create the job-local PX4 wrapper after ``job_dir`` exists.

        PX4's POSIX startup script does not consume the ``PX4_MAVLINK_*``
        environment variables emitted by :mod:`.stack`; it reads its own
        ``px4-rc.mavlink`` defaults instead.  The launch planner therefore
        patches a copy inside this disposable job and records the exact
        source/generated hashes.  No source checkout file is ever modified.

        The planner is deliberately attempted for dry-runs as well.  A clean
        developer checkout then gets a complete diagnostic wrapper with failed
        readiness checks rather than a misleading claim that PX4 is launchable.
        For a real run, ``start`` turns a strict-readiness failure into a
        blocked result before any external process is spawned.
        """

        self.px4_launch_plan = None
        self._px4_launch_error = None
        if not self.px4_launch_enabled:
            return
        if self._custom_process_specs:
            self._px4_launch_error = "custom process_specs selected; PX4 wrapper not applied"
            return
        if self.stack_plan is None:
            self._px4_launch_error = self._stack_plan_error or "stack plan is unavailable"
            return
        if self.job_dir is None:  # pragma: no cover - guarded by prepare()
            self._px4_launch_error = "job directory is unavailable"
            return

        # Ask the planner for strict diagnostics first.  On failure, rebuild in
        # non-strict mode so the generated template/evidence remains available
        # to the operator and to CI.  This second pass is idempotent and still
        # writes only below the disposable job directory.
        try:
            self.px4_launch_plan = build_px4_launch_plan(
                self.stack_plan,
                job_dir=self.job_dir,
                px4_workdir=self._stack_px4_workdir,
                dry_run=self.dry_run,
                strict=self.px4_launch_strict,
            )
        except (Px4LaunchError, OSError, TypeError, ValueError) as exc:
            self._px4_launch_error = str(exc)
            try:
                self.px4_launch_plan = build_px4_launch_plan(
                    self.stack_plan,
                    job_dir=self.job_dir,
                    px4_workdir=self._stack_px4_workdir,
                    dry_run=self.dry_run,
                    strict=False,
                )
            except (Px4LaunchError, OSError, TypeError, ValueError) as diagnostic_exc:
                # Keep the original strict error as the primary cause; the
                # diagnostic failure is useful in evidence but should not hide
                # the actionable message.
                self._px4_launch_error = (
                    f"{self._px4_launch_error}; diagnostic plan failed: {diagnostic_exc}"
                )
        if (
            self.px4_launch_plan is not None
            and self.px4_launch_strict
            and not self.px4_launch_plan.ready_to_launch
            and self._px4_launch_error is None
        ):
            failed = [name for name, value in self.px4_launch_plan.readiness if not value]
            self._px4_launch_error = "strict PX4 launch readiness failed: " + ", ".join(failed)

    def _px4_launch_evidence(self) -> dict[str, object]:
        """Return the stable launch-plan document embedded in job/result JSON."""

        if not self.px4_launch_enabled:
            return {
                "schema_version": 1,
                "event": "simulator_px4_launch_disabled",
                "enabled": False,
                "strict_requested": self.px4_launch_strict,
                "reason": "px4_launch_enabled=false",
                "truth_free": True,
            }
        if self.px4_launch_plan is not None:
            document = self.px4_launch_plan.to_mapping_with_hash()
            # These fields are lifecycle metadata, not part of the planner's
            # canonical hash.  Keep them outside the hash-bound core so the
            # plan can still be verified byte-for-byte by its own API.
            document["enabled"] = True
            document["strict_requested"] = self.px4_launch_strict
            document["artifacts_verified"] = self.px4_launch_plan.verify()
            if self._px4_launch_error is not None:
                document["orchestrator_error"] = self._px4_launch_error
            return document
        return {
            "schema_version": 1,
            "event": "simulator_px4_launch_error",
            "enabled": True,
            "strict_requested": self.px4_launch_strict,
            "error": self._px4_launch_error or "PX4 launch plan was not generated",
            "stack_plan_sha256": None if self.stack_plan is None else self.stack_plan.evidence_hash,
            "truth_free": True,
        }

    def _readiness_expected_processes(self) -> tuple[str, ...]:
        """Return the process names that must be alive for this job.

        A repository-owned stack always expects PX4 and Gazebo, even when the
        capability probe says one executable is absent.  Recording the absent
        process as a synthetic readiness entry is more useful than silently
        reporting an empty process list.  Custom process specs remain caller
        owned and use their individual ``required`` flags.
        """

        if self.stack_plan is not None and not self._custom_process_specs:
            return ("gazebo", "px4")
        return tuple(
            spec.name for spec in self.process_specs if bool(getattr(spec, "required", True))
        )

    def _runtime_heartbeat_observation(self) -> SimulatorHeartbeatObservation | None:
        """Adapt interactive-router counters into the readiness contract."""

        router = self.interactive_router
        if router is None:
            return None
        evidence = router.evidence()
        endpoint = _endpoint_text(self.interactive_router_config.px4_input_endpoint)
        return SimulatorHeartbeatObservation.from_mapping(
            evidence,
            endpoint=endpoint,
            source="interactive_router",
        )

    def _capture_readiness(self) -> SimulatorReadinessResult | None:
        """Capture process/heartbeat health without launching or stopping anything."""

        expected = self._readiness_expected_processes()
        # Keep a sequence rather than a dict so duplicate process names are
        # rejected by the probe instead of silently overwriting one handle.
        managed = tuple((item.spec.name, item) for item in self._managed)
        heartbeat = self._runtime_heartbeat_observation()
        endpoint = (
            _endpoint_text(self.interactive_router_config.px4_input_endpoint)
            if self.interactive_router is not None
            else _endpoint_text(("127.0.0.1", self.px4_telemetry_port))
        )
        try:
            result = self.readiness_probe.probe(
                managed,
                expected_processes=expected,
                heartbeat_endpoint=endpoint,
                require_heartbeat=self.readiness_require_heartbeat,
                # The interactive router already owns its telemetry socket;
                # use its counters as the heartbeat observation and never
                # attempt a second bind.  Legacy/headless callers may opt into
                # a direct loopback receive window with readiness_timeout_s.
                timeout_s=0.0 if heartbeat is not None else self.readiness_timeout_s,
                heartbeat_observation=heartbeat,
            )
        except (SimulatorReadinessError, TypeError, ValueError) as exc:
            self._readiness_error = str(exc)
            self._readiness_result = None
            return None
        self._readiness_error = None
        self._readiness_result = result
        return result

    def _readiness_evidence(self) -> dict[str, object]:
        """Return hash-bound runtime readiness evidence for job/result JSON."""

        result = self._readiness_result or self._capture_readiness()
        if result is None:
            return {
                "schema_version": 1,
                "event": "simulator_runtime_readiness_error",
                "enabled": True,
                "error": self._readiness_error or "readiness probe did not run",
                "truth_free": True,
            }
        document = result.to_mapping_with_hash()
        document["enabled"] = True
        document["strict_requested"] = self.readiness_strict
        return document

    def _stack_ports_for_plan(self) -> SimulatorStackPorts:
        """Build planner ports from the already validated control topology."""

        host, control_port = _parse_loopback_udpout(self.control_adapter.config.endpoint)
        qgc_input_port = self.control_gateway.config.qgc_input_port
        values = {
            "host": host,
            "px4_control_port": control_port,
            "px4_telemetry_port": self.px4_telemetry_port,
            "qgc_output_port": self.qgc_output_port,
            "qgc_input_port": qgc_input_port,
            "metadata_port": DEFAULT_METADATA_PORT,
            "rtsp_port": DEFAULT_RTSP_PORT,
        }
        if self._stack_ports_input is not None:
            if isinstance(self._stack_ports_input, SimulatorStackPorts):
                selected = self._stack_ports_input
            elif isinstance(self._stack_ports_input, Mapping):
                try:
                    selected = SimulatorStackPorts.from_mapping(self._stack_ports_input)
                except (SimulatorStackError, TypeError, ValueError) as exc:
                    raise SimulatorOrchestratorError(f"invalid stack_ports: {exc}") from exc
            else:
                raise SimulatorOrchestratorError(
                    "stack_ports must be SimulatorStackPorts or a mapping"
                )
            values = {
                "host": selected.host,
                "px4_control_port": selected.px4_control_port,
                "px4_telemetry_port": selected.px4_telemetry_port,
                "qgc_output_port": selected.qgc_output_port,
                "qgc_input_port": selected.qgc_input_port,
                "metadata_port": selected.metadata_port,
                "rtsp_port": selected.rtsp_port,
            }
        try:
            selected = SimulatorStackPorts(**values)
        except (SimulatorStackError, TypeError, ValueError) as exc:
            raise SimulatorOrchestratorError(f"invalid SIM stack ports: {exc}") from exc
        # A supplied ports object is an explicit contract, not a hint.  Catch
        # mismatches before the planner can silently route control elsewhere.
        if selected.host != host or selected.px4_control_port != control_port:
            raise SimulatorOrchestratorError(
                "stack plan PX4 control endpoint conflicts with control_adapter endpoint"
            )
        if selected.qgc_input_port != qgc_input_port:
            raise SimulatorOrchestratorError(
                "stack plan QGC input port conflicts with simulator control gateway"
            )
        if selected.qgc_output_port != self.qgc_output_port:
            raise SimulatorOrchestratorError(
                "stack plan QGC output port conflicts with interactive router"
            )
        if selected.px4_telemetry_port != self.px4_telemetry_port:
            raise SimulatorOrchestratorError(
                "stack plan PX4 telemetry port conflicts with interactive router"
            )
        return selected

    def _validate_stack_plan_alignment(self, plan: SimulatorStackPlan) -> None:
        """Verify a plan cannot divert QGC/AI output to another endpoint."""

        if plan.scenario_id != self.runtime_input.scenario_id:
            raise SimulatorStackError("stack plan scenario_id does not match runtime scenario")
        if plan.scenario_manifest_hash != self.runtime_input.scenario_manifest_hash:
            raise SimulatorStackError(
                "stack plan scenario_manifest_hash does not match runtime scenario"
            )
        host, control_port = _parse_loopback_udpout(self.control_adapter.config.endpoint)
        expected_qgc_input = self.control_gateway.config.qgc_input_port
        if plan.ports.host != host or plan.ports.px4_control_port != control_port:
            raise SimulatorStackError(
                "stack plan PX4 control endpoint conflicts with control adapter"
            )
        if plan.ports.qgc_input_port != expected_qgc_input:
            raise SimulatorStackError("stack plan QGC input port conflicts with control gateway")
        if plan.ports.qgc_output_port != self.qgc_output_port:
            raise SimulatorStackError(
                "stack plan QGC output port conflicts with interactive router"
            )
        if plan.ports.px4_telemetry_port != self.px4_telemetry_port:
            raise SimulatorStackError(
                "stack plan PX4 telemetry port conflicts with interactive router"
            )

    def _default_process_specs(self) -> tuple[SimulatorProcessSpec, ...]:
        specs: list[SimulatorProcessSpec] = []
        if self.stack_plan is not None:
            # The planner is the source of truth for executable arguments and
            # the PX4/Gazebo resource environment.  Keep one immutable env
            # snapshot on each process spec so process launch and evidence use
            # exactly the same plan.
            env = self.stack_plan.environment_mapping
            tool_caps = dict(self.stack_plan.tool_capabilities)
            if tool_caps.get("px4", False):
                specs.append(
                    SimulatorProcessSpec(
                        "px4",
                        tuple(self.stack_plan.px4_command),
                        env=env,
                        cwd=_infer_px4_workdir(self.stack_plan),
                    )
                )
            if tool_caps.get("gazebo", False):
                specs.append(
                    SimulatorProcessSpec(
                        "gazebo",
                        tuple(self.stack_plan.gazebo_command),
                        env=env,
                    )
                )
        else:
            # Compatibility fallback for callers using a non-repository
            # scenario or explicitly disabling stack planning.
            px4 = self.tools["px4"].executable
            gazebo = self.tools["gazebo"].executable
            if px4:
                specs.append(SimulatorProcessSpec("px4", (px4, "-s", "etc/init.d-posix/rcS")))
            if gazebo:
                specs.append(SimulatorProcessSpec("gazebo", (gazebo, "sim", "-r")))
        if self.qgc_path is not None:
            if not self.qgc_path.is_file():
                raise SimulatorOrchestratorError(f"QGC executable not found: {self.qgc_path}")
            if not self.headless:
                command: tuple[str, ...] = (str(self.qgc_path),)
                if self.interactive:
                    command = self._qgc_interactive_command(command)
                specs.append(SimulatorProcessSpec("qgc", command))
        return tuple(specs)

    def _qgc_interactive_command(self, command: Sequence[str]) -> tuple[str, ...]:
        """Append the native QGC isolated-HIL UDP topology to one command.

        QGC binds ``--isolated-hil-px4-udp-port`` locally for telemetry and
        sends its outbound datagrams to ``--isolated-hil-px4-target-port``.
        The latter is the interactive router's QGC input; no second UI or
        custom joystick surface is introduced.
        """

        values = [str(item) for item in command]
        if "--isolated-hil" not in values:
            values.append("--isolated-hil")
        options = {
            "--isolated-hil-px4-udp-port": str(self.interactive_router_config.qgc_output_port),
            "--isolated-hil-px4-target-port": str(self.interactive_router_config.qgc_input_port),
        }
        for option, value in options.items():
            _ensure_qgc_option(values, option, value)
        return tuple(values)

    def _apply_px4_launch_plan(
        self, specs: Sequence[SimulatorProcessSpec]
    ) -> tuple[SimulatorProcessSpec, ...]:
        """Use the patched job-local PX4 wrapper for a real stack launch.

        Dry-runs intentionally retain the raw planner command so their
        process-spec contract stays side-effect free.  A non-dry run uses the
        wrapper only after strict readiness has passed; this prevents PX4 from
        silently falling back to its stock 18570/14580 UDP defaults.
        """

        plan = self.px4_launch_plan
        if self.dry_run or not self.px4_launch_enabled or plan is None or not plan.ready_to_launch:
            return tuple(specs)
        result: list[SimulatorProcessSpec] = []
        applied = False
        for spec in specs:
            if spec.name.lower() != "px4":
                result.append(spec)
                continue
            environment = dict(spec.env or {})
            environment.update(plan.environment_mapping)
            command = tuple(plan.command)
            # The generated wrapper is POSIX shell by design because PX4's
            # ``rcS``/``px4-rc.mavlink`` are POSIX scripts.  On Windows hosts
            # prefer an installed sh/bash when available; Linux/container
            # runners execute the wrapper directly via its shebang.
            if os.name == "nt":
                shell = shutil.which("sh") or shutil.which("bash")
                if shell:
                    command = (shell, str(plan.wrapper_path))
            result.append(
                replace(
                    spec,
                    command=command,
                    cwd=plan.px4_workdir,
                    env=environment,
                )
            )
            applied = True
        if not applied:
            # A repository stack without a PX4 spec is an invariant violation;
            # leave the tuple untouched so custom process-spec compatibility is
            # preserved and the start path can report its normal tool error.
            return tuple(specs)
        return tuple(result)

    def _launch_specs(self) -> tuple[SimulatorProcessSpec, ...]:
        """Return process specs with interactive QGC flags applied.

        Custom specs are deliberately preserved.  If a caller supplies a
        custom ``qgc`` entry, only the missing isolated-HIL flags are appended;
        all other arguments and process ordering remain caller-owned.
        """

        if not self.interactive:
            return self.process_specs
        result: list[SimulatorProcessSpec] = []
        for spec in self.process_specs:
            if spec.name.lower() == "qgc":
                result.append(replace(spec, command=self._qgc_interactive_command(spec.command)))
            else:
                result.append(spec)
        return tuple(result)

    def _validate_isolation(self) -> None:
        for sensor in self.runtime_input.sensors:
            validate_simulation_endpoint(sensor.stream_uri)
        # Reserve/check all loopback UDP ports encoded in the scenario.
        for sensor in self.runtime_input.sensors:
            uri = sensor.stream_uri
            if uri.startswith("udp://"):
                from urllib.parse import urlparse

                parsed = urlparse(uri)
                if parsed.port is not None:
                    ensure_port_available(parsed.hostname or "127.0.0.1", parsed.port)
        # The gateway binds UDP; probing with a TCP socket can report a
        # transient Windows WSAEACCES even when the UDP endpoint is usable.
        _ensure_udp_port_available(
            self.control_gateway.config.qgc_input_host,
            self.control_gateway.config.qgc_input_port,
        )
        if self.interactive:
            # The router binds QGC-input and PX4-telemetry-input.  QGC owns
            # the local output/receive port, so check it here before launching
            # the process and let QGC claim it after the router is ready.
            for host, port in (
                (
                    self.interactive_router_config.qgc_input_host,
                    self.interactive_router_config.qgc_input_port,
                ),
                (
                    self.interactive_router_config.qgc_output_host,
                    self.interactive_router_config.qgc_output_port,
                ),
                (
                    self.interactive_router_config.px4_input_host,
                    self.interactive_router_config.px4_input_port,
                ),
            ):
                ensure_port_available(host, port)

    def prepare(self) -> Path:
        if self.job_dir is not None:
            return self.job_dir
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.job_dir = self.job_root / f"{stamp}-{self.runtime_input.scenario_id}-{self.seed}"
        self.job_dir.mkdir(parents=True, exist_ok=False)
        (self.job_dir / "logs").mkdir()
        (self.job_dir / "artifacts").mkdir()
        self._retained_processes = ()
        self._prepare_px4_launch_plan()
        if self._managed or self._readiness_result is None:
            self._capture_readiness()
        self._write_json("scenario-runtime.json", self.runtime_input.to_mapping())
        self._write_json("runtime-descriptor.json", self.runtime_descriptor.to_mapping())
        self._write_json("stack-plan.json", self._stack_evidence())
        # ``build_px4_launch_plan`` writes its hash-bound document itself when
        # successful.  Error/disabled paths still get an explicit document so
        # every job has the same evidence shape.
        if self.px4_launch_plan is None:
            self._write_json("px4-launch.json", self._px4_launch_evidence())
        self._write_json("readiness.json", self._readiness_evidence())
        self._write_json(
            "authority.json",
            {"state": self.authority.state.value, "events": list(self.authority.events)},
        )
        self._write_json("control.json", self._control_evidence())
        self._write_json("control-gateway.json", self.control_gateway.evidence())
        self._write_json("interactive-router.json", self._interactive_evidence())
        self._write_json("ai.json", self._ai_evidence())
        self._write_json(
            "job.json",
            {
                "scenario_path": str(self.scenario_path),
                "scenario_sha256": canonical_sha256(
                    json.loads(self.scenario_path.read_text(encoding="utf-8"))
                ),
                "frames": self.frames,
                "realtime": self.realtime,
                "headless": self.headless,
                "dry_run": self.dry_run,
                "control_transport": (
                    "in_process_recording_mavlink" if self.dry_run else "pymavlink_loopback"
                ),
                "external_io_opened": False if self.dry_run else None,
                "fault": self.fault,
                "interactive": self.interactive,
                "qgc_control_endpoint": (
                    f"{self.control_gateway.config.qgc_input_host}:"
                    f"{self.control_gateway.config.qgc_input_port}"
                ),
                "px4_control_endpoint": self.control_adapter.config.endpoint,
                "throttle_wire_mode": self.control_adapter.config.throttle_wire_mode,
                "manual_watchdog_s": self.control_adapter.config.manual_watchdog_s,
                "qgc_control_mode": self.interactive_router_config.qgc_control_mode,
                "stack_plan_enabled": self.stack_plan_enabled,
                "stack_plan_sha256": (
                    None if self.stack_plan is None else self.stack_plan.evidence_hash
                ),
                "stack_plan_path": str(self.job_dir / "stack-plan.json"),
                "stack_plan_error": self._stack_plan_error,
                "px4_launch_enabled": self.px4_launch_enabled,
                "px4_launch_strict": self.px4_launch_strict,
                "px4_launch_sha256": (
                    None if self.px4_launch_plan is None else self.px4_launch_plan.evidence_hash
                ),
                "px4_launch_path": str(self.job_dir / "px4-launch.json"),
                "px4_launch_error": self._px4_launch_error,
                "readiness_strict": self.readiness_strict,
                "readiness_require_heartbeat": self.readiness_require_heartbeat,
                "readiness_timeout_s": self.readiness_timeout_s,
                "readiness_sha256": self._readiness_evidence().get("sha256"),
                "readiness_path": str(self.job_dir / "readiness.json"),
                "live_ai": self.live_ai,
                "live_ai_transport": (
                    "interactive_router_px4_telemetry" if self.live_ai else "disabled"
                ),
                "interactive_router": (
                    None
                    if not self.interactive
                    else {
                        "qgc_input": _endpoint_text(
                            self.interactive_router_config.qgc_input_endpoint
                        ),
                        "qgc_output": _endpoint_text(
                            self.interactive_router_config.qgc_output_endpoint
                        ),
                        "px4_telemetry_input": _endpoint_text(
                            self.interactive_router_config.px4_input_endpoint
                        ),
                        "px4_command_output": self.interactive_router_config.px4_output_endpoint,
                    }
                ),
            },
        )
        return self.job_dir

    def start(self) -> SimulatorRunResult:
        with self._lifecycle_lock:
            if self._last_result is not None and self._started:
                return self._last_result
            job_dir = self.prepare()
            if (
                self._stack_plan_error is not None
                and (not self._stack_plan_compatibility or not self.dry_run)
                and not self._custom_process_specs
            ):
                result = self._result(
                    "blocked", error="invalid SIM stack plan: " + self._stack_plan_error
                )
                self._last_result = result
                self._started = True
                self._write_result(result)
                if not self.dry_run:
                    raise SimulatorOrchestratorError(result.error or "invalid SIM stack plan")
                return result
            if (
                self.px4_launch_enabled
                and self.px4_launch_strict
                and self._px4_launch_error is not None
                and not self._custom_process_specs
            ):
                result = self._result(
                    "blocked",
                    error="invalid PX4 launch plan: " + self._px4_launch_error,
                )
                self._last_result = result
                self._started = True
                self._write_result(result)
                if not self.dry_run:
                    raise SimulatorOrchestratorError(result.error or "invalid PX4 launch plan")
                return result
            if self.dry_run:
                result = self._result("planned")
                self._last_result = result
                self._started = True
                self._write_result(result)
                return result
            missing = [name for name in ("px4", "gazebo") if not self.tools[name].available]
            if self._custom_process_specs:
                missing = []
            if missing:
                result = self._result(
                    "blocked", error="missing simulator tools: " + ", ".join(missing)
                )
                self._last_result = result
                self._started = True
                self._write_result(result)
                raise SimulatorOrchestratorError(result.error or "simulator tools unavailable")
            try:
                launch_specs = self._launch_specs()
                launch_specs = self._apply_px4_launch_plan(launch_specs)
                # Bind the selected router before launching QGC.  QGC's
                # isolated UDP link can then send its first heartbeat/manual
                # frame without racing the proxy bind.  Non-QGC processes are
                # started first so PX4/Gazebo are ready to receive telemetry.
                non_qgc = tuple(spec for spec in launch_specs if spec.name.lower() != "qgc")
                qgc_specs = tuple(spec for spec in launch_specs if spec.name.lower() == "qgc")
                for spec in non_qgc:
                    self._launch_process(spec, job_dir)
                self._start_control_path()
                if self._active_control_error() is not None:
                    raise SimulatorOrchestratorError(
                        "SIM control path failed to bind/start: "
                        + str(self._active_control_error())
                    )
                for spec in qgc_specs:
                    self._launch_process(spec, job_dir)
                readiness = self._capture_readiness()
                if self.readiness_strict and (readiness is None or not readiness.ready):
                    reason = (
                        self._readiness_error
                        if readiness is None
                        else ", ".join(readiness.errors)
                        or "required process/heartbeat readiness not satisfied"
                    )
                    raise SimulatorOrchestratorError("SIM runtime readiness failed: " + reason)
                result = self._result("running")
                self._last_result = result
                self._started = True
                self._stopped = False
                self._write_result(result)
                return result
            except Exception as exc:
                self.stop()
                result = self._result("failed", error=str(exc))
                self._last_result = result
                self._started = True
                self._write_result(result)
                raise SimulatorOrchestratorError(str(exc)) from exc

    def _launch_process(self, spec: SimulatorProcessSpec, job_dir: Path) -> None:
        """Launch one managed process and retain its log/evidence record."""

        log_path = job_dir / "logs" / f"{spec.name}.log"
        log = log_path.open("w", encoding="utf-8")
        try:
            launch_kwargs: dict[str, Any] = {
                "cwd": str(spec.cwd) if spec.cwd is not None else str(job_dir),
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            }
            if spec.env:
                merged_env = os.environ.copy()
                merged_env.update({str(key): str(value) for key, value in spec.env.items()})
                launch_kwargs["env"] = merged_env
            try:
                process = self._process_factory(
                    list(spec.command),
                    **launch_kwargs,
                )
            except TypeError:
                # Older fake factories accepted only the command positional
                # argument.  Retry without kwargs exactly as pre-stack code
                # did, keeping tests and downstream launchers source-
                # compatible while real subprocesses receive the plan env.
                process = self._process_factory(list(spec.command))
        except BaseException:
            # Release the parent copy when process creation fails.  The child
            # never owns a usable handle in this branch, and leaving it open
            # turns a launch error into an unraisable ResourceWarning later.
            log.close()
            raise
        else:
            # ``Popen`` duplicates/inherits the stream for the child.  The
            # orchestrator only retains the path in ``_ManagedProcess``; it
            # must not retain a second open descriptor for the lifetime of a
            # simulator run (especially on Windows where this blocks cleanup
            # and shows up under ``-W error``).
            log.close()
        self._managed.append(_ManagedProcess(spec, process, log_path))

    def _start_control_path(self) -> None:
        """Start either the interactive telemetry router or legacy gateway."""

        if self.interactive:
            self._start_interactive_router()
        else:
            self._start_control_gateway()

    def _active_control_error(self) -> str | None:
        return self._interactive_error if self.interactive else self._gateway_error

    def stop(self) -> None:
        with self._lifecycle_lock:
            if (
                self._stopped
                and not self._managed
                and self._gateway_thread is None
                and self._interactive_thread is None
            ):
                return
            self._stopped = True
            if self.interactive and self.interactive_router is not None:
                self.interactive_router.stop()
            else:
                self.control_gateway.stop()
        if self._gateway_thread is not None:
            self._gateway_thread.join(timeout=2)
            self._gateway_thread = None
        if self._interactive_thread is not None:
            self._interactive_thread.join(timeout=2)
            self._interactive_thread = None
        # Let the AI executor own the final authority transition.  The active
        # router/gateway normally already emitted neutral in its ``finally``;
        # this second call is idempotent and covers dry-run/manual callers
        # that stepped AI without starting the transport.
        self.ai_executor.stop()
        self.control_adapter.shutdown()
        for managed in reversed(self._managed):
            process = managed.process
            # Lightweight process doubles used by embedders may expose only
            # ``returncode`` (or neither accessor).  Use the same defensive
            # poll helper as result serialization so cleanup cannot mask the
            # original lifecycle error with an AttributeError.
            if _safe_process_poll(process) is None:
                try:
                    send_signal = getattr(process, "send_signal", None)
                    terminate = getattr(process, "terminate", None)
                    if os.name == "nt" and callable(send_signal):
                        send_signal(signal.CTRL_BREAK_EVENT)
                    elif callable(terminate):
                        terminate()
                    elif callable(send_signal):
                        # POSIX fakes occasionally implement only
                        # ``send_signal``; retain a best-effort fallback.
                        send_signal(signal.SIGTERM)
                    wait = getattr(process, "wait", None)
                    if callable(wait):
                        wait(timeout=5)
                except Exception:
                    try:
                        kill = getattr(process, "kill", None)
                        if callable(kill):
                            kill()
                    except Exception:
                        pass
            try:
                managed.log_path.open("a", encoding="utf-8").close()
            except OSError:
                pass
        # Capture final return codes before clearing managed handles so the
        # stopped result retains process-health evidence.  ``_result`` uses
        # this snapshot once the live handles have been released.
        self._capture_readiness()
        self._retained_processes = self._process_evidence()
        self._managed.clear()
        if self.job_dir is not None:
            self._write_json(
                "authority.json",
                {"state": self.authority.state.value, "events": list(self.authority.events)},
            )
            self._write_json("control.json", self._control_evidence())
            self._write_json("control-gateway.json", self.control_gateway.evidence())
            self._write_json("interactive-router.json", self._interactive_evidence())
            self._write_json("ai.json", self._ai_evidence())
            self._write_json("readiness.json", self._readiness_evidence())
        self.control_adapter.close()
        if self.job_dir is not None:
            final = self._result("stopped")
            self._last_result = final
            self._write_result(final)

    def __enter__(self) -> SimulatorOrchestrator:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.stop()

    def enable_ai(self) -> None:
        """Explicitly enable the shared SIM AI executor."""

        self.ai_executor.enable_ai()
        if self.job_dir is not None:
            self._write_json("ai.json", self._ai_evidence())
            self._refresh_published_result()

    def _consume_live_telemetry(self, frame: Any, timestamp_s: float) -> Any:
        """Feed one validated PX4 frame into the optional live AI bridge.

        The router invokes this callback on its receive thread.  The bridge
        owns its own lock and the executor serializes authority/output, so a
        concurrent QGC manual frame still wins deterministically.  Persisting
        ``ai.json`` here makes a running job inspectable without waiting for
        shutdown; transport forwarding remains independent of this callback.
        """

        bridge = self.live_ai_bridge
        if bridge is None:
            return None
        decision = bridge.ingest(frame, timestamp_s=timestamp_s)
        if self.job_dir is not None:
            self._write_json("ai.json", self._ai_evidence())
        return decision

    def _consume_live_watchdog(self, timestamp_s: float) -> Any:
        """Drive live-bridge freshness and executor watchdog from the router.

        The callback runs on the interactive-router event-loop thread.  It is
        deliberately side-band with respect to UDP forwarding: a bridge or
        evidence-write failure is handled by the router's watchdog fallback,
        while successful ticks return the bridge decision without invoking the
        adapter a second time.
        """

        bridge = self.live_ai_bridge
        if bridge is None:
            return None
        decision = bridge.watchdog_tick(now_s=float(timestamp_s))
        if self.job_dir is not None:
            self._write_json("ai.json", self._ai_evidence())
        return decision

    def _ai_evidence(self) -> dict[str, object]:
        """Compose stable executor evidence with optional live-bridge data."""

        evidence = dict(self.ai_executor.evidence())
        evidence["live_ai_enabled"] = self.live_ai
        evidence["live_ai_transport"] = (
            "interactive_router_px4_telemetry" if self.live_ai else "disabled"
        )
        evidence["live_bridge"] = (
            self.live_ai_bridge.evidence()
            if self.live_ai_bridge is not None
            else {
                "enabled": False,
                "frames_observed": 0,
                "ai_steps": 0,
                "truth_free_input_contract": True,
            }
        )
        return evidence

    def step_ai(
        self,
        snapshot: SimulatorTelemetrySnapshot | dict[str, object],
        *,
        now_s: float | None = None,
    ) -> Any:
        """Run one observation-driven AI step and persist ``ai.json``.

        A mapping is accepted for CLI/integration callers; it is converted to
        the same truth-free :class:`SimulatorTelemetrySnapshot` contract used
        by live sensors.  No alternate actuator or mission path is exposed.
        """

        if isinstance(snapshot, dict):
            snapshot = SimulatorTelemetrySnapshot.from_mapping(snapshot)
        if not isinstance(snapshot, SimulatorTelemetrySnapshot):
            raise TypeError("snapshot must be SimulatorTelemetrySnapshot or mapping")
        decision = self.ai_executor.step(snapshot, now_s=now_s)
        if self.job_dir is not None:
            self._write_json("ai.json", self._ai_evidence())
            self._refresh_published_result()
        return decision

    ai_step = step_ai

    def _refresh_published_result(self) -> None:
        """Refresh ``result.json`` after an out-of-band AI control step."""

        if self._last_result is None:
            return
        refreshed = self._result(self._last_result.status, error=self._last_result.error)
        self._last_result = refreshed
        self._write_result(refreshed)

    def _result(self, status: str, *, error: str | None = None) -> SimulatorRunResult:
        assert self.job_dir is not None
        if self._managed or self._readiness_result is None:
            self._capture_readiness()
        readiness_evidence = self._readiness_evidence()
        self._write_json("readiness.json", readiness_evidence)
        gateway_evidence = self.control_gateway.evidence()
        if self._gateway_error is not None:
            gateway_evidence = {**gateway_evidence, "startup_error": self._gateway_error}
        interactive_evidence = self._interactive_evidence()
        if self._interactive_error is not None:
            interactive_evidence = {
                **interactive_evidence,
                "startup_error": self._interactive_error,
            }
        processes = self._process_evidence()
        if not processes and self._retained_processes:
            # Shutdown clears live handles before publishing the final result;
            # retain their last observed PID/returncode/log metadata instead
            # of silently changing the public result contract to an empty
            # process list.
            processes = self._retained_processes
        return SimulatorRunResult(
            status,
            self.runtime_input.scenario_id,
            self.runtime_input.scenario_manifest_hash,
            self.seed,
            self.job_dir,
            self.runtime_descriptor,
            {name: asdict(cap) for name, cap in self.tools.items()},
            self.authority.state.value,
            self._control_evidence(),
            gateway_evidence,
            processes,
            error,
            interactive_evidence,
            self._ai_evidence(),
            self._stack_evidence(),
            self.stack_plan_enabled,
            self._px4_launch_evidence(),
            readiness_evidence,
        )

    def _process_evidence(self) -> tuple[dict[str, Any], ...]:
        """Serialize currently managed process metadata defensively."""

        if self.job_dir is None:
            return ()
        return tuple(
            {
                "name": item.spec.name,
                "pid": _safe_process_pid(item.process),
                "returncode": _safe_process_poll(item.process),
                "log": str(item.log_path),
                "cwd": str(item.spec.cwd) if item.spec.cwd is not None else str(self.job_dir),
                "env_keys": sorted(item.spec.env) if item.spec.env else [],
            }
            for item in self._managed
        )

    def _start_control_gateway(self) -> None:
        """Run the local QGC input path without creating a second UI process."""
        self._gateway_error = None
        self._gateway_started.clear()
        self._gateway_finished.clear()

        def serve() -> None:
            try:
                self.control_gateway.run(duration_s=0.0)
            except Exception as exc:  # pragma: no cover - OS/socket boundary.
                self._gateway_error = str(exc)
            finally:
                # This event means the thread has entered its target.  The
                # readiness contract below deliberately waits on the
                # gateway's bind-after-listen event instead of this marker.
                self._gateway_started.set()
                self._gateway_finished.set()

        self._gateway_thread = threading.Thread(
            target=serve,
            name="multidetect-sim-control-gateway",
            daemon=True,
        )
        self._gateway_thread.start()
        # ``ready_event`` is set only after the UDP socket successfully binds.
        # Waiting on the thread-start marker alone used to publish ``running``
        # while the bind could still fail (or before the port was usable).
        if not self.control_gateway.ready_event.wait(timeout=self.gateway_start_timeout_s):
            if self._gateway_error is not None:
                raise SimulatorOrchestratorError(self._gateway_error)
            if self._gateway_finished.is_set():
                raise SimulatorOrchestratorError("SIM control gateway exited during startup")
            raise SimulatorOrchestratorError(
                "SIM control gateway did not become ready within "
                f"{self.gateway_start_timeout_s:.3f}s"
            )
        if self._gateway_error is not None:
            raise SimulatorOrchestratorError(self._gateway_error)
        # A test double or an unexpectedly short-lived gateway may set ready
        # and finish in the same scheduling slice; do not hide that failure.
        if self._gateway_finished.wait(timeout=0.02):
            raise SimulatorOrchestratorError("SIM control gateway exited during startup")

    def _start_interactive_router(self) -> None:
        """Start the full QGC↔PX4 telemetry/control proxy on loopback."""

        router = self.interactive_router
        if router is None:
            raise SimulatorOrchestratorError("interactive router is disabled")
        self._interactive_error = None
        self._interactive_started.clear()
        self._interactive_finished.clear()

        def serve() -> None:
            try:
                router.run(duration_s=0.0)
            except Exception as exc:  # pragma: no cover - OS/socket boundary.
                self._interactive_error = str(exc)
            finally:
                self._interactive_started.set()
                self._interactive_finished.set()

        self._interactive_thread = threading.Thread(
            target=serve,
            name="multidetect-sim-interactive-router",
            daemon=True,
        )
        self._interactive_thread.start()
        if not router.ready_event.wait(timeout=self.gateway_start_timeout_s):
            if self._interactive_error is not None:
                raise SimulatorOrchestratorError(self._interactive_error)
            if self._interactive_finished.is_set():
                raise SimulatorOrchestratorError("SIM interactive router exited during startup")
            raise SimulatorOrchestratorError(
                "SIM interactive router did not become ready within "
                f"{self.gateway_start_timeout_s:.3f}s"
            )
        if self._interactive_error is not None:
            raise SimulatorOrchestratorError(self._interactive_error)
        if self._interactive_finished.wait(timeout=0.02):
            raise SimulatorOrchestratorError("SIM interactive router exited during startup")

    def _interactive_evidence(self) -> dict[str, object]:
        if self.interactive_router is None:
            return {
                "schema_version": 1,
                "event": "simulator_interactive_router_disabled",
                "enabled": False,
                "reason": "legacy_control_gateway_selected",
                "literal_loopback_only": True,
                "qgc_control_mode": self.interactive_router_config.qgc_control_mode,
                "qgc_flight_commands_allowed": False,
                "qgc_mission_writes_allowed": False,
                "qgc_parameter_writes_allowed": False,
            }
        return {
            **self.interactive_router.evidence(),
            "enabled": True,
            "selected": self.interactive,
        }

    def _stack_evidence(self) -> dict[str, object]:
        """Return hash-bound stack evidence (or an explicit disabled/error record)."""

        if not self.stack_plan_enabled:
            return {
                "schema_version": 1,
                "event": "simulator_stack_plan_disabled",
                "enabled": False,
                "reason": "stack_plan_enabled=false",
                "scenario_id": self.runtime_input.scenario_id,
                "scenario_manifest_hash": self.runtime_input.scenario_manifest_hash,
                "truth_free": True,
            }
        if self.stack_plan is not None:
            # Keep the planner's hash-bound document byte-for-byte canonical;
            # lifecycle metadata such as source/enabled lives in job.json and
            # result.json rather than being appended after ``sha256``.
            return self.stack_plan.to_mapping_with_hash()
        return {
            "schema_version": 1,
            "event": "simulator_stack_plan_error",
            "enabled": True,
            "scenario_id": self.runtime_input.scenario_id,
            "scenario_manifest_hash": self.runtime_input.scenario_manifest_hash,
            "error": self._stack_plan_error or "stack plan was not generated",
            "compatibility_fallback": self._stack_plan_compatibility,
            "truth_free": True,
        }

    def _control_evidence(self) -> dict[str, object]:
        """Return adapter evidence with an explicit dry-run I/O boundary."""

        evidence = self.control_adapter.evidence()
        if not self.dry_run:
            return evidence
        recording = self._dry_run_connection
        messages = getattr(getattr(recording, "mav", None), "messages", None)
        return {
            **evidence,
            "dry_run": True,
            "external_io_opened": False,
            "transport_sink": "in_process_recording_mavlink",
            "recorded_messages": 0 if messages is None else len(messages),
        }

    def _write_result(self, result: SimulatorRunResult) -> None:
        self._write_json("result.json", result.to_mapping())

    def _write_json(self, name: str, value: object) -> None:
        if self.job_dir is None:
            return
        path = self.job_dir / name
        # Write in the same directory and replace the destination in one
        # rename so readers never observe a partially written JSON file.  A
        # thread-specific temporary name avoids collisions with an evaluator
        # or a stale file left by an interrupted process.  The dedicated lock
        # serializes writers from the router callback, ``stop()``, and direct
        # ``step_ai()`` calls without imposing a lock on MAVLink transport.
        with self._evidence_write_lock:
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                payload = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                # On Windows a reader that opened the previous destination
                # without FILE_SHARE_DELETE can transiently make ReplaceFile
                # fail with WinError 5.  Keep the atomic contract, but retry a
                # short bounded window so a tailer/evaluator does not turn a
                # harmless read race into a failed telemetry callback.
                replace_error: PermissionError | None = None
                for attempt in range(24):
                    try:
                        os.replace(temporary, path)
                    except PermissionError as exc:
                        replace_error = exc
                        if attempt == 23:
                            raise
                        threading.Event().wait(0.002 * (attempt + 1))
                    else:
                        replace_error = None
                        break
                if replace_error is not None:  # pragma: no cover - guarded by raise above
                    raise replace_error
            finally:
                # ``os.replace`` removes the temporary on success.  Best
                # effort cleanup keeps failed disk writes from polluting a
                # job directory while never masking the original exception.
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def _endpoint_text(endpoint: tuple[str, int]) -> str:
    """Render a loopback endpoint consistently in job evidence."""

    host, port = endpoint
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _safe_process_pid(process: object) -> int | None:
    """Return a process object's PID without letting evidence generation fail.

    The orchestrator deliberately accepts small process doubles for dry-runs
    and integration tests.  Those doubles do not always expose the complete
    :class:`subprocess.Popen` surface (and a property can itself raise while
    a process is being torn down).  Process evidence is advisory, so an
    unavailable or malformed PID is represented as ``None`` rather than
    aborting the lifecycle result.
    """

    if process is None:
        return None
    try:
        value = getattr(process, "pid", None)
    except Exception:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        # Avoid silently truncating a non-integral float (``int(1.5)``) while
        # still accepting string/numpy-style integer values from test doubles.
        if isinstance(value, float) and not value.is_integer():
            return None
        result = int(value)
    except Exception:
        return None
    return result if result >= 0 else None


def _safe_process_returncode(value: object) -> int | None:
    """Normalize one Popen-like return-code value for JSON evidence."""

    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    except Exception:
        return None


def _safe_process_poll(process: object) -> int | None:
    """Poll a Popen-like object defensively and return a normalized code.

    ``Popen.poll()`` returns ``None`` while a process is running.  If a test
    double omits ``poll`` or raises during shutdown, fall back to its public
    ``returncode`` attribute.  The fallback keeps result serialization and
    cleanup compatible with lightweight fakes without treating arbitrary
    malformed values as a successful exit.
    """

    if process is None:
        return None
    poll = None
    try:
        poll = getattr(process, "poll", None)
    except Exception:
        poll = None
    if callable(poll):
        try:
            observed = poll()
        except Exception:
            observed = None
        else:
            # A genuine ``None`` is the running sentinel.  For a malformed
            # poll result, consult ``returncode`` below before giving up.
            if observed is None:
                return None
            normalized = _safe_process_returncode(observed)
            if normalized is not None:
                return normalized
    try:
        fallback = getattr(process, "returncode", None)
    except Exception:
        return None
    return _safe_process_returncode(fallback)


def _ensure_qgc_option(values: list[str], option: str, expected: str) -> None:
    """Require one isolated-HIL option to match the router endpoint.

    A caller-owned QGC command may use either ``--name value`` or
    ``--name=value`` spelling.  Silently preserving a conflicting value would
    make QGC bind a different socket than the evidence/router topology, so
    reject conflicts and duplicate declarations before any process starts.
    """

    occurrences: list[tuple[int, str, int]] = []
    index = 0
    while index < len(values):
        token = values[index]
        if token == option:
            if index + 1 >= len(values) or values[index + 1].startswith("--"):
                raise SimulatorOrchestratorError(f"QGC option {option} requires a value")
            occurrences.append((index, values[index + 1], 2))
            index += 2
            continue
        prefix = option + "="
        if token.startswith(prefix):
            occurrences.append((index, token[len(prefix) :], 1))
        index += 1
    if not occurrences:
        values.extend((option, expected))
        return
    if len(occurrences) != 1 or occurrences[0][1] != expected:
        observed = [item[1] for item in occurrences]
        raise SimulatorOrchestratorError(
            f"QGC option {option} conflicts with SIM router endpoint: "
            f"expected {expected}, observed {observed}"
        )


# Keep the same two accepted IPv6 spellings as the adapter and interactive
# router.  Stack planning must not reject the bracketed form emitted by
# ``SimulatorStackPorts.px4_control_endpoint``.
_LOOPBACK_UDP_OUT = re.compile(
    r"^udpout:(?:(?P<ipv4_host>127\.0\.0\.1):(?P<ipv4_port>\d{1,5})|"
    r"(?P<ipv6_host>\[::1\]|::1):(?P<ipv6_port>\d{1,5}))$"
)


def _parse_loopback_udpout(endpoint: str) -> tuple[str, int]:
    """Parse the adapter's literal loopback ``udpout`` endpoint."""

    match = _LOOPBACK_UDP_OUT.fullmatch(endpoint)
    if match is None:
        raise SimulatorOrchestratorError(
            "SIM control endpoint must be literal udpout:127.0.0.1:<port> or udpout:::1:<port>"
        )
    host = match.group("ipv4_host") or match.group("ipv6_host")
    # Strip URI brackets so callers can compare the result with socket hosts
    # and ``SimulatorStackPorts.host`` without a second normalization step.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    port = int(match.group("ipv4_port") or match.group("ipv6_port"))
    if not 1024 <= port <= 65535:
        raise SimulatorOrchestratorError("SIM stack ports must be in 1024..65535")
    return host, port


def _infer_repository_root(scenario_path: Path) -> Path | None:
    """Prefer the checkout containing the supplied scenario's SIM assets."""

    for candidate in (scenario_path.parent, *scenario_path.parents):
        if (candidate / "simulation" / "gazebo").is_dir():
            return candidate
    return None


def _infer_px4_workdir(plan: SimulatorStackPlan) -> Path | None:
    """Resolve the working directory required by PX4's relative ``rcS`` path.

    PX4 SITL is normally launched from its build directory while the
    executable itself lives one level below it (``<build>/bin/px4``).  The
    planner exposes an explicit ``PX4_WORKDIR`` when the caller supplied one;
    otherwise use that conventional absolute-executable layout or a PATH
    lookup.  Returning ``None`` keeps dry-run/custom command compatibility,
    while real launches with a bare unresolved ``px4`` still retain the
    caller's job directory and produce the native PX4 error in its log.
    """

    environment = plan.environment_mapping
    configured = environment.get("PX4_WORKDIR")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    executable = Path(plan.px4_executable).expanduser()
    if not executable.is_absolute():
        resolved = shutil.which(plan.px4_executable)
        if resolved:
            executable = Path(resolved)
    if not executable.is_absolute() or not executable.is_file():
        return None
    parent = executable.parent
    # PX4's standard layout is <workdir>/bin/px4.  Do not blindly walk two
    # levels for arbitrary names; the parent directory is the least
    # surprising fallback for a custom absolute binary.
    if parent.name.lower() == "bin" and parent.parent.is_dir():
        return parent.parent.resolve()
    return parent.resolve()


def _ensure_udp_port_available(host: str, port: int) -> None:
    """Check the exact UDP transport used by the SIM control gateway."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    # A just-closed UDP socket can remain briefly reserved by Winsock (and
    # endpoint-security/network-filter drivers can transiently hold the same
    # tuple).  Probe a few times instead of turning that harmless race into a
    # flaky orchestrator failure.  A genuinely occupied endpoint still fails
    # after the bounded retry window; we never silently select another port.
    last_error: OSError | None = None
    for attempt in range(4):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind((host, int(port)))
            return
        except OSError as exc:
            last_error = exc
            if attempt < 3:
                threading.Event().wait(0.015 * (attempt + 1))
    assert last_error is not None  # the loop executes at least once
    raise SimulatorOrchestratorError(
        f"SIM UDP port {host}:{port} is unavailable: {last_error}"
    ) from last_error


__all__ = [
    "SimulatorOrchestrator",
    "SimulatorProcessSpec",
    "SimulatorRunResult",
    "SimulatorOrchestratorError",
]

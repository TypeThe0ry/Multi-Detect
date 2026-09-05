"""Deterministic closed-loop SIM fixture for manual and AI flight.

This module is the small executable bridge between the bounded flight model
and the production SIM control stack.  It is deliberately *not* a second
operator UI and it does not bypass :class:`SimulatorMavlinkControlAdapter`:

``telemetry -> AI policy -> adapter -> MAVLink sink -> flight model``

Manual commands use the same adapter and sink.  The sink only translates the
wire-level ``MANUAL_CONTROL`` tuple back into a normalized setpoint and then
lets the model advance.  Consequently tests can exercise a game-like,
interactive vehicle without importing Gazebo, while a real interactive job
uses the exact same authority/adapter contract to send to PX4 SITL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import threading
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

from ..autonomy.contracts import ExecutableMission
from ..clock import ManualClock
from ..mission_progress import (
    MissionProgressConfig,
    MissionProgressSnapshot,
    MissionProgressTracker,
)
from ..runtime import RuntimeDescriptor, RuntimeMode
from .ai_executor import (
    SimulatorAiDecision,
    SimulatorAiExecutor,
    SimulatorAiExecutorConfig,
    SimulatorTelemetrySnapshot,
)
from .control import ControlAuthorityState, SimulatorControlAuthority
from .flight_model import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_MISSION_START,
    DeterministicFlightModel,
    FlightControlSetpoint,
    FlightMode,
    FlightModelConfig,
    FlightModelError,
    FlightTelemetrySnapshot,
)
from .mavlink_control import (
    SimulatorControlReceipt,
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
)


class SimulatorClosedLoopError(RuntimeError):
    """Raised when a closed-loop fixture is used after shutdown or invalidly."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise SimulatorClosedLoopError(f"{name} must be finite numeric")
    return float(value)


class SimulatorFlightModelMav:
    """MAVLink ``mav``-shaped sink backed by one deterministic flight model.

    ``pymavlink`` calls ``manual_control_send`` with integer wire values.  The
    sink validates the same target and axis bounds as the real adapter, keeps
    an audit trail, and stores the latest normalized command for the next
    model tick.  It never exposes model state to the AI policy.
    """

    def __init__(
        self,
        model: DeterministicFlightModel,
        *,
        throttle_wire_mode: str = "qgc_legacy",
        target_system: int = 1,
    ) -> None:
        if not isinstance(model, DeterministicFlightModel):
            raise TypeError("model must be a DeterministicFlightModel")
        if throttle_wire_mode not in {"qgc_legacy", "signed"}:
            raise ValueError("throttle_wire_mode must be qgc_legacy or signed")
        if (
            isinstance(target_system, bool)
            or not isinstance(target_system, int)
            or not 1 <= target_system <= 255
        ):
            raise ValueError("target_system must be an integer in [1, 255]")
        self.model = model
        self.throttle_wire_mode = throttle_wire_mode
        self.target_system = target_system
        self._lock = threading.RLock()
        self._closed = False
        self._last_setpoint = FlightControlSetpoint(source="neutral")
        self.messages: list[tuple[int, int, int, int, int, int]] = []
        self.command_messages: list[dict[str, object]] = []

    @property
    def last_setpoint(self) -> FlightControlSetpoint:
        with self._lock:
            return self._last_setpoint

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def manual_control_send(
        self,
        target: int,
        x: int,
        y: int,
        z: int,
        r: int,
        buttons: int,
    ) -> None:
        with self._lock:
            if self._closed:
                raise SimulatorClosedLoopError("flight-model MAVLink sink is closed")
            values = (target, x, y, z, r, buttons)
            if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
                raise SimulatorClosedLoopError("MANUAL_CONTROL fields must be integers")
            if target != self.target_system:
                raise SimulatorClosedLoopError("MANUAL_CONTROL target_system mismatch")
            if any(abs(value) > 1000 for value in (x, y, z, r)):
                raise SimulatorClosedLoopError("MANUAL_CONTROL axis outside [-1000, 1000]")
            if not 0 <= buttons <= 0xFFFF:
                raise SimulatorClosedLoopError("MANUAL_CONTROL buttons outside uint16")
            if self.throttle_wire_mode == "qgc_legacy":
                if z < 0:
                    raise SimulatorClosedLoopError("qgc_legacy throttle cannot be negative")
                throttle = z / 1000.0
            else:
                throttle = (z + 1000) / 2000.0
            # Adapter mapping is x=pitch, y=roll, r=yaw.
            self._last_setpoint = FlightControlSetpoint(
                roll=y / 1000.0,
                pitch=x / 1000.0,
                yaw=r / 1000.0,
                throttle=throttle,
                source="mavlink",
            )
            self.messages.append((target, x, y, z, r, buttons))

    def command_long_send(
        self,
        target_system: int,
        target_component: int,
        command: int,
        confirmation: int = 0,
        param1: float = 0.0,
        param2: float = 0.0,
        param3: float = 0.0,
        param4: float = 0.0,
        param5: float = 0.0,
        param6: float = 0.0,
        param7: float = 0.0,
    ) -> None:
        """Apply a lifecycle ``COMMAND_LONG`` to the local model.

        The signature mirrors pymavlink's generated ``mav`` method.  Unknown
        commands are recorded as rejected by the model and never mutate
        vehicle state (fail-closed).
        """

        with self._lock:
            self._require_open()
            self._validate_target(target_system, target_component)
            if isinstance(command, bool) or not isinstance(command, int):
                raise SimulatorClosedLoopError("COMMAND_LONG command must be an integer")
            if isinstance(confirmation, bool) or not isinstance(confirmation, int):
                raise SimulatorClosedLoopError("COMMAND_LONG confirmation must be an integer")
            params = (param1, param2, param3, param4, param5, param6, param7)
            if any(
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
                for value in params
            ):
                raise SimulatorClosedLoopError("COMMAND_LONG parameters must be finite numbers")
            accepted = self.model.command_long(command, *params)
            self.command_messages.append(
                {
                    "target_system": target_system,
                    "target_component": target_component,
                    "command": command,
                    "confirmation": confirmation,
                    "accepted": accepted,
                    "params": [float(value) for value in params],
                }
            )

    def set_mode_send(self, target_system: int, base_mode: int, custom_mode: int) -> None:
        """Apply a SET_MODE-like message to the local model."""

        with self._lock:
            self._require_open()
            self._validate_target(target_system, 0)
            if (
                isinstance(base_mode, bool)
                or not isinstance(base_mode, int)
                or isinstance(custom_mode, bool)
                or not isinstance(custom_mode, int)
            ):
                raise SimulatorClosedLoopError("SET_MODE fields must be integers")
            accepted = self.model.set_mode(custom_mode, reason="SET_MODE")
            self.command_messages.append(
                {
                    "target_system": target_system,
                    "target_component": 0,
                    "command": "SET_MODE",
                    "base_mode": base_mode,
                    "custom_mode": custom_mode,
                    "accepted": accepted,
                }
            )

    def _validate_target(self, target_system: int, target_component: int) -> None:
        if (
            isinstance(target_system, bool)
            or not isinstance(target_system, int)
            or target_system != self.target_system
        ):
            raise SimulatorClosedLoopError("MAVLink target_system mismatch")
        if (
            isinstance(target_component, bool)
            or not isinstance(target_component, int)
            or not 0 <= target_component <= 255
        ):
            raise SimulatorClosedLoopError("MAVLink target_component outside [0, 255]")

    def _require_open(self) -> None:
        if self._closed:
            raise SimulatorClosedLoopError("flight-model MAVLink sink is closed")

    def evidence(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": 1,
                "transport": "in_process_mavlink_sink",
                "target_system": self.target_system,
                "throttle_wire_mode": self.throttle_wire_mode,
                "messages": len(self.messages),
                "last_message": None if not self.messages else list(self.messages[-1]),
                "last_setpoint": {
                    "source": self._last_setpoint.source,
                    "roll": self._last_setpoint.roll,
                    "pitch": self._last_setpoint.pitch,
                    "yaw": self._last_setpoint.yaw,
                    "throttle": self._last_setpoint.throttle,
                },
                "command_messages": list(self.command_messages),
                "armed": self.model.armed,
                "link_healthy": self.model.link_healthy,
                "flight_mode": self.model.flight_mode,
                "closed": self._closed,
                "truth_free": True,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True


class SimulatorFlightModelConnection:
    """Connection-shaped wrapper consumed by ``SimulatorMavlinkControlAdapter``."""

    def __init__(
        self,
        model: DeterministicFlightModel,
        *,
        throttle_wire_mode: str = "qgc_legacy",
        target_system: int = 1,
    ) -> None:
        self.model = model
        self.mav = SimulatorFlightModelMav(
            model,
            throttle_wire_mode=throttle_wire_mode,
            target_system=target_system,
        )

    @property
    def closed(self) -> bool:
        return self.mav.closed

    def advance(self, dt_s: Real) -> FlightTelemetrySnapshot:
        if self.closed:
            raise SimulatorClosedLoopError("flight-model connection is closed")
        delta = _finite(dt_s, "dt_s")
        if delta <= 0.0:
            raise SimulatorClosedLoopError("dt_s must be greater than zero")
        try:
            return self.model.step(self.mav.last_setpoint, delta)
        except FlightModelError as exc:
            raise SimulatorClosedLoopError(str(exc)) from exc

    def evidence(self) -> dict[str, object]:
        return self.mav.evidence()

    def close(self) -> None:
        self.mav.close()

    def command_long(self, command: int, *params: Real) -> bool:
        """Connection-shaped lifecycle command helper for tests/integrations."""

        values = tuple(params) + (0.0,) * (7 - len(params))
        if len(params) > 7:
            raise SimulatorClosedLoopError("COMMAND_LONG accepts at most seven parameters")
        self.mav.command_long_send(
            self.mav.target_system,
            1,
            command,
            0,
            *values,
        )
        return bool(self.model.command_history[-1].get("accepted", False))

    def set_mode(self, mode: str | int) -> bool:
        if isinstance(mode, bool) or not isinstance(mode, (str, int)):
            return False
        if isinstance(mode, str):
            # The generated MAVLink method carries an integer custom mode;
            # string mode names use the direct model helper for convenience.
            return self.model.set_mode(mode, reason="SET_MODE")
        self.mav.set_mode_send(self.mav.target_system, 0, mode)
        return bool(self.model.command_history[-1].get("accepted", False))

    def arm(self) -> bool:
        return self.command_long(MAV_CMD_COMPONENT_ARM_DISARM, 1.0)

    def disarm(self) -> bool:
        return self.command_long(MAV_CMD_COMPONENT_ARM_DISARM, 0.0)


@dataclass(frozen=True, slots=True)
class SimulatorClosedLoopStep:
    """One deterministic control/physics tick and its observable evidence."""

    index: int
    dt_s: float
    telemetry_before: FlightTelemetrySnapshot
    telemetry_after: FlightTelemetrySnapshot
    decision: SimulatorAiDecision | None = None
    control_receipt: SimulatorControlReceipt | None = None
    watchdog_receipt: SimulatorControlReceipt | None = None
    mission_progress: MissionProgressSnapshot | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "index": self.index,
            "dt_s": self.dt_s,
            "telemetry_before": self.telemetry_before.to_mapping(),
            "telemetry_after": self.telemetry_after.to_mapping(),
            "decision": None if self.decision is None else self.decision.to_mapping(),
            "control_receipt": _receipt_mapping(self.control_receipt),
            "watchdog_receipt": _receipt_mapping(self.watchdog_receipt),
            "mission_progress": (
                None if self.mission_progress is None else self.mission_progress.to_mapping()
            ),
        }


class SimulatorClosedLoop:
    """Run a game-like deterministic rotorcraft/VTOL/fixed-wing SIM.

    All three airframe modes use the same AI, manual, watchdog and MAVLink
    authority path; only the bounded kinematic model differs at the vehicle
    dynamics boundary.
    """

    def __init__(
        self,
        mode: FlightMode | str = FlightMode.VTOL,
        *,
        seed: int = 0,
        wind_mps: tuple[Real, Real] = (0.0, 0.0),
        dt_s: Real = 0.1,
        model_config: FlightModelConfig | None = None,
        control_config: SimulatorMavlinkControlConfig | None = None,
        ai_config: SimulatorAiExecutorConfig | None = None,
        initial_authority: ControlAuthorityState | str = ControlAuthorityState.PAUSED,
        clock: ManualClock | None = None,
        runtime_descriptor: RuntimeDescriptor | None = None,
        mission: ExecutableMission | None = None,
        mission_progress_config: MissionProgressConfig | None = None,
    ) -> None:
        self.model = DeterministicFlightModel(
            mode,
            seed=seed,
            wind_mps=wind_mps,
            config=model_config,
        )
        self.default_dt_s = _finite(dt_s, "dt_s")
        if self.default_dt_s <= 0.0 or self.default_dt_s > self.model.config.max_dt_s:
            raise SimulatorClosedLoopError(
                f"dt_s must be within (0, {self.model.config.max_dt_s:g}]"
            )
        if clock is not None and not isinstance(clock, ManualClock):
            raise TypeError("clock must be a ManualClock or None")
        self.clock = clock or ManualClock()
        # The flight model starts at simulation time zero.  Requiring the
        # injected clock to start at that same point prevents freshness and
        # watchdog decisions from silently mixing two time domains.
        if not math.isclose(self.clock.monotonic(), self.model.time_s, abs_tol=1e-12):
            raise SimulatorClosedLoopError(
                "clock must start at the model simulation time (0 seconds)"
            )
        if runtime_descriptor is not None:
            if not isinstance(runtime_descriptor, RuntimeDescriptor):
                raise TypeError("runtime_descriptor must be a RuntimeDescriptor or None")
            if runtime_descriptor.mode is not RuntimeMode.SIM:
                raise SimulatorClosedLoopError(
                    "closed-loop simulator requires a SIM runtime descriptor"
                )
            if not runtime_descriptor.deterministic:
                raise SimulatorClosedLoopError(
                    "closed-loop simulator requires a deterministic runtime descriptor"
                )
        self.runtime_descriptor = runtime_descriptor
        if mission is not None and not isinstance(mission, ExecutableMission):
            raise SimulatorClosedLoopError("mission must be an ExecutableMission or None")
        if mission_progress_config is not None and not isinstance(
            mission_progress_config, MissionProgressConfig
        ):
            raise SimulatorClosedLoopError(
                "mission_progress_config must be MissionProgressConfig or None"
            )
        self.mission_tracker = (
            None
            if mission is None
            else MissionProgressTracker(mission, config=mission_progress_config)
        )
        if control_config is not None and not isinstance(
            control_config, SimulatorMavlinkControlConfig
        ):
            raise TypeError("control_config must be SimulatorMavlinkControlConfig")
        selected_control = control_config or SimulatorMavlinkControlConfig()
        self.authority = SimulatorControlAuthority()
        self.connection = SimulatorFlightModelConnection(
            self.model,
            throttle_wire_mode=selected_control.throttle_wire_mode,
            target_system=selected_control.target_system,
        )
        self.adapter = SimulatorMavlinkControlAdapter(
            self.authority,
            config=selected_control,
            connection=self.connection,
        )
        # Use simulation time for every AI timestamp, including shutdown and
        # disable paths.  Falling back to wall-clock monotonic time here would
        # make otherwise identical runs produce different evidence hashes.
        self.ai_executor = SimulatorAiExecutor(
            self.adapter,
            config=ai_config,
            clock=self.clock.monotonic,
        )
        try:
            selected_authority = (
                initial_authority
                if isinstance(initial_authority, ControlAuthorityState)
                else ControlAuthorityState(str(initial_authority).lower())
            )
        except ValueError as exc:
            self.connection.close()
            raise SimulatorClosedLoopError(
                f"invalid initial authority: {initial_authority}"
            ) from exc
        if selected_authority is ControlAuthorityState.MANUAL:
            self.authority.resume_manual()
        elif selected_authority is ControlAuthorityState.AI:
            self.ai_executor.enable_ai()
        elif selected_authority is ControlAuthorityState.FAILSAFE:
            self.authority.failsafe("initial authority")
        elif selected_authority is not ControlAuthorityState.PAUSED:
            self.connection.close()
            raise SimulatorClosedLoopError("unsupported initial authority")
        self._lock = threading.RLock()
        self._closed = False
        self._step_index = 0
        self._history: list[SimulatorClosedLoopStep] = []
        self._last_control_receipt: SimulatorControlReceipt | None = None
        # Match the live bridge lifecycle gate: an AI opt-in while the model
        # is disarmed or link state is unknown remains paused until explicit
        # ARM and a positively healthy link observation arrive.
        self._refresh_lifecycle_gate()

    @property
    def telemetry(self) -> FlightTelemetrySnapshot:
        return self.model.telemetry

    @property
    def time_s(self) -> float:
        return self.clock.monotonic()

    @property
    def history(self) -> tuple[SimulatorClosedLoopStep, ...]:
        with self._lock:
            return tuple(self._history)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def enable_ai(self) -> None:
        with self._lock:
            self._require_open()
            self.ai_executor.enable_ai()
            self._refresh_lifecycle_gate()

    @property
    def armed(self) -> bool:
        return self.model.armed

    @property
    def ai_ready(self) -> bool:
        """Whether lifecycle observations satisfy the AI startup gate."""

        return self.model.armed is True and self.model.link_healthy is True

    @property
    def mission_progress(self) -> MissionProgressSnapshot | None:
        """Latest observation-only progress snapshot, when a mission is bound."""

        with self._lock:
            return None if self.mission_tracker is None else self.mission_tracker.snapshot

    def bind_mission(
        self,
        mission: ExecutableMission,
        *,
        config: MissionProgressConfig | None = None,
    ) -> None:
        """Bind a compiled mission to the shared progress/evidence path.

        Binding is intentionally separate from mission upload/start.  It only
        installs the hash-bound observer; the existing PX4 transaction and
        authority gates remain the only paths that can start a vehicle.
        """

        with self._lock:
            self._require_open()
            try:
                self.mission_tracker = MissionProgressTracker(mission, config=config)
            except (TypeError, ValueError) as exc:
                raise SimulatorClosedLoopError(str(exc)) from exc

    def clear_mission(self) -> None:
        """Remove the optional progress observer without touching flight state."""

        with self._lock:
            self._require_open()
            self.mission_tracker = None

    def start_mission(self) -> bool:
        """Send the bounded ``MISSION_START`` lifecycle command."""

        return self.command_long(MAV_CMD_MISSION_START)

    def command_long(self, command: int, *params: Real) -> bool:
        """Send a lifecycle command through the in-process MAV sink."""

        with self._lock:
            self._require_open()
            accepted = self.connection.command_long(command, *params)
            self._refresh_lifecycle_gate()
            if accepted and command == MAV_CMD_MISSION_START and self.mission_tracker is not None:
                self.mission_tracker.start()
            # A disarm while AI is active is a hard safety transition.  Emit
            # one neutral frame and keep authority in FAILSAFE until the
            # caller explicitly re-enables it.
            if command == MAV_CMD_COMPONENT_ARM_DISARM and params:
                value = float(params[0])
                if math.isclose(value, 0.0, abs_tol=1e-6) and self.ai_executor.enabled:
                    if self.authority.state is not ControlAuthorityState.FAILSAFE:
                        try:
                            receipt = self.adapter.request_failsafe(
                                "vehicle_disarmed", timestamp_s=self._synchronized_time()
                            )
                            self._last_control_receipt = receipt
                        except (RuntimeError, ValueError, TypeError):
                            self.authority.failsafe("vehicle_disarmed")
            return accepted

    def arm(self) -> bool:
        return self.command_long(MAV_CMD_COMPONENT_ARM_DISARM, 1.0)

    def disarm(self) -> bool:
        return self.command_long(MAV_CMD_COMPONENT_ARM_DISARM, 0.0)

    def set_mode(self, mode: str | int) -> bool:
        with self._lock:
            self._require_open()
            accepted = self.connection.set_mode(mode)
            self._refresh_lifecycle_gate()
            return accepted

    def set_link_healthy(self, value: bool | None) -> None:
        with self._lock:
            self._require_open()
            self.model.set_link_healthy(value)
            self._refresh_lifecycle_gate()

    def disable_ai(self) -> None:
        with self._lock:
            self._require_open()
            self.ai_executor.disable_ai()

    def release_manual_control(self) -> None:
        with self._lock:
            self._require_open()
            self.ai_executor.release_manual_control()

    def take_manual_control(self) -> None:
        with self._lock:
            self._require_open()
            self.ai_executor.take_manual_control()

    def submit_manual(
        self,
        *,
        roll: Real,
        pitch: Real,
        yaw: Real,
        throttle: Real,
    ) -> SimulatorControlReceipt:
        """Submit a pilot command through the same adapter as QGC."""

        with self._lock:
            self._require_open()
            receipt = self.adapter.submit_manual(
                roll=roll,
                pitch=pitch,
                yaw=yaw,
                throttle=throttle,
                timestamp_s=self._synchronized_time(),
            )
            self._last_control_receipt = receipt
            return receipt

    manual_control = submit_manual

    def step(
        self,
        dt_s: Real | None = None,
        *,
        observation: SimulatorTelemetrySnapshot | None = None,
    ) -> SimulatorClosedLoopStep:
        """Evaluate AI (when enabled), apply watchdog, and advance physics once.

        ``observation`` is an optional sensor-side replacement for the model's
        current snapshot.  It is useful for feeding a detector/ranging result
        (for example a qualified target bearing) into the exact same AI path;
        it is still required to be the truth-free telemetry contract and never
        exposes a scenario truth object to the executor.
        """

        with self._lock:
            self._require_open()
            delta = self.default_dt_s if dt_s is None else _finite(dt_s, "dt_s")
            if delta <= 0.0 or delta > self.model.config.max_dt_s:
                raise SimulatorClosedLoopError(
                    f"dt_s must be within (0, {self.model.config.max_dt_s:g}]"
                )
            current_time_s = self._synchronized_time()
            before = self.model.telemetry
            # A router/QGC integration may call the generated MAV sink
            # directly; refresh the gate so an observed ARM command is enough
            # to release a previously enabled AI stream.
            self._refresh_lifecycle_gate()
            if observation is not None and not isinstance(observation, SimulatorTelemetrySnapshot):
                raise TypeError("observation must be SimulatorTelemetrySnapshot or None")
            decision: SimulatorAiDecision | None = None
            if self.ai_executor.enabled:
                decision = self.ai_executor.step(
                    before if observation is None else observation,
                    now_s=current_time_s,
                )
                if decision.receipt is not None:
                    self._last_control_receipt = decision.receipt
            # Check against the end of this tick before applying the stored
            # command.  A long scheduler stall therefore produces neutral
            # output for the whole next integration interval.
            watchdog = self.adapter.check_watchdog(now_s=current_time_s + float(delta))
            if watchdog is not None:
                self._last_control_receipt = watchdog
            after = self.connection.advance(delta)
            # Advance the same injected clock only after the physics step has
            # succeeded.  Every timestamp observed by AI, watchdog, telemetry
            # and evidence is therefore derived from one monotonic source.
            self.clock.advance(float(delta))
            if not math.isclose(self.clock.monotonic(), after.observed_at_s, abs_tol=1e-12):
                raise SimulatorClosedLoopError(
                    "shared clock diverged from flight-model simulation time"
                )
            mission_progress = (
                None
                if self.mission_tracker is None
                else self.mission_tracker.update(after, now_s=self.clock.monotonic())
            )
            # A QGC frame can enter through ``SimulatorInteractiveRouter``
            # and call the shared adapter directly (rather than the
            # convenience ``submit_manual`` method on this closed-loop
            # wrapper).  Refresh the observable receipt from the adapter so
            # the step evidence names the actual manual/AI/watchdog frame
            # applied to the flight-model sink.
            latest_receipt = self.adapter.last_receipt
            if latest_receipt is not None:
                self._last_control_receipt = latest_receipt
            result = SimulatorClosedLoopStep(
                index=self._step_index,
                dt_s=float(delta),
                telemetry_before=before,
                telemetry_after=after,
                decision=decision,
                control_receipt=self._last_control_receipt,
                watchdog_receipt=watchdog,
                mission_progress=mission_progress,
            )
            self._step_index += 1
            self._history.append(result)
            return result

    tick = step

    def step_with_observation(
        self,
        observation: SimulatorTelemetrySnapshot,
        dt_s: Real | None = None,
    ) -> SimulatorClosedLoopStep:
        """Named form of :meth:`step` for sensor-bridge integrations."""

        return self.step(dt_s, observation=observation)

    def run(self, steps: int, *, dt_s: Real | None = None) -> tuple[SimulatorClosedLoopStep, ...]:
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise SimulatorClosedLoopError("steps must be a positive integer")
        return tuple(self.step(dt_s) for _ in range(steps))

    def evidence(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": 1,
                "event": "simulator_closed_loop_evidence",
                "mode": self.model.mode.value,
                "seed": self.model.seed,
                "time_s": self._synchronized_time(),
                "clock": {
                    "kind": "manual",
                    "monotonic_s": self.clock.monotonic(),
                    "utc_now": self.clock.utc_now().isoformat(),
                    "shared_with_runtime": True,
                },
                "runtime_descriptor": (
                    None
                    if self.runtime_descriptor is None
                    else self.runtime_descriptor.to_mapping()
                ),
                "runtime_descriptor_hash": (
                    None
                    if self.runtime_descriptor is None
                    else self.runtime_descriptor.canonical_hash
                ),
                "step_count": self._step_index,
                "authority": {
                    "state": self.authority.state.value,
                    "ai_enabled": self.authority.ai_enabled,
                    "manual_override": self.authority.manual_override,
                    "events": list(self.authority.events),
                },
                "lifecycle_gate": {
                    "armed": self.model.armed,
                    "link_healthy": self.model.link_healthy,
                    "ai_ready": self.ai_ready,
                    "status": ("released" if self.ai_ready else "waiting_for_armed_and_link"),
                },
                "control": self.adapter.evidence(),
                "transport": self.connection.evidence(),
                "ai": self.ai_executor.evidence(),
                "mission_progress": (
                    None
                    if self.mission_tracker is None or self.mission_tracker.snapshot is None
                    else {
                        **self.mission_tracker.snapshot.to_mapping(),
                        "sha256": self.mission_tracker.snapshot.canonical_hash,
                    }
                ),
                "flight_model": self.model.evidence_with_hash(),
                "last_step": None if not self._history else self._history[-1].to_mapping(),
                "truth_free": True,
                "closed": self._closed,
            }

    @property
    def evidence_hash(self) -> str:
        payload = json.dumps(
            self.evidence(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def evidence_with_hash(self) -> dict[str, object]:
        result = self.evidence()
        result["sha256"] = self.evidence_hash
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # The executor owns the AI watchdog/neutral transition.  This is
            # idempotent and does not alter a manual-only stream.
            self.ai_executor.stop()
            self.adapter.close()
            self._closed = True

    def _refresh_lifecycle_gate(self) -> None:
        """Pause AI until both armed and link-health are positively observed."""

        if not self.ai_executor.enabled:
            return
        if self.ai_ready:
            if (
                self.authority.state is ControlAuthorityState.PAUSED
                and not self.authority.manual_override
            ):
                try:
                    self.ai_executor.release_manual_control()
                except (RuntimeError, ValueError, TypeError):
                    pass
            return
        if self.authority.state is ControlAuthorityState.AI:
            self.authority.pause()

    def _require_open(self) -> None:
        if self._closed:
            raise SimulatorClosedLoopError("simulator closed loop is closed")

    def _synchronized_time(self) -> float:
        """Return the shared clock value and fail closed on clock drift."""

        clock_time = self.clock.monotonic()
        if not math.isclose(clock_time, self.model.time_s, abs_tol=1e-12):
            raise SimulatorClosedLoopError(
                "shared clock diverged from flight-model simulation time"
            )
        return clock_time

    def __enter__(self) -> SimulatorClosedLoop:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()


def _receipt_mapping(receipt: SimulatorControlReceipt | None) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {
        "source": receipt.source,
        "state": receipt.state,
        "endpoint": receipt.endpoint,
        "target_system": receipt.target_system,
        "x": receipt.x,
        "y": receipt.y,
        "z": receipt.z,
        "r": receipt.r,
        "timestamp_s": receipt.timestamp_s,
    }


__all__ = [
    "SimulatorClosedLoop",
    "SimulatorClosedLoopError",
    "SimulatorClosedLoopStep",
    "SimulatorFlightModelConnection",
    "SimulatorFlightModelMav",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    """Run the no-external-I/O closed-loop fixture from the command line."""

    parser = argparse.ArgumentParser(description="Run the deterministic SIM closed-loop fixture")
    parser.add_argument("--mode", choices=("vtol", "fixed-wing", "multirotor"), default="vtol")
    parser.add_argument("--seed", type=int, default=42017)
    parser.add_argument("--steps", type=int, default=60)
    parser.add_argument("--dt", type=float, default=0.1)
    parser.add_argument("--wind-east", dest="wind_east", type=float, default=0.0)
    parser.add_argument("--wind-north", dest="wind_north", type=float, default=0.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        sim = SimulatorClosedLoop(
            args.mode,
            seed=args.seed,
            wind_mps=(args.wind_east, args.wind_north),
            dt_s=args.dt,
        )
        try:
            sim.arm()
            sim.enable_ai()
            sim.run(args.steps)
            result = sim.evidence_with_hash()
        finally:
            sim.close()
    except (SimulatorClosedLoopError, FlightModelError, ValueError) as exc:
        print(json.dumps({"event": "simulator_closed_loop_failed", "error": str(exc)}))
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

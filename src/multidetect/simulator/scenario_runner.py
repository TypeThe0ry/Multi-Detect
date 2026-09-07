"""Manifest-driven deterministic SIM scenario runner.

The process orchestrator in :mod:`multidetect.simulator.orchestrator` owns
PX4/Gazebo/QGC lifecycles.  This module owns the *software* scenario loop used
by CI and by a headless pre-flight check: it loads only the runtime channel of
a scenario manifest, drives the deterministic multirotor/VTOL/fixed-wing model, applies
declared sensor faults to observations, and sends those observations through
the same AI/manual/authority/MAVLink path as an interactive job.

The evaluator-only ``truth.json`` channel is intentionally never loaded here.
The runner therefore cannot accidentally turn a target ground-truth pose into
an AI observation.  A caller may provide an observation provider (for example
the production detector/ranging bridge); the provider receives measured
telemetry only and must return :class:`SimulatorTelemetrySnapshot`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from numbers import Real
from pathlib import Path
from typing import TYPE_CHECKING

from ..autonomy.contracts import ExecutableMission
from ..autonomy.serialization import canonical_sha256
from ..clock import ManualClock
from ..mission_progress import MissionProgressConfig, MissionProgressSnapshot
from ..product_version import load_product_version
from ..runtime import (
    RuntimeDescriptor,
    RuntimeMode,
    RuntimeProfile,
    RuntimeRunReport,
    write_runtime_run_report,
)
from ..simulation import FaultEvent, FaultKind, ScenarioRuntimeInput, load_runtime_scenario
from .ai_executor import SimulatorAiExecutorConfig, SimulatorTelemetrySnapshot
from .closed_loop import SimulatorClosedLoop, SimulatorClosedLoopStep
from .control import ControlAuthorityState
from .flight_model import (
    FlightControlSetpoint,
    FlightMode,
    FlightModelConfig,
    FlightTelemetrySnapshot,
)
from .mavlink_control import SimulatorMavlinkControlConfig
from .stack import (
    DEFAULT_REPOSITORY_ROOT,
    SimulatorStackError,
    parse_builtin_world_uri,
    read_world_coordinates,
)

if TYPE_CHECKING:
    from .scene_state import SimulatorSceneSnapshot, SimulatorSceneStream


class SimulatorScenarioRunnerError(RuntimeError):
    """Raised when a scenario cannot be run under the SIM contract."""


ObservationProvider = Callable[[FlightTelemetrySnapshot], SimulatorTelemetrySnapshot]
ManualProvider = Callable[
    [int, FlightTelemetrySnapshot],
    FlightControlSetpoint | Mapping[str, object] | None,
]


@dataclass(frozen=True, slots=True)
class SimulatorScenarioRunnerConfig:
    """Deterministic loop settings and optional fault/authority controls."""

    frames: int = 600
    dt_s: float = 0.1
    mode: FlightMode | str | None = None
    enable_ai: bool = True
    # Scenario fixtures may issue a deterministic ARM command at run start,
    # mirroring the operator action in an interactive QGC session.  The
    # command applies to both manual-only and AI runs; callers that want to
    # exercise the disarmed gate can set this to ``False``.  The underlying
    # flight model still constructs disarmed and remains so until this
    # explicit command is sent.
    arm_on_start: bool = True
    initial_authority: ControlAuthorityState | str = ControlAuthorityState.PAUSED
    active_fault_ids: tuple[str, ...] | None = None
    # Built-in Gazebo worlds carry the authoritative WGS-84 origin.  Resolve
    # it automatically when the legacy 0/0 defaults are left in place so the
    # deterministic fallback and PX4/Gazebo share one geospatial frame.  Set
    # this to False for an intentional local/Null-Island fixture, or provide
    # non-zero coordinates as an explicit override.
    use_world_origin: bool = True
    initial_latitude_deg: float = 0.0
    initial_longitude_deg: float = 0.0
    initial_altitude_m: float = 10.0
    initial_speed_mps: float = 0.0
    history_limit: int = 600
    # Scene recording is opt-in so historical runner/matrix evidence hashes
    # remain stable.  When enabled, the same steps additionally produce a
    # hash-bound, truth-free 3-D replay stream for a renderer or replayer.
    record_scene_stream: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.frames, bool)
            or not isinstance(self.frames, int)
            or not 1 <= self.frames <= 1_000_000
        ):
            raise ValueError("frames must be an integer in [1, 1000000]")
        _finite_positive(self.dt_s, "dt_s")
        if self.mode is not None:
            FlightMode.parse(self.mode)
        if not isinstance(self.enable_ai, bool):
            raise ValueError("enable_ai must be bool")
        if not isinstance(self.arm_on_start, bool):
            raise ValueError("arm_on_start must be bool")
        if not isinstance(self.use_world_origin, bool):
            raise ValueError("use_world_origin must be bool")
        if not isinstance(self.record_scene_stream, bool):
            raise ValueError("record_scene_stream must be bool")
        if not isinstance(self.initial_authority, (ControlAuthorityState, str)):
            raise ValueError("initial_authority must be a ControlAuthorityState or string")
        try:
            ControlAuthorityState(
                self.initial_authority
                if isinstance(self.initial_authority, str)
                else self.initial_authority.value
            )
        except ValueError as exc:
            raise ValueError("initial_authority is invalid") from exc
        if self.active_fault_ids is not None:
            if isinstance(self.active_fault_ids, (str, bytes, bytearray)):
                raise ValueError("active_fault_ids must be a sequence of IDs")
            ids = tuple(self.active_fault_ids)
            if any(not isinstance(item, str) or not item.strip() for item in ids):
                raise ValueError("active_fault_ids must contain non-empty strings")
            if len(ids) != len(set(ids)):
                raise ValueError("active_fault_ids must be unique")
            object.__setattr__(self, "active_fault_ids", ids)
        for name in (
            "initial_latitude_deg",
            "initial_longitude_deg",
            "initial_altitude_m",
            "initial_speed_mps",
        ):
            _finite(self.__getattribute__(name), name)
        # Keep the runner's geodetic contract identical to FlightModel and
        # target geolocation.  The exact poles have no stable local tangent
        # plane/longitude direction, so accepting them here would only move
        # the failure later into FlightModelConfig construction.
        if not -90.0 < self.initial_latitude_deg < 90.0:
            raise ValueError("initial_latitude_deg must be within (-90, 90)")
        if not -180.0 <= self.initial_longitude_deg <= 180.0:
            raise ValueError("initial_longitude_deg must be within [-180, 180]")
        if self.initial_altitude_m < 0.0 or self.initial_speed_mps < 0.0:
            raise ValueError("initial altitude/speed cannot be negative")
        if (
            isinstance(self.history_limit, bool)
            or not isinstance(self.history_limit, int)
            or not 1 <= self.history_limit <= 1_000_000
        ):
            raise ValueError("history_limit must be an integer in [1, 1000000]")


@dataclass(frozen=True, slots=True)
class SimulatorScenarioRunResult:
    """Serializable result and evidence for one runtime-channel execution."""

    scenario_id: str
    scenario_version: int
    scenario_manifest_hash: str
    seed: int
    mode: str
    requested_frames: int
    completed_frames: int
    terminal_reason: str
    active_fault_ids: tuple[str, ...]
    fault_events: tuple[dict[str, object], ...]
    steps: tuple[dict[str, object], ...]
    evidence: dict[str, object]
    closed: bool
    runtime_descriptor: RuntimeDescriptor
    scene_stream: SimulatorSceneStream | None = None
    runtime_input_hash: str | None = None

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "event": "simulator_scenario_run_finished",
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "seed": self.seed,
            "mode": self.mode,
            "requested_frames": self.requested_frames,
            "completed_frames": self.completed_frames,
            "terminal_reason": self.terminal_reason,
            "active_fault_ids": list(self.active_fault_ids),
            "fault_events": list(self.fault_events),
            "steps": list(self.steps),
            "evidence": self.evidence,
            "world_origin": self.evidence.get("world_origin"),
            "closed": self.closed,
            "runtime_descriptor": self.runtime_descriptor.to_mapping(),
            "runtime_descriptor_hash": self.runtime_descriptor.canonical_hash,
            "truth_loaded": False,
            "truth_free": True,
        }
        if self.scene_stream is not None:
            result["scene_stream"] = self.scene_stream.to_mapping_with_hash()
        return result

    @property
    def evidence_hash(self) -> str:
        payload = json.dumps(
            self.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def runtime_descriptor_hash(self) -> str:
        """Canonical identity hash for the runtime used by this result."""

        return self.runtime_descriptor.canonical_hash

    @property
    def runtime_run_report(self) -> RuntimeRunReport:
        """Return the shared completion contract for this bounded SIM run.

        A scenario runner is intentionally configured with a finite frame
        budget, so consuming that budget is not evidence that an unbounded
        observation stream naturally exhausted.  The report therefore marks
        the result as a deterministic prefix with ``stream_exhausted=False``
        while retaining the exact runtime-channel hash and processed frame
        identities.  The existing scenario evidence keeps its historical
        ``frame_budget_complete`` terminal reason; this report is the common
        Replay/SIM/HIL/REAL completion contract layered on top of it.
        """

        clock = self.evidence.get("clock")
        if not isinstance(clock, Mapping):
            raise SimulatorScenarioRunnerError(
                "runtime completion report requires shared clock evidence"
            )
        finished_at_s = clock.get("monotonic_s")
        input_hash = self.runtime_input_hash or self.scenario_manifest_hash
        return RuntimeRunReport(
            descriptor_hash=self.runtime_descriptor.canonical_hash,
            input_hash=input_hash,
            processed_frame_ids=tuple(
                f"sim:{self.scenario_id}:{index}" for index in range(self.completed_frames)
            ),
            started_at_s=0.0,
            finished_at_s=finished_at_s,
            stream_exhausted=False,
            max_frames=self.requested_frames,
        )

    def write_runtime_run_report(self, path: str | Path) -> Path:
        """Persist the shared bounded-prefix report next to scenario evidence."""

        return write_runtime_run_report(path, self.runtime_run_report)

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.evidence_hash
        return result


@dataclass(slots=True)
class _FaultRuntime:
    event: FaultEvent
    active_frames: int = 0
    fired: bool = False
    battery_applied: bool = False


class SimulatorScenarioRunner:
    """Run a verified runtime scenario through the deterministic closed loop."""

    def __init__(
        self,
        scenario: str | Path | ScenarioRuntimeInput,
        *,
        config: SimulatorScenarioRunnerConfig | None = None,
        observation_provider: ObservationProvider | None = None,
        manual_provider: ManualProvider | None = None,
        control_config: SimulatorMavlinkControlConfig | None = None,
        ai_config: SimulatorAiExecutorConfig | None = None,
        model_config: FlightModelConfig | None = None,
        mission: ExecutableMission | None = None,
        mission_progress_config: MissionProgressConfig | None = None,
    ) -> None:
        self.config = config or SimulatorScenarioRunnerConfig()
        if not isinstance(self.config, SimulatorScenarioRunnerConfig):
            raise TypeError("config must be SimulatorScenarioRunnerConfig")
        if observation_provider is not None and not callable(observation_provider):
            raise TypeError("observation_provider must be callable")
        if manual_provider is not None and not callable(manual_provider):
            raise TypeError("manual_provider must be callable")
        if mission is not None and not isinstance(mission, ExecutableMission):
            raise TypeError("mission must be an ExecutableMission or None")
        if mission_progress_config is not None and not isinstance(
            mission_progress_config, MissionProgressConfig
        ):
            raise TypeError("mission_progress_config must be MissionProgressConfig or None")
        if mission is None and mission_progress_config is not None:
            raise TypeError("mission_progress_config requires a mission")
        self.observation_provider = observation_provider
        self.manual_provider = manual_provider
        self.mission = mission
        self.scenario_path: Path | None = None
        if isinstance(scenario, ScenarioRuntimeInput):
            self.runtime_input = scenario
        else:
            self.scenario_path = Path(scenario).expanduser().resolve()
            if not self.scenario_path.is_file():
                raise SimulatorScenarioRunnerError(
                    f"scenario manifest not found: {self.scenario_path}"
                )
            try:
                self.runtime_input = load_runtime_scenario(self.scenario_path, mode=RuntimeMode.SIM)
            except Exception as exc:
                raise SimulatorScenarioRunnerError(f"invalid SIM scenario: {exc}") from exc
        self.mode = _infer_mode(self.runtime_input, self.config.mode)
        self._faults = self._select_faults(self.runtime_input.faults)
        self._fault_runtime = [_FaultRuntime(event) for event in self._faults]
        initial_latitude, initial_longitude, self._world_origin_evidence = (
            _select_initial_world_origin(
                self.runtime_input,
                self.config,
                scenario_path=self.scenario_path,
            )
        )
        self.clock = ManualClock()
        self.runtime_descriptor = RuntimeDescriptor(
            mode=RuntimeMode.SIM,
            profile=RuntimeProfile.sim(),
            backend_id="multidetect.simulator.scenario_runner",
            airframe_profile_hash=canonical_sha256(self.runtime_input.vehicle.to_mapping()),
            scenario_id=self.runtime_input.scenario_id,
            scenario_manifest_hash=self.runtime_input.scenario_manifest_hash,
            scenario_seed=self.runtime_input.seed,
            deterministic=True,
            fault_injection_enabled=bool(self._faults),
        )
        selected_model_config = model_config or FlightModelConfig(
            initial_latitude_deg=initial_latitude,
            initial_longitude_deg=initial_longitude,
            initial_altitude_m=self.config.initial_altitude_m,
            initial_speed_mps=self.config.initial_speed_mps,
        )
        if model_config is not None:
            # A fully supplied model config owns the initial coordinates.  Do
            # not claim that the SDF origin was applied when the caller chose
            # a different model state explicitly.
            self._world_origin_evidence = {
                "latitude_deg": selected_model_config.initial_latitude_deg,
                "longitude_deg": selected_model_config.initial_longitude_deg,
                "elevation_m": None,
                "source": "model_config",
                "resolved": False,
                "world_asset": self.runtime_input.world.world_asset,
            }
        self.sim = SimulatorClosedLoop(
            self.mode,
            seed=self.runtime_input.seed,
            wind_mps=(
                self.runtime_input.world.wind_east_mps,
                self.runtime_input.world.wind_north_mps,
            ),
            dt_s=self.config.dt_s,
            model_config=selected_model_config,
            control_config=control_config,
            ai_config=ai_config,
            initial_authority=self.config.initial_authority,
            clock=self.clock,
            runtime_descriptor=self.runtime_descriptor,
            mission=mission,
            mission_progress_config=mission_progress_config,
        )
        self._battery_remaining_pct = 100.0
        self._last_observation: SimulatorTelemetrySnapshot | None = None
        self._result: SimulatorScenarioRunResult | None = None

    @property
    def runtime(self) -> ScenarioRuntimeInput:
        return self.runtime_input

    @property
    def descriptor(self) -> RuntimeDescriptor:
        """Runtime identity bound to this scenario execution."""

        return self.runtime_descriptor

    @property
    def closed(self) -> bool:
        return self.sim.closed

    @property
    def mission_progress(self) -> MissionProgressSnapshot | None:
        """Latest hash-bound observation for the optional compiled mission."""

        return self.sim.mission_progress

    def bind_mission(
        self,
        mission: ExecutableMission,
        *,
        config: MissionProgressConfig | None = None,
    ) -> None:
        """Bind a compiled mission to the runner's shared progress observer."""

        if self._result is not None:
            raise SimulatorScenarioRunnerError("cannot bind a mission after run completion")
        try:
            self.sim.bind_mission(mission, config=config)
        except (TypeError, ValueError) as exc:
            raise SimulatorScenarioRunnerError(str(exc)) from exc
        self.mission = mission

    def clear_mission(self) -> None:
        """Remove the progress observer without changing scenario state."""

        if self._result is not None:
            raise SimulatorScenarioRunnerError("cannot clear a mission after run completion")
        try:
            self.sim.clear_mission()
        except (RuntimeError, ValueError, TypeError) as exc:
            raise SimulatorScenarioRunnerError(str(exc)) from exc
        self.mission = None

    def start_mission(self) -> bool:
        """Send ``MAV_CMD_MISSION_START`` through the same closed-loop sink."""

        if self._result is not None:
            raise SimulatorScenarioRunnerError("cannot start a mission after run completion")
        try:
            return self.sim.start_mission()
        except (RuntimeError, ValueError, TypeError) as exc:
            raise SimulatorScenarioRunnerError(str(exc)) from exc

    def run(self, *, close: bool = True) -> SimulatorScenarioRunResult:
        """Execute the configured frame budget and return canonical evidence."""

        if self._result is not None:
            return self._result
        if self.config.record_scene_stream:
            # Keep ``python -m multidetect.simulator.scene_state`` free of a
            # package-import cycle.  The scene-state types are only needed
            # for the explicit recording path, not for the legacy runner.
            from .scene_state import SimulatorSceneSnapshot, SimulatorSceneStream

        steps: list[dict[str, object]] = []
        scene_snapshots: list[SimulatorSceneSnapshot] = []
        terminal_reason = "frame_budget_complete"
        try:
            # Manual and AI scenarios share the same lifecycle contract.  A
            # manual_provider used to submit setpoints while the model stayed
            # disarmed because arming was nested under ``enable_ai``; the
            # adapter accepted those frames but the physics correctly froze.
            # Issue the explicit fixture ARM once for either control source,
            # while retaining ``arm_on_start=False`` as the disarmed-gate test.
            if self.config.arm_on_start:
                self.sim.arm()
            if self.config.enable_ai:
                self.sim.enable_ai()
            for index in range(self.config.frames):
                before = self.sim.telemetry
                observed = self._observation(before)
                manual = self.manual_provider(index, before) if self.manual_provider else None
                if manual is not None:
                    self._submit_manual(manual)
                step = self.sim.step(observation=observed)
                self._last_observation = observed
                step_mapping = self._step_mapping(step, index=index, observation=observed)
                steps.append(step_mapping)
                if self.config.record_scene_stream:
                    scene_snapshots.append(
                        SimulatorSceneSnapshot.from_step_mapping(
                            step_mapping,
                            sequence=len(scene_snapshots),
                            scenario_id=self.runtime_input.scenario_id,
                            scenario_version=self.runtime_input.scenario_version,
                            scenario_manifest_hash=self.runtime_input.scenario_manifest_hash,
                            mode=self.mode.value,
                            # Read the existing deterministic local NED state
                            # without changing the flight-model API/parity.
                            local_north_m=self.sim.model._state.north_m,
                            local_east_m=self.sim.model._state.east_m,
                            active_fault_ids=self._active_fault_ids_at(
                                step.telemetry_after.observed_at_s
                            ),
                        )
                    )
                if self._terminal_fault_active(before.observed_at_s):
                    terminal_reason = "fault_terminal"
                    break
        except Exception as exc:
            terminal_reason = f"error:{type(exc).__name__}:{exc}"
            if not close:
                raise
        finally:
            if close:
                self.sim.close()
        evidence = self.sim.evidence()
        # Keep the origin decision alongside the truth-free flight evidence so
        # consumers can distinguish an SDF-aligned run from an explicit local
        # override without loading evaluator truth.
        evidence["world_origin"] = dict(self._world_origin_evidence)
        result = SimulatorScenarioRunResult(
            scenario_id=self.runtime_input.scenario_id,
            scenario_version=self.runtime_input.scenario_version,
            scenario_manifest_hash=self.runtime_input.scenario_manifest_hash,
            seed=self.runtime_input.seed,
            mode=self.mode.value,
            requested_frames=self.config.frames,
            completed_frames=len(steps),
            terminal_reason=terminal_reason,
            active_fault_ids=tuple(item.event.event_id for item in self._fault_runtime),
            fault_events=tuple(self._fault_mapping(item) for item in self._fault_runtime),
            steps=tuple(steps[-self.config.history_limit :]),
            evidence=evidence,
            closed=self.sim.closed,
            runtime_descriptor=self.runtime_descriptor,
            runtime_input_hash=canonical_sha256(self.runtime_input.to_channel_mapping()),
        )
        if scene_snapshots:
            # Bind the stream to the runner evidence *without* introducing a
            # circular hash: the source hash is the result digest before the
            # optional stream is attached.  Replays can therefore prove which
            # exact runner output produced the frame sequence.
            scene_stream = SimulatorSceneStream(
                scenario_id=self.runtime_input.scenario_id,
                scenario_version=self.runtime_input.scenario_version,
                scenario_manifest_hash=self.runtime_input.scenario_manifest_hash,
                seed=self.runtime_input.seed,
                mode=self.mode.value,
                release_label=load_product_version().release_label,
                world_origin=self._world_origin_evidence,
                source_evidence_hash=result.evidence_hash,
                snapshots=tuple(scene_snapshots),
            )
            result = replace(result, scene_stream=scene_stream)
        self._result = result
        return result

    def close(self) -> None:
        self.sim.close()

    def _select_faults(self, faults: Sequence[FaultEvent]) -> tuple[FaultEvent, ...]:
        selected = tuple(faults)
        requested = self.config.active_fault_ids
        if requested is None:
            return selected
        known = {item.event_id for item in selected}
        missing = sorted(set(requested) - known)
        if missing:
            raise SimulatorScenarioRunnerError("unknown fault event(s): " + ", ".join(missing))
        allowed = set(requested)
        return tuple(item for item in selected if item.event_id in allowed)

    def _observation(self, telemetry: FlightTelemetrySnapshot) -> SimulatorTelemetrySnapshot:
        observed = telemetry
        if self.observation_provider is not None:
            observed = self.observation_provider(telemetry)
            if not isinstance(observed, SimulatorTelemetrySnapshot):
                raise TypeError("observation_provider must return SimulatorTelemetrySnapshot")
            if observed.observed_at_s != telemetry.observed_at_s:
                raise SimulatorScenarioRunnerError(
                    "observation_provider cannot change the simulation timestamp"
                )
        for runtime in self._fault_runtime:
            if self._fault_is_active(runtime, telemetry.observed_at_s):
                runtime.active_frames += 1
                # Record the first actual observation transformed by this
                # event.  This applies equally to finite and indefinite
                # faults; previously only the latter flipped ``fired`` in
                # ``_fault_is_active``, making a finite dropout look like it
                # had never been exercised even when ``active_frames`` was
                # non-zero.
                runtime.fired = True
                observed = self._apply_fault(runtime, observed)
        return observed

    def _fault_is_active(self, runtime: _FaultRuntime, time_s: float) -> bool:
        event = runtime.event
        if time_s < event.at_s:
            return False
        if event.duration_s is None:
            # An omitted duration is an indefinite fault.  ``fired`` records
            # the first activation in ``_observation``, while the transform
            # remains active on every subsequent observation (important for a
            # battery step, GPS bias, or a persistent camera dropout).
            return True
        return time_s < event.at_s + event.duration_s

    def _apply_fault(
        self, runtime: _FaultRuntime, observed: SimulatorTelemetrySnapshot
    ) -> SimulatorTelemetrySnapshot:
        event = runtime.event
        magnitude = 0.0 if event.magnitude is None else float(event.magnitude)
        if event.kind is FaultKind.CAMERA_DROPOUT:
            return replace(
                observed,
                target_visible=False,
                target_bearing_deg=None,
                target_distance_m=None,
                target_confidence=None,
            )
        if event.kind is FaultKind.GPS_DROPOUT:
            return replace(
                observed,
                latitude_deg=math.nan,
                longitude_deg=math.nan,
                gps_fix_type=0,
                gps_horizontal_accuracy_m=math.nan,
                gps_vertical_accuracy_m=math.nan,
                satellites_visible=0,
                position_observed_at_s=None,
                gps_observed_at_s=None,
            )
        if event.kind is FaultKind.GPS_DRIFT:
            # ``magnitude`` is a deterministic east bias in metres.  A fixed
            # half-size north bias makes the fixture non-axis-aligned while
            # remaining easy to reproduce and inspect.
            if math.isfinite(observed.latitude_deg) and math.isfinite(observed.longitude_deg):
                north_deg = (magnitude * 0.5) / 111_320.0
                scale = 111_320.0 * max(0.01, math.cos(math.radians(observed.latitude_deg)))
                east_deg = magnitude / scale
                return replace(
                    observed,
                    latitude_deg=observed.latitude_deg + north_deg,
                    longitude_deg=observed.longitude_deg + east_deg,
                    gps_horizontal_accuracy_m=max(
                        observed.gps_horizontal_accuracy_m
                        if math.isfinite(observed.gps_horizontal_accuracy_m)
                        else 0.0,
                        abs(magnitude),
                    ),
                )
            return observed
        if event.kind in {
            FaultKind.TARGET_DRIFT,
            FaultKind.TARGET_DISTANCE_DRIFT,
        }:
            # ``magnitude`` is a signed radial drift in metres per second.
            # Apply it only to the observation-owned range tuple and only
            # while the detector says a target is visible.  The scenario
            # runner never reads the evaluator truth channel, so this remains
            # a deterministic sensor perturbation rather than a target pose
            # shortcut.
            if (
                observed.target_visible
                and observed.target_distance_m is not None
                and math.isfinite(observed.target_distance_m)
            ):
                elapsed = max(0.0, observed.observed_at_s - event.at_s)
                return replace(
                    observed,
                    target_distance_m=max(
                        0.0,
                        float(observed.target_distance_m) + magnitude * elapsed,
                    ),
                )
            return observed
        if event.kind is FaultKind.TARGET_BEARING_DRIFT:
            # ``magnitude`` is a signed degrees-per-second relative-bearing
            # drift.  Keep the result in the same [-180, 180) domain used by
            # the policy so wrap-around cannot create a false large turn.
            if (
                observed.target_visible
                and observed.target_bearing_deg is not None
                and math.isfinite(observed.target_bearing_deg)
            ):
                elapsed = max(0.0, observed.observed_at_s - event.at_s)
                bearing = _wrap_relative_bearing(
                    float(observed.target_bearing_deg) + magnitude * elapsed
                )
                return replace(observed, target_bearing_deg=bearing)
            return observed
        if event.kind is FaultKind.TIMESTAMP_SKEW:
            # Do not alter ``observed_at_s``: it is the runner's shared clock
            # anchor and changing it would create a second time domain.  Only
            # sensor freshness cursors are perturbed.  A negative shift that
            # would precede t=0 is represented as unavailable instead of an
            # invalid negative timestamp, preserving fail-closed policy
            # behavior without terminating the scenario loop.
            return replace(
                observed,
                altitude_observed_at_s=_skew_timestamp(observed.altitude_observed_at_s, magnitude),
                position_observed_at_s=_skew_timestamp(observed.position_observed_at_s, magnitude),
                gps_observed_at_s=_skew_timestamp(observed.gps_observed_at_s, magnitude),
            )
        if event.kind is FaultKind.AIRSPEED_FREEZE:
            previous = self._last_observation or observed
            return replace(
                observed,
                airspeed_mps=previous.airspeed_mps,
                ground_speed_mps=previous.ground_speed_mps,
            )
        if event.kind is FaultKind.BATTERY_STEP_DROP:
            if not runtime.battery_applied:
                self._battery_remaining_pct = max(
                    0.0,
                    self._battery_remaining_pct - abs(magnitude or 10.0),
                )
                runtime.battery_applied = True
            return replace(observed, battery_remaining_pct=self._battery_remaining_pct)
        if event.kind is FaultKind.WIND_GUST:
            return replace(
                observed,
                wind_east_mps=observed.wind_east_mps + magnitude,
                wind_north_mps=observed.wind_north_mps + magnitude * 0.5,
            )
        if event.kind is FaultKind.AUTONOMY_PROCESS_EXIT:
            return replace(observed, link_healthy=False)
        if event.kind is FaultKind.VTOL_TRANSITION_TIMEOUT:
            return replace(observed, link_healthy=False)
        return observed

    def _terminal_fault_active(self, time_s: float) -> bool:
        return any(
            runtime.active_frames > 0
            and runtime.event.kind
            in {FaultKind.AUTONOMY_PROCESS_EXIT, FaultKind.VTOL_TRANSITION_TIMEOUT}
            and self._fault_is_still_active(runtime, time_s)
            for runtime in self._fault_runtime
        )

    def _active_fault_ids_at(self, time_s: float) -> tuple[str, ...]:
        """Return faults active at one sampled simulation timestamp."""

        return tuple(
            runtime.event.event_id
            for runtime in self._fault_runtime
            if self._fault_is_active(runtime, time_s)
        )

    @staticmethod
    def _fault_is_still_active(runtime: _FaultRuntime, time_s: float) -> bool:
        event = runtime.event
        return event.duration_s is None or time_s < event.at_s + event.duration_s

    def _submit_manual(self, command: FlightControlSetpoint | Mapping[str, object]) -> None:
        if isinstance(command, FlightControlSetpoint):
            values = command
        else:
            values = FlightControlSetpoint(
                roll=command.get("roll", 0.0),
                pitch=command.get("pitch", 0.0),
                yaw=command.get("yaw", 0.0),
                throttle=command.get("throttle", 0.5),
                timestamp_s=command.get("timestamp_s"),
                source="manual",
            )
        self.sim.submit_manual(
            roll=values.roll,
            pitch=values.pitch,
            yaw=values.yaw,
            throttle=values.throttle,
        )

    @staticmethod
    def _step_mapping(
        step: SimulatorClosedLoopStep,
        *,
        index: int,
        observation: SimulatorTelemetrySnapshot,
    ) -> dict[str, object]:
        return {
            "index": index,
            "simulation_time_s": step.telemetry_after.observed_at_s,
            "observation": observation.to_mapping(),
            "decision": None if step.decision is None else step.decision.to_mapping(),
            "control_receipt": None
            if step.control_receipt is None
            else {
                "source": step.control_receipt.source,
                "state": step.control_receipt.state,
                "x": step.control_receipt.x,
                "y": step.control_receipt.y,
                "z": step.control_receipt.z,
                "r": step.control_receipt.r,
            },
            "watchdog": None if step.watchdog_receipt is None else step.watchdog_receipt.source,
            "telemetry_after": step.telemetry_after.to_mapping(),
            "mission_progress": (
                None if step.mission_progress is None else step.mission_progress.to_mapping()
            ),
        }

    @staticmethod
    def _fault_mapping(runtime: _FaultRuntime) -> dict[str, object]:
        return {
            **runtime.event.to_mapping(),
            "active_frames": runtime.active_frames,
            "fired": runtime.fired,
            "battery_applied": runtime.battery_applied,
        }


def _select_initial_world_origin(
    runtime: ScenarioRuntimeInput,
    config: SimulatorScenarioRunnerConfig,
    *,
    scenario_path: Path | None,
) -> tuple[float, float, dict[str, object]]:
    """Select the deterministic fallback's geodetic frame.

    The original runner silently started every manifest at ``0, 0`` even when
    its Gazebo world declared a concrete WGS-84 origin.  That made target
    geolocation and QGC map evidence disagree with the 3-D scene.  Preserve
    explicit caller coordinates, otherwise resolve a repository-owned
    ``builtin://gazebo`` SDF and use its spherical coordinates.  Resolution is
    reported in evidence so a missing/malformed asset cannot masquerade as an
    aligned run.
    """

    configured_latitude = float(config.initial_latitude_deg)
    configured_longitude = float(config.initial_longitude_deg)
    base = {
        "latitude_deg": configured_latitude,
        "longitude_deg": configured_longitude,
        "elevation_m": None,
        "source": "runner_config_override",
        "resolved": False,
        "world_asset": runtime.world.world_asset,
    }
    if not config.use_world_origin:
        base["source"] = "runner_config_opt_out"
        return configured_latitude, configured_longitude, base

    # Non-zero coordinates are an explicit override.  This preserves the
    # historical API for custom fixtures while making the 0/0 defaults safe.
    if configured_latitude != 0.0 or configured_longitude != 0.0:
        return configured_latitude, configured_longitude, base

    world_asset = runtime.world.world_asset
    if not isinstance(world_asset, str) or not world_asset.startswith("builtin://gazebo/"):
        base["source"] = "runner_config_default"
        base["reason"] = "world_asset_not_builtin_gazebo"
        return configured_latitude, configured_longitude, base

    repository_root = _repository_root_for_scenario(scenario_path)
    try:
        reference = parse_builtin_world_uri(
            world_asset,
            repository_root=repository_root,
            scenario_id=runtime.scenario_id,
        )
        coordinates = read_world_coordinates(reference.path)
    except (OSError, RuntimeError, SimulatorStackError, TypeError, ValueError) as exc:
        base["source"] = "runner_config_default"
        base["reason"] = f"world_origin_unresolved:{type(exc).__name__}"
        return configured_latitude, configured_longitude, base
    if coordinates is None:
        base["source"] = "runner_config_default"
        base["reason"] = "world_origin_missing_or_nonfinite"
        return configured_latitude, configured_longitude, base

    latitude, longitude, elevation = coordinates
    aligned = {
        "latitude_deg": latitude,
        "longitude_deg": longitude,
        "elevation_m": elevation,
        "source": "gazebo_world_sdf",
        "resolved": True,
        "world_asset": world_asset,
        # Keep the canonical evidence portable across checkout roots.  The
        # resolved absolute path is still used above for file I/O, but a
        # drive letter or temporary extraction directory must not change the
        # deterministic run hash when the same repository is migrated.
        "world_path": _canonical_repository_path(
            reference.path,
            repository_root=repository_root,
        ),
    }
    return latitude, longitude, aligned


def _canonical_repository_path(
    path: str | Path,
    *,
    repository_root: str | Path | None,
) -> str:
    """Return a stable repository-relative path for canonical evidence.

    Runtime launchers must keep using resolved absolute paths, while
    hash-bound SIM evidence must survive a checkout moving from one drive or
    temporary directory to another.  Repository-owned files are therefore
    emitted as POSIX paths relative to the detected repository root.  An
    external fixture is represented by a deterministic ``external/<name>``
    token; its content hash and scenario identity remain the authoritative
    binding, so leaking a host-specific parent directory is unnecessary.
    """

    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()
    if repository_root is not None:
        root = Path(repository_root).expanduser()
        try:
            root = root.resolve()
        except OSError:
            root = root.absolute()
        try:
            return candidate.relative_to(root).as_posix()
        except ValueError:
            pass
    name = candidate.name or "asset"
    return f"external/{name}"


def _repository_root_for_scenario(scenario_path: Path | None) -> Path:
    """Find the repository containing the scenario's simulation assets."""

    candidates: list[Path] = []
    if scenario_path is not None:
        candidates.extend((scenario_path.parent, *scenario_path.parents))
    candidates.append(DEFAULT_REPOSITORY_ROOT)
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "simulation" / "gazebo" / "worlds").is_dir():
            return resolved
    return DEFAULT_REPOSITORY_ROOT


def _infer_mode(runtime: ScenarioRuntimeInput, override: FlightMode | str | None) -> FlightMode:
    if override is not None:
        return FlightMode.parse(override)
    haystack = f"{runtime.vehicle.model} {runtime.vehicle.airframe_profile_id}".lower()
    if any(
        token in haystack
        for token in ("multirotor", "multi-rotor", "multicopter", "quadcopter", "quad", "x500")
    ):
        return FlightMode.MULTIROTOR
    if any(token in haystack for token in ("fixed", "plane", "wing")) and "vtol" not in haystack:
        return FlightMode.FIXED_WING
    return FlightMode.VTOL


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite numeric")
    return float(value)


def _finite_positive(value: object, name: str) -> float:
    result = _finite(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _wrap_relative_bearing(value: float) -> float:
    """Normalize a relative bearing to the policy's shortest-turn domain."""

    return (float(value) + 180.0) % 360.0 - 180.0


def _skew_timestamp(value: float | None, skew_s: float) -> float | None:
    """Apply a signed timestamp skew while keeping snapshots JSON-safe."""

    if value is None:
        return None
    shifted = float(value) + float(skew_s)
    return shifted if shifted >= 0.0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a manifest-driven deterministic SIM scenario")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument(
        "--mission",
        type=Path,
        help=(
            "optional ExecutableMission or PlannedMissionCompilation JSON to bind "
            "to each scenario step"
        ),
    )
    parser.add_argument(
        "--start-mission",
        action="store_true",
        help="explicitly send MISSION_START after binding --mission",
    )
    parser.add_argument("--frames", type=int, default=600)
    parser.add_argument("--dt", type=float, default=0.1)
    parser.add_argument("--mode", choices=("vtol", "fixed-wing", "multirotor"))
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--fault", action="append", dest="fault_ids")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--runtime-report",
        type=Path,
        help="optional path for the shared hash-bound RuntimeRunReport",
    )
    args = parser.parse_args(argv)
    try:
        mission = None if args.mission is None else _load_mission_document(args.mission)
        if args.start_mission and mission is None:
            raise ValueError("--start-mission requires --mission")
        runner = SimulatorScenarioRunner(
            args.scenario,
            config=SimulatorScenarioRunnerConfig(
                frames=args.frames,
                dt_s=args.dt,
                mode=args.mode,
                enable_ai=not args.no_ai,
                active_fault_ids=None if args.fault_ids is None else tuple(args.fault_ids),
            ),
            mission=mission,
        )
        if args.start_mission:
            runner.start_mission()
        result = runner.run()
        if args.runtime_report is not None:
            result.write_runtime_run_report(args.runtime_report)
    except (OSError, SimulatorScenarioRunnerError, ValueError, TypeError) as exc:
        print(json.dumps({"event": "simulator_scenario_run_failed", "error": str(exc)}))
        return 2
    payload = json.dumps(result.to_mapping_with_hash(), ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    # ``run(close=True)`` deliberately returns a closed evidence bundle even
    # when a provider/model step fails.  The CLI must still expose that
    # terminal failure through its process exit status rather than looking
    # successful to CI merely because evidence was serialised.
    return 2 if result.terminal_reason.startswith("error:") else 0


def _load_mission_document(path: str | Path) -> ExecutableMission:
    """Load a strict mission or compilation artifact for the scenario CLI."""

    from ..mission_planning import load_executable_mission_document

    return load_executable_mission_document(path)


__all__ = [
    "ManualProvider",
    "ObservationProvider",
    "SimulatorScenarioRunResult",
    "SimulatorScenarioRunner",
    "SimulatorScenarioRunnerConfig",
    "SimulatorScenarioRunnerError",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

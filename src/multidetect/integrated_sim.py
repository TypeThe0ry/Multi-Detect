"""Bind a live Gazebo SIM sensor backend to the canonical production core.

This is intentionally parallel to :mod:`multidetect.integrated_replay`, but it
does not relabel a recording as simulation.  The backend owns a live Gazebo
camera/telemetry transport, publishes a genuine ``RuntimeMode.SIM`` descriptor,
and feeds the same detector and :class:`~multidetect.live.LiveMissionRunner`
used by HIL/REAL.  Scenario truth remains outside this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .autonomy.serialization import canonical_sha256, require_sha256
from .backends.gazebo_sim import GazeboSimBackend, GazeboSimTransportStats
from .live import LiveMissionRunner, LiveRunConfig, LiveRunResult
from .mission import MissionController
from .operator_link import RuntimeBackendHealth, RuntimeStatusMessage
from .operator_status import build_runtime_status_from_descriptor
from .runtime import RuntimeCapability, RuntimeMode, RuntimeProfile
from .simulator.perception_bridge import SimulatorPerceptionBridge

_PERCEPTION_BRIDGE_OPTION = "simulator_perception_bridge"
_PERCEPTION_SINK_OPTION = "simulator_perception_sink"
_MISSING = object()


@dataclass(frozen=True, slots=True)
class IntegratedSimIdentity:
    """Stable provenance for one live Gazebo sensor-to-production-core run."""

    runtime_descriptor_hash: str
    scenario_manifest_hash: str
    sensor_binding_hash: str
    mission_config_hash: str
    perception_identity_hash: str
    ranging_identity_hash: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version not in {1, 2}:
            raise ValueError("IntegratedSimIdentity schema_version must be 1 or 2")
        for field_name in (
            "runtime_descriptor_hash",
            "scenario_manifest_hash",
            "sensor_binding_hash",
            "mission_config_hash",
            "perception_identity_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                require_sha256(getattr(self, field_name), field_name),
            )
        if self.schema_version == 1:
            if self.ranging_identity_hash is not None:
                raise ValueError("schema-v1 integrated SIM identity cannot bind ranging")
        else:
            if self.ranging_identity_hash is None:
                raise ValueError("schema-v2 integrated SIM identity requires ranging_identity_hash")
            object.__setattr__(
                self,
                "ranging_identity_hash",
                require_sha256(self.ranging_identity_hash, "ranging_identity_hash"),
            )

    def to_mapping(self) -> dict[str, object]:
        mapping: dict[str, object] = {
            "schema_version": self.schema_version,
            "runtime_descriptor_hash": self.runtime_descriptor_hash,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "sensor_binding_hash": self.sensor_binding_hash,
            "mission_config_hash": self.mission_config_hash,
            "perception_identity_hash": self.perception_identity_hash,
        }
        if self.ranging_identity_hash is not None:
            mapping["ranging_identity_hash"] = self.ranging_identity_hash
        return mapping

    @property
    def run_identity_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(slots=True)
class IntegratedSimSession:
    backend: GazeboSimBackend
    identity: IntegratedSimIdentity
    runtime_status: RuntimeStatusMessage
    runner: LiveMissionRunner
    # The bridge is retained so callers can inspect the exact observation
    # adapter/evidence that was bound to this session.  A custom sink does not
    # necessarily expose a bridge and therefore leaves this field as ``None``.
    perception_bridge: SimulatorPerceptionBridge | None = None

    def run(self) -> tuple[LiveRunResult, GazeboSimTransportStats]:
        result = self.runner.run()
        if result.processed_frames != self.backend.frame_count:
            raise RuntimeError(
                "integrated SIM did not consume the complete bounded live sensor stream: "
                f"processed {result.processed_frames}, expected {self.backend.frame_count}"
            )
        transport = self.backend.frame_source.require_complete()
        return result, transport

    def perception_evidence(self) -> dict[str, object]:
        """Return truth-free perception-feed evidence for this SIM session.

        ``LiveMissionRunner`` owns the frame loop and counters.  The optional
        bridge owns conversion history, so exposing both makes it possible to
        audit the exact production observation path without adding a second
        transport or reading simulator truth.
        """

        if self.perception_bridge is None:
            return {
                "attached": False,
                "truth_free": True,
                "secondary_transport_emitted": False,
                "runner_feed_count": int(
                    getattr(self.runner, "_simulator_perception_feed_count", 0)
                ),
                "runner_error_count": int(
                    getattr(self.runner, "_simulator_perception_error_count", 0)
                ),
            }
        evidence = dict(self.perception_bridge.evidence())
        evidence.update(
            {
                "attached": True,
                "runner_feed_count": int(
                    getattr(self.runner, "_simulator_perception_feed_count", 0)
                ),
                "runner_error_count": int(
                    getattr(self.runner, "_simulator_perception_error_count", 0)
                ),
            }
        )
        return evidence


def _resolve_perception_binding(
    runner_options: dict[str, Any],
) -> tuple[SimulatorPerceptionBridge | None, Callable[..., Any] | None]:
    """Resolve the one SIM production-perception callback.

    The integrated runner has one callback slot.  Accepting both a bridge and
    an independent sink would make it very easy to execute the same
    observation twice (and, when both have executors, emit two control
    streams).  Treat the two options as mutually exclusive and bind a bridge
    through its ``feed_result`` method, which preserves one shared executor
    and transport when the caller supplied one.

    When neither option is supplied, install an observation-only bridge by
    default.  This guarantees that the integrated SIM path always exercises
    the production telemetry/track/range/avoidance conversion, while keeping
    flight control opt-in: the default bridge has no executor and emits no
    MAVLink frames.
    """

    bridge_value = runner_options.pop(_PERCEPTION_BRIDGE_OPTION, _MISSING)
    sink_value = runner_options.pop(_PERCEPTION_SINK_OPTION, _MISSING)
    if bridge_value is not _MISSING and sink_value is not _MISSING:
        raise TypeError(
            f"{_PERCEPTION_BRIDGE_OPTION} and {_PERCEPTION_SINK_OPTION} are mutually exclusive"
        )

    if bridge_value is not _MISSING:
        # An explicit ``None`` is useful to disable the default observation
        # feed for a compatibility caller; omission remains enabled-by-default.
        if bridge_value is None:
            return None, None
        if not isinstance(bridge_value, SimulatorPerceptionBridge):
            raise TypeError(
                f"{_PERCEPTION_BRIDGE_OPTION} must be a SimulatorPerceptionBridge or None"
            )
        return bridge_value, bridge_value.feed_result

    if sink_value is not _MISSING:
        if sink_value is None:
            return None, None
        if not callable(sink_value):
            raise TypeError(f"{_PERCEPTION_SINK_OPTION} must be callable or None")
        return None, sink_value

    bridge = SimulatorPerceptionBridge()
    return bridge, bridge.feed_result


def build_integrated_sim_session(
    *,
    backend: GazeboSimBackend,
    mission: MissionController,
    detector: Any,
    mission_config_hash: str,
    perception_identity_hash: str,
    ranging_identity_hash: str | None = None,
    config: LiveRunConfig | None = None,
    **runner_options: Any,
) -> IntegratedSimSession:
    """Build the same production live runner around a genuine SIM backend."""

    if not isinstance(backend, GazeboSimBackend):
        raise TypeError("backend must be a GazeboSimBackend")
    if not isinstance(mission, MissionController):
        raise TypeError("mission must be a MissionController")
    if detector is None or not callable(getattr(detector, "detect", None)):
        raise TypeError("detector must expose a callable detect(image) method")
    descriptor = backend.descriptor
    if descriptor.mode is not RuntimeMode.SIM:
        raise ValueError("integrated SIM requires RuntimeMode.SIM")
    if descriptor.profile != RuntimeProfile.sim():
        raise ValueError("integrated SIM requires the exact SIM RuntimeProfile")
    if descriptor.recording_hash is not None:
        raise ValueError("integrated SIM cannot bind or relabel a Replay recording")
    if descriptor.scenario_manifest_hash != backend.scenario_input.scenario_manifest_hash:
        raise ValueError("integrated SIM descriptor does not bind the runtime scenario")
    if descriptor.scenario_id != backend.scenario_input.scenario_id:
        raise ValueError("integrated SIM scenario identity mismatch")
    if descriptor.scenario_seed != backend.scenario_input.seed:
        raise ValueError("integrated SIM scenario seed mismatch")
    if RuntimeCapability.FLIGHT_CONTROL_OUTPUT in descriptor.profile.capabilities:
        raise ValueError("integrated SIM profile cannot expose flight-control output")

    # Bind the production observation feed before forwarding the remaining
    # runner options.  ``LiveMissionRunner`` exposes one sink slot; keeping the
    # resolution here prevents callers from accidentally creating parallel
    # bridges/executors for the same frame stream.
    perception_bridge, perception_sink = _resolve_perception_binding(runner_options)

    reserved = {
        "frame_source",
        "telemetry_provider",
        "clock",
        "performance_clock",
        "runtime_status",
    }
    conflict = sorted(reserved.intersection(runner_options))
    if conflict:
        raise TypeError("integrated SIM owns " + ", ".join(conflict))
    if runner_options.get("fixed_wing_aim_executor") is not None:
        raise ValueError("integrated SIM cannot attach a flight-control executor")

    ranging_components = (
        runner_options.get("ranging_engine"),
        runner_options.get("ranging_config"),
        runner_options.get("adaptive_ranging_policy"),
    )
    ranging_configured = any(component is not None for component in ranging_components)
    if ranging_configured and not all(component is not None for component in ranging_components):
        raise ValueError(
            "integrated SIM ranging requires engine, config and adaptive policy together"
        )
    if ranging_configured != (ranging_identity_hash is not None):
        raise ValueError(
            "integrated SIM ranging components and ranging identity must be bound together"
        )

    selected_config = config or LiveRunConfig(
        max_frames=backend.frame_count,
        display=False,
    )
    if not isinstance(selected_config, LiveRunConfig):
        raise TypeError("config must be a LiveRunConfig")
    if selected_config.observe_pixhawk_lifecycle:
        raise ValueError("integrated SIM core cannot attach a REAL Pixhawk lifecycle")
    if selected_config.display:
        raise ValueError("integrated SIM uses the shared QGC Fly View, not an OpenCV UI")
    if selected_config.max_frames is None:
        selected_config = replace(selected_config, max_frames=backend.frame_count)
    elif selected_config.max_frames != backend.frame_count:
        raise ValueError("integrated SIM max_frames must equal the bounded live sensor stream")

    identity = IntegratedSimIdentity(
        runtime_descriptor_hash=descriptor.canonical_hash,
        scenario_manifest_hash=backend.scenario_input.scenario_manifest_hash,
        sensor_binding_hash=backend.sensor_binding_hash,
        mission_config_hash=mission_config_hash,
        perception_identity_hash=perception_identity_hash,
        ranging_identity_hash=ranging_identity_hash,
        schema_version=2 if ranging_identity_hash is not None else 1,
    )
    runtime_status = build_runtime_status_from_descriptor(
        sequence=0,
        descriptor=descriptor,
        backend_health=RuntimeBackendHealth.HEALTHY,
        run_identity_hash=identity.run_identity_hash,
        produced_at_s=backend.clock.monotonic(),
    )
    runner = LiveMissionRunner.from_runtime_backend(
        backend=backend,
        mission=mission,
        detector=detector,
        config=selected_config,
        runtime_status=runtime_status,
        simulator_perception_sink=perception_sink,
        **runner_options,
    )
    return IntegratedSimSession(
        backend=backend,
        identity=identity,
        runtime_status=runtime_status,
        runner=runner,
        perception_bridge=perception_bridge,
    )


__all__ = [
    "IntegratedSimIdentity",
    "IntegratedSimSession",
    "build_integrated_sim_session",
]

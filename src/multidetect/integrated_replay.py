"""Fail-closed binding of raw Replay inputs to the canonical live mission core.

This module deliberately contains no detector implementation and no ground-truth
loader.  A caller supplies the same production perception object used by the
live-camera launcher; :class:`RawSensorReplayBackend` supplies only verified raw
frames, read-only telemetry and the deterministic runtime clock.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .autonomy.serialization import canonical_sha256, require_sha256
from .backends.raw_replay import RawSensorReplayBackend
from .live import LiveMissionRunner, LiveRunConfig, LiveRunResult
from .mission import MissionController
from .operator_link import RuntimeBackendHealth, RuntimeStatusMessage
from .operator_status import build_runtime_status_from_descriptor
from .runtime import RuntimeCapability, RuntimeMode, RuntimeProfile


@dataclass(frozen=True, slots=True)
class IntegratedReplayIdentity:
    """Stable provenance for one raw-input, production-core Replay run."""

    runtime_descriptor_hash: str
    recording_hash: str
    mission_config_hash: str
    perception_identity_hash: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("IntegratedReplayIdentity schema_version must be 1")
        for field_name in (
            "runtime_descriptor_hash",
            "recording_hash",
            "mission_config_hash",
            "perception_identity_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                require_sha256(getattr(self, field_name), field_name),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runtime_descriptor_hash": self.runtime_descriptor_hash,
            "recording_hash": self.recording_hash,
            "mission_config_hash": self.mission_config_hash,
            "perception_identity_hash": self.perception_identity_hash,
        }

    @property
    def run_identity_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(slots=True)
class IntegratedReplaySession:
    """A single-pass deterministic Replay session and its operator identity."""

    backend: RawSensorReplayBackend
    identity: IntegratedReplayIdentity
    runtime_status: RuntimeStatusMessage
    runner: LiveMissionRunner

    def run(self) -> LiveRunResult:
        """Run every verified frame and reject a silent prefix-only result."""

        result = self.runner.run()
        if result.processed_frames != self.backend.frame_count:
            raise RuntimeError(
                "integrated Replay did not consume the complete verified recording: "
                f"processed {result.processed_frames}, expected {self.backend.frame_count}"
            )
        return result


def build_integrated_replay_session(
    *,
    backend: RawSensorReplayBackend,
    mission: MissionController,
    detector: Any,
    mission_config_hash: str,
    perception_identity_hash: str,
    config: LiveRunConfig | None = None,
    **runner_options: Any,
) -> IntegratedReplaySession:
    """Bind verified raw Replay data to ``LiveMissionRunner``.

    The builder owns the raw source, telemetry provider, runtime/performance
    clocks, run identity and runtime status.  It also excludes the one live
    component which can emit flight-control setpoints.  Read-only operator and
    evidence adapters may still be supplied through ``runner_options``.
    """

    if not isinstance(backend, RawSensorReplayBackend):
        raise TypeError("backend must be a RawSensorReplayBackend")
    if not isinstance(mission, MissionController):
        raise TypeError("mission must be a MissionController")
    if detector is None or not callable(getattr(detector, "detect", None)):
        raise TypeError("detector must expose a callable detect(image) method")

    descriptor = backend.descriptor
    if descriptor.mode is not RuntimeMode.REPLAY:
        raise ValueError("integrated Replay requires RuntimeMode.REPLAY")
    if descriptor.profile != RuntimeProfile.replay():
        raise ValueError("integrated Replay requires the exact replay RuntimeProfile")
    if not descriptor.deterministic or descriptor.fault_injection_enabled:
        raise ValueError("integrated Replay requires deterministic, fault-free provenance")
    if descriptor.recording_hash != backend.recording.recording_hash:
        raise ValueError("runtime descriptor does not bind the verified recording")
    if RuntimeCapability.FLIGHT_CONTROL_OUTPUT in descriptor.profile.capabilities:
        raise ValueError("integrated Replay profile cannot expose flight-control output")

    reserved = {
        "frame_source",
        "telemetry_provider",
        "clock",
        "performance_clock",
        "runtime_status",
    }
    conflict = sorted(reserved.intersection(runner_options))
    if conflict:
        raise TypeError("integrated Replay owns " + ", ".join(conflict))
    if runner_options.get("fixed_wing_aim_executor") is not None:
        raise ValueError("integrated Replay cannot attach a flight-control executor")

    selected_config = config or LiveRunConfig(
        max_frames=backend.frame_count,
        display=False,
    )
    if not isinstance(selected_config, LiveRunConfig):
        raise TypeError("config must be a LiveRunConfig")
    if selected_config.observe_pixhawk_lifecycle:
        raise ValueError("integrated Replay cannot observe a live Pixhawk lifecycle")
    if selected_config.display:
        raise ValueError(
            "integrated Replay is headless; synchronized QGC video is a separate E4 gate"
        )
    if selected_config.max_frames is None:
        selected_config = replace(selected_config, max_frames=backend.frame_count)
    elif selected_config.max_frames != backend.frame_count:
        raise ValueError("integrated Replay max_frames must equal the complete verified recording")

    identity = IntegratedReplayIdentity(
        runtime_descriptor_hash=descriptor.canonical_hash,
        recording_hash=backend.recording.recording_hash,
        mission_config_hash=mission_config_hash,
        perception_identity_hash=perception_identity_hash,
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
        **runner_options,
    )
    return IntegratedReplaySession(
        backend=backend,
        identity=identity,
        runtime_status=runtime_status,
        runner=runner,
    )


__all__ = [
    "IntegratedReplayIdentity",
    "IntegratedReplaySession",
    "build_integrated_replay_session",
]

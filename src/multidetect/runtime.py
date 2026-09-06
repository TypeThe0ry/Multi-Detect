"""Runtime identity plus the compatible post-detection observation contract.

``RuntimeBackend`` is intentionally the legacy ``FrameObservation`` stream
boundary used by deterministic downstream regression.  It does not execute a
``FrameSource`` or detector.  Pre-detection Replay/SIM/HIL/REAL adapters use
``RawSensorRuntimeBackend`` and feed their sources into the canonical live
pipeline instead of replacing detector, ranging or mission logic.  This module
defines that boundary only; it does not provide a raw-sensor replay adapter.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    optional_sha256,
    optional_text,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .clock import Clock
from .compat import StrEnum
from .domain import FrameObservation


class RuntimeMode(StrEnum):
    REPLAY = "replay"
    SIM = "sim"
    HIL = "hil"
    REAL = "real"


class RuntimeCapability(StrEnum):
    OBSERVE = "observe"
    REPLAY_INPUT = "replay_input"
    SIMULATION_TRANSPORT = "simulation_transport"
    HIL_TRANSPORT = "hil_transport"
    FLIGHT_CONTROL_OUTPUT = "flight_control_output"


class RuntimeEndpointClass(StrEnum):
    READ_ONLY = "read_only"
    RECORDING = "recording"
    SIMULATOR = "simulator"
    HIL_AUTOPILOT = "hil_autopilot"
    REAL_VEHICLE = "real_vehicle"


class RuntimeProfileId(StrEnum):
    OBSERVER = "observer"
    REPLAY = "replay"
    SIM = "sim"
    HIL = "hil"
    FLIGHT = "flight"


_PROFILE_CAPABILITIES = {
    RuntimeProfileId.OBSERVER: (RuntimeCapability.OBSERVE,),
    RuntimeProfileId.REPLAY: (
        RuntimeCapability.OBSERVE,
        RuntimeCapability.REPLAY_INPUT,
    ),
    RuntimeProfileId.SIM: (
        RuntimeCapability.OBSERVE,
        RuntimeCapability.SIMULATION_TRANSPORT,
    ),
    RuntimeProfileId.HIL: (
        RuntimeCapability.OBSERVE,
        RuntimeCapability.HIL_TRANSPORT,
    ),
    RuntimeProfileId.FLIGHT: (
        RuntimeCapability.OBSERVE,
        RuntimeCapability.FLIGHT_CONTROL_OUTPUT,
    ),
}
_PROFILE_ENDPOINTS = {
    RuntimeProfileId.OBSERVER: RuntimeEndpointClass.READ_ONLY,
    RuntimeProfileId.REPLAY: RuntimeEndpointClass.RECORDING,
    RuntimeProfileId.SIM: RuntimeEndpointClass.SIMULATOR,
    RuntimeProfileId.HIL: RuntimeEndpointClass.HIL_AUTOPILOT,
    RuntimeProfileId.FLIGHT: RuntimeEndpointClass.REAL_VEHICLE,
}


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Immutable least-privilege runtime capability profile.

    Capabilities and endpoint classes are exact for each profile.  They cannot
    be expanded by toggling a descriptor boolean or environment variable.
    """

    profile_id: RuntimeProfileId
    capabilities: tuple[RuntimeCapability, ...]
    endpoint_class: RuntimeEndpointClass
    vehicle_identity_hash: str | None = None
    firmware_identity_hash: str | None = None
    airframe_profile_hash: str | None = None
    signing_identity_hash: str | None = None
    endpoint_binding_hash: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        if not isinstance(self.profile_id, RuntimeProfileId):
            raise AutonomyContractError("profile_id must be a RuntimeProfileId")
        if not isinstance(self.endpoint_class, RuntimeEndpointClass):
            raise AutonomyContractError("endpoint_class must be a RuntimeEndpointClass")
        if not isinstance(self.capabilities, Sequence) or isinstance(
            self.capabilities, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("capabilities must be an array")
        capabilities = tuple(self.capabilities)
        if any(not isinstance(item, RuntimeCapability) for item in capabilities):
            raise AutonomyContractError("capabilities must contain only RuntimeCapability values")
        object.__setattr__(self, "capabilities", capabilities)
        expected_capabilities = _PROFILE_CAPABILITIES[self.profile_id]
        if capabilities != expected_capabilities:
            raise AutonomyContractError(
                f"{self.profile_id.value} profile capabilities must be exactly "
                f"{tuple(item.value for item in expected_capabilities)}"
            )
        if self.endpoint_class is not _PROFILE_ENDPOINTS[self.profile_id]:
            raise AutonomyContractError(
                f"{self.profile_id.value} profile has an invalid endpoint class"
            )
        for name in (
            "vehicle_identity_hash",
            "firmware_identity_hash",
            "airframe_profile_hash",
            "signing_identity_hash",
            "endpoint_binding_hash",
        ):
            object.__setattr__(self, name, optional_sha256(getattr(self, name), name))
        bindings = (
            self.vehicle_identity_hash,
            self.firmware_identity_hash,
            self.airframe_profile_hash,
            self.signing_identity_hash,
            self.endpoint_binding_hash,
        )
        if self.profile_id is RuntimeProfileId.FLIGHT:
            required = (
                self.vehicle_identity_hash,
                self.airframe_profile_hash,
                self.signing_identity_hash,
                self.endpoint_binding_hash,
            )
            if any(value is None for value in required):
                raise AutonomyContractError(
                    "flight profile requires vehicle, airframe, signing and endpoint bindings"
                )
        elif self.profile_id is RuntimeProfileId.HIL:
            required = (
                self.vehicle_identity_hash,
                self.airframe_profile_hash,
                self.endpoint_binding_hash,
            )
            if any(value is None for value in required):
                raise AutonomyContractError(
                    "hil profile requires vehicle, airframe and endpoint bindings"
                )
            if self.signing_identity_hash is not None:
                raise AutonomyContractError(
                    "hil profile cannot carry a REAL signing identity binding"
                )
        elif any(value is not None for value in bindings):
            raise AutonomyContractError(
                f"{self.profile_id.value} profile cannot carry vehicle output bindings"
            )

    @classmethod
    def from_mapping(cls, raw: object) -> RuntimeProfile:
        value = strict_object(
            raw,
            name="RuntimeProfile",
            required=frozenset({"schema_version", "profile_id", "capabilities", "endpoint_class"}),
            optional=frozenset(
                {
                    "vehicle_identity_hash",
                    "firmware_identity_hash",
                    "airframe_profile_hash",
                    "signing_identity_hash",
                    "endpoint_binding_hash",
                }
            ),
        )
        try:
            profile_id = RuntimeProfileId(require_text(value["profile_id"], "profile_id"))
            endpoint_class = RuntimeEndpointClass(
                require_text(value["endpoint_class"], "endpoint_class")
            )
            raw_capabilities = value["capabilities"]
            if not isinstance(raw_capabilities, Sequence) or isinstance(
                raw_capabilities, (str, bytes, bytearray)
            ):
                raise AutonomyContractError("capabilities must be an array")
            capabilities = tuple(
                RuntimeCapability(require_text(item, "capabilities item"))
                for item in raw_capabilities
            )
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported runtime profile value: {exc}") from exc
        return cls(
            profile_id=profile_id,
            capabilities=capabilities,
            endpoint_class=endpoint_class,
            vehicle_identity_hash=optional_sha256(
                value.get("vehicle_identity_hash"), "vehicle_identity_hash"
            ),
            firmware_identity_hash=optional_sha256(
                value.get("firmware_identity_hash"), "firmware_identity_hash"
            ),
            airframe_profile_hash=optional_sha256(
                value.get("airframe_profile_hash"), "airframe_profile_hash"
            ),
            signing_identity_hash=optional_sha256(
                value.get("signing_identity_hash"), "signing_identity_hash"
            ),
            endpoint_binding_hash=optional_sha256(
                value.get("endpoint_binding_hash"), "endpoint_binding_hash"
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    @classmethod
    def observer(cls) -> RuntimeProfile:
        return cls(
            RuntimeProfileId.OBSERVER,
            _PROFILE_CAPABILITIES[RuntimeProfileId.OBSERVER],
            _PROFILE_ENDPOINTS[RuntimeProfileId.OBSERVER],
        )

    @classmethod
    def replay(cls) -> RuntimeProfile:
        return cls(
            RuntimeProfileId.REPLAY,
            _PROFILE_CAPABILITIES[RuntimeProfileId.REPLAY],
            _PROFILE_ENDPOINTS[RuntimeProfileId.REPLAY],
        )

    @classmethod
    def sim(cls) -> RuntimeProfile:
        return cls(
            RuntimeProfileId.SIM,
            _PROFILE_CAPABILITIES[RuntimeProfileId.SIM],
            _PROFILE_ENDPOINTS[RuntimeProfileId.SIM],
        )

    @classmethod
    def hil(
        cls,
        *,
        vehicle_identity_hash: str,
        firmware_identity_hash: str | None = None,
        airframe_profile_hash: str,
        endpoint_binding_hash: str,
    ) -> RuntimeProfile:
        return cls(
            RuntimeProfileId.HIL,
            _PROFILE_CAPABILITIES[RuntimeProfileId.HIL],
            _PROFILE_ENDPOINTS[RuntimeProfileId.HIL],
            vehicle_identity_hash=vehicle_identity_hash,
            firmware_identity_hash=firmware_identity_hash,
            airframe_profile_hash=airframe_profile_hash,
            endpoint_binding_hash=endpoint_binding_hash,
        )

    @classmethod
    def flight(
        cls,
        *,
        vehicle_identity_hash: str,
        firmware_identity_hash: str | None = None,
        airframe_profile_hash: str,
        signing_identity_hash: str,
        endpoint_binding_hash: str,
    ) -> RuntimeProfile:
        return cls(
            RuntimeProfileId.FLIGHT,
            _PROFILE_CAPABILITIES[RuntimeProfileId.FLIGHT],
            _PROFILE_ENDPOINTS[RuntimeProfileId.FLIGHT],
            vehicle_identity_hash=vehicle_identity_hash,
            firmware_identity_hash=firmware_identity_hash,
            airframe_profile_hash=airframe_profile_hash,
            signing_identity_hash=signing_identity_hash,
            endpoint_binding_hash=endpoint_binding_hash,
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id.value,
            "capabilities": [item.value for item in self.capabilities],
            "endpoint_class": self.endpoint_class.value,
        }
        for name in (
            "vehicle_identity_hash",
            "firmware_identity_hash",
            "airframe_profile_hash",
            "signing_identity_hash",
            "endpoint_binding_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def canonical_sha256(self) -> str:
        return self.canonical_hash

    @property
    def sha256(self) -> str:
        return self.canonical_hash


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """Fail-closed identity and capability declaration for one runtime."""

    mode: RuntimeMode
    profile: RuntimeProfile
    backend_id: str
    airframe_profile_hash: str
    scenario_id: str | None = None
    scenario_manifest_hash: str | None = None
    scenario_seed: int | None = None
    recording_hash: str | None = None
    deterministic: bool = False
    fault_injection_enabled: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        if not isinstance(self.mode, RuntimeMode):
            raise AutonomyContractError("mode must be a RuntimeMode")
        if not isinstance(self.profile, RuntimeProfile):
            raise AutonomyContractError("profile must be a RuntimeProfile")
        object.__setattr__(self, "backend_id", require_text(self.backend_id, "backend_id"))
        object.__setattr__(
            self,
            "airframe_profile_hash",
            require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        if (
            self.profile.airframe_profile_hash is not None
            and self.profile.airframe_profile_hash != self.airframe_profile_hash
        ):
            raise AutonomyContractError(
                "descriptor airframe_profile_hash does not match runtime profile"
            )
        object.__setattr__(self, "scenario_id", optional_text(self.scenario_id, "scenario_id"))
        object.__setattr__(
            self,
            "scenario_manifest_hash",
            optional_sha256(self.scenario_manifest_hash, "scenario_manifest_hash"),
        )
        if self.scenario_seed is not None:
            object.__setattr__(
                self,
                "scenario_seed",
                require_int(self.scenario_seed, "scenario_seed", minimum=0),
            )
        object.__setattr__(
            self, "recording_hash", optional_sha256(self.recording_hash, "recording_hash")
        )
        for name in ("deterministic", "fault_injection_enabled"):
            require_bool(getattr(self, name), name)
        allowed_profiles = {
            RuntimeMode.REPLAY: {RuntimeProfileId.REPLAY},
            RuntimeMode.SIM: {RuntimeProfileId.SIM},
            RuntimeMode.HIL: {RuntimeProfileId.HIL},
            RuntimeMode.REAL: {RuntimeProfileId.OBSERVER, RuntimeProfileId.FLIGHT},
        }
        if self.profile.profile_id not in allowed_profiles[self.mode]:
            raise AutonomyContractError(
                f"{self.profile.profile_id.value} profile cannot run in {self.mode.value} mode"
            )
        if self.mode is RuntimeMode.REAL:
            if any(
                value is not None
                for value in (
                    self.scenario_id,
                    self.scenario_manifest_hash,
                    self.scenario_seed,
                    self.recording_hash,
                )
            ):
                raise AutonomyContractError(
                    "REAL runtime cannot bind simulation or recording provenance"
                )
            if self.fault_injection_enabled:
                raise AutonomyContractError("REAL runtime cannot enable scenario fault injection")
            if self.deterministic:
                raise AutonomyContractError("REAL runtime cannot claim deterministic time")
        elif self.mode in {RuntimeMode.SIM, RuntimeMode.HIL}:
            if any(
                value is None
                for value in (
                    self.scenario_id,
                    self.scenario_manifest_hash,
                    self.scenario_seed,
                )
            ):
                raise AutonomyContractError(
                    f"{self.mode.value.upper()} runtime requires scenario id, "
                    "manifest hash and seed"
                )
            if self.recording_hash is not None:
                raise AutonomyContractError(
                    f"{self.mode.value.upper()} runtime cannot bind a replay recording"
                )
        elif self.mode is RuntimeMode.REPLAY:
            if not self.deterministic:
                raise AutonomyContractError("Replay runtime must be deterministic")
            if self.recording_hash is None:
                raise AutonomyContractError("Replay runtime requires recording_hash")
            scenario_values = (
                self.scenario_id,
                self.scenario_manifest_hash,
                self.scenario_seed,
            )
            if any(value is None for value in scenario_values) != all(
                value is None for value in scenario_values
            ):
                raise AutonomyContractError(
                    "Replay scenario provenance must be supplied as id, manifest hash and seed"
                )
        if self.fault_injection_enabled and self.scenario_manifest_hash is None:
            raise AutonomyContractError("fault injection requires scenario manifest provenance")

    @classmethod
    def from_mapping(cls, raw: object) -> RuntimeDescriptor:
        value = strict_object(
            raw,
            name="RuntimeDescriptor",
            required=frozenset(
                {
                    "schema_version",
                    "mode",
                    "profile",
                    "backend_id",
                    "airframe_profile_hash",
                    "deterministic",
                    "fault_injection_enabled",
                }
            ),
            optional=frozenset(
                {
                    "scenario_id",
                    "scenario_manifest_hash",
                    "scenario_seed",
                    "recording_hash",
                }
            ),
        )
        try:
            mode = RuntimeMode(require_text(value["mode"], "mode"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported runtime mode: {exc}") from exc
        return cls(
            mode=mode,
            profile=RuntimeProfile.from_mapping(value["profile"]),
            backend_id=require_text(value["backend_id"], "backend_id"),
            airframe_profile_hash=require_sha256(
                value["airframe_profile_hash"], "airframe_profile_hash"
            ),
            scenario_id=optional_text(value.get("scenario_id"), "scenario_id"),
            scenario_manifest_hash=optional_sha256(
                value.get("scenario_manifest_hash"), "scenario_manifest_hash"
            ),
            scenario_seed=(
                None
                if value.get("scenario_seed") is None
                else require_int(value["scenario_seed"], "scenario_seed", minimum=0)
            ),
            recording_hash=optional_sha256(value.get("recording_hash"), "recording_hash"),
            deterministic=require_bool(value["deterministic"], "deterministic"),
            fault_injection_enabled=require_bool(
                value["fault_injection_enabled"], "fault_injection_enabled"
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    @property
    def is_simulated(self) -> bool:
        return self.mode in {RuntimeMode.REPLAY, RuntimeMode.SIM, RuntimeMode.HIL}

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "profile": self.profile.to_mapping(),
            "backend_id": self.backend_id,
            "airframe_profile_hash": self.airframe_profile_hash,
            "deterministic": self.deterministic,
            "fault_injection_enabled": self.fault_injection_enabled,
        }
        if self.scenario_id is not None:
            result["scenario_id"] = self.scenario_id
            result["scenario_manifest_hash"] = self.scenario_manifest_hash
            result["scenario_seed"] = self.scenario_seed
        if self.recording_hash is not None:
            result["recording_hash"] = self.recording_hash
        return result

    def provenance_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": self.mode.value}
        if self.scenario_id is not None:
            result.update(
                {
                    "scenario_id": self.scenario_id,
                    "scenario_manifest_hash": self.scenario_manifest_hash,
                    "scenario_seed": self.scenario_seed,
                }
            )
        if self.recording_hash is not None:
            result["recording_hash"] = self.recording_hash
        return result

    @property
    def provenance_hash(self) -> str:
        return canonical_sha256(self.provenance_mapping())

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def canonical_sha256(self) -> str:
        return self.canonical_hash

    @property
    def sha256(self) -> str:
        return self.canonical_hash


@runtime_checkable
class RuntimeBackend(Protocol):
    """Post-detection observation stream; Ground Truth is intentionally absent.

    ``FrameObservation`` already contains detections and normalized telemetry.
    Keep this legacy name for API compatibility; new code should use the
    explicit ``ObservationRuntimeBackend`` alias when describing this boundary.
    """

    @property
    def descriptor(self) -> RuntimeDescriptor: ...

    @property
    def clock(self) -> Clock: ...

    @property
    def input_hash(self) -> str: ...

    def observations(self) -> Iterator[FrameObservation]: ...

    def close(self) -> None: ...


ObservationRuntimeBackend = RuntimeBackend


@runtime_checkable
class ObservationProcessor(Protocol):
    """The existing MissionController-compatible perception entry point."""

    def process_observation(
        self,
        frame: FrameObservation,
        *,
        now_s: float,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimeFrameResult:
    frame_id: str
    captured_at_s: float
    result: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_id", require_text(self.frame_id, "frame_id"))
        object.__setattr__(
            self,
            "captured_at_s",
            require_real(self.captured_at_s, "captured_at_s", minimum=0.0),
        )


@dataclass(frozen=True, slots=True)
class RuntimeRunReport:
    descriptor_hash: str
    input_hash: str
    processed_frame_ids: tuple[str, ...]
    started_at_s: float
    finished_at_s: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "descriptor_hash",
            require_sha256(self.descriptor_hash, "descriptor_hash"),
        )
        object.__setattr__(self, "input_hash", require_sha256(self.input_hash, "input_hash"))
        if not isinstance(self.processed_frame_ids, Sequence) or isinstance(
            self.processed_frame_ids, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("processed_frame_ids must be an array")
        object.__setattr__(
            self,
            "processed_frame_ids",
            tuple(
                require_text(item, f"processed_frame_ids[{index}]")
                for index, item in enumerate(self.processed_frame_ids)
            ),
        )
        if len(self.processed_frame_ids) != len(set(self.processed_frame_ids)):
            raise AutonomyContractError("processed_frame_ids must be unique")
        object.__setattr__(
            self,
            "started_at_s",
            require_real(self.started_at_s, "started_at_s", minimum=0.0),
        )
        object.__setattr__(
            self,
            "finished_at_s",
            require_real(self.finished_at_s, "finished_at_s", minimum=0.0),
        )
        if self.finished_at_s < self.started_at_s:
            raise AutonomyContractError("runtime report cannot finish before it started")


@dataclass(slots=True)
class AutonomyRuntime:
    """Drive post-detection observations through the shared downstream core.

    This runner deliberately does not invoke ``FrameSource`` or a detector;
    same-detector raw-sensor modes bind their sources to ``LiveMissionRunner``.
    """

    backend: RuntimeBackend
    processor: ObservationProcessor

    def run(self, *, max_frames: int | None = None) -> tuple[RuntimeFrameResult, ...]:
        results, _ = self.run_with_report(max_frames=max_frames)
        return results

    def run_with_report(
        self, *, max_frames: int | None = None
    ) -> tuple[tuple[RuntimeFrameResult, ...], RuntimeRunReport]:
        if max_frames is not None:
            if isinstance(max_frames, bool) or not isinstance(max_frames, int):
                raise TypeError("max_frames must be an integer or None")
            if max_frames < 0:
                raise ValueError("max_frames must be non-negative")
        descriptor = self.backend.descriptor
        if not isinstance(descriptor, RuntimeDescriptor):
            raise TypeError("backend descriptor must be a RuntimeDescriptor")
        descriptor_hash = descriptor.canonical_hash
        input_hash = require_sha256(self.backend.input_hash, "input_hash")
        results: list[RuntimeFrameResult] = []
        started_at_s = self.backend.clock.monotonic()
        iterator: Iterator[FrameObservation] | None = None
        try:
            iterator = self.backend.observations()
            while max_frames is None or len(results) < max_frames:
                try:
                    frame = next(iterator)
                except StopIteration:
                    break
                now_s = self.backend.clock.monotonic()
                result = self.processor.process_observation(frame, now_s=now_s)
                results.append(
                    RuntimeFrameResult(
                        frame_id=frame.frame_id,
                        captured_at_s=frame.captured_at_s,
                        result=result,
                    )
                )
        finally:
            if iterator is not None:
                close_iterator = getattr(iterator, "close", None)
                if callable(close_iterator):
                    close_iterator()
            self.backend.close()
        # Runtime identity is an input boundary, not a mutable status field.
        # Re-read it after resource cleanup and reject a backend that swapped
        # descriptor or input hash while the processor was running.  Reports
        # must describe the exact stream that was consumed, even when a
        # launcher keeps a long-lived backend object around for another mode.
        final_descriptor = self.backend.descriptor
        if not isinstance(final_descriptor, RuntimeDescriptor):
            raise TypeError("backend descriptor must remain a RuntimeDescriptor")
        final_input_hash = require_sha256(self.backend.input_hash, "input_hash")
        if final_descriptor.canonical_hash != descriptor_hash or final_input_hash != input_hash:
            raise RuntimeError("runtime backend provenance changed during run")
        finished_at_s = self.backend.clock.monotonic()
        frozen_results = tuple(results)
        return frozen_results, RuntimeRunReport(
            descriptor_hash=descriptor_hash,
            input_hash=input_hash,
            processed_frame_ids=tuple(item.frame_id for item in frozen_results),
            started_at_s=started_at_s,
            finished_at_s=finished_at_s,
        )


__all__ = [
    "AutonomyRuntime",
    "ObservationRuntimeBackend",
    "ObservationProcessor",
    "RuntimeBackend",
    "RuntimeCapability",
    "RuntimeDescriptor",
    "RuntimeEndpointClass",
    "RuntimeFrameResult",
    "RuntimeMode",
    "RuntimeProfile",
    "RuntimeProfileId",
    "RuntimeRunReport",
]

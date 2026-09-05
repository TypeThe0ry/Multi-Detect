"""Deterministic post-detection replay of immutable FrameObservation values."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..clock import ManualClock
from ..domain import FrameObservation
from ..replay import ReplayRecording
from ..runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile


@dataclass(slots=True)
class ReplayBackend:
    """Replay detections and telemetry through the shared downstream core.

    Frames already contain ``Detection`` values.  This compatibility backend
    therefore proves MissionController/downstream determinism, not raw-pixel or
    production-detector parity.  Prefer the explicit
    ``ObservationReplayBackend`` name in new architecture code.
    """

    recording: ReplayRecording
    descriptor: RuntimeDescriptor | None = None
    clock: ManualClock = field(default_factory=ManualClock)
    _started: bool = field(init=False, default=False, repr=False)
    _closed: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.recording, ReplayRecording):
            raise TypeError("ObservationReplayBackend recording must be a ReplayRecording")
        if not isinstance(self.clock, ManualClock):
            raise TypeError("ObservationReplayBackend requires a ManualClock")
        if self.descriptor is None:
            self.descriptor = RuntimeDescriptor(
                mode=RuntimeMode.REPLAY,
                profile=RuntimeProfile.replay(),
                backend_id=f"replay:{self.recording.recording_id}",
                airframe_profile_hash=self.recording.airframe_profile_hash,
                recording_hash=self.recording.recording_hash,
                deterministic=True,
            )
        if not isinstance(self.descriptor, RuntimeDescriptor):
            raise TypeError("ObservationReplayBackend descriptor must be a RuntimeDescriptor")
        if self.descriptor.mode is not RuntimeMode.REPLAY:
            raise ValueError("ReplayBackend requires a replay RuntimeDescriptor")
        if not self.descriptor.deterministic:
            raise ValueError("ReplayBackend descriptor must be deterministic")
        if self.descriptor.recording_hash != self.recording.recording_hash:
            raise ValueError("ReplayBackend descriptor recording_hash does not match recording")
        if self.descriptor.airframe_profile_hash != self.recording.airframe_profile_hash:
            raise ValueError(
                "ReplayBackend descriptor airframe_profile_hash does not match recording"
            )

    @classmethod
    def from_jsonl(
        cls,
        path: str | Path,
        *,
        recording_id: str | None = None,
        airframe_profile_hash: str,
        scenario_id: str | None = None,
        scenario_manifest_hash: str | None = None,
        scenario_seed: int | None = None,
        clock: ManualClock | None = None,
    ) -> ReplayBackend:
        recording = ReplayRecording.from_jsonl(
            path,
            recording_id=recording_id,
            airframe_profile_hash=airframe_profile_hash,
        )
        return cls(
            recording=recording,
            descriptor=RuntimeDescriptor(
                mode=RuntimeMode.REPLAY,
                profile=RuntimeProfile.replay(),
                backend_id=f"replay:{recording.recording_id}",
                airframe_profile_hash=recording.airframe_profile_hash,
                scenario_id=scenario_id,
                scenario_manifest_hash=scenario_manifest_hash,
                scenario_seed=scenario_seed,
                recording_hash=recording.recording_hash,
                deterministic=True,
                fault_injection_enabled=False,
            ),
            clock=clock or ManualClock(),
        )

    def observations(self) -> Iterator[FrameObservation]:
        if self._closed:
            raise RuntimeError("ReplayBackend is closed")
        if self._started:
            raise RuntimeError("ReplayBackend recordings are single-pass")
        self._started = True
        for frame in self.recording.frames:
            if self._closed:
                raise RuntimeError("ReplayBackend is closed")
            self.clock.advance_to(frame.captured_at_s)
            yield frame

    @property
    def input_hash(self) -> str:
        return self.recording.recording_hash

    def close(self) -> None:
        self._closed = True


ObservationReplayBackend = ReplayBackend


__all__ = ["ObservationReplayBackend", "ReplayBackend"]

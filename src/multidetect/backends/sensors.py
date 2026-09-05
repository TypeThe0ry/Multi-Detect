"""Pre-detection world-backend contract for the shared live pipeline.

World adapters provide pixels and normalized read-only telemetry.  Detector,
tracking, ranging, geolocation, mission logic and safety stay outside the
backend so Replay, SIM, HIL and REAL cannot replace the production core with a
mode-specific shortcut.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..clock import Clock
from ..runtime import RuntimeDescriptor
from ..telemetry import TelemetryProvider
from ..vision import FrameSource


@runtime_checkable
class RawSensorRuntimeBackend(Protocol):
    """Bind pre-detection frames and normalized read-only telemetry.

    The detector is intentionally absent: a mode launcher must pass
    ``frame_source`` and ``telemetry_provider`` to the canonical
    :class:`LiveMissionRunner` while injecting the same production detector and
    downstream components.  Ground Truth, expected answers and precomputed
    detections are also intentionally absent.  This protocol is a binding
    contract, not a concrete Replay/SIM/HIL adapter.

    Resource ownership remains with ``LiveMissionRunner``: it opens/closes the
    frame source and closes the telemetry provider when that provider exposes a
    ``close`` method.  A backend-level ``close`` method is deliberately not part
    of this protocol, avoiding two owners for the same transport.

    ``clock`` is the runtime time-domain binding.  Launchers must use
    ``LiveMissionRunner.from_runtime_backend`` so the same clock drives core
    freshness, deadline, wire and audit decisions.  A deterministic descriptor
    is accepted only with ``ManualClock``; concrete adapters remain responsible
    for advancing it from verified sensor timestamps.
    """

    @property
    def descriptor(self) -> RuntimeDescriptor: ...

    @property
    def clock(self) -> Clock: ...

    @property
    def frame_source(self) -> FrameSource: ...

    @property
    def telemetry_provider(self) -> TelemetryProvider: ...


__all__ = ["RawSensorRuntimeBackend"]

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from .domain import VehicleTelemetry


class TelemetryProvider(Protocol):
    """Produces a point-in-time, read-only vehicle telemetry snapshot."""

    def snapshot(self, *, now_s: float) -> VehicleTelemetry: ...


@dataclass(frozen=True, slots=True)
class FailClosedTelemetryProvider:
    """Default live-camera provider: absent avionics evidence denies deployment."""

    person_detector_healthy: bool | None = None

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        del now_s
        return VehicleTelemetry(
            altitude_agl_m=float("nan"),
            roll_deg=float("nan"),
            pitch_deg=float("nan"),
            ground_speed_mps=float("nan"),
            in_allowed_zone=None,
            geofence_healthy=None,
            position_healthy=None,
            link_healthy=None,
            flight_mode_allows_deploy=None,
            release_zone_clear=None,
            person_detector_healthy=self.person_detector_healthy,
        )


def with_person_detector_health(
    telemetry: VehicleTelemetry, *, healthy: bool | None
) -> VehicleTelemetry:
    """Attach independently configured safety-object detector health to a snapshot."""

    return replace(telemetry, person_detector_healthy=healthy)


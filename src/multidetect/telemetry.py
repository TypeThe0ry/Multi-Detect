from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Protocol

from .domain import VehicleTelemetry
from .zone_evidence import (
    ZoneEvidenceProvider,
    ZoneEvidenceVerification,
    unavailable_zone_evidence_verification,
    verify_zone_evidence,
)


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


class AuthenticatedZoneTelemetryProvider:
    """Adds authenticated zone predicates to read-only vehicle telemetry.

    Any unavailable, stale, tampered or position-mismatched evidence is converted
    to unknown predicates so the safety rule engine remains fail-closed.
    """

    def __init__(
        self,
        base_provider: TelemetryProvider,
        zone_evidence_provider: ZoneEvidenceProvider,
        *,
        mission_id: str,
        maximum_age_s: float,
        maximum_position_delta_m: float,
    ) -> None:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("zone evidence mission ID cannot be empty")
        for name, value in (
            ("maximum age", maximum_age_s),
            ("maximum position delta", maximum_position_delta_m),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"zone evidence {name} must be finite and non-negative")
        self.base_provider = base_provider
        self.zone_evidence_provider = zone_evidence_provider
        self.mission_id = mission_id.strip()
        self.maximum_age_s = float(maximum_age_s)
        self.maximum_position_delta_m = float(maximum_position_delta_m)
        self._last_verification: ZoneEvidenceVerification | None = None

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def last_verification(self) -> ZoneEvidenceVerification | None:
        return self._last_verification

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        base = self.base_provider.snapshot(now_s=now_s)
        try:
            evidence = self.zone_evidence_provider.snapshot(now_s=now_s)
            verification = verify_zone_evidence(
                evidence,
                base,
                mission_id=self.mission_id,
                now_s=now_s,
                maximum_age_s=self.maximum_age_s,
                maximum_position_delta_m=self.maximum_position_delta_m,
            )
        except (OSError, TypeError, ValueError) as exc:
            verification = unavailable_zone_evidence_verification(
                source_id="zone-evidence-unavailable",
                reason=f"zone evidence unavailable: {type(exc).__name__}",
            )
            evidence = None
        self._last_verification = verification
        if not verification.valid or evidence is None:
            return _without_zone_evidence(base)
        return replace(
            base,
            in_allowed_zone=evidence.in_allowed_zone,
            geofence_healthy=evidence.geofence_healthy,
            release_zone_clear=evidence.release_zone_clear,
        )

    def close(self) -> None:
        close = getattr(self.base_provider, "close", None)
        if callable(close):
            close()


def _without_zone_evidence(telemetry: VehicleTelemetry) -> VehicleTelemetry:
    return replace(
        telemetry,
        in_allowed_zone=None,
        geofence_healthy=None,
        release_zone_clear=None,
    )


def with_person_detector_health(
    telemetry: VehicleTelemetry, *, healthy: bool | None
) -> VehicleTelemetry:
    """Attach independently configured safety-object detector health to a snapshot."""

    return replace(telemetry, person_detector_healthy=healthy)


def with_observed_flight_mode_permission(
    telemetry: VehicleTelemetry,
    *,
    allowed_modes: tuple[str, ...],
) -> VehicleTelemetry:
    """Derive mission-level mode permission from read-only Pixhawk observations."""

    normalized_modes = frozenset(mode.strip().upper() for mode in allowed_modes if mode.strip())
    if not normalized_modes:
        raise ValueError("observed flight-mode permission requires allowed modes")
    current_mode = (
        telemetry.flight_mode.strip().upper()
        if isinstance(telemetry.flight_mode, str) and telemetry.flight_mode.strip()
        else None
    )
    if telemetry.link_healthy is False or telemetry.armed is False:
        allowed: bool | None = False
    elif telemetry.link_healthy is not True or telemetry.armed is not True or current_mode is None:
        allowed = None
    else:
        allowed = current_mode in normalized_modes
    return replace(telemetry, flight_mode_allows_deploy=allowed)


__all__ = [
    "AuthenticatedZoneTelemetryProvider",
    "FailClosedTelemetryProvider",
    "TelemetryProvider",
    "with_observed_flight_mode_permission",
    "with_person_detector_health",
]

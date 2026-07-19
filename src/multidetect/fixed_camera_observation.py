from __future__ import annotations

import math
from dataclasses import dataclass

from .compat import StrEnum
from .domain import VehicleTelemetry
from .unified_tracking import UnifiedTrackSnapshot


class FixedCameraObservationState(StrEnum):
    SEARCH = "search"
    TRK = "trk"
    LCK = "lck"
    ALIGNED = "aligned"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FixedCameraObservationConfig:
    center_tolerance_fraction: float = 0.025
    maximum_target_age_s: float = 0.30
    maximum_attitude_age_s: float = 0.50

    def __post_init__(self) -> None:
        if not math.isfinite(self.center_tolerance_fraction) or not (
            0.001 <= self.center_tolerance_fraction <= 0.25
        ):
            raise ValueError("center_tolerance_fraction must be in [0.001, 0.25]")
        for name, value in (
            ("maximum_target_age_s", self.maximum_target_age_s),
            ("maximum_attitude_age_s", self.maximum_attitude_age_s),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class FixedCameraObservation:
    state: FixedCameraObservationState
    reason: str
    produced_at_s: float
    target_id: str | None = None
    error_x_fraction: float = 0.0
    error_y_fraction: float = 0.0
    target_age_s: float | None = None
    attitude_age_s: float | None = None
    roll_deg: float | None = None
    pitch_deg: float | None = None
    heading_deg: float | None = None
    aligned: bool = False
    fixed_camera: bool = True


class FixedCameraObservationEngine:
    """Merge one primary LCK with fresh V6X attitude for the fixed RGB view.

    The normalized errors are measured from the optical image center: positive X
    is right and positive Y is down. The output is observation metadata used by
    the QGC overlay and audit trail; the camera has no movable axes.
    """

    def __init__(self, config: FixedCameraObservationConfig | None = None) -> None:
        self.config = config or FixedCameraObservationConfig()

    def evaluate(
        self,
        *,
        track: UnifiedTrackSnapshot | None,
        telemetry: VehicleTelemetry,
        now_s: float,
    ) -> FixedCameraObservation:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("fixed-camera observation time must be finite and non-negative")
        if track is None:
            return self._status(FixedCameraObservationState.SEARCH, "no_target", now_s)
        if not track.locked or not track.primary:
            return self._status(
                FixedCameraObservationState.TRK,
                "trk_only",
                now_s,
                target_id=track.track_id,
            )

        center_x, center_y = track.bbox.center
        error_x = center_x - 0.5
        error_y = center_y - 0.5
        target_age_s = now_s - track.last_seen_at_s
        common = {
            "target_id": track.track_id,
            "error_x_fraction": error_x,
            "error_y_fraction": error_y,
            "target_age_s": target_age_s,
        }
        if not track.actionable:
            return self._status(
                FixedCameraObservationState.LCK,
                "lck_uncertain",
                now_s,
                **common,
            )
        if target_age_s < 0.0 or target_age_s > self.config.maximum_target_age_s:
            return self._status(
                FixedCameraObservationState.STALE,
                "target_stale",
                now_s,
                **common,
            )

        attitude_values = (telemetry.roll_deg, telemetry.pitch_deg, telemetry.heading_deg)
        if not all(math.isfinite(value) for value in attitude_values) or not math.isfinite(
            telemetry.attitude_observed_at_s
        ):
            return self._status(
                FixedCameraObservationState.STALE,
                "attitude_unavailable",
                now_s,
                **common,
            )
        attitude_age_s = now_s - telemetry.attitude_observed_at_s
        attitude = {
            "attitude_age_s": attitude_age_s,
            "roll_deg": telemetry.roll_deg,
            "pitch_deg": telemetry.pitch_deg,
            "heading_deg": telemetry.heading_deg % 360.0,
        }
        if attitude_age_s < 0.0 or attitude_age_s > self.config.maximum_attitude_age_s:
            return self._status(
                FixedCameraObservationState.STALE,
                "attitude_stale",
                now_s,
                **common,
                **attitude,
            )

        aligned = (
            abs(error_x) <= self.config.center_tolerance_fraction
            and abs(error_y) <= self.config.center_tolerance_fraction
        )
        return self._status(
            FixedCameraObservationState.ALIGNED if aligned else FixedCameraObservationState.LCK,
            "optical_axis_aligned" if aligned else "optical_axis_offset",
            now_s,
            aligned=aligned,
            **common,
            **attitude,
        )

    @staticmethod
    def _status(
        state: FixedCameraObservationState,
        reason: str,
        produced_at_s: float,
        **values: object,
    ) -> FixedCameraObservation:
        return FixedCameraObservation(
            state=state,
            reason=reason,
            produced_at_s=produced_at_s,
            **values,
        )


__all__ = [
    "FixedCameraObservation",
    "FixedCameraObservationConfig",
    "FixedCameraObservationEngine",
    "FixedCameraObservationState",
]

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass

from .domain import VehicleTelemetry
from .multimodal_ranging import RangeSolution


@dataclass(frozen=True, slots=True)
class TargetWorldSpeedConfig:
    minimum_window_s: float = 0.60
    maximum_window_s: float = 1.50
    minimum_samples: int = 4
    stationary_speed_mps: float = 0.25
    stationary_displacement_m: float = 0.20
    maximum_sample_gap_s: float = 0.50

    def __post_init__(self) -> None:
        numeric = (
            self.minimum_window_s,
            self.maximum_window_s,
            self.stationary_speed_mps,
            self.stationary_displacement_m,
            self.maximum_sample_gap_s,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in numeric):
            raise ValueError("target-speed limits must be finite and positive")
        if self.minimum_window_s >= self.maximum_window_s:
            raise ValueError("target-speed window limits are reversed")
        if self.minimum_samples < 3:
            raise ValueError("target-speed estimator requires at least three samples")


@dataclass(frozen=True, slots=True)
class _PositionSample:
    captured_at_s: float
    north_m: float
    east_m: float


class TargetWorldSpeedEstimator:
    """Robust target speed from range/bearing plus optional aircraft Local-NED.

    Adding the aircraft position to the target-relative offset removes platform
    translation. A bounded regression window and a residual-aware deadband keep
    detector-box jitter from turning a parked target into apparent motion.
    """

    def __init__(self, config: TargetWorldSpeedConfig | None = None) -> None:
        self.config = config or TargetWorldSpeedConfig()
        self._samples: dict[str, deque[_PositionSample]] = defaultdict(deque)

    def update(
        self,
        *,
        target_id: str,
        solution: RangeSolution,
        telemetry: VehicleTelemetry,
        captured_at_s: float,
    ) -> float | None:
        if (
            solution.north_offset_m is None
            or solution.east_offset_m is None
            or not math.isfinite(captured_at_s)
        ):
            return None
        north_m = solution.north_offset_m
        east_m = solution.east_offset_m
        local_values = (
            telemetry.local_north_m,
            telemetry.local_east_m,
            telemetry.local_position_observed_at_s,
        )
        if all(math.isfinite(value) for value in local_values) and abs(
            captured_at_s - telemetry.local_position_observed_at_s
        ) <= self.config.maximum_sample_gap_s:
            north_m += telemetry.local_north_m
            east_m += telemetry.local_east_m
        history = self._samples[target_id]
        if history and captured_at_s <= history[-1].captured_at_s:
            history.clear()
        history.append(_PositionSample(captured_at_s, north_m, east_m))
        while history and captured_at_s - history[0].captured_at_s > self.config.maximum_window_s:
            history.popleft()
        if len(history) < self.config.minimum_samples:
            return None
        window_s = history[-1].captured_at_s - history[0].captured_at_s
        if window_s < self.config.minimum_window_s:
            return None
        north_slope, north_residual = _linear_slope(history, "north_m")
        east_slope, east_residual = _linear_slope(history, "east_m")
        speed_mps = math.hypot(north_slope, east_slope)
        displacement_m = speed_mps * window_s
        residual_m = math.hypot(north_residual, east_residual)
        stationary_gate_m = max(
            self.config.stationary_displacement_m,
            3.0 * residual_m,
        )
        if (
            speed_mps <= self.config.stationary_speed_mps
            or displacement_m <= stationary_gate_m
        ):
            return 0.0
        return speed_mps if math.isfinite(speed_mps) else None

    def retain(self, target_ids: set[str]) -> None:
        self._samples = defaultdict(
            deque,
            (
                (target_id, samples)
                for target_id, samples in self._samples.items()
                if target_id in target_ids
            ),
        )


def _linear_slope(samples: deque[_PositionSample], field_name: str) -> tuple[float, float]:
    times = [sample.captured_at_s for sample in samples]
    values = [getattr(sample, field_name) for sample in samples]
    mean_t = sum(times) / len(times)
    mean_value = sum(values) / len(values)
    denominator = sum((value - mean_t) ** 2 for value in times)
    if denominator <= 1e-9:
        return 0.0, 0.0
    slope = sum(
        (captured_at_s - mean_t) * (value - mean_value)
        for captured_at_s, value in zip(times, values, strict=True)
    ) / denominator
    residual = math.sqrt(
        sum(
            (
                value
                - (mean_value + slope * (captured_at_s - mean_t))
            )
            ** 2
            for captured_at_s, value in zip(times, values, strict=True)
        )
        / len(times)
    )
    return slope, residual


__all__ = ["TargetWorldSpeedConfig", "TargetWorldSpeedEstimator"]

"""Telemetry-based early detection of a fallen SIM vehicle."""

from __future__ import annotations

from typing import Any


class VehicleLossDetector:
    """Detect an uncommanded hard fall or sustained tumbling near the ground."""

    def __init__(
        self,
        *,
        minimum_fall_m: float = 8.0,
        ground_altitude_m: float = 2.5,
        tumble_angle_deg: float = 90.0,
        consecutive_steps: int = 3,
    ) -> None:
        self.minimum_fall_m = minimum_fall_m
        self.ground_altitude_m = ground_altitude_m
        self.tumble_angle_deg = tumble_angle_deg
        self.consecutive_steps = consecutive_steps
        self._low_tumble_steps = 0

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result and abs(result) != float("inf") else None

    @classmethod
    def _altitude(cls, observation: dict[str, Any]) -> float | None:
        pose = observation.get("pose") or {}
        ned = pose.get("local_ned_m")
        if isinstance(ned, (list, tuple)) and len(ned) >= 3:
            down = cls._number(ned[2])
            if down is not None:
                return -down
        return None

    @classmethod
    def _tumbling(cls, observation: dict[str, Any], threshold: float) -> bool:
        attitude = observation.get("attitude") or {}
        roll = cls._number(attitude.get("roll_deg"))
        pitch = cls._number(attitude.get("pitch_deg"))
        return (roll is not None and abs(roll) >= threshold) or (
            pitch is not None and abs(pitch) >= threshold
        )

    @staticmethod
    def _landed(observation: dict[str, Any]) -> bool:
        flight = observation.get("flight_state") or {}
        return observation.get("landed") is True or flight.get("landed_state") == 1

    def update(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        action: dict[str, Any],
        commanded_delta_ned: list[float] | tuple[float, ...],
    ) -> dict[str, Any] | None:
        kind = str(action.get("kind", "")).lower()
        up = self._number(action.get("up_m")) or 0.0
        if kind in {"land", "landing"} or self._landed(after):
            self._low_tumble_steps = 0
            return None

        before_alt = self._altitude(before)
        after_alt = self._altitude(after)
        commanded_down = 0.0
        if len(commanded_delta_ned) >= 3:
            commanded_down = max(0.0, self._number(commanded_delta_ned[2]) or 0.0)
        if before_alt is not None and after_alt is not None:
            excess_loss = before_alt - after_alt - commanded_down
            if before_alt >= self.ground_altitude_m and excess_loss >= self.minimum_fall_m:
                self._low_tumble_steps = 0
                return {
                    "reason": "uncommanded_altitude_loss",
                    "altitude_before_m": before_alt,
                    "altitude_after_m": after_alt,
                    "excess_loss_m": excess_loss,
                }

        if (
            after_alt is not None
            and 0.0 <= after_alt <= self.ground_altitude_m
            and up >= 0.0
            and self._tumbling(after, self.tumble_angle_deg)
        ):
            self._low_tumble_steps += 1
        else:
            self._low_tumble_steps = 0
        if self._low_tumble_steps >= self.consecutive_steps:
            return {
                "reason": "sustained_tumble_near_ground",
                "altitude_after_m": after_alt,
                "consecutive_steps": self._low_tumble_steps,
            }
        return None


def replay_vehicle_loss(steps: list[dict[str, Any]]) -> int | None:
    """Return the first step index at which recorded steps prove vehicle loss."""
    detector = VehicleLossDetector()
    for step in steps:
        action = step.get("action") or {}
        outcome = step.get("outcome") or {}
        event = detector.update(
            step.get("obs_before") or {},
            step.get("obs_after") or {},
            action,
            outcome.get("commanded_delta_ned") or (0.0, 0.0, 0.0),
        )
        if event is not None:
            return int(step.get("step_index", 0))
    return None

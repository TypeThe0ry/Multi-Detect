from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .domain import MissionPhase
from .mission import MissionController
from .payload_confirmation_hil import (
    MissionPayloadConfirmationHilAdapter,
    PayloadConfirmationHilCodec,
    PayloadConfirmationHilReceipt,
)
from .payload_hil_mission import (
    MissionPayloadHilAdapter,
    MissionPayloadHilError,
    MissionPayloadHilOutcome,
)


class PayloadConfirmationReceiver(Protocol):
    def receive_until_confirmed(
        self,
        adapter: MissionPayloadConfirmationHilAdapter,
        *,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> PayloadConfirmationHilReceipt: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class InertPayloadHilCycleOutcome:
    controller: MissionPayloadHilOutcome
    confirmation: PayloadConfirmationHilReceipt
    simulation_only: bool = True
    inert_load_required: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False


class InertPayloadHilCycleCoordinator:
    """Run the two separately authenticated HIL feedback legs with bounded waits."""

    def __init__(
        self,
        *,
        mission: MissionController,
        controller_adapter: MissionPayloadHilAdapter,
        confirmation_receiver: PayloadConfirmationReceiver,
        confirmation_codec: PayloadConfirmationHilCodec,
        controller_module_id: str,
        allowed_confirmation_sensor_ids: frozenset[str],
        confirmation_timeout_s: float,
        confirmation_maximum_age_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if controller_adapter.mission is not mission:
            raise ValueError("payload HIL cycle adapters must share one mission controller")
        if not isinstance(controller_module_id, str) or not controller_module_id.strip():
            raise ValueError("payload HIL controller module ID cannot be empty")
        if not allowed_confirmation_sensor_ids:
            raise ValueError("payload HIL cycle requires an independent confirmation sensor")
        for name, value in (
            ("confirmation_timeout_s", confirmation_timeout_s),
            ("confirmation_maximum_age_s", confirmation_maximum_age_s),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not callable(clock):
            raise TypeError("payload HIL cycle clock must be callable")
        self.mission = mission
        self.controller_adapter = controller_adapter
        self.confirmation_receiver = confirmation_receiver
        self.confirmation_codec = confirmation_codec
        self.controller_module_id = controller_module_id.strip()
        self.allowed_confirmation_sensor_ids = allowed_confirmation_sensor_ids
        self.confirmation_timeout_s = confirmation_timeout_s
        self.confirmation_maximum_age_s = confirmation_maximum_age_s
        self.clock = clock

    def execute(self, *, now_s: float) -> InertPayloadHilCycleOutcome:
        controller_outcome = self.controller_adapter.request_and_exchange(now_s=now_s)
        release_id = controller_outcome.request.release_id
        try:
            confirmation_adapter = MissionPayloadConfirmationHilAdapter(
                mission=self.mission,
                release_id=release_id,
                controller_module_id=self.controller_module_id,
                allowed_sensor_ids=self.allowed_confirmation_sensor_ids,
                codec=self.confirmation_codec,
                maximum_age_s=self.confirmation_maximum_age_s,
            )
            confirmation = self.confirmation_receiver.receive_until_confirmed(
                confirmation_adapter,
                timeout_s=self.confirmation_timeout_s,
                clock=self.clock,
            )
        except Exception as exc:
            completed_at_s = self._completed_at(now_s)
            if self.mission.status().phase is MissionPhase.VERIFYING_RELEASE:
                self.mission.report_simulated_failure(
                    release_id=release_id,
                    reason=f"independent payload confirmation failed: {type(exc).__name__}",
                    uncertain=True,
                    now_s=completed_at_s,
                )
            raise MissionPayloadHilError(
                "independent payload confirmation produced no safe result"
            ) from exc
        return InertPayloadHilCycleOutcome(controller_outcome, confirmation)

    def close(self) -> None:
        self.confirmation_receiver.close()

    def _completed_at(self, lower_bound_s: float) -> float:
        try:
            completed_at_s = float(self.clock())
        except (TypeError, ValueError, OverflowError):
            return lower_bound_s
        if not math.isfinite(completed_at_s) or completed_at_s < 0:
            return lower_bound_s
        return max(lower_bound_s, completed_at_s)


__all__ = [
    "InertPayloadHilCycleCoordinator",
    "InertPayloadHilCycleOutcome",
    "PayloadConfirmationReceiver",
]

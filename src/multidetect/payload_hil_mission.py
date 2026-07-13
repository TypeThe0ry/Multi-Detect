from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .mission import MissionController
from .payload_hil_protocol import (
    PayloadHilReleaseRequest,
    PayloadHilResult,
    PayloadHilResultStatus,
)
from .payload_hil_udp import PayloadHilExchange


class MissionPayloadHilError(RuntimeError):
    """The inert controller exchange could not safely advance the mission."""


class PayloadHilExchangeClient(Protocol):
    def exchange(
        self,
        request: PayloadHilReleaseRequest,
        *,
        maximum_result_age_s: float,
    ) -> PayloadHilExchange: ...


@dataclass(frozen=True, slots=True)
class MissionPayloadHilOutcome:
    request: PayloadHilReleaseRequest
    exchange: PayloadHilExchange
    execution_result: PayloadHilResult
    simulation_only: bool = True
    inert_load_required: bool = True
    physical_release_enabled: bool = False


class MissionPayloadHilAdapter:
    """Bridge one authorized mission release to an authenticated inert HIL exchange.

    The mission first performs its atomic safety recheck and consumes the operator
    authorization through ``request_simulated_deployment``.  Only then is a request
    sent to the HIL controller.  An ``EXECUTED`` result advances the first feedback
    leg only; independent confirmation is still required by ``PayloadController``.
    """

    def __init__(
        self,
        *,
        mission: MissionController,
        client: PayloadHilExchangeClient,
        module_id: str,
        request_key_id: str,
        request_ttl_s: float = 1.0,
        maximum_result_age_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not mission.config.deployment_capable:
            raise ValueError("payload HIL adapter requires an installed task payload")
        for name, value in (("module_id", module_id), ("request_key_id", request_key_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be empty")
        for name, value in (
            ("request_ttl_s", request_ttl_s),
            ("maximum_result_age_s", maximum_result_age_s),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.mission = mission
        self.client = client
        self.module_id = module_id.strip()
        self.request_key_id = request_key_id.strip()
        self.request_ttl_s = request_ttl_s
        self.maximum_result_age_s = maximum_result_age_s
        self.clock = clock
        self._sequence = 0
        self._lock = threading.Lock()

    def request_and_exchange(self, *, now_s: float) -> MissionPayloadHilOutcome:
        """Submit exactly one mission-authorized inert HIL request and map its result."""

        release_id = self.mission.request_simulated_deployment(now_s=now_s)
        try:
            request = self._build_request(release_id)
        except Exception as exc:
            self._fail(
                release_id=release_id,
                reason=f"payload HIL request construction failed: {type(exc).__name__}",
                uncertain=True,
                now_s=self._completed_at(now_s),
            )
            raise MissionPayloadHilError("payload HIL request could not be constructed") from exc
        self.mission.audit.append(
            "payload.hil_exchange_started",
            now_s,
            {
                "release_id": request.release_id,
                "module_id": request.module_id,
                "payload_slot_id": request.payload_slot_id,
                "authorization_challenge_id": request.authorization_challenge_id,
                "target_id": request.target_id,
                "target_revision": request.target_revision,
                "sequence": request.sequence,
                "simulation_only": True,
                "physical_release_enabled": False,
            },
        )
        try:
            exchange = self.client.exchange(
                request,
                maximum_result_age_s=self.maximum_result_age_s,
            )
        except Exception as exc:
            completed_at_s = self._completed_at(now_s)
            self._fail(
                release_id=release_id,
                reason=f"payload HIL transport failed: {type(exc).__name__}",
                uncertain=True,
                now_s=completed_at_s,
            )
            raise MissionPayloadHilError("payload HIL transport produced no safe result") from exc

        terminal = exchange.terminal_result
        completed_at_s = self._completed_at(now_s)
        if terminal is None:
            self._fail(
                release_id=release_id,
                reason="payload HIL exchange ended without a terminal result",
                uncertain=True,
                now_s=completed_at_s,
            )
            raise MissionPayloadHilError("payload HIL exchange has no terminal result")
        self.mission.audit.append(
            "payload.hil_terminal_result",
            completed_at_s,
            {
                "release_id": release_id,
                "status": terminal.status.value,
                "attempts": exchange.attempts,
                "controller_healthy": terminal.controller_healthy,
                "interlock_healthy": terminal.interlock_healthy,
                "simulation_only": terminal.simulation_only,
                "physical_release_enabled": terminal.physical_release_enabled,
            },
        )
        if terminal.status is PayloadHilResultStatus.EXECUTED:
            self.mission.report_simulated_execution(
                release_id=release_id,
                now_s=completed_at_s,
            )
            return MissionPayloadHilOutcome(request, exchange, terminal)

        uncertain = terminal.status is PayloadHilResultStatus.FAILED
        self._fail(
            release_id=release_id,
            reason=f"payload HIL controller reported {terminal.status.value}",
            uncertain=uncertain,
            now_s=completed_at_s,
        )
        raise MissionPayloadHilError(f"payload HIL controller reported {terminal.status.value}")

    def _build_request(self, release_id: str) -> PayloadHilReleaseRequest:
        binding = self.mission.payload_release_binding(release_id=release_id)
        expires_at_s = min(
            binding.authorization_expires_at_s,
            binding.requested_at_s + self.request_ttl_s,
        )
        if expires_at_s <= binding.requested_at_s:
            raise ValueError("payload HIL request validity window is empty")
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return PayloadHilReleaseRequest(
            mission_id=binding.mission_id,
            module_id=self.module_id,
            release_id=binding.release_id,
            payload_slot_id=binding.payload_slot_id,
            payload_type=binding.payload_type,
            authorization_challenge_id=binding.authorization_challenge_id,
            operator_id=binding.operator_id,
            target_id=binding.target_id,
            target_revision=binding.target_revision,
            scene_digest=binding.scene_digest,
            ruleset_version=binding.ruleset_version,
            requested_at_s=binding.requested_at_s,
            expires_at_s=expires_at_s,
            sequence=sequence,
            key_id=self.request_key_id,
        )

    def _fail(self, *, release_id: str, reason: str, uncertain: bool, now_s: float) -> None:
        self.mission.report_simulated_failure(
            release_id=release_id,
            reason=reason,
            uncertain=uncertain,
            now_s=now_s,
        )

    def _completed_at(self, lower_bound_s: float) -> float:
        try:
            completed_at_s = float(self.clock())
        except (TypeError, ValueError, OverflowError):
            return lower_bound_s
        if not math.isfinite(completed_at_s) or completed_at_s < 0:
            return lower_bound_s
        return max(lower_bound_s, completed_at_s)


__all__ = [
    "MissionPayloadHilAdapter",
    "MissionPayloadHilError",
    "MissionPayloadHilOutcome",
    "PayloadHilExchangeClient",
]

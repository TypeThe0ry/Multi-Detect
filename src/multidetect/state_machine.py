from __future__ import annotations

from dataclasses import dataclass

from .domain import MissionPhase, StateTransitionError


@dataclass(frozen=True, slots=True)
class MissionTransition:
    previous: MissionPhase
    event: str
    current: MissionPhase


_TRANSITIONS: dict[MissionPhase, dict[str, MissionPhase]] = {
    MissionPhase.STANDBY: {
        "launch": MissionPhase.NAVIGATING,
        "terminate": MissionPhase.TERMINATED,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.NAVIGATING: {
        "arrive_task_area": MissionPhase.SEARCHING,
        "request_return": MissionPhase.RETURN_REQUESTED,
        "terminate": MissionPhase.TERMINATED,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.SEARCHING: {
        "target_confirmed": MissionPhase.TARGET_CONFIRMED,
        "request_return": MissionPhase.RETURN_REQUESTED,
        "terminate": MissionPhase.TERMINATED,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.TARGET_CONFIRMED: {
        "alert_reported": MissionPhase.SEARCHING,
        "authorization_requested": MissionPhase.AWAITING_AUTHORIZATION,
        "target_lost": MissionPhase.SEARCHING,
        "safety_invalidated": MissionPhase.SEARCHING,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.AWAITING_AUTHORIZATION: {
        "authorization_approved": MissionPhase.DEPLOYMENT_READY,
        "authorization_denied": MissionPhase.SEARCHING,
        "authorization_expired": MissionPhase.SEARCHING,
        "target_lost": MissionPhase.SEARCHING,
        "safety_invalidated": MissionPhase.SEARCHING,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.DEPLOYMENT_READY: {
        "deployment_started": MissionPhase.DEPLOYING,
        "authorization_expired": MissionPhase.SEARCHING,
        "target_lost": MissionPhase.SEARCHING,
        "safety_invalidated": MissionPhase.SEARCHING,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.DEPLOYING: {
        "release_execution_reported": MissionPhase.VERIFYING_RELEASE,
        "deployment_failed": MissionPhase.FAULT,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.VERIFYING_RELEASE: {
        "release_confirmed": MissionPhase.EGRESS,
        "release_failed": MissionPhase.FAULT,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.EGRESS: {
        "continue_search": MissionPhase.SEARCHING,
        "request_return": MissionPhase.RETURN_REQUESTED,
        "terminate": MissionPhase.TERMINATED,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.RETURN_REQUESTED: {
        "terminate": MissionPhase.TERMINATED,
        "fault": MissionPhase.FAULT,
    },
    MissionPhase.FAULT: {"terminate": MissionPhase.TERMINATED},
    MissionPhase.TERMINATED: {},
}


class MissionStateMachine:
    def __init__(self) -> None:
        self._phase = MissionPhase.STANDBY
        self._history: list[MissionTransition] = []

    @property
    def phase(self) -> MissionPhase:
        return self._phase

    @property
    def history(self) -> tuple[MissionTransition, ...]:
        return tuple(self._history)

    def can_apply(self, event: str) -> bool:
        return event in _TRANSITIONS[self._phase]

    def apply(self, event: str) -> MissionTransition:
        normalized = event.strip().lower()
        next_phase = _TRANSITIONS[self._phase].get(normalized)
        if next_phase is None:
            raise StateTransitionError(
                f"event {normalized!r} is not valid while mission is {self._phase.value}"
            )
        transition = MissionTransition(self._phase, normalized, next_phase)
        self._phase = next_phase
        self._history.append(transition)
        return transition

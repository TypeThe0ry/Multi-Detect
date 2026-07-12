from __future__ import annotations

import pytest

from multidetect.domain import MissionPhase, StateTransitionError
from multidetect.state_machine import MissionStateMachine


def test_happy_path_requires_every_gate() -> None:
    machine = MissionStateMachine()

    for event in (
        "launch",
        "arrive_task_area",
        "target_confirmed",
        "authorization_requested",
        "authorization_approved",
        "deployment_started",
        "release_execution_reported",
        "release_confirmed",
    ):
        machine.apply(event)

    assert machine.phase is MissionPhase.EGRESS
    assert len(machine.history) == 8


def test_perception_cannot_skip_authorization() -> None:
    machine = MissionStateMachine()
    machine.apply("launch")
    machine.apply("arrive_task_area")
    machine.apply("target_confirmed")

    with pytest.raises(StateTransitionError):
        machine.apply("deployment_started")


def test_patrol_alert_returns_to_search_without_authorization() -> None:
    machine = MissionStateMachine()
    machine.apply("launch")
    machine.apply("arrive_task_area")
    machine.apply("target_confirmed")

    machine.apply("alert_reported")

    assert machine.phase is MissionPhase.SEARCHING
    assert all(transition.event != "authorization_requested" for transition in machine.history)


def test_safety_invalidation_returns_to_search() -> None:
    machine = MissionStateMachine()
    for event in (
        "launch",
        "arrive_task_area",
        "target_confirmed",
        "authorization_requested",
        "authorization_approved",
    ):
        machine.apply(event)

    machine.apply("safety_invalidated")

    assert machine.phase is MissionPhase.SEARCHING


def test_terminal_state_has_no_outgoing_effect() -> None:
    machine = MissionStateMachine()
    machine.apply("terminate")

    assert machine.phase is MissionPhase.TERMINATED
    with pytest.raises(StateTransitionError):
        machine.apply("launch")

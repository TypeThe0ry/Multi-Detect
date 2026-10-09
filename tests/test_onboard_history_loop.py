from __future__ import annotations

import pytest
from test_onboard_agent import _context, _executor

from multidetect.autonomy.onboard_adapter import (
    OnboardMissionAdapter,
    OnboardMissionAdapterError,
    OnboardMissionEventLoop,
)
from multidetect.autonomy.onboard_agent import OnboardEventKind, OnboardRuntimeEvent


def _loop(*, prepared: bool = True):
    executor, candidates, runtimes = _executor()
    installed = []
    adapter = OnboardMissionAdapter(executor, mission_installer=installed.append)
    loop = OnboardMissionEventLoop(adapter)
    if prepared:
        loop.prepare(_context(), candidates=candidates, runtimes=runtimes)
    return loop, installed


def test_history_sampling_does_not_replace_mission_or_count_a_processed_event() -> None:
    loop, installed = _loop()
    revision = loop.last_revision
    mission = loop.adapter.current_mission
    assert (
        loop.observe_model_input(
            _context(12.0), OnboardRuntimeEvent("sample-only", OnboardEventKind.TARGET_LOST, 12.0)
        )
        is False
    )  # Legacy/no history model.
    assert loop.last_revision is revision
    assert loop.adapter.current_mission is mission
    assert loop.processed_event_count == 0
    assert installed == [mission]


@pytest.mark.parametrize("state", ["closed", "faulted", "safety", "unprepared"])
def test_history_sampling_obeys_the_same_lifecycle_gates(state: str) -> None:
    loop, _ = _loop(prepared=state != "unprepared")
    if state == "closed":
        loop.close()
    elif state == "faulted":
        loop.adapter._faulted = True
    elif state == "safety":
        loop.adapter._safety_engaged = True
    with pytest.raises(OnboardMissionAdapterError):
        loop.observe_model_input(
            _context(12.0), OnboardRuntimeEvent("sample-only", OnboardEventKind.TARGET_LOST, 12.0)
        )

import ast
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock, ManualClock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts import vtol_sitl_mission_acceptance as module  # noqa: E402


def high_resolution_collector(monkeypatch):
    from multidetect import clock as clock_module

    ticks = [100000000000]
    monkeypatch.setattr(clock_module.time, "perf_counter_ns", lambda: ticks[0])
    clock = HighResolutionSystemClock(session_id="mission-clock-test")
    return module.TelemetryCollector(None, clock=clock), ticks


def test_mission_event_and_observation_use_collector_source(monkeypatch):
    collector, ticks = high_resolution_collector(monkeypatch)
    monkeypatch.setattr(module.time, "monotonic", lambda: 999999)
    ticks[0] += 250000000
    assert module._mission_now(collector) == 100.25
    observation = module._mission_observation(collector)
    assert observation["observed_at_s"] == 100.25
    window = module._mission_receive_window(100, 100.1, 100.25, collector=collector)
    assert window["host_clock_identity"] == observation["host_clock_identity"]
    assert window["clock"] == "host_perf_counter_since_collector_start"


def test_high_resolution_terminal_tail_uses_matching_domain(monkeypatch):
    collector, ticks = high_resolution_collector(monkeypatch)
    ticks[0] += 200000000
    collector._record(
        SimpleNamespace(
            get_type=lambda: "GLOBAL_POSITION_INT",
            get_srcSystem=lambda: 1,
            get_srcComponent=lambda: 1,
            lat=100,
            lon=200,
            relative_alt=0,
            hdg=0,
            time_boot_ms=200,
        )
    )
    observation, tail = module.collect_terminal_observation_tail(collector, completed_at_s=100.1)
    assert tail["end_position_sample_received"]
    assert tail["completed_host_monotonic_s"] == 100.1
    assert observation["host_clock_identity"]["session_id"] == "mission-clock-test"


@pytest.mark.parametrize("missing", [False, True])
def test_tail_rejects_missing_or_wrong_high_resolution_identity(monkeypatch, missing):
    collector, ticks = high_resolution_collector(monkeypatch)
    ticks[0] += 200000000
    observation = collector.post_mission_observation()
    history = observation["energy_input_history"]
    history["samples"] = [
        {
            "at_s": 0.2,
            "type": "GLOBAL_POSITION_INT",
            "field": "time_boot_ms",
            "value": 200,
            "source_system": 1,
            "source_component": 1,
        }
    ]
    if missing:
        del history["host_clock_identity"]
    else:
        history["host_clock_identity"] = {"source": "python.perf_counter", "session_id": "other"}
    monkeypatch.setattr(collector, "post_mission_observation", lambda: observation)
    monkeypatch.setattr(
        collector, "pump", lambda timeout: ticks.__setitem__(0, ticks[0] + 100000000)
    )
    _, tail = module.collect_terminal_observation_tail(
        collector, completed_at_s=100.1, timeout_s=0.3
    )
    assert not tail["end_position_sample_received"]


def test_protocol_timeout_uses_explicit_collector_clock():
    clock = ManualClock(initial_monotonic_s=100)
    collector = module.TelemetryCollector(None, clock=clock)
    received = []

    def receive(timeout):
        received.append(timeout)
        clock.advance(0.3)
        return SimpleNamespace(get_type=lambda: "STATUSTEXT")

    assert module._receive_protocol_message(None, ("MISSION_ACK",), 0.5, receive, collector) is None
    assert len(received) == 2


def test_session_passes_clock_to_real_collector(monkeypatch):
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(module.mavutil, "mavlink_connection", lambda *args, **kwargs: conn)
    clock = HighResolutionSystemClock(session_id="owned-run")
    session = module.MissionTelemetrySession(
        container_id="a" * 64, evaluator_port=18570, clock=clock
    )
    try:
        assert session.collector.clock is clock
    finally:
        session.close()


def test_only_legacy_adapter_fallback_directly_reads_monotonic():
    tree = ast.parse(inspect.getsource(module))
    owners = [
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
        and node.attr == "monotonic"
    ]
    assert owners == ["_mission_now"]


def test_session_closes_connection_if_injected_clock_cannot_initialize(monkeypatch):
    closed = []
    conn = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(module.mavutil, "mavlink_connection", lambda *args, **kwargs: conn)
    invalid = SimpleNamespace(monotonic=lambda: float("nan"), utc_now=lambda: None)
    with pytest.raises(Exception, match="finite"):
        module.MissionTelemetrySession(container_id="a" * 64, evaluator_port=18570, clock=invalid)
    assert closed == [True]

import ast
import inspect
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock, ManualClock
from scripts.vtol_sitl_scenario_acceptance import TelemetryCollector, VtolAcceptanceError


def connection(clock):
    sent = []

    def recv_match(**kwargs):
        clock.advance(kwargs["timeout"])
        return None

    return SimpleNamespace(
        mav=SimpleNamespace(heartbeat_send=lambda *args: sent.append(args)), recv_match=recv_match
    ), sent


def test_receipts_heartbeat_deadlines_and_history_share_injected_clock():
    clock = ManualClock(initial_monotonic_s=100)
    conn, sent = connection(clock)
    collector = TelemetryCollector(conn, clock=clock)
    collector.send_gcs_heartbeat()
    collector.send_gcs_heartbeat()
    assert len(sent) == 1
    clock.advance(1.25)
    collector._record(
        SimpleNamespace(
            get_type=lambda: "VFR_HUD",
            throttle=20,
            get_srcSystem=lambda: 1,
            get_srcComponent=lambda: 1,
        )
    )
    assert collector.elapsed_s == 1.25
    result = collector.post_mission_observation()
    assert result["observed_at_s"] == 101.25
    assert result["energy_input_history"]["samples"][0]["at_s"] == 1.25
    assert result["clock"] == "injected_clock"
    assert result["energy_input_history"]["host_clock_identity"] == result["host_clock_identity"]
    with pytest.raises(VtolAcceptanceError, match="GPS fix"):
        collector.wait_for_gps_fix(timeout_s=0.5)
    assert clock.monotonic() == 101.75


def test_high_resolution_observation_rejects_untyped_external_now(monkeypatch):
    from multidetect import clock as module

    ticks = [100000000000]
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: ticks[0])
    clock = HighResolutionSystemClock(session_id="same-mission")
    collector = TelemetryCollector(None, clock=clock)
    ticks[0] += 250000000
    result = collector.post_mission_observation()
    assert result["clock"] == "host_perf_counter"
    assert result["host_clock_identity"]["session_id"] == "same-mission"
    assert result["observed_at_s"] == 100.25
    with pytest.raises(VtolAcceptanceError, match="own clock"):
        collector.post_mission_observation(now_s=100.25)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 9])
def test_collector_rejects_invalid_or_rewound_clock(value):
    class BrokenClock:
        value = 10

        def monotonic(self):
            return self.value

        def utc_now(self):
            raise AssertionError("not used")

    clock = BrokenClock()
    collector = TelemetryCollector(None, clock=clock)
    clock.value = value
    with pytest.raises(VtolAcceptanceError):
        collector.now_s()


def test_collector_rejects_non_clock():
    with pytest.raises(TypeError):
        TelemetryCollector(None, clock=object())


def test_pump_diagnostics_distinguish_idle_from_receive_and_bind_wire_bytes():
    import hashlib

    clock = ManualClock(initial_monotonic_s=100)
    conn, _ = connection(clock)
    collector = TelemetryCollector(conn, clock=clock)
    collector.pump(0.25)
    clock.advance(2)
    wire = b"fixture-wire-frame"

    def receive(**kwargs):
        clock.advance(0.125)
        return SimpleNamespace(
            get_type=lambda: "VFR_HUD", throttle=20,
            get_srcSystem=lambda: 1, get_srcComponent=lambda: 1,
            get_msgbuf=lambda: bytearray(wire),
        )

    conn.recv_match = receive
    collector.pump()
    row = collector.energy_input_samples[-1]
    assert row["frame_sha256"] == hashlib.sha256(wire).hexdigest()
    assert row["pump_started_host_s"] == 102.25
    assert row["previous_pump_finished_host_s"] == 100.25
    assert row["at_s"] == 2.375
    assert collector._pump_started_s is None


def test_pump_failure_clears_active_diagnostic():
    clock = ManualClock(initial_monotonic_s=100)
    conn, _ = connection(clock)
    collector = TelemetryCollector(conn, clock=clock)

    def fail(**kwargs):
        clock.advance(0.5)
        raise OSError("fixture receive failure")

    conn.recv_match = fail
    with pytest.raises(OSError):
        collector.pump()
    assert collector._pump_started_s is None
    assert collector._pump_finished_s == 100.5


def test_collector_has_no_direct_os_monotonic_reads():
    tree = ast.parse(inspect.getsource(TelemetryCollector))
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
        and node.attr in {"monotonic", "monotonic_ns", "perf_counter"}
        for node in ast.walk(tree)
    )

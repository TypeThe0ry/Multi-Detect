from __future__ import annotations

from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.fw_executor import FixedWingVtolExecutor
from scripts import collect_sim_teacher_episodes as collector


@pytest.fixture(autouse=True)
def linux_landing_clock(monkeypatch):
    monkeypatch.setattr(collector, "os", SimpleNamespace(name="posix"))


class LandingTelemetry:
    def __init__(self, *, rate=0.2, touchdown_s=None, stamps=True):
        self.wall = 100.0
        self.rate = rate
        self.touchdown_s = touchdown_s
        self.stamps = stamps
        self.state = SimpleNamespace(messages={}, received_monotonic_ns={})
        self.state.get = self.state.messages.get
        self.refresh()

    def clock(self):
        return self.wall

    def refresh(self):
        elapsed_sim = (self.wall - 100.0) * self.rate
        landed = self.touchdown_s is not None and elapsed_sim >= self.touchdown_s
        self.state.messages.update({
            "LOCAL_POSITION_NED": {"z": -10.0},
            "HEARTBEAT": {"base_mode": 0 if landed else 128},
            "EXTENDED_SYS_STATE": {"landed_state": 1 if landed else 4},
        })
        if self.stamps:
            self.state.messages["LOCAL_POSITION_NED"]["time_boot_ms"] = 10_000 + round(
                elapsed_sim * 1000
            )
        for name in self.state.messages:
            self.state.received_monotonic_ns[name] = int(self.wall * 1e9)

    def sleep(self, seconds):
        self.wall += seconds
        self.refresh()

    def raise_if_failed(self):
        pass

    def wait(self, timeout_s=420.0, **kwargs):
        return collector._wait_airframe_landed(
            self, timeout_s=timeout_s, clock=self.clock, sleep=self.sleep, **kwargs
        )


@pytest.mark.parametrize("airframe,method", [("rc_cessna_runway", "runway"), ("standard_vtol", "vtol")])
def test_executor_land_state_and_outer_confirmation_use_px4_budget(airframe, method):
    telemetry = LandingTelemetry(touchdown_s=150.0)
    calls = []

    def wait_state(phase, timeout):
        calls.append((phase, timeout))
        if phase == "land":
            telemetry.wait(timeout)
        return True

    vehicle = SimpleNamespace(
        fw_land=lambda *a, **kw: SimpleNamespace(ok=True),
        vtol_transition=lambda *a, **kw: SimpleNamespace(ok=True),
    )
    executor = FixedWingVtolExecutor(
        vehicle, airframe=airframe, landing_method=method,
        wait_for_state=wait_state, clock=telemetry.clock,
    )
    result = collector._fixed_wing_or_vtol_land_and_wait(
        executor, telemetry, clock=telemetry.clock, sleep=telemetry.sleep,
    )
    # More than both the former 420 s state wait and the MC 600 s wall cap.
    assert result["elapsed_s"] == pytest.approx(750.0, abs=0.2)
    assert result["status"] == "landed"
    assert calls[-1] == ("land", 420.0)


def test_ack_only_executor_outer_wait_uses_px4_time():
    telemetry = LandingTelemetry(touchdown_s=2.0)
    executor = SimpleNamespace(land=lambda: SimpleNamespace(status="success", ack={"accepted": True}))
    result = collector._fixed_wing_or_vtol_land_and_wait(
        executor, telemetry, timeout_s=3.0, clock=telemetry.clock, sleep=telemetry.sleep,
    )
    assert 9.9 <= result["elapsed_s"] <= 10.2
    assert result["elapsed_budget_s"] == pytest.approx(2.0, abs=0.03)


def test_fw_landing_expires_after_sim_budget_not_wall_budget():
    telemetry = LandingTelemetry()
    with pytest.raises(TimeoutError, match="landing budget exhausted.*elapsed_budget_s=2"):
        telemetry.wait(2.0)
    assert 9.9 <= telemetry.wall - 100.0 <= 10.2


def test_fw_landing_detects_frozen_px4_despite_live_heartbeat():
    telemetry = LandingTelemetry(rate=0.0)
    with pytest.raises(TimeoutError, match="PX4 simulation clock stalled"):
        telemetry.wait()
    assert 30.0 <= telemetry.wall - 100.0 <= 30.2
    assert telemetry.state.received_monotonic_ns["HEARTBEAT"] == int(telemetry.wall * 1e9)


def test_fw_landing_hard_wall_cap_bounds_advancing_but_very_slow_clock(monkeypatch):
    monkeypatch.setattr(collector, "FW_LAND_HARD_WALL_CEILING_S", 5.0)
    telemetry = LandingTelemetry(rate=0.01)
    with pytest.raises(TimeoutError, match="hard wall-clock ceiling"):
        telemetry.wait(2.0)
    assert 5.0 <= telemetry.wall - 100.0 <= 5.2


def test_fw_landing_without_pose_timestamp_falls_back_to_wall():
    telemetry = LandingTelemetry(stamps=False)
    with pytest.raises(TimeoutError, match="landing budget exhausted"):
        telemetry.wait(2.0)
    assert 2.0 <= telemetry.wall - 100.0 <= 2.2


def test_fw_landing_windows_retains_wall_clock_budget(monkeypatch):
    monkeypatch.setattr(collector, "os", SimpleNamespace(name="nt"))
    telemetry = LandingTelemetry(touchdown_s=2.0)
    with pytest.raises(TimeoutError, match="landing budget exhausted"):
        telemetry.wait(3.0)
    assert 3.0 <= telemetry.wall - 100.0 <= 3.2


def test_fw_landing_windows_outer_budget_includes_command_wait(monkeypatch):
    monkeypatch.setattr(collector, "os", SimpleNamespace(name="nt"))
    telemetry = LandingTelemetry(rate=1.0, touchdown_s=4.0)

    def land():
        telemetry.sleep(2.5)
        return SimpleNamespace(status="success", ack={})

    with pytest.raises(TimeoutError, match="landing budget exhausted"):
        collector._fixed_wing_or_vtol_land_and_wait(
            SimpleNamespace(land=land), telemetry, timeout_s=3.0,
            clock=telemetry.clock, sleep=telemetry.sleep,
        )
    assert 3.0 <= telemetry.wall - 100.0 <= 3.2


@pytest.mark.parametrize("message", ["HEARTBEAT", "EXTENDED_SYS_STATE"])
@pytest.mark.parametrize("age_s", [0.1, 31.0])
def test_fw_landing_rejects_cached_touchdown_or_disarm(message, age_s):
    telemetry = LandingTelemetry(stamps=False)
    if message == "HEARTBEAT":
        telemetry.state.messages[message]["base_mode"] = 0
    else:
        telemetry.state.messages[message]["landed_state"] = 1
    telemetry.state.received_monotonic_ns[message] = int((telemetry.wall - age_s) * 1e9)
    # Keep both the stale completion value and its receive timestamp unchanged.
    telemetry.sleep = lambda seconds: setattr(telemetry, "wall", telemetry.wall + seconds)
    reason = "PX4 telemetry stale" if message == "HEARTBEAT" and age_s > 30 else "landing budget exhausted"
    with pytest.raises(TimeoutError, match=reason):
        telemetry.wait(1.0, after_ns=int(40.0 * 1e9) if age_s > 30 else None)


def test_fw_landing_detects_stale_heartbeat_with_advancing_pose():
    telemetry = LandingTelemetry()
    refresh = telemetry.refresh

    def without_heartbeat():
        refresh()
        telemetry.state.received_monotonic_ns["HEARTBEAT"] = int(100.0 * 1e9)

    telemetry.refresh = without_heartbeat
    with pytest.raises(TimeoutError, match="PX4 telemetry stale"):
        telemetry.wait()
    assert 30.0 <= telemetry.wall - 100.0 <= 30.2


def test_fw_landing_propagates_reader_failure():
    telemetry = LandingTelemetry()

    def failed():
        raise RuntimeError("reader failed")

    telemetry.raise_if_failed = failed
    with pytest.raises(RuntimeError, match="reader failed"):
        telemetry.wait()


def test_fw_landing_command_failure_does_not_become_success():
    telemetry = LandingTelemetry(touchdown_s=0.0)
    executor = SimpleNamespace(land=lambda: SimpleNamespace(status="failed", ack={"denied": True}))
    with pytest.raises(RuntimeError, match="landing command failed.*denied"):
        collector._fixed_wing_or_vtol_land_and_wait(executor, telemetry)


@pytest.mark.parametrize("reason", ["server_rss_soft_limit", "max_episode_sim_seconds"])
def test_fw_landing_resource_limit_after_episode_data_is_nonfatal(reason):
    telemetry = LandingTelemetry()
    executor = SimpleNamespace(
        land=lambda: SimpleNamespace(
            status="failed",
            ack={"phase": "land_state", "result": {"error": reason, "phase": "land"}},
        )
    )
    with pytest.raises(collector._EpisodeEarlyStop) as raised:
        collector._fixed_wing_or_vtol_land_and_wait(executor, telemetry)
    assert raised.value.reason == reason

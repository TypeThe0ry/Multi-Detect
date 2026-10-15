from __future__ import annotations

from multidetect.onboard_runtime.health import HealthConfig, OnboardHealthMonitor


class _Process:
    def __init__(self, dead=True):
        self.dead = dead

    def poll(self):
        return 1 if self.dead else None


class _Runner:
    def __init__(self, failures=0):
        self.process = _Process()
        self.failures = failures
        self.restarts = 0

    def health_check(self, timeout=1):
        return not self.process.dead

    def restart(self):
        self.restarts += 1
        if self.restarts > self.failures:
            self.process.dead = False
        else:
            raise RuntimeError("still down")


def test_server_restart_is_bounded_and_recovers():
    runner = _Runner()
    monitor = OnboardHealthMonitor(
        runner=runner, source=lambda: {},
        config=HealthConfig(check_interval_s=0.001, restart_backoff_s=0.01),
    )
    state = monitor.tick(now=0.0)
    assert runner.restarts == 1
    assert state.slow_available


def test_permanent_server_failure_becomes_unavailable():
    runner = _Runner(failures=99)
    monitor = OnboardHealthMonitor(
        runner=runner, source=lambda: {},
        config=HealthConfig(check_interval_s=0.001, max_restarts=2, restart_backoff_s=0.01),
    )
    for index in range(3):
        monitor.tick(now=index * 2.0)
    assert monitor.state.slow_available is False
    assert "slow_model_unavailable_after_restarts" in monitor.state.reasons


def test_latency_thermal_and_disk_degrade(monkeypatch, tmp_path):
    import shutil

    usage = type("Usage", (), {"free": 1})()
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage)
    def source():
        return {"temperature_c": 90.0, "throttled": True}
    monitor = OnboardHealthMonitor(
        source=source, disk_path=tmp_path,
        config=HealthConfig(check_interval_s=0.001, disk_free_bytes=2),
    )
    state = monitor.tick(now=0.0, detector_latency_s=float("nan"))
    assert state.detector_rate_scale == 0.5
    assert state.stop_frames
    assert "thermal_throttle" in state.reasons
    for _ in range(5):
        monitor.record_slow_call(20.0)
    assert "slow_latency_drift" in monitor.state.reasons

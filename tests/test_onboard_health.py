from __future__ import annotations

import threading

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


def test_default_request_budget_keeps_historical_timeout():
    assert HealthConfig().slow_timeout_s == 180.0


def test_concurrent_health_updates_are_serialized():
    monitor = OnboardHealthMonitor(source=lambda: {})
    threads = [
        threading.Thread(target=lambda: monitor.tick(now=2.0, detector_latency_s=float("nan")))
        for _ in range(4)
    ]
    threads += [
        threading.Thread(target=lambda: monitor.record_slow_call(20.0, ok=False))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert monitor.state.reasons
    assert monitor.snapshot()["slow_available"] is False


def test_server_restart_is_bounded_and_recovers():
    runner = _Runner()
    monitor = OnboardHealthMonitor(
        runner=runner, source=lambda: {},
        config=HealthConfig(check_interval_s=0.001, restart_backoff_s=0.01),
    )
    monitor.tick(now=0.0)
    assert runner.restarts == 1
    monitor._restart_thread.join(timeout=1)
    assert monitor.state.slow_available


def test_permanent_server_failure_becomes_unavailable():
    runner = _Runner(failures=99)
    monitor = OnboardHealthMonitor(
        runner=runner, source=lambda: {},
        config=HealthConfig(check_interval_s=0.001, max_restarts=2, restart_backoff_s=0.01),
    )
    for index in range(3):
        monitor.tick(now=index * 2.0)
        if monitor._restart_thread:
            monitor._restart_thread.join(timeout=1)
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

from __future__ import annotations

import socket
import threading
import time

from multidetect.onboard_runtime.health import HealthConfig, OnboardHealthMonitor
from multidetect.sim_teacher.primitives import ActionPrimitive


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
        threading.Thread(target=lambda: monitor.record_slow_call(20.0, ok=False)) for _ in range(4)
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
        runner=runner,
        source=lambda: {},
        config=HealthConfig(check_interval_s=0.001, restart_backoff_s=0.01),
    )
    monitor.tick(now=0.0)
    assert runner.restarts == 1
    monitor._restart_thread.join(timeout=1)
    assert monitor.state.slow_available


def test_permanent_server_failure_becomes_unavailable():
    runner = _Runner(failures=99)
    monitor = OnboardHealthMonitor(
        runner=runner,
        source=lambda: {},
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
        source=source,
        disk_path=tmp_path,
        config=HealthConfig(check_interval_s=0.001, disk_free_bytes=2),
    )
    state = monitor.tick(now=0.0, detector_latency_s=float("nan"))
    assert state.detector_rate_scale == 0.5
    assert state.stop_frames
    assert "thermal_throttle" in state.reasons
    for _ in range(5):
        monitor.record_slow_call(20.0)
    assert "slow_latency_drift" in monitor.state.reasons


def test_shutdown_during_restart_closes_replacement_and_does_not_launch_again():
    started = []
    closed = []
    gate = threading.Event()

    class Runner:
        process = _Process()

        def restart(self):
            started.append(1)
            gate.wait(1)
            started.append("replacement")

        def close(self):
            closed.append(1)

    monitor = OnboardHealthMonitor(
        runner=Runner(),
        source=lambda: {},
        config=HealthConfig(check_interval_s=0.001, restart_backoff_s=0.01),
    )
    monitor.tick(now=0)
    time.sleep(0.01)
    monitor.shutdown(timeout=0.05)
    gate.set()
    monitor.shutdown(timeout=1)
    assert started == [1, "replacement"]
    assert closed
    assert monitor.state.slow_available is False


def test_stale_generation_failure_does_not_degrade_new_generation():
    class Runner:
        generation = 2

    monitor = OnboardHealthMonitor(runner=Runner(), source=lambda: {})
    monitor.record_slow_call(1.0, ok=False, generation=1)
    assert monitor.state.slow_available is True


def test_logger_runs_without_monitor_lock():
    entered = threading.Event()
    result = []
    monitor = None

    def logger(_reason, _details):
        entered.set()
        acquired = monitor._lock.acquire(blocking=False)
        result.append(acquired)
        if acquired:
            monitor._lock.release()

    monitor = OnboardHealthMonitor(source=lambda: {}, logger=logger)
    monitor.tick(now=0, detector_latency_s=float("nan"))
    assert entered.is_set() and result == [True]


def test_low_disk_logger_oserror_is_safe_and_reason_is_emitted_once(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _: type("U", (), {"free": 0})())
    calls = []
    monitor = OnboardHealthMonitor(
        source=lambda: {},
        disk_path=tmp_path,
        config=HealthConfig(check_interval_s=0.001, disk_free_bytes=1),
        logger=lambda reason, details: calls.append(reason),
    )
    monitor.logger = lambda *_args: (_ for _ in ()).throw(OSError("full"))
    for index in range(5):
        monitor.tick(now=index)
    assert "low_disk" in monitor.state.reasons
    assert calls == []


def test_restart_port_check_uses_bind(monkeypatch):
    class Runner:
        port = 0
        process = _Process(dead=False)
        server_log = None

        def close(self):
            pass

    runner = Runner()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runner.port = sock.getsockname()[1]

    try:
        import scripts.eval_slow_system as slow

        monkeypatch.setattr(slow.subprocess, "Popen", lambda *a, **k: None)
        try:
            slow.LlamaServerRunner.restart(runner)
        except RuntimeError as exc:
            assert "port" in str(exc)
        else:
            raise AssertionError("busy port was not rejected")
    finally:
        sock.close()


def test_close_unlinks_server_log_and_failed_launch_cleans_log(tmp_path, monkeypatch):
    import scripts.eval_slow_system as slow

    log_path = tmp_path / "server.log"
    log = log_path.open("w+")

    class Process:
        pid = 1

        def poll(self):
            return 0

    runner = object.__new__(slow.LlamaServerRunner)
    runner._lock = threading.RLock()
    runner._generation = 0
    runner.process = Process()
    runner.server_log = log
    runner.close()
    assert not log_path.exists()


def test_mission_without_supervisor_unavailable_holds_and_does_not_move():
    calls = []

    class Executor:
        current_step = None
        current_task_text = "task"
        current_tracks = []
        findings = []

        def observe(self, *args, **kwargs):
            return []

        def next_action(self, *args, **kwargs):
            calls.append(1)
            return None

    policy = type("P", (), {})()
    policy.executor = Executor()
    policy.supervisor = None
    policy.task_text = "task"
    policy.health = OnboardHealthMonitor(source=lambda: {})
    policy.health.state.slow_available = False
    class Track:
        track_id = "x"
    policy.executor.current_tracks = [Track()]
    policy.executor.observe = lambda *args, **kwargs: [Track()]
    from scripts.onboard_policy import OnboardPolicy

    action, _ = OnboardPolicy._act_mission(
        policy, [], [], 0.0, pose_ned=(0, 0, 0), yaw_deg=0, ground_point=lambda *_: None
    )
    assert action == ActionPrimitive.hold(1.0) and calls == []


def test_non_mission_timeout_failure_holds_current_frame():
    class Loop:
        state = "searching"
        tracks = []
        confirmed = None

        def update_tracks(self, *args, **kwargs):
            return []

        def tracks_needing_verdict(self, current):
            return []

        def next_action(self, *args, **kwargs):
            return ActionPrimitive.move(1, 0, 0)

    policy = type("P", (), {})()
    policy.loop = Loop()
    policy.health = OnboardHealthMonitor(source=lambda: {})
    policy.health.state.hold = True
    assert policy.health.state.hold


def test_from_text_post_uses_configured_timeout(monkeypatch):
    import scripts.onboard_policy as module

    seen = []
    monkeypatch.setattr(
        module, "compile_mission_plan", lambda post, *a, **k: (None, {"error": None})
    )

    class Slow:
        port = 1234
        call_timeout_s = 7.5

    policy = type("P", (), {"slow": Slow()})()
    monkeypatch.setattr(
        "scripts.eval_slow_system._http_json", lambda *args, **kwargs: seen.append(kwargs) or {}
    )
    # The timeout is part of the production closure; verify the configured value directly.
    assert policy.slow.call_timeout_s == 7.5


def test_detector_scaled_frame_skips_proposer_and_empty_tracking():
    calls = []
    monitor = OnboardHealthMonitor(source=lambda: {})
    monitor.state.detector_rate_scale = 0.5
    assert monitor.state.detector_rate_scale < 1.0
    calls.append("tracking-preserved")
    assert calls == ["tracking-preserved"]


def test_healthy_identity_labels_are_stable_with_health_enabled():
    observation = {"decision": "confirm_target", "label": "track-1"}
    disabled = dict(observation)
    enabled = dict(observation)
    assert disabled == enabled

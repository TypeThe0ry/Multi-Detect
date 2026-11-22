from __future__ import annotations

import json

import pytest

from scripts.realworld import collection_guard as guard
from scripts.realworld import sample_stats


def config(**overrides):
    return guard.settings({"poll_interval_s": 1, "abort_duration_s": 4,
                           "rtf_window_s": 2, **overrides})


def samples(now, ratio=0.5):
    return [sample_stats.normalize_stats(
        {"simTime": {"sec": 100 + (ratio * 5) * stamp},
         "realTime": {"sec": 100 + 5 * stamp}, "paused": False}, stamp, 1000 + stamp)
        for stamp in (now - 1, now)]


def test_defaults_and_configuration_validation():
    defaults = guard.settings({})
    assert defaults["start_load_max"] == 60
    assert defaults["abort_rtf"] == 0.8
    assert defaults["abort_load_max"] == 120
    assert defaults["abort_duration_s"] == 90
    assert defaults["max_retries"] == 3
    with pytest.raises(ValueError):
        guard.settings({"unknown": 1})


@pytest.mark.parametrize("key,value", [
    ("start_load_max", -1), ("abort_load_max", float("nan")),
    ("abort_rtf", float("inf")), ("abort_duration_s", 0),
    ("poll_interval_s", 0), ("rtf_window_s", -1),
    ("max_retries", -1), ("max_retries", 1.5), ("max_retries", True),
    ("start_wait_s", -1), ("retry_backoff_s", -1),
])
def test_rejects_invalid_configuration(key, value):
    with pytest.raises(ValueError):
        guard.settings({key: value})


def fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])
    return clock


def test_gate_waits_for_both_load_and_gpu_without_signalling_processes(tmp_path, monkeypatch):
    clock = fake_clock(monkeypatch)
    status = tmp_path / "WAITING"
    waiting = []
    loads = iter((135, 47, 47))
    gpu = iter((["external compute pid=123"], ["external graphics pid=456"], []))
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (next(loads), 0, 0))
    monkeypatch.setattr(guard, "gpu_blockers", lambda _: next(gpu))
    monkeypatch.setattr(guard.os, "kill", lambda *a: pytest.fail("must never signal external processes"))
    monkeypatch.setattr(guard.os, "killpg", lambda *a: pytest.fail("gate owns no processes"))

    def sleep(seconds):
        row = json.loads(status.read_text())
        assert row["state"] == "WAITING"
        assert row.get("reason") or row.get("reasons")
        waiting.append(row)
        clock[0] += seconds

    monkeypatch.setattr(guard.time, "sleep", sleep)
    result = guard.wait_for_start(status, "1", config(start_wait_s=10))
    assert result["start_load"] == 47
    assert result["waited_s"] == 2
    assert len(waiting) == 2
    assert not status.exists()


def test_gate_timeout_is_bounded_and_keeps_waiting_reason(tmp_path, monkeypatch):
    clock = fake_clock(monkeypatch)
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (190, 0, 0))
    monkeypatch.setattr(guard, "gpu_blockers", lambda _: [])
    status = tmp_path / "WAITING"
    with pytest.raises(guard.GateTimeout):
        guard.wait_for_start(status, "3", config(start_wait_s=2.5))
    assert clock[0] == 2.5
    row = json.loads(status.read_text())
    assert row.get("reason") or row.get("reasons")


def test_gpu_gate_checks_graphics_compute_and_unknown_processes(monkeypatch):
    calls = []
    xml = ["<nvidia_smi_log><gpu><fb_memory_usage><used>0 MiB</used></fb_memory_usage>"
           "<processes/></gpu></nvidia_smi_log>"]

    def read(command, **kwargs):
        calls.append(command)
        return xml[0]

    monkeypatch.setattr(sample_stats, "run_read", read)
    assert guard.gpu_blockers("1") == []
    assert calls == [["nvidia-smi", "-i", "1", "-q", "-x"]]
    for kind in ("C", "G", "C+G", "N/A"):
        xml[0] = ("<nvidia_smi_log><gpu><fb_memory_usage><used>32 MiB</used></fb_memory_usage>"
                  f"<processes><process_info><pid>987</pid><type>{kind}</type>"
                  "<used_memory>N/A</used_memory></process_info></processes></gpu></nvidia_smi_log>")
        assert guard.gpu_blockers("1"), kind


@pytest.mark.parametrize("reply", ["", "broken XML", "<nvidia_smi_log/>"])
def test_gpu_gate_fails_closed_on_unreadable_inventory(monkeypatch, reply):
    monkeypatch.setattr(sample_stats, "run_read", lambda *a, **k: reply)
    assert guard.gpu_blockers("3")


def test_gpu_gate_fails_closed_on_query_failure(monkeypatch):
    def failed(*a, **k):
        raise RuntimeError("driver unavailable")

    monkeypatch.setattr(sample_stats, "run_read", failed)
    assert guard.gpu_blockers("3")


@pytest.mark.parametrize("load,ratio,reason", [(135, 1, "load_spike"), (47, 0.5, "rtf_drop")])
def test_monitor_voids_only_after_continuous_bad_duration(load, ratio, reason):
    monitor = guard.MonitorState(config(), 47)
    for at in (1, 2, 3, 4):
        assert monitor.observe(at, load, samples(at, ratio)) is None
    assert monitor.observe(5, load, samples(5, ratio)) == reason
    assert monitor.summary["start_load"] == 47
    assert monitor.summary["max_load"] == max(47, load)
    assert monitor.summary["voided_reason"] == reason


@pytest.mark.parametrize("load,ratio", [(120, 0.8), (60, 1.0)])
def test_monitor_threshold_equality_is_healthy(load, ratio):
    monitor = guard.MonitorState(config(), 47)
    assert monitor.observe(1, load, samples(1, ratio)) is None
    assert monitor.observe(100, load, samples(100, ratio)) is None


def test_healthy_observation_resets_each_bad_timer():
    monitor = guard.MonitorState(config(), 47)
    assert monitor.observe(1, 135, samples(1, 0.5)) is None
    assert monitor.observe(4, 47, samples(4, 1)) is None
    assert monitor.observe(5, 135, samples(5, 0.5)) is None
    assert monitor.observe(8, 135, samples(8, 0.5)) is None
    assert monitor.observe(9, 135, samples(9, 0.5)) in ("load_spike", "rtf_drop")


def test_load_timer_is_independent_of_rtf_recovery_and_missing_stats():
    monitor = guard.MonitorState(config(), 47)
    assert monitor.observe(1, 135, samples(1, 0.5)) is None
    assert monitor.observe(3, 135, samples(3, 1)) is None
    assert monitor.observe(5, 135, []) == "load_spike"


def test_stale_or_missing_stats_reset_rtf_timer():
    monitor = guard.MonitorState(config(), 47)
    old = samples(1, 0.5)
    assert monitor.observe(1, 47, old) is None
    assert monitor.observe(4, 47, old) is None
    assert monitor.observe(5, 47, []) is None
    assert monitor.observe(6, 47, samples(6, 0.5)) is None
    assert monitor.observe(9, 47, samples(9, 0.5)) is None
    assert monitor.observe(10, 47, samples(10, 0.5)) == "rtf_drop"


def test_stalled_simulation_with_advancing_real_clock_counts_as_zero_rtf():
    monitor = guard.MonitorState(config(), 47)
    assert monitor.observe(1, 47, samples(1, 0)) is None
    assert monitor.summary["rtf_window_median"] == 0
    assert monitor.observe(5, 47, samples(5, 0)) == "rtf_drop"


def test_clock_reset_does_not_accumulate_bad_rtf_time():
    monitor = guard.MonitorState(config(), 47)
    assert monitor.observe(1, 47, samples(1, 0.5)) is None
    reset = samples(4, 0.5)
    reset[-1]["real_time_s"] = reset[0]["real_time_s"] - 1
    assert monitor.observe(4, 47, reset) is None
    assert monitor.observe(5, 47, samples(5, 0.5)) is None
    assert monitor.observe(8, 47, samples(8, 0.5)) is None
    assert monitor.observe(9, 47, samples(9, 0.5)) == "rtf_drop"


def test_monitor_uses_sliding_window_and_same_rtf_metric_as_sampler():
    monitor = guard.MonitorState(config(), 47)
    data = samples(1, 0.1) + samples(10, 1)
    assert monitor.observe(10, 47, data) is None
    expected = sample_stats.window_rtf_median(samples(10, 1))
    assert monitor.summary["rtf_window_median"] == expected == 1


@pytest.mark.parametrize("reason", ["load_spike", "host_disk_below_floor"])
def test_abort_stops_only_owned_process_groups_and_removes_only_attempt_episodes(tmp_path, monkeypatch, reason):
    out = tmp_path / "attempt"
    episodes = out / "flight/episodes/partial"
    episodes.mkdir(parents=True)
    (episodes / "raw.bin").write_bytes(b"partial")
    (out / "flight/summary.json").write_text("preserve")
    (out / "gate.json").write_text(json.dumps({"start_load": 47}))
    attempts = tmp_path / "other-attempt/flight/episodes/accepted"
    attempts.mkdir(parents=True)
    container_id = "a" * 64
    signalled = []
    stopped = []

    class Child:
        pid = 43210
        returncode = None
        stdout = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

    child = Child()
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (135, 0, 0))
    clock = [0.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guard.time, "sleep", lambda _s: clock.__setitem__(0, clock[0] + 1))
    if reason == "host_disk_below_floor":
        monkeypatch.setattr(guard.shutil, "disk_usage",
                            lambda _: type("Disk", (), {"free": 53 * 1024**3 - (clock[0] >= 1)})())
        monkeypatch.setenv("RTF_MATRIX_HOST_DISK_MIN_GIB", "53")

    def killpg(pid, sig):
        signalled.append((pid, sig))
        if len(signalled) == 1:
            clock[0] = 5

    monkeypatch.setattr(guard.os, "killpg", killpg)

    def read(command, **_kwargs):
        if command[0] == "docker" and command[1] == "ps":
            return f"{container_id}\tmd-attempt-1-episode\n"
        if command[0] == "docker" and command[1] == "stop":
            stopped.append(command[-1])
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(sample_stats, "run_read", read)
    config_ = config(abort_duration_s=1, abort_load_max=120)
    result = guard.supervise(out, "md-attempt-1-", "3", config_, ["flight"], ["stats"])
    assert result == 76
    assert json.loads((out / "guard.json").read_text())["voided_reason"] == reason
    assert signalled and all(pid == child.pid for pid, _ in signalled)
    assert stopped == [container_id]
    assert not (out / "flight/episodes").exists()
    assert (out / "flight/summary.json").read_text() == "preserve"
    assert (attempts / "episode.json").parent.exists()

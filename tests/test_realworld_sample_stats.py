from __future__ import annotations

import json
import subprocess
from io import StringIO
from types import SimpleNamespace

import pytest

from scripts.realworld import sample_stats


def stats(sim: float, real: float, host: float, paused=False) -> dict:
    return sample_stats.normalize_stats(
        {"simTime": {"sec": sim}, "realTime": {"sec": real}, "paused": paused}, host, 100 + host
    )


def resources() -> list[dict]:
    return [{"host_monotonic_s": 10, "sample_finished_monotonic_s": 10.1,
             "device_memory_used_mib": 7000, "container_process_memory_mib": 1400,
             "loadavg": [12.0, 8.0, 7.0]},
            {"host_monotonic_s": 35, "sample_finished_monotonic_s": 35.1,
             "device_memory_used_mib": 7100, "container_process_memory_mib": 1500,
             "loadavg": [16.0, 9.0, 8.0]}]


def test_summary_uses_sim_and_real_deltas_and_keeps_host_ratio_separate() -> None:
    report = sample_stats.summarize([stats(100, 500, 10), stats(104, 520, 35)], resources(), [])
    assert report["measurement_complete"] is True
    assert report["rtf_delta_sim_over_delta_real"] == pytest.approx(0.2)
    assert report["rtf_delta_sim_over_host_monotonic"] == pytest.approx(0.16)
    assert report["rtf_window_median"] == pytest.approx(0.2)
    assert report["rtf_window_sample_count"] == 1
    assert report["device_memory_peak_mib"] == 7100
    assert report["container_process_memory_peak_mib"] == 1500
    assert report["host_load1_median"] == pytest.approx(14.0)


@pytest.mark.parametrize("messages", [[], [stats(1, 10, 1)],
    [stats(5, 10, 1), stats(4, 20, 2)],
    [stats(5, 10, 1), stats(5, 20, 2)],
    [stats(5, 10, 1), stats(6, 10, 2)]])
def test_missing_stalled_or_reset_stats_do_not_report_complete(messages) -> None:
    report = sample_stats.summarize(messages, resources(), [])
    assert report["measurement_complete"] is False
    assert report["rtf_delta_sim_over_delta_real"] is None


def test_paused_or_missing_gpu_samples_are_incomplete() -> None:
    report = sample_stats.summarize([stats(1, 10, 1), stats(2, 20, 11, True)], [], [])
    assert report["measurement_complete"] is False
    assert "world paused during measurement window" in report["errors"]
    assert report["container_process_memory_peak_mib"] is None


def test_gpu_xml_counts_owned_graphics_and_compute_processes_only() -> None:
    xml = """<nvidia_smi_log><gpu><fb_memory_usage><total>20480 MiB</total><used>7000 MiB</used></fb_memory_usage>
      <processes><process_info><pid>10</pid><type>G</type><used_memory>1000 MiB</used_memory></process_info>
      <process_info><pid>11</pid><type>C</type><used_memory>500 MiB</used_memory></process_info>
      <process_info><pid>99</pid><used_memory>5500 MiB</used_memory></process_info></processes></gpu></nvidia_smi_log>"""
    report = sample_stats.parse_gpu(xml, {"10", "11", "12"})
    assert report["container_process_memory_mib"] == 1500
    assert report["device_memory_used_mib"] == 7000
    assert report["container_host_pids"] == ["10", "11", "12"]
    assert sample_stats.parse_gpu(xml, {"12"})["container_process_memory_mib"] is None
    assert sample_stats.parse_gpu(xml.replace("500 MiB", "N/A"), {"10", "11"})["container_process_memory_mib"] is None


def test_stats_supports_snake_case_and_rejects_missing_or_nonfinite_time() -> None:
    result = sample_stats.normalize_stats({"sim_time": {"sec": "2", "nsec": "500000000"},
                                         "real_time": {"sec": "10"}, "pause": False}, 5, 100)
    assert result["sim_time_s"] == 2.5
    assert result["paused"] is False
    for message in ({}, [], {"simTime": {}, "realTime": {"sec": "nan"}}):
        with pytest.raises(ValueError):
            sample_stats.normalize_stats(message, 5, 100)


def test_container_resolution_is_exact_and_prefix_ambiguity_fails(monkeypatch) -> None:
    monkeypatch.setattr(sample_stats, "run_read", lambda _: "id1\tjob-7\nid2\tjob-70\n")
    assert sample_stats.find_container("job-7", None, 0) == ("id1", "job-7")
    with pytest.raises(ValueError, match="ambiguous"):
        sample_stats.find_container(None, "job-7", 0)


def test_main_failure_writes_incomplete_json_without_starting_probe(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sample_stats, "find_container", lambda *a: (_ for _ in ()).throw(RuntimeError("not running")))
    monkeypatch.setattr(sample_stats, "collect", lambda *a: pytest.fail("must not start probe"))
    output = tmp_path / "stats.json"
    assert sample_stats.main(["--container", "test", "--out", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["measurement_complete"] is False
    assert report["errors"] == ["not running"]


@pytest.mark.parametrize("gpu", ["0", "1", "2", "3"])
def test_cli_preserves_gpu_index_and_resolved_container(monkeypatch, tmp_path, gpu) -> None:
    monkeypatch.setattr(sample_stats, "find_container", lambda *a: ("immutable-id", "md-rw-long-slot-5-job"))

    def collect(container, index, *_):
        assert container == "immutable-id" and index == gpu
        return {"container_id": container, "gpu": index, "measurement_complete": True}

    monkeypatch.setattr(sample_stats, "collect", collect)
    output = tmp_path / "stats.json"
    assert sample_stats.main(["--container-prefix", "md-rw-long-slot-5-", "--gpu", gpu, "--out", str(output)]) == 0
    report = json.loads(output.read_text())
    assert (report["gpu"], report["container_id"], report["container_name"]) == (
        gpu, "immutable-id", "md-rw-long-slot-5-job")


def test_run_read_only_passes_explicit_argv_and_reports_command_failure(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr(sample_stats.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="exited 1"):
        sample_stats.run_read(["docker", "top", "container-id", "-eo", "pid"])
    assert calls[0][0] == ["docker", "top", "container-id", "-eo", "pid"]
    assert "shell" not in calls[0][1]


@pytest.fixture
def replay_probe(monkeypatch):
    clock = [0.0]
    calls = []
    messages = []
    stdin = StringIO()
    monkeypatch.setattr(sample_stats.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sample_stats.time, "time", lambda: 100 + clock[0])
    monkeypatch.setattr(sample_stats.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(sample_stats, "host_sample", lambda: {"loadavg": [1, 2, 3], "pressure": {}})

    class Stdout:
        def __iter__(self):
            for row in messages:
                clock[0] = row["host_monotonic_s"]
                yield json.dumps(row["raw"])

    class Process:
        stdout = Stdout()
        pid = 123
        returncode = 124

        def wait(self, timeout):
            assert self.stdin.closed
            return self.returncode

        def terminate(self):
            pytest.fail("normally bounded subscriber needs no termination")

    def popen(command, **kwargs):
        calls.append(command)
        assert kwargs["stdin"] == subprocess.PIPE
        process = Process()
        process.stdin = stdin
        return process

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            # Replay receipt timestamps on the reader, then restore the main thread's clock.
            before = clock[0]
            self.target()
            clock[0] = before

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

    def read(command, timeout=10):
        calls.append(command)
        if command[:2] == ["docker", "top"]:
            return "PID\n42\n"
        return "<nvidia_smi_log><gpu><fb_memory_usage><used>1000 MiB</used><total>20480 MiB</total></fb_memory_usage><processes><process_info><pid>42</pid><used_memory>500 MiB</used_memory></process_info></processes></gpu></nvidia_smi_log>"

    monkeypatch.setattr(sample_stats.subprocess, "Popen", popen)
    monkeypatch.setattr(sample_stats.threading, "Thread", Thread)
    monkeypatch.setattr(sample_stats, "run_read", read)
    return SimpleNamespace(messages=messages, calls=calls, clock=clock)


def test_collect_uses_only_read_only_probe_commands_and_same_window(replay_probe) -> None:
    replay_probe.messages.extend([
        stats(0, 5, 5),
        stats(1, 90, 90, True),
        stats(3, 120, 120, None),  # Gazebo may omit paused=false.
        stats(8, 125, 125, True),  # Warmup does not reset the clock or enter the measurement.
        stats(47, 164, 164),
        stats(48, 165, 165),
        stats(108, 225, 225),
        stats(168, 285, 285),
        stats(0, 286, 286, True),  # After the right edge: neither a reset nor a pause in the window.
    ])
    report = sample_stats.collect("immutable-id", "0", 45, 120, 2)
    assert report["simulation_ready_monotonic_s"] == 120
    assert report["window_start_monotonic_s"] == 165
    assert report["window_end_monotonic_s"] == 285
    assert [row["host_monotonic_s"] for row in report["stats_samples"]] == [165, 225, 285]
    assert [row["host_monotonic_s"] for row in report["resource_samples"]] == list(range(165, 285, 2))
    assert report["stats_window_coverage_fraction"] == 1
    assert report["rtf_window_median"] == 1
    assert report["container_process_memory_peak_mib"] == 500
    assert report["measurement_complete"] is True
    calls = replay_probe.calls
    assert calls[0][:5] == ["docker", "exec", "-i", "immutable-id", "bash"]
    assert "gz topic -e --json-output -t /world/default/stats" in calls[0][6]
    assert [command for command in calls if command[0] == "nvidia-smi"] == [
        ["nvidia-smi", "-i", "0", "-q", "-x"]] * report["resource_sample_count"]
    assert sum(command[:2] == ["docker", "exec"] for command in calls) == 1
    assert all(command[:2] in (["docker", "exec"], ["docker", "top"], ["nvidia-smi", "-i"]) for command in calls)


def test_collect_still_rejects_insufficient_coverage_after_readiness(replay_probe) -> None:
    replay_probe.messages.extend([stats(1, 120, 120), stats(46, 165, 165), stats(85, 204, 204)])
    report = sample_stats.collect("immutable-id", "0", 45, 120, 2)
    assert replay_probe.clock[0] == report["window_end_monotonic_s"] == 285
    assert report["stats_window_coverage_fraction"] == pytest.approx(39 / 120)
    assert report["rtf_window_median"] == 1
    assert report["measurement_complete"] is False
    assert report["errors"] == ["Gazebo stats cover less than 80% of the requested window"]


def test_collect_does_not_reanchor_when_paused_in_window(replay_probe) -> None:
    replay_probe.messages.extend([stats(1, 120, 120), stats(46, 165, 165), stats(47, 166, 166, True),
                                 stats(166, 285, 285)])
    report = sample_stats.collect("immutable-id", "0", 45, 120, 2)
    assert report["stats_window_coverage_fraction"] == 1
    assert report["measurement_complete"] is False
    assert report["errors"] == ["world paused during measurement window"]


def test_collect_rejects_readiness_after_startup_deadline(replay_probe) -> None:
    replay_probe.messages.extend([stats(0, 5, 5), stats(5, 25, 25, True), stats(6, 121, 121)])
    report = sample_stats.collect("immutable-id", "0", 45, 120, 2, startup_wait_s=120)
    assert report["simulation_ready_monotonic_s"] is None
    assert report["window_start_monotonic_s"] is None
    assert report["window_end_monotonic_s"] is None
    assert report["measurement_complete"] is False
    assert "no advancing, unpaused Gazebo stats before startup deadline" in report["errors"]
    assert len(replay_probe.calls) == 1  # No resource queries before readiness.


def test_collect_times_out_silent_subscriber_and_closes_control_pipe(monkeypatch, tmp_path) -> None:
    # Exercise the real shell lifecycle with a silent topic stub, never Docker or Gazebo.
    gz = tmp_path / "gz"
    gz.write_text("#!/bin/bash\nexec sleep 10\n")
    gz.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    real_popen = subprocess.Popen

    def launch_local(command, **kwargs):
        assert command[:4] == ["docker", "exec", "-i", "immutable-id"]
        return real_popen(command[4:], **kwargs)

    monkeypatch.setattr(sample_stats.subprocess, "Popen", launch_local)
    monkeypatch.setattr(sample_stats, "run_read", lambda _: pytest.fail("no resource queries without readiness"))
    report = sample_stats.collect("immutable-id", "0", 45, 120, 2, startup_wait_s=0.1)
    assert report["measurement_complete"] is False
    assert report["simulation_ready_monotonic_s"] is None
    assert report["errors"][0] == "no advancing, unpaused Gazebo stats before startup deadline"
    assert not any(error.startswith(("stats subscriber", "stats reader")) for error in report["errors"])

from types import SimpleNamespace

import pytest

from scripts import clock_offset_probe as module


def rows():
    return [
        {"host_before_ns": 100, "host_after_ns": 120, "container_ns": 10, "container_id": "a" * 64},
        {
            "host_before_ns": 200,
            "host_after_ns": 208,
            "container_ns": 102,
            "container_id": "a" * 64,
        },
    ]


def test_intersection_is_conditional_not_point_estimate():
    result = module.describe_probes(rows())
    assert result["conditional_offset_interval_ns"] == [98, 106]
    assert result["conditional_interval_width_ns"] == 8
    assert result["minimum_round_trip_ns"] == 8
    assert not result["equal_clock_rates_verified"]
    assert not result["training_label_eligible"]


def test_incompatible_offsets_report_null_not_average():
    samples = rows()
    samples[1]["container_ns"] = 150
    result = module.describe_probes(samples)
    assert not result["constant_offset_compatible"]
    assert result["conditional_offset_interval_ns"] is None
    assert result["conditional_interval_width_ns"] is None


def test_negative_offset_is_valid_and_large_integers_preserved():
    samples = rows()
    for sample in samples:
        sample["container_ns"] += 1788856937257446631
    assert module.describe_probes(samples)["conditional_offset_interval_ns"] == [
        98 - 1788856937257446631,
        106 - 1788856937257446631,
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r[0].update(host_before_ns=True),
        lambda r: r[0].update(host_after_ns=99),
        lambda r: r[1].update(host_before_ns=119),
        lambda r: r[1].update(container_ns=10),
        lambda r: r[1].update(container_id="b" * 64),
        lambda r: r[0].update(container_ns=1.5),
        lambda r: r[0].update(extra=1),
    ],
)
def test_invalid_probes(change):
    samples = rows()
    change(samples)
    with pytest.raises(ValueError):
        module.describe_probes(samples)


def test_fixed_command_causal_brackets(monkeypatch):
    clock = iter([100, 120, 200, 208])
    output = iter(["10\n", "102\n"])
    calls = []
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(clock))
    monkeypatch.setattr(module, "_resolution", lambda: 0)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=next(output))

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.sample_exec_probes("docker", "a" * 64, 2) == [
        dict(row, host_resolution_ns=0) for row in rows()
    ]
    assert all(call[0][1:3] == ["exec", "a" * 64] for call in calls)
    assert all(call[1]["timeout"] == 5 for call in calls)


@pytest.mark.parametrize("code,output", [(1, "100"), (0, "10\n20"), (0, "NaN"), (0, "")])
def test_probe_process_failure_rejected(monkeypatch, code, output):
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stdout=output)
    )
    with pytest.raises(ValueError):
        module.sample_exec_probes("docker", "a" * 64)


@pytest.mark.parametrize("identity,count", [("name", 3), ("a" * 64, True), ("a" * 64, 33)])
def test_invalid_invocations_never_execute(monkeypatch, identity, count):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid invocation executed")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        module.sample_probes("docker", identity, count)


def test_resolution_expands_interval_and_allows_equal_ticks():
    samples = rows()
    for row in samples:
        row["host_after_ns"] = row["host_before_ns"]
        row["host_resolution_ns"] = 20
    result = module.describe_probes(samples)
    assert result["conditional_offset_interval_ns"] == [78, 110]
    assert result["minimum_round_trip_ns"] == 0
    assert result["host_quantization_limited"]
    assert not result["timestamp_accuracy_verified"]


@pytest.mark.parametrize("resolution", [True, -1, 1.5, 1000000001])
def test_invalid_resolution_rejected(resolution):
    samples = rows()
    samples[0]["host_resolution_ns"] = resolution
    with pytest.raises(ValueError):
        module.describe_probes(samples)


def local_process(monkeypatch, server=None):
    import subprocess
    import sys

    original = subprocess.Popen
    processes = []

    def launch(args, **kwargs):
        assert args[1:4] == ["exec", "-i", "a" * 64]
        process = original(
            [sys.executable, "-u", "-c", server or module.SERVER, args[-1]], **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    return processes


def test_persistent_real_local_protocol_and_cleanup(monkeypatch):
    # Windows fixture uses QPC for the remote side; production server runs in
    # Linux where monotonic_ns does not use Windows GetTickCount64.
    processes = local_process(
        monkeypatch, module.SERVER.replace("time.monotonic_ns()", "time.perf_counter_ns()")
    )
    samples = module.sample_probes("docker", "a" * 64, 6)
    assert len(samples) == 6
    assert all(r["host_resolution_ns"] > 0 for r in samples)
    assert len(processes) == 1
    assert processes[0].returncode == 0
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize(
    "server",
    [
        'print("WRONG", flush=True)',
        'import sys; print("READY", flush=True); sys.stdin.readline(); print("7 100", flush=True)',
        'import sys; print("READY", flush=True); '
        'sys.stdin.readline(); print("0 invalid", flush=True)',
    ],
)
def test_bad_persistent_protocol_is_terminal_and_cleaned(monkeypatch, server):
    processes = local_process(monkeypatch, server)
    with pytest.raises(ValueError):
        module.sample_probes("docker", "a" * 64)
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_persistent_timeout_cleans_waiting_child(monkeypatch):
    processes = local_process(monkeypatch)

    def timeout(*args, **kwargs):
        raise module.queue.Empty

    monkeypatch.setattr(module.queue.Queue, "get", timeout)
    with pytest.raises(ValueError, match="timeout"):
        module.sample_probes("docker", "a" * 64)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_server_accepts_exact_lf_or_crlf_requests(monkeypatch, capsys, newline):
    import io
    import sys

    monkeypatch.setattr(sys, "argv", ["clock-probe", "2"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"0{newline}1{newline}"))
    exec(module.SERVER, {})
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "READY"
    assert lines[1].startswith("0 ") and lines[2].startswith("1 ")

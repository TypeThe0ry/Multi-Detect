from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="Linux GPU queue requires flock")

from scripts import gpu_job_queue as q  # noqa: E402


class FakeProcess:
    def __init__(self, pid, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code
        self.signals = []

    def poll(self):
        return self.exit_code

    def wait(self):
        assert self.exit_code is not None
        return self.exit_code

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")
        self.exit_code = -9


class FakeSystem:
    def __init__(self):
        self.memory = "0, 100\n1, 200\n2, 0\n3, 0\n"
        self.processes = {}
        self.identities = {}
        self.launches = []
        self.queries = []
        self.spawn_error = None

    def run(self, args, **kwargs):
        self.queries.append((args, kwargs))
        if isinstance(self.memory, Exception):
            raise self.memory
        return SimpleNamespace(stdout=self.memory)

    def popen(self, args, **kwargs):
        if self.spawn_error:
            raise self.spawn_error
        pid = 10000 + len(self.launches)
        self.launches.append((args, kwargs))
        self.identities[pid] = {"pid": pid, "start_ticks": pid * 10, "boot_id": "boot",
                                "token": kwargs["env"][q.TOKEN_ENV]}
        self.processes[pid] = FakeProcess(pid)
        return self.processes[pid]

    def snapshot(self, pid):
        return self.identities.get(pid)


@pytest.fixture
def setup_queue(tmp_path):
    script = tmp_path / "work with spaces.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    system = FakeSystem()
    queue = q.Queue(tmp_path / "jobq", runner=system.run, popen=system.popen,
                    snapshot=system.snapshot, sleep=lambda _: None)
    return queue, system, script


def job(script, name="one", **kwargs):
    return {"name": name, "command": str(script), "any_of": [0, 1], **kwargs}


def record(queue, state, name):
    return q.read_json(queue.root / state / f"{name}.json")


def finish(queue, system, name, code=0):
    runtime = record(queue, "running", name)["runtime"]
    q.write_json(queue.control(runtime["token"], "result.json"),
                 {"token": runtime["token"], "exit_code": code, "end": "finished"})
    system.processes[runtime["pid"]].exit_code = 0
    system.identities.pop(runtime["pid"])


def test_fills_both_default_gpus_and_does_not_spill_onto_2_3(setup_queue):
    queue, system, script = setup_queue
    for name in ("a", "b", "c"):
        queue.submit(job(script, name))
    queue.tick()
    assert record(queue, "running", "a")["runtime"]["gpus"] == [0]
    assert record(queue, "running", "b")["runtime"]["gpus"] == [1]
    assert record(queue, "pending", "c")["name"] == "c"
    assert len(system.launches) == 2
    args, options = system.launches[0]
    assert args[2] == "_worker"
    assert options["start_new_session"] and options["close_fds"]
    assert options["stdin"] == subprocess.DEVNULL
    assert system.queries[0][1]["timeout"] == 10
    assert system.queries[0][1]["check"] is True


def test_external_memory_threshold_is_strict_and_missing_gpu_is_busy(setup_queue):
    queue, system, script = setup_queue
    system.memory = "0, 4000\n1, 3999\n"
    queue.submit(job(script))
    queue.submit({"name": "missing", "gpus": [2], "command": str(script)})
    queue.tick()
    assert record(queue, "running", "one")["runtime"]["gpus"] == [1]
    assert record(queue, "pending", "missing")


def test_explicit_multigpu_is_all_or_nothing_and_priority_wins(setup_queue):
    queue, system, script = setup_queue
    queue.submit({"name": "multi", "gpus": [0, 2], "command": str(script), "priority": 10})
    queue.submit(job(script, "low"))
    queue.submit({"name": "three", "any_of": [3], "command": str(script)})
    queue.tick()
    assert record(queue, "running", "multi")["runtime"]["gpus"] == [0, 2]
    assert record(queue, "running", "low")["runtime"]["gpus"] == [1]
    assert record(queue, "running", "three")["runtime"]["gpus"] == [3]
    assert len(system.launches) == 3


def test_multigpu_does_not_launch_if_one_device_busy(setup_queue):
    queue, system, script = setup_queue
    system.memory = "0, 0\n1, 5000\n"
    queue.submit({"name": "multi", "gpus": [0, 1], "command": str(script)})
    queue.tick()
    assert not system.launches


@pytest.mark.parametrize("output", ["", "N/A", "0, N/A", "0, -1", "0, 0\n0, 1",
                                    FileNotFoundError("nvidia-smi"),
                                    subprocess.TimeoutExpired("nvidia-smi", 10),
                                    subprocess.CalledProcessError(1, "nvidia-smi")])
def test_gpu_query_failure_fails_closed(setup_queue, output):
    queue, system, script = setup_queue
    system.memory = output
    queue.submit(job(script))
    queue.tick()
    assert not system.launches
    assert record(queue, "pending", "one")


def test_completes_and_starts_dependent_in_same_tick(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script, "first"))
    queue.submit(job(script, "next", after=["first"]))
    queue.tick()
    assert len(system.launches) == 1
    runtime = record(queue, "running", "first")["runtime"]
    assert Path(runtime["log"]).is_file()
    assert runtime["pid"] == runtime["identity"]["pid"]
    finish(queue, system, "first")
    queue.tick()
    final = record(queue, "done", "first")["runtime"]
    assert final["exit"] == 0 and final["end"] == "finished" and final["start"]
    assert record(queue, "running", "next")
    assert len(system.launches) == 2
    assert runtime["pid"] not in queue.children


def test_failed_dependency_propagates_and_unknown_dependency_waits(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script, "first"))
    queue.submit(job(script, "a", after=["b"]))
    queue.submit(job(script, "b", after=["first"]))
    queue.submit(job(script, "unknown", after=["later"]))
    queue.tick()
    finish(queue, system, "first", 7)
    queue.tick()
    assert record(queue, "failed", "first")["runtime"]["exit"] == 7
    for name in ("a", "b"):
        assert "dependency" in record(queue, "failed", name)["runtime"]["error"]
    assert record(queue, "pending", "unknown")
    assert len(system.launches) == 1
    unknown = next(row for row in queue.listing() if row["name"] == "unknown")
    assert unknown["waiting_for"] == ["later"]


def test_restart_adopts_exact_identity_then_uses_exit_receipt(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    restarted = q.Queue(queue.root, runner=system.run, popen=system.popen,
                        snapshot=system.snapshot, sleep=lambda _: None)
    restarted.submit({"name": "next", "gpus": [0], "command": str(script)})
    restarted.tick()
    assert len(system.launches) == 1  # even though fake memory remains empty
    finish(queue, system, "one")
    restarted.tick()
    assert record(queue, "done", "one")["runtime"]["exit"] == 0
    assert record(queue, "running", "next")


@pytest.mark.parametrize("changed", ["start_ticks", "boot_id", "token", "missing"])
def test_reused_or_missing_pid_never_signalled_and_lease_retained(setup_queue, changed):
    queue, system, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    pid = record(queue, "running", "one")["runtime"]["pid"]
    if changed == "missing":
        system.identities.pop(pid)
    else:
        system.identities[pid] = {**system.identities[pid], changed: "different"}
    with pytest.raises(ValueError, match="no signal sent"):
        queue.cancel("one")
    queue.tick()
    assert record(queue, "running", "one")["runtime"]["status"] == "lost"
    assert not system.processes[pid].signals
    assert not list((queue.root / ".control").glob("*.cancel"))


def test_cancel_pending_and_running_only_request_own_worker(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script, "pending"))
    assert queue.cancel("pending") == "cancelled"
    assert record(queue, "failed", "pending")["runtime"]["status"] == "cancelled"
    queue.submit(job(script))
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]
    assert queue.cancel("one") == "cancellation requested"
    assert queue.control(runtime["token"], "cancel").is_file()
    assert not system.processes[runtime["pid"]].signals
    with pytest.raises(ValueError, match="already"):
        queue.cancel("pending")


def test_interrupted_launch_does_not_rerun(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script))
    path = queue.root / "pending/one.json"
    value = q.read_json(path)
    value["runtime"] = {"token": "incomplete", "gpus": [0], "status": "starting"}
    path.unlink()
    q.write_json(queue.root / "running/one.json", value)
    queue.tick()
    assert "interrupted" in record(queue, "failed", "one")["runtime"]["error"]
    assert not system.launches


@pytest.mark.parametrize("failure", ["spawn", "identity"])
def test_launch_failure_recorded_without_retry(setup_queue, failure):
    queue, system, script = setup_queue
    if failure == "spawn":
        system.spawn_error = OSError("fake spawn failure")
    else:
        queue.snapshot = lambda _: None
    queue.submit(job(script))
    queue.tick()
    assert "launch failed" in record(queue, "failed", "one")["runtime"]["error"]
    queue.tick()
    assert len(system.launches) == (0 if failure == "spawn" else 1)


@pytest.mark.parametrize("changes", [{"name": "../bad"}, {"gpus": [0]}, {"any_of": []},
                                     {"any_of": [True]}, {"any_of": [0, 0]},
                                     {"any_of": [-1]}, {"priority": True},
                                     {"command": "relative.sh"}, {"after": ["one"]},
                                     {"after": "other"}, {"runtime": {"pid": 1}}])
def test_invalid_submission_rejected(setup_queue, changes):
    queue, _, script = setup_queue
    with pytest.raises(ValueError):
        queue.submit(job(script, **changes))
    assert not queue.records()


def test_duplicate_names_and_dependency_cycles_rejected(setup_queue):
    queue, _, script = setup_queue
    queue.submit(job(script, "a", after=["b"]))
    with pytest.raises(ValueError, match="cycle"):
        queue.submit(job(script, "b", after=["a"]))
    with pytest.raises(ValueError, match="already exists"):
        queue.submit(job(script, "a"))


def test_single_instance_lock_and_release(tmp_path):
    path = tmp_path / "lock"
    with q.file_lock(path, blocking=False):
        with pytest.raises(RuntimeError, match="another queue daemon"):
            with q.file_lock(path, blocking=False):
                pytest.fail("second owner")
    with q.file_lock(path, blocking=False):
        assert path.exists()


def test_proc_identity_handles_parentheses_pid_reuse_zombie_and_missing_token(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-id\n")
    stat = proc / "42/stat"
    stat.write_text("42 (bash ) tricky) S " + "0 " * 18 + "1234 0 0")
    (proc / "42/environ").write_bytes(f"OTHER=x\0{q.TOKEN_ENV}=abc\0".encode())
    assert q.process_identity(42, proc) == {"pid": 42, "start_ticks": 1234,
                                           "token": "abc", "boot_id": "boot-id"}
    stat.write_text(stat.read_text().replace(") S ", ") Z "))
    assert q.process_identity(42, proc) is None
    stat.write_text(stat.read_text().replace(") Z ", ") S "))
    (proc / "42/environ").write_bytes(b"OTHER=x\0")
    assert q.process_identity(42, proc) is None
    assert q.process_identity(43, proc) is None


@pytest.mark.parametrize("code", [0, 5, -15])
def test_worker_executes_argv_with_visible_devices_and_persists_exit(setup_queue, code):
    queue, system, script = setup_queue
    queue.submit({"name": "one", "gpus": [1, 2], "command": str(script)})
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]
    script_process = FakeProcess(888, code)
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return script_process

    assert q.worker(queue.root, "one", runtime["token"], popen=popen,
                    snapshot=lambda _: runtime["identity"], sleep=lambda _: None) == 0
    args, options = calls[0]
    assert args == ["/bin/bash", str(script)]  # spaces are never interpreted as shell code
    assert options["env"]["CUDA_VISIBLE_DEVICES"] == "1,2"
    assert options["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert options["cwd"] == str(script.parent)
    queue.tick()
    final = record(queue, "done" if code == 0 else "failed", "one")["runtime"]
    assert final["exit"] == code and final["script_pid"] == 888 and final["end"]


def test_worker_cancel_term_then_kill_only_child_it_started(setup_queue):
    queue, _, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]
    child = FakeProcess(888)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        queue.control(runtime["token"], "cancel").touch()

    assert q.worker(queue.root, "one", runtime["token"], popen=lambda *a, **k: child,
                    snapshot=lambda _: runtime["identity"], sleep=sleep,
                    monotonic=lambda: len(sleeps), cancel_grace=1) == 0
    assert child.signals == ["TERM", "KILL"]
    queue.tick()
    assert record(queue, "failed", "one")["runtime"]["status"] == "cancelled"


def test_worker_never_executes_without_committed_identity(setup_queue):
    queue, _, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]
    identity = {**runtime["identity"], "start_ticks": "different"}

    def forbidden(*_args, **_kwargs):
        pytest.fail("uncommitted worker must not execute script")

    assert q.worker(queue.root, "one", runtime["token"], popen=forbidden,
                    snapshot=lambda _: identity, sleep=lambda _: None) == 1
    assert q.worker(queue.root, "one", "wrong-token", popen=forbidden,
                    snapshot=lambda _: identity, sleep=lambda _: None) == 1


def test_cli_submit_list_cancel_without_subprocess(tmp_path, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("CLI must not run real subprocesses")

    monkeypatch.setattr(q.Queue, "tick", forbidden)
    script = tmp_path / "test.sh"
    script.write_text("exit 0\n")
    source = tmp_path / "job.json"
    source.write_text(json.dumps(job(script)))
    prefix = ["--queue-dir", str(tmp_path / "jobq")]
    assert q.main([*prefix, "submit", str(source)]) == 0
    capsys.readouterr()
    assert q.main([*prefix, "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["queue_state"] == "pending"
    assert q.main([*prefix, "cancel", "one"]) == 0
    assert q.main([*prefix, "cancel", "missing"]) == 1


def test_manual_pending_job_is_validated_and_quarantined(setup_queue):
    queue, system, _ = setup_queue
    q.write_json(queue.root / "pending/bad.json", {"name": "bad", "command": "invalid"})
    queue.tick()
    assert "invalid job" in record(queue, "failed", "bad")["runtime"]["error"]
    assert not system.launches


def test_bad_json_is_quarantined_without_blocking_valid_jobs(setup_queue):
    queue, system, script = setup_queue
    (queue.root / "pending/bad.json").write_text("{unfinished", encoding="utf-8")
    queue.submit(job(script))
    queue.tick()
    assert "invalid job" in record(queue, "failed", "bad")["runtime"]["error"]
    assert record(queue, "running", "one")
    assert len(system.launches) == 1


def test_corrupt_running_record_stops_scheduling(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    (queue.root / "running/one.json").write_text("{unfinished", encoding="utf-8")
    with pytest.raises(ValueError):
        queue.tick()
    assert len(system.launches) == 1


def test_worker_cancel_before_start_never_spawns_script(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]
    queue.cancel("one")

    def forbidden(*_args, **_kwargs):
        pytest.fail("cancelled worker must not execute script")

    assert q.worker(queue.root, "one", runtime["token"], popen=forbidden,
                    snapshot=lambda _: runtime["identity"], sleep=lambda _: None) == 0
    queue.tick()
    assert record(queue, "failed", "one")["runtime"]["status"] == "cancelled"
    assert len(system.launches) == 1


def test_worker_spawn_error_persists_failed_receipt(setup_queue):
    queue, _, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]

    def failed(*_args, **_kwargs):
        raise OSError("fake bash spawn error")

    assert q.worker(queue.root, "one", runtime["token"], popen=failed,
                    snapshot=lambda _: runtime["identity"], sleep=lambda _: None) == 0
    queue.tick()
    final = record(queue, "failed", "one")["runtime"]
    assert final["exit"] is None and "fake bash spawn error" in final["error"]


def test_reconcile_completion_even_when_gpu_query_fails(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    queue.submit(job(script, "next"))
    finish(queue, system, "one")
    system.memory = FileNotFoundError("nvidia-smi")
    queue.tick()
    assert record(queue, "done", "one")["runtime"]["exit"] == 0
    assert record(queue, "pending", "next")


def test_daemon_lock_prevents_second_run_without_starting_subprocess(setup_queue):
    queue, system, _ = setup_queue
    with q.file_lock(queue.root / ".daemon.lock", blocking=False):
        assert q.main(["--queue-dir", str(queue.root), "run"]) == 1
    assert not system.launches


@pytest.mark.parametrize("invalid", [{"name": []}, {"name": {}},
                                     {"name": "bad", "runtime": []}])
def test_malformed_pending_fields_are_quarantined(setup_queue, invalid):
    queue, system, script = setup_queue
    q.write_json(queue.root / "pending/bad.json", invalid)
    queue.submit(job(script))
    queue.tick()
    assert record(queue, "failed", "bad")["invalid_job"] == invalid
    assert record(queue, "running", "one")
    assert len(system.launches) == 1


def test_transient_proc_failure_recovers_lease_when_identity_matches(setup_queue):
    queue, system, script = setup_queue
    queue.submit(job(script))
    queue.tick()
    runtime = record(queue, "running", "one")["runtime"]
    identity = system.identities.pop(runtime["pid"])
    queue.tick()
    assert record(queue, "running", "one")["runtime"]["status"] == "lost"
    system.identities[runtime["pid"]] = identity
    queue.tick()
    assert record(queue, "running", "one")["runtime"]["status"] == "running"
    assert len(system.launches) == 1


def test_daemon_sigterm_stops_scheduling_and_keeps_jobs_alive(setup_queue, monkeypatch):
    queue, system, script = setup_queue
    queue.submit(job(script))
    handlers = {}
    tick = queue.tick

    def tick_then_stop():
        tick()
        handlers[q.signal.SIGTERM](q.signal.SIGTERM, None)

    monkeypatch.setattr(q.signal, "signal",
                        lambda signum, handler: handlers.update({signum: handler}))
    monkeypatch.setattr(queue, "tick", tick_then_stop)
    monkeypatch.setattr(q, "Queue", lambda *args, **kwargs: queue)
    assert q.main(["--queue-dir", str(queue.root), "run"]) == 0
    assert record(queue, "running", "one")
    assert len(system.launches) == 1
    assert all(not process.signals for process in system.processes.values())
    with q.file_lock(queue.root / ".daemon.lock", blocking=False):
        pass

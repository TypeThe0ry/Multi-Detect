import json
from pathlib import Path
from types import SimpleNamespace

from scripts.run_nightly_regression import SitlLock, compare_reports, run


def test_lock_reclaims_stale_and_releases(tmp_path):
    lock = tmp_path / "sitl.lock"
    lock.mkdir()
    owner = lock / "owner"
    owner.write_text("old 999999 2020-01-01T00:00:00+00:00\n", encoding="utf-8")
    import os, time
    old = time.time() - 300
    os.utime(owner, (old, old))
    held = SitlLock(
        lock,
        retry_s=0,
        runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=""),
        memory_fn=lambda: 6.0,
    )
    held.acquire(wait=False)
    assert held.owner.read_text(encoding="utf-8").startswith("nightly-regression ")
    held.release()
    assert not lock.exists()


def test_compare_reports_detects_regressions():
    old = {"suites": [{"name": "main", "summary": {"expectation_pass_rate": 1.0, "plan_accuracy": 1.0}, "metrics": {"false_findings": 0}}]}
    new = {"suites": [{"name": "main", "summary": {"expectation_pass_rate": .5, "plan_accuracy": .75}, "metrics": {"false_findings": 1}}]}
    found = compare_reports(new, old)
    assert {x["metric"] for x in found} == {"expectation_pass_rate", "plan_accuracy", "false_findings"}


def test_dry_run_prints_all_suites(tmp_path, capsys):
    assert run(["--dry-run", "--out", str(tmp_path)]) == 0
    # The e2e operator suite joins the default plan once its runner and config exist.
    import scripts.run_nightly_regression as mod

    e2e = (mod.ROOT / "scripts/run_e2e_operator_suite.py").exists() and (
        mod.ROOT / "configs/onboard/suite-e2e-operator.json"
    ).exists()
    assert capsys.readouterr().out.count('"suite"') == 4 + int(e2e)


def test_model_set_dry_run_reports_linux_runtime_settings(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SIM_GPU", "2")
    monkeypatch.setenv("MULTIDETECT_SIM_IMAGE", "multidetect/px4-sitl-gazebo:gpu")
    assert run([
        "--model-set", "r21idr3-r24plan", "--dry-run", "--out", str(tmp_path),
        "--lock-path", "/tmp/nightly-sitl.lock",
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    runtime = rows[0]
    assert runtime == {
        "model_set": "r21idr3-r24plan",
        "lock_path": "/tmp/nightly-sitl.lock",
        "sim_control_port": 14540,
        "sim_telemetry_port": 14541,
        "sim_environment": {
            "MULTIDETECT_SIM_GPU": "2",
            "MULTIDETECT_SIM_IMAGE": "multidetect/px4-sitl-gazebo:gpu",
            "MULTIDETECT_SITL_LOCK": "/tmp/nightly-sitl.lock",
        },
    }
    configs = {
        Path(row["command"][row["command"].index("--suite") + 1]).name
        for row in rows[1:]
    }
    assert configs == {
        "suite-main-r21idr3-r24plan.json",
        "suite-airframes-r21idr3-r24plan.json",
        "suite-follow-r21idr3-r24plan.json",
        "suite-robustness-r21idr3-r24plan.json",
    }


def test_keep_px4_ulog_reaches_mission_suite(tmp_path, capsys):
    assert run(["--suites", "main", "--dry-run", "--keep-px4-ulog", "--out", str(tmp_path)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "--keep-px4-ulog" in rows[1]["command"]


def test_model_set_can_be_selected_from_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_MODEL_SET", "r21idr3-r24plan")
    assert run(["--dry-run", "--suites", "main", "--out", str(tmp_path)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["model_set"] == "r21idr3-r24plan"
    assert Path(rows[1]["command"][rows[1]["command"].index("--suite") + 1]).name == (
        "suite-main-r21idr3-r24plan.json"
    )


def test_nightly_e2e_child_receives_held_lock_marker(tmp_path, monkeypatch):
    import scripts.run_nightly_regression as mod

    class NoopLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self, *_args, **_kwargs):
            pass

        def release(self):
            pass

    seen = []

    def runner(command, **kwargs):
        seen.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "SitlLock", NoopLock)
    assert run(["--suites", "e2e", "--out", str(tmp_path)], runner=runner) == 0
    assert seen and seen[0][1]["MULTIDETECT_SITL_LOCK_HELD"] == "1"


def test_sim_environment_is_passed_to_suite_process(tmp_path, monkeypatch):
    import scripts.run_nightly_regression as mod

    calls = []

    class NoopLock:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    monkeypatch.setattr(mod, "SitlLock", NoopLock)
    monkeypatch.setattr(mod, "preflight", lambda *a, **k: None)
    monkeypatch.setenv("MULTIDETECT_SIM_GPU", "1")
    monkeypatch.setenv("MULTIDETECT_SIM_IMAGE", "multidetect/px4-sitl-gazebo:gpu")
    def fake(cmd, **kwargs):
        calls.append(kwargs["env"])
        return SimpleNamespace(returncode=0)

    assert run([
        "--suites", "main", "--out", str(tmp_path / "out"),
        "--lock-path", str(tmp_path / "lock"),
    ], runner=fake) == 0
    assert calls[0]["MULTIDETECT_SIM_GPU"] == "1"
    assert calls[0]["MULTIDETECT_SIM_IMAGE"] == "multidetect/px4-sitl-gazebo:gpu"
    assert calls[0]["MULTIDETECT_SITL_LOCK"] == str((tmp_path / "lock").resolve())


def test_failure_does_not_stop_following(tmp_path):
    calls = []
    def fake(cmd, **kwargs):
        calls.append(cmd[cmd.index("--suite") + 1])
        return SimpleNamespace(returncode=7 if len(calls) == 1 else 0)
    import scripts.run_nightly_regression as mod
    class NoopLock:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(mod, "SitlLock", NoopLock)
    old_preflight = mod.preflight
    mod.preflight = lambda *a, **k: None
    try:
        assert run(["--suites", "main", "airframes", "--out", str(tmp_path), "--lock-path", str(tmp_path / "lock")], runner=fake) == 1
    finally:
        mod.preflight = old_preflight
        monkeypatch.undo()
    assert len(calls) == 2


def test_pid_alive_probes_without_signalling(monkeypatch):
    # os.kill(pid, 0) sends CTRL_C_EVENT on Windows; the probe must never signal.
    import psutil

    import scripts.run_nightly_regression as module

    def forbidden(*args, **kwargs):
        raise AssertionError("pid_alive must not send a signal")

    monkeypatch.setattr(module.os, "kill", forbidden)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 4242)
    assert module.pid_alive(4242) is True
    assert module.pid_alive(4243) is False

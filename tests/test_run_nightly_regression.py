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

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
    held = SitlLock(lock, retry_s=0)
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
    assert capsys.readouterr().out.count('"suite"') == 4


def test_failure_does_not_stop_following(tmp_path):
    calls = []
    def fake(cmd, **kwargs):
        calls.append(cmd[cmd.index("--suite") + 1])
        return SimpleNamespace(returncode=7 if len(calls) == 1 else 0)
    import scripts.run_nightly_regression as mod
    old_preflight = mod.preflight
    mod.preflight = lambda *a, **k: None
    try:
        assert run(["--suites", "main", "airframes", "--out", str(tmp_path), "--lock-path", str(tmp_path / "lock")], runner=fake) == 1
    finally:
        mod.preflight = old_preflight
    assert len(calls) == 2

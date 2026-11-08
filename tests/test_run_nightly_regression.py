import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_nightly_regression import (
    FAILURE_CATEGORIES, SitlLock, compare_reports, failure_category,
    final_campaign_exception, mission_triage, run, write_nightly_report,
)


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


def test_nightly_resume_requests_campaign_retry(tmp_path, capsys):
    assert run(["--suites", "main", "--dry-run", "--out", str(tmp_path)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "--retry-failed-episodes" in rows[1]["command"]


def test_model_set_can_be_selected_from_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_MODEL_SET", "r21idr3-r24plan")
    assert run(["--dry-run", "--suites", "main", "--out", str(tmp_path)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["model_set"] == "r21idr3-r24plan"
    assert Path(rows[1]["command"][rows[1]["command"].index("--suite") + 1]).name == (
        "suite-main-r21idr3-r24plan.json"
    )


def test_custom_suite_uses_nightly_model_set_and_child_harness(tmp_path, capsys):
    config = tmp_path / "follow-track.json"
    config.write_text('{"defaults": {}, "missions": []}\n', encoding="utf-8")
    assert run([
        "--model-set", "r21idr3-r24plan", "--custom-suite",
        f"follow-track={config}", "--suites", "follow-track", "--dry-run",
        "--out", str(tmp_path / "out"),
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["model_set"] == "r21idr3-r24plan"
    assert len(rows) == 2
    assert rows[1]["suite"] == "follow-track"
    assert Path(rows[1]["command"][rows[1]["command"].index("--suite") + 1]) == config


def test_custom_suite_rejects_missing_config(tmp_path):
    with pytest.raises(SystemExit):
        run([
            "--custom-suite", f"follow-track={tmp_path / 'missing.json'}",
            "--suites", "follow-track", "--dry-run", "--out", str(tmp_path / "out"),
        ])


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


@pytest.mark.parametrize(("exception", "category"), [
    ("TimeoutError: timed out waiting for offboard_entered after 30s", "offboard_timeout"),
    ("TimeoutError: timed out waiting for land after 120s", "land_timeout"),
    ("RuntimeError: landing_timeout", "land_timeout"),
    ("RuntimeError: PX4 rejected DISARM command with MAV_RESULT=2", "disarm_rejected"),
    ("TimeoutError: episode telemetry stalled after wall-clock budget", "telemetry_stall"),
    ("TimeoutError: timed out waiting for airframe landing: PX4 simulation clock stalled (telemetry)", "telemetry_stall"),
    ("TimeoutError: timed out waiting for airframe landing: PX4 telemetry stale (heartbeat)", "telemetry_stall"),
    ("TimeoutError: timed out waiting for airframe landing: landing budget exhausted", "land_timeout"),
    ("RuntimeError: sensor stream ended unexpectedly", "sensor_stream_eof"),
    ("EOFError: sensor stream EOF", "sensor_stream_eof"),
    ("RuntimeError: PX4 pre-arm health checks did not pass: GPS", "preflight_health"),
    ("RuntimeError: preflight health failed", "preflight_health"),
    ("ValueError: mission plan refused: unsafe route", "plan_refused"),
    ("ValueError: bad configuration", "other"),
    ("TimeoutError: timed out waiting for disarmed heartbeat after collector DISARM; landed_state=4", "other"),
    ("TimeoutError: sensor stream produced no frame before timeout", "other"),
    ("BrokenPipeError: [Errno 32] Broken pipe", "other"),
])
def test_failure_categories(exception, category):
    assert failure_category(exception, row={}) == category


def test_sensor_broken_pipe_requires_sensor_traceback():
    exception = "BrokenPipeError: [Errno 32] Broken pipe"
    context = '  File "src/multidetect/sim_teacher/streams.py", line 176, in request\n    self.request_stream.write(payload)'
    assert failure_category(exception, context, row={}) == "sensor_stream_eof"
    assert failure_category(exception, context.replace("/", "\\"), row={}) == "sensor_stream_eof"
    assert failure_category("EOFError: unexpected end of stream", context, row={}) == "sensor_stream_eof"
    assert failure_category(exception, 'File "transport.py", in send', row={}) == "other"


def test_plan_refusal_fallback_does_not_override_final_exception():
    row = {"planned_skills": ["refuse"]}
    assert failure_category(None, row=row) == "plan_refused"
    assert failure_category("TimeoutError: telemetry stalled", row=row) == "telemetry_stall"
    assert failure_category(None, row={"step_outcomes": [{"reason": "plan_refused"}]}) == "plan_refused"


def _trace(exception):
    return f'Traceback (most recent call last):\n  File "collector.py", line 1, in run\n    run_episode()\n{exception}\n'


def _suite_output(root, rows, logs=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "suite-report.json").write_text(json.dumps({
        "summary": {"expectation_pass_rate": .5, "plan_accuracy": 1.0}, "missions": rows,
    }), encoding="utf-8")
    for (mission, world), text in (logs or {}).items():
        path = root / mission / "campaign" / f"{world}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _row(ident, *, status="complete", checks=None, episode=1, world="world"):
    checks = {"skills": True} if checks is None else checks
    return {"id": ident, "world": world, "episode": episode, "status": status,
            "mission_completed": status == "complete", "expectation_checks": checks,
            "expectations_passed": all(checks.values())}


def test_final_exception_ignores_causes_events_and_prior_attempts(tmp_path):
    log = tmp_path / "world.log"
    log.write_text(
        _trace("TimeoutError: offboard timeout")
        + "During handling of the above exception, another exception occurred:\n\n"
        + _trace("custom.CampaignFailure: 中文 | <unsafe>\nDetails: second message line")
        + json.dumps({"error": "Traceback (most recent call last):\nEOFError: stale", "phase": "cleanup"})
        + "\n", encoding="utf-8",
    )
    exception, context, error = final_campaign_exception(log)
    assert exception == "custom.CampaignFailure: 中文 | <unsafe>\nDetails: second message line"
    assert "offboard timeout" not in context
    assert error is None
    assert failure_category(exception, context, row={}) == "other"


def test_missing_log_and_no_traceback_are_distinct(tmp_path):
    log = tmp_path / "world.log"
    assert final_campaign_exception(log)[2].startswith("FileNotFoundError:")
    log.write_text('{"error": "TimeoutError: offboard timeout"}\n', encoding="utf-8")
    assert final_campaign_exception(log) == (None, "", None)


def test_triage_preserves_episodes_worlds_and_expected_safe_abort(tmp_path):
    rows = [
        _row("mission", episode=1),
        _row("mission", episode=2, status="missing", checks={"skills": False}),
        _row("mission", world="other", checks={"min_count_exact": False}),
        _row("safe-abort"),
        _row("no-log", status="missing"),
    ]
    rows[3]["mission_completed"] = False
    _suite_output(tmp_path / "main", rows, {
        ("mission", "world"): _trace("TimeoutError: offboard timeout"),
        ("mission", "other"): "collector complete\n",
        ("safe-abort", "world"): "collector complete\n",
    })
    report = json.loads((tmp_path / "main/suite-report.json").read_text(encoding="utf-8"))
    result = mission_triage(report, tmp_path / "main")
    assert len(result) == 5
    assert [r["failure_category"] for r in result] == [None, "offboard_timeout", "other", None, "other"]
    assert result[2]["failed_expectations"] == ["min_count_exact"]
    assert result[2]["complete"] is True
    assert result[3]["complete"] is True and result[3]["mission_completed"] is False
    assert result[4]["log_error"].startswith("FileNotFoundError:")


def test_triage_reads_episode_container_failure_snapshot_into_nightly_table(tmp_path):
    source = tmp_path / "main"
    _suite_output(source, [_row("failure", status="missing")], {
        ("failure", "world"): _trace("TimeoutError: PX4 simulation clock stalled"),
    })
    episode_path = source / "failure/campaign/world/episodes/episode-0000-abcd/episode.json"
    episode_path.parent.mkdir(parents=True)
    episode_path.write_text(
        json.dumps(
            {
                "episode_index": 0,
                "finished_wall_time": 100.0,
                "container_failure_diagnostics": {
                    "container_name": "sim-episode-0",
                    "container_id": "container-id",
                    "failure_class": "clock_stalled_alive",
                    "failure_state": {
                        "state": {
                            "exit_code": 0,
                            "oom_killed": False,
                            "finished_at": "0001-01-01T00:00:00Z",
                        },
                        "px4_process_alive": True,
                        "px4_host_pids": [789],
                        "host_load_average": [12.5, 10.0, 8.0],
                        "gpu_memory_mib": [{"index": 0, "used_mib": 1234, "total_mib": 20480}],
                    },
                    "cleanup_state": {
                        "state": {
                            "running": False,
                            "exit_code": 137,
                            "oom_killed": False,
                            "finished_at": "2026-11-05T00:00:00Z",
                        },
                        "px4_process_alive": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    report = json.loads((source / "suite-report.json").read_text(encoding="utf-8"))
    triage = mission_triage(report, source)
    assert triage[0]["failure_class"] == "clock_stalled_alive"
    assert triage[0]["container_failure_diagnostics"]["container_id"] == "container-id"

    merged = {
        "generated_at": "2026-11-05T00:00:00+08:00",
        "suites": [{
            "name": "main",
            "summary": {},
            "metrics": {},
            "exit_code": 1,
            "missions": triage,
        }],
        "regressions": [],
    }
    out = tmp_path / "triage-report"
    write_nightly_report(merged, out)
    text = (out / "nightly-report.md").read_text(encoding="utf-8")
    assert "clock_stalled_alive" in text
    assert "sim-episode-0 (container-id)" in text
    assert "| 789 |" in text
    assert "failure: ExitCode=0; OOMKilled=False; FinishedAt=0001-01-01T00:00:00Z" in text
    assert "cleanup: ExitCode=137; OOMKilled=False; FinishedAt=2026-11-05T00:00:00Z" in text
    assert "failure=True; cleanup=False" in text
    assert "[12.5, 10.0, 8.0]" in text
    assert "GPU0: 1234/20480" in text


def test_report_only_reads_synthetic_suites_without_running_or_writing_sources(tmp_path, monkeypatch):
    import scripts.run_nightly_regression as mod

    def forbidden(*args, **kwargs):
        raise AssertionError("report-only must not run collectors, preflight or acquire locks")

    monkeypatch.setattr(mod, "SitlLock", forbidden)
    monkeypatch.setattr(mod, "preflight", forbidden)
    source = tmp_path / "inputs/main"
    _suite_output(source, [
        _row("bad|mission", status="missing", checks={"skills": False, "max_x": False}),
        _row("good"), _row("no-log", status="missing"),
    ], {
        ("bad|mission", "world"): _trace("RuntimeError: sensor stream EOF | <closed>\n详情"),
        ("good", "world"): "complete\n",
    })
    second = tmp_path / "inputs/follow"
    _suite_output(second, [_row("follow", status="missing")], {
        ("follow", "world"): _trace("TimeoutError: telemetry stalled"),
    })
    before = {p: p.read_bytes() for p in (tmp_path / "inputs").rglob("*") if p.is_file()}
    out = tmp_path / "reports"
    assert run(["--report-only", str(source), str(second), "--out", str(out)], runner=forbidden) == 0
    assert before == {p: p.read_bytes() for p in (tmp_path / "inputs").rglob("*") if p.is_file()}
    report = json.loads((out / "nightly-report.json").read_text(encoding="utf-8"))
    histogram = report["failure_categories"]
    assert set(histogram) == set(FAILURE_CATEGORIES)
    assert histogram == {key: int(key in {"sensor_stream_eof", "telemetry_stall", "other"}) for key in FAILURE_CATEGORIES}
    assert report["suites"][0]["exit_code"] is None
    text = (out / "nightly-report.md").read_text(encoding="utf-8")
    assert "bad&#124;mission" in text
    assert "RuntimeError: sensor stream EOF &#124; &lt;closed&gt;<br>详情" in text
    assert "skills, max_x" in text
    assert "日志不可读：FileNotFoundError:" in text
    assert "| sensor_stream_eof | 1 |" in text


@pytest.mark.parametrize("relative_out", [None, ".", "nested"])
def test_report_only_requires_output_outside_sources(tmp_path, relative_out):
    source = tmp_path / "main"
    _suite_output(source, [])
    args = ["--report-only", str(source)]
    if relative_out is not None:
        args += ["--out", str(source / relative_out)]
    with pytest.raises(SystemExit, match="2"):
        run(args)
    assert sorted(p.name for p in source.iterdir()) == ["suite-report.json"]


def test_regular_run_includes_triage_from_suite_output(tmp_path, monkeypatch):
    import scripts.run_nightly_regression as mod

    class NoopLock:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def runner(cmd, **kwargs):
        source = Path(cmd[cmd.index("--out") + 1])
        _suite_output(source, [_row("failure", status="missing")], {
            ("failure", "world"): _trace("TimeoutError: waiting for land timed out"),
        })
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(mod, "SitlLock", NoopLock)
    monkeypatch.setattr(mod, "preflight", lambda *args: None)
    # The scheduler creates the suite directory before calling the child runner.
    (tmp_path / "out").mkdir()
    assert run(["--suites", "main", "--out", str(tmp_path / "out")], runner=runner) == 1
    report = json.loads((tmp_path / "out/nightly-report.json").read_text(encoding="utf-8"))
    assert report["suites"][0]["missions"][0]["failure_category"] == "land_timeout"
    assert report["failure_categories"]["land_timeout"] == 1

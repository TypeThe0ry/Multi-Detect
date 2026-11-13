import json
from pathlib import Path

from scripts.nightly_trend import build_trend, discover_runs, render_markdown, write_outputs


def _run(root: Path, name: str, generated: str, commit: str, *, passed: bool) -> None:
    run = root / name
    suite = run / "main"
    mission = suite / "mission-a" / "campaign"
    mission.mkdir(parents=True)
    (mission / "world.log").write_text("complete\n" if passed else
        "Traceback (most recent call last):\nRuntimeError: sensor stream EOF\n", encoding="utf-8")
    row = {"id": "mission-a", "world": "world", "episode": 1,
           "status": "complete" if passed else "missing", "mission_completed": passed,
           "expectation_checks": {"skills": passed}, "expectations_passed": passed}
    report = {"missions": [row], "provenance": {"code": {"commit": commit, "short_commit": commit[:12]}}}
    (suite / "suite-report.json").write_text(json.dumps(report), encoding="utf-8")
    aggregate = {"generated_at": generated, "suites": [{"name": "main", "report": str(suite / "suite-report.json")}]}
    (run / "nightly-report.json").write_text(json.dumps(aggregate), encoding="utf-8")


def test_discover_builds_matrix_regression_flaky_and_provenance(tmp_path):
    _run(tmp_path, "run-2", "2026-11-02T00:00:00+00:00", "a" * 40, passed=True)
    _run(tmp_path, "run-1", "2026-11-01T00:00:00+00:00", "b" * 40, passed=False)
    runs = discover_runs(tmp_path / "run-*")
    assert [run["name"] for run in runs] == ["run-1", "run-2"]
    trend = build_trend(runs)
    entry = trend["matrix"][0]
    assert [cell["status"] for cell in entry["runs"]] == ["fail", "pass"]
    assert entry["flaky"] is True
    assert trend["regressions"] == []
    assert trend["runs"][0]["commits"]["main"]["short_commit"] == "bbbbbbbbbbbb"


def test_regression_and_outputs_are_readable(tmp_path):
    _run(tmp_path, "old", "2026-11-01T00:00:00+00:00", "a" * 40, passed=True)
    _run(tmp_path, "new", "2026-11-02T00:00:00+00:00", "b" * 40, passed=False)
    trend = build_trend(discover_runs(tmp_path / "*"))
    assert trend["regressions"][0]["mission"] == "mission-a"
    assert trend["regressions"][0]["failure_category"] == "sensor_stream_eof"
    assert "F (sensor_stream_eof)" in render_markdown(trend)
    out = tmp_path / "out"
    write_outputs(trend, out)
    assert json.loads((out / "nightly-trend.json").read_text()) == trend
    assert (out / "nightly-trend.md").is_file()

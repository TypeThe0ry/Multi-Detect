from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_release_pre9 as runner


def test_stage_order_is_fixed_and_complete() -> None:
    assert tuple(stage.name for stage in runner.stage_definitions()) == runner.STAGE_NAMES


def test_only_stage_selects_one_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = runner.build_parser().parse_args(
        ["--dry-run", "--only-stage", "parity", "--run-dir", str(tmp_path / "run")]
    )
    assert runner.run(args) == 0
    output = capsys.readouterr().out
    assert "parity" in output
    assert "static_checks" not in output
    assert not list(tmp_path.iterdir())


def test_from_stage_skips_prior_success_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "parity.status.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    called: list[str] = []
    monkeypatch.setattr(
        runner, "_execute_stage", lambda stage, root, target: called.append(stage.name)
    )
    args = runner.build_parser().parse_args(["--from-stage", "parity", "--run-dir", str(run_dir)])
    assert runner.run(args) == 0
    assert called[0] == "static_checks"
    assert "parity" not in called


def test_failure_stops_and_records_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(stage, root, run_dir):
        if stage.name == "version_bump":
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_execute_stage", fail)
    args = runner.build_parser().parse_args(["--run-dir", str(tmp_path / "run")])
    with pytest.raises(RuntimeError, match="boom"):
        runner.run(args)
    marker = tmp_path / "run" / "version_bump.status.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "failed"
    assert not (tmp_path / "run" / "parity.status.json").exists()

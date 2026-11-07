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


def test_every_and_separator_runs_its_own_subprocess():
    """static_checks joins three commands; only the first '&&' used to be split,
    so pytest received '&&' as a path and the stage failed with exit 4."""
    import pytest

    from scripts.run_release_pre9 import _split_commands

    assert _split_commands(["a", "1", "&&", "b", "&&", "c", "2"]) == [["a", "1"], ["b"], ["c", "2"]]
    assert _split_commands(["only"]) == [["only"]]
    with pytest.raises(ValueError):
        _split_commands(["a", "&&", "&&", "b"])


def test_preflight_uses_posix_disks_and_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner.os, "name", "posix")
    monkeypatch.setenv("TMPDIR", str(tmp_path / ".tmp"))
    (tmp_path / ".tmp").mkdir()
    report = runner.preflight_report(tmp_path)
    assert "disk_root_free_bytes" in report
    assert "disk_repo_free_bytes" in report
    assert report["temp_expected"] is True
    assert report["msvc_vswhere"] is None


def test_preflight_requires_explicit_posix_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner.os, "name", "posix")
    monkeypatch.delenv("TMPDIR", raising=False)
    (tmp_path / ".tmp").mkdir()
    assert runner.preflight_report(tmp_path)["temp_expected"] is False


def test_command_format_follows_host_shell() -> None:
    command = ["tool", "value with spaces"]
    assert runner._format_command(command, platform="posix") == "tool 'value with spaces'"
    assert runner._format_command(command, platform="nt") == 'tool "value with spaces"'


def test_stage_shell_tools_keep_native_suffix() -> None:
    powershell_stage = next(
        stage for stage in runner.stage_definitions() if stage.name == "windows_gcs_build"
    )
    assert powershell_stage.command[0] == runner.executable_name("powershell")

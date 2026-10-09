"""CPU owned-child result fixtures, never a Windows Job or Docker execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import supervise_teacher_origin_window_v1 as wrapper


def _setup_owned_fixture(
    monkeypatch,
    tmp_path,
    *,
    status="origin-window-recorded",
    contract=None,
    code=0,
    missing=False,
    drift=False,
):
    contract = "multidetect.teacher-SIM-origin-window-v1" if contract is None else contract
    monkeypatch.setattr(wrapper, "ROOT", tmp_path)
    monkeypatch.setattr(
        wrapper, "SOURCE_PATHS", ("scripts/supervise_onboard_navigation_exact_load.py",)
    )
    record, calls = wrapper.owned.record, []

    def fixture_record(path, **kwargs):
        path = Path(path)
        if path.name == "supervise_onboard_navigation_exact_load.py":
            calls.append(path)
            return {
                "path": str(path),
                "bytes": 29530,
                "sha256": "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69"
                if not drift or len(calls) == 1
                else "a" * 64,
            }
        return record(path, **kwargs)

    def fixture_child(command, output, label, deadline):
        if not missing:
            diagnostics = Path(command[command.index("--output-directory") + 1])
            diagnostics.mkdir()
            wrapper.owned.write_new(
                diagnostics / "origin-window.json",
                wrapper.owned.canonical({"contract": contract, "status": status}),
            )
        return {"native_exit_code": code, "owned_tree_empty": True, "watchdog_fired": False}

    monkeypatch.setattr(wrapper.owned, "record", fixture_record)
    monkeypatch.setattr(wrapper.owned, "run_child", fixture_child)
    monkeypatch.setattr(wrapper.owned, "child_complete", lambda row: row["native_exit_code"] == 0)
    directory = tmp_path / "artifacts/diagnostics"
    directory.mkdir(parents=True)
    return directory / "supervised", directory / "captured"


def test_matching_success_keeps_all_authority_false(monkeypatch, tmp_path):
    output, capture = _setup_owned_fixture(monkeypatch, tmp_path)
    assert wrapper.supervise(output, capture) == 0
    report = json.loads((output / "supervised-run.json").read_bytes())
    assert report["worker_status"] == "origin-window-recorded"
    assert set(report["claims"].values()) == {False}
    assert report["selected_sources_unchanged_during_run"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"missing": True},
        {"status": "planned"},
        {"status": "origin-window-failed"},
        {"contract": "other-contract"},
        {"code": 1},
        {"drift": True},
    ],
)
def test_zero_native_alone_or_source_drift_is_not_success(monkeypatch, tmp_path, kwargs):
    output, capture = _setup_owned_fixture(monkeypatch, tmp_path, **kwargs)
    assert wrapper.supervise(output, capture) == 1
    assert (output / "supervised-run.json").is_file()


@pytest.mark.parametrize("kwargs", [{"timeout_s": 0}, {"timeout_s": True}, {"timeout_s": 601}])
def test_bad_time_budget_refused_before_spawn(monkeypatch, tmp_path, kwargs):
    output, capture = _setup_owned_fixture(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        wrapper.supervise(output, capture, **kwargs)
    assert not output.exists()


def test_existing_or_nested_or_escaped_output_refused(monkeypatch, tmp_path):
    output, capture = _setup_owned_fixture(monkeypatch, tmp_path)
    output.mkdir()
    with pytest.raises(ValueError, match="fresh"):
        wrapper.supervise(output, capture)
    with pytest.raises(ValueError, match="non-nested"):
        wrapper.supervise(capture, capture / "nested")
    with pytest.raises(ValueError, match="contained"):
        wrapper.supervise(tmp_path / "outside", capture)

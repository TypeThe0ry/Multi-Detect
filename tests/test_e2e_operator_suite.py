import json
import subprocess
from pathlib import Path

import pytest

from scripts.run_e2e_operator_suite import (
    SitlLock,
    build_commands,
    cleanup_owned,
    load_suite,
    render_dry_run,
)

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "configs/onboard/suite-e2e-operator.json"


def test_suite_loads_five_bilingual_prompt_missions():
    cases = load_suite(SUITE)
    assert len(cases) == 5
    assert {case["id"] for case in cases} == {
        "count-cones",
        "count-fire-hydrants",
        "monitor-backpacks",
        "patrol-fence-backpacks",
        "inspect-postbox",
    }
    for case in cases:
        package = case.get("mission_package", case)
        assert package["task_text"] if "task_text" in package else case.get("prompt")
        labels = package.get("ref_labels", case.get("ref_labels", {}))
        assert all(" / " in label for label in labels.values())


def test_suite_rejects_duplicate_ids_and_missing_task(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps({"tasks": [{"id": "same", "prompt": "x"}, {"id": "same", "prompt": "y"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_suite(path)
    path.write_text(json.dumps({"tasks": [{"id": "empty"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="provide"):
        load_suite(path)


def test_dry_run_command_order_and_owned_container(tmp_path):
    cases = load_suite(SUITE)
    commands = build_commands(
        cases[0], tmp_path / "count-cones", tmp_path / "count-cones/package.json"
    )
    assert "--operator-link" in commands["session"]
    assert "--exit-after-task" in commands["session"]
    assert "--auto-approve" in commands["gcs"]
    assert "--evaluate" in commands["evaluate"]
    assert commands["container"][0].startswith("multidetect-e2e-count-cones-")
    rendered = render_dry_run(cases, tmp_path)
    assert "session:" in rendered and "gcs:" in rendered and "evaluate:" in rendered
    assert "--auto-approve" in rendered and "multidetect-e2e-count-cones-" in rendered


def test_cleanup_targets_only_recorded_processes_and_owned_container(monkeypatch):
    calls = []

    class Proc:
        pid = 12345

        def __init__(self):
            self.stopped = False

        def poll(self):
            return None if not self.stopped else 0

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

    proc = Proc()
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    cleanup_owned([proc], ["multidetect-e2e-count-cones-a1b2c3d4", "other-container"])
    assert proc.stopped
    assert calls == [["docker", "rm", "-f", "multidetect-e2e-count-cones-a1b2c3d4"]]


def test_lock_release_does_not_remove_another_owners_lock(tmp_path):
    path = tmp_path / "sitl.lock"
    path.mkdir()
    (path / "owner").write_text("someone-else 42 now\n", encoding="utf-8")
    lock = SitlLock(path=path)
    lock.acquired = True
    lock.release()
    assert path.is_dir()
    assert (path / "owner").read_text(encoding="utf-8").startswith("someone-else")


def test_lock_owner_uses_shared_pid_field_and_releases(tmp_path):
    path = tmp_path / "sitl.lock"
    lock = SitlLock(path=path, owner="suite-test")
    lock.acquire(timeout=0)
    owner = (path / "owner").read_text(encoding="utf-8").split()
    assert owner[0] == "suite-test"
    assert owner[1].isdigit()
    lock.release()
    assert not path.exists()


def test_suite_skips_nested_lock_when_parent_marker_is_set(tmp_path, monkeypatch):
    import scripts.run_e2e_operator_suite as suite

    class NoNestedAcquire:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self, *_args, **_kwargs):
            raise AssertionError("nested e2e suite must not acquire the parent SITL lock")

        def release(self):
            raise AssertionError("nested e2e suite must not release the parent SITL lock")

    monkeypatch.setattr(suite, "SitlLock", NoNestedAcquire)
    monkeypatch.setattr(suite, "load_suite", lambda _path: [])
    monkeypatch.setenv("MULTIDETECT_SITL_LOCK_HELD", "1")
    assert suite.run_suite(tmp_path / "suite.json", output_root=tmp_path / "out") == 0

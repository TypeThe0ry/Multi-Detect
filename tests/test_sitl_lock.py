from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.sitl_lock as lock_module
from scripts.sitl_lock import SitlLock, running_sitl_containers, sitl_lock_context


def test_container_gate_matches_only_sitl_prefixes():
    def runner(*_args, **_kwargs):
        return SimpleNamespace(stdout="md-sim-teacher-a\nr15-worker-1\nredis\n")

    assert running_sitl_containers(runner) == ("md-sim-teacher-a", "r15-worker-1")


def test_container_gate_fails_closed_when_docker_is_unavailable():
    def runner(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    assert running_sitl_containers(runner) is None


def test_sitl_lock_context_acquires_sets_marker_and_restores_environment(monkeypatch):
    events = []

    class FakeLock:
        def acquire(self, *args, **kwargs):
            events.append(("acquire", args, kwargs))

        def release(self):
            events.append(("release",))

    monkeypatch.delenv("MULTIDETECT_SITL_LOCK_HELD", raising=False)
    with sitl_lock_context(FakeLock(), 3, wait=False):
        assert os.environ["MULTIDETECT_SITL_LOCK_HELD"] == "1"
    assert "MULTIDETECT_SITL_LOCK_HELD" not in os.environ
    assert events == [("acquire", (3,), {"wait": False}), ("release",)]


def test_sitl_lock_context_skips_nested_acquire_and_preserves_marker(monkeypatch):
    events = []

    class FakeLock:
        def acquire(self, *args, **kwargs):
            events.append("acquire")

        def release(self):
            events.append("release")

    monkeypatch.setenv("MULTIDETECT_SITL_LOCK_HELD", "1")
    with sitl_lock_context(FakeLock()):
        assert os.environ["MULTIDETECT_SITL_LOCK_HELD"] == "1"
    assert os.environ["MULTIDETECT_SITL_LOCK_HELD"] == "1"
    assert events == []


def test_sitl_lock_context_restores_nonheld_previous_value(monkeypatch):
    class FakeLock:
        def acquire(self):
            pass

        def release(self):
            pass

    monkeypatch.setenv("MULTIDETECT_SITL_LOCK_HELD", "0")
    with sitl_lock_context(FakeLock()):
        assert os.environ["MULTIDETECT_SITL_LOCK_HELD"] == "1"
    assert os.environ["MULTIDETECT_SITL_LOCK_HELD"] == "0"


@pytest.mark.parametrize("busy,memory", [(True, 8.0), (False, 4.99)])
def test_lock_does_not_acquire_when_prerequisites_fail(tmp_path, busy, memory):
    def runner(*_args, **_kwargs):
        return SimpleNamespace(stdout="md-active\n" if busy else "")

    lock = SitlLock(
        tmp_path / "lock",
        retry_s=0,
        runner=runner,
        memory_fn=lambda: memory,
    )
    with pytest.raises(RuntimeError, match="prerequisites"):
        lock.acquire(wait=False)
    assert not lock.path.exists()


def test_lock_waits_for_gate_and_releases_owned_directory(tmp_path):
    calls = 0

    def runner(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(stdout="md-active\n" if calls == 1 else "")

    lock = SitlLock(
        tmp_path / "lock",
        retry_s=0,
        runner=runner,
        memory_fn=lambda: 6.0,
    )
    lock.acquire()
    assert lock.acquired
    assert "start=" in lock.owner.read_text(encoding="utf-8")
    lock.release()
    assert not lock.path.exists()


def test_dead_owner_is_reclaimed_only_after_grace_period(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    path.mkdir()
    owner = path / "owner"
    owner.write_text("old 424242 2026-01-01T00:00:00+00:00 start=1.0\n", encoding="utf-8")
    old = time.time() - 121
    os.utime(owner, (old, old))
    monkeypatch.setattr(lock_module, "_process_snapshot", lambda _pid: (False, None))
    lock = SitlLock(path, retry_s=0, runner=lambda *_a, **_k: SimpleNamespace(stdout=""), memory_fn=lambda: 6.0)
    lock.acquire(wait=False)
    assert lock.owner.exists()
    lock.release()


def test_live_owner_with_reused_pid_is_reclaimed_after_grace(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    path.mkdir()
    owner = path / "owner"
    owner.write_text("old 424242 2026-01-01T00:00:00+00:00 start=1.0\n", encoding="utf-8")
    old = time.time() - 121
    os.utime(owner, (old, old))

    def snapshot(pid):
        return (True, 2.0)

    monkeypatch.setattr(lock_module, "_process_snapshot", snapshot)
    lock = SitlLock(path, retry_s=0, runner=lambda *_a, **_k: SimpleNamespace(stdout=""), memory_fn=lambda: 6.0)
    lock.acquire(wait=False)
    assert lock.owner.read_text(encoding="utf-8").startswith(
        f"nightly-regression {os.getpid()} "
    )
    lock.release()

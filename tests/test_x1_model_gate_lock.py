"""CPU-only checks for the X1 gate's host-lock handoff."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts.realworld.x1_lock import (
    FLIGHT_LOCK_FD_ENV, LockBusy, demo_flight_lock, flight_lock, sidecar, world_owner, world_phase,
)


def test_gate_hands_off_to_multiple_children_and_excludes_standalone_flights(monkeypatch, tmp_path):
    monkeypatch.delenv(FLIGHT_LOCK_FD_ENV, raising=False)
    lock = tmp_path / "x1.lock"
    child = """
import os
import sys
from pathlib import Path
from scripts.realworld.x1_lock import FLIGHT_LOCK_FD_ENV, demo_flight_lock

lock = Path(sys.argv[1])
with demo_flight_lock(lock):
    print('admitted', flush=True)
os.fstat(int(os.environ[FLIGHT_LOCK_FD_ENV]))
raise SystemExit(int(sys.argv[2]))
"""
    with flight_lock(lock, wait=0) as stream:
        fd = stream.fileno()
        for status in (0, 42):
            result = subprocess.run(
                [sys.executable, "-c", child, str(lock), str(status)],
                env={**os.environ, FLIGHT_LOCK_FD_ENV: str(fd)},
                pass_fds=(fd,), capture_output=True, text=True, timeout=5,
            )
            assert (result.returncode, result.stdout, result.stderr) == (status, "admitted\n", "")
            with pytest.raises(LockBusy), demo_flight_lock(lock):
                pytest.fail("an independent demo overlapped the gate")
    with demo_flight_lock(lock):
        pass


def test_without_handoff_demo_shares_and_blocks_exclusive_gate(monkeypatch, tmp_path):
    monkeypatch.delenv(FLIGHT_LOCK_FD_ENV, raising=False)
    lock = tmp_path / "x1.lock"
    with demo_flight_lock(lock), demo_flight_lock(lock):
        with pytest.raises(LockBusy), flight_lock(lock, wait=0):
            pytest.fail("gate ran alongside an existing shared flight")
    with flight_lock(lock, wait=0):
        pass


def test_zero_wait_does_not_request_world_yield(tmp_path):
    lock = tmp_path / "x1.lock"
    with world_owner(lock), world_phase(lock):
        with pytest.raises(LockBusy, match="timed out"), flight_lock(lock, wait=0):
            pytest.fail("gate ran alongside the world worker")
        assert not sidecar(lock, "flight-request").exists()


@pytest.mark.parametrize("value", ["", "abc", "-1", " 3", "9" * 32])
def test_invalid_env_fails_closed(monkeypatch, tmp_path, value):
    monkeypatch.setenv(FLIGHT_LOCK_FD_ENV, value)
    lock = tmp_path / "x1.lock"
    lock.touch()
    with pytest.raises(ValueError, match=FLIGHT_LOCK_FD_ENV), demo_flight_lock(lock):
        pytest.fail("invalid handoff fell back to a shared lock")


def test_handoff_rejects_closed_fd(monkeypatch, tmp_path):
    lock = tmp_path / "x1.lock"
    with flight_lock(lock, wait=0) as stream:
        fd = stream.fileno()
    monkeypatch.setenv(FLIGHT_LOCK_FD_ENV, str(fd))
    with pytest.raises(ValueError, match=FLIGHT_LOCK_FD_ENV), demo_flight_lock(lock):
        pytest.fail("a closed descriptor was accepted")


def test_handoff_rejects_exclusive_fd_for_different_inode(monkeypatch, tmp_path):
    lock = tmp_path / "x1.lock"
    lock.touch()
    with flight_lock(tmp_path / "other.lock", wait=0) as stream:
        monkeypatch.setenv(FLIGHT_LOCK_FD_ENV, str(stream.fileno()))
        with pytest.raises(ValueError, match="does not match"), demo_flight_lock(lock):
            pytest.fail("a descriptor for a different file was accepted")


def test_handoff_rejects_separate_description_of_same_inode(monkeypatch, tmp_path):
    lock = tmp_path / "x1.lock"
    lock.touch()
    with lock.open("a") as unlocked_stream:
        monkeypatch.setenv(FLIGHT_LOCK_FD_ENV, str(unlocked_stream.fileno()))
        for context in (flight_lock(lock, wait=0), flight_lock(lock, wait=0, shared=True)):
            with context:
                with pytest.raises(ValueError, match="exclusive flock"), demo_flight_lock(lock):
                    pytest.fail("an independently opened descriptor was accepted")
        with pytest.raises(ValueError, match="exclusive flock"), demo_flight_lock(lock):
            pytest.fail("an unlocked descriptor took over a free lock")


def test_handoff_rejects_shared_descriptor_without_upgrading(monkeypatch, tmp_path):
    lock = tmp_path / "x1.lock"
    with flight_lock(lock, wait=0, shared=True) as stream:
        monkeypatch.setenv(FLIGHT_LOCK_FD_ENV, str(stream.fileno()))
        with pytest.raises(ValueError, match="exclusive flock"), demo_flight_lock(lock):
            pytest.fail("a shared descriptor was upgraded")
        with flight_lock(lock, wait=0, shared=True):
            pass


def test_handoff_keeps_parent_lock_on_context_exception(monkeypatch, tmp_path):
    lock = tmp_path / "x1.lock"
    with flight_lock(lock, wait=0) as stream:
        monkeypatch.setenv(FLIGHT_LOCK_FD_ENV, str(stream.fileno()))
        with pytest.raises(RuntimeError, match="fake flight failed"), demo_flight_lock(lock):
            raise RuntimeError("fake flight failed")
        with demo_flight_lock(lock):
            os.fstat(stream.fileno())
        with pytest.raises(LockBusy), flight_lock(lock, wait=0, shared=True):
            pytest.fail("failure released the parent's lock")

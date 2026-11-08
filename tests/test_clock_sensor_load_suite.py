from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import clock_sensor_load_lease as lease


SCRIPT = lease.ROOT / "scripts/run_clock_sensor_load_suite.sh"


def test_suite_child_starts_only_after_any_slot_is_leased(monkeypatch):
    events: list[object] = []

    class FakeLock:
        slot = 2

        def __init__(self, path, **kwargs):
            events.append(("construct", path, kwargs))

        def acquire(self, *, wait):
            assert wait is True
            events.append(("acquire", self.slot))

        def release(self):
            events.append(("release", self.slot))

    def runner(command, *, cwd, env, check):
        assert events[-1] == ("acquire", 2)
        assert command == ["bash", str(SCRIPT), "--extra-test-arg"]
        assert cwd == lease.ROOT
        assert check is False
        assert env["MULTIDETECT_SITL_SLOTS"] == "3"
        assert env["MULTIDETECT_SITL_SLOT"] == "2"
        assert env["MULTIDETECT_SITL_LOCK_HELD"] == "1"
        events.append(("child", env["MULTIDETECT_SITL_SLOT"]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lease, "default_lock_path", lambda: Path("/tmp/test-sitl-lock"))
    assert lease.run_with_slot_lease(
        SCRIPT,
        ["--extra-test-arg"],
        runner=runner,
        lock_factory=FakeLock,
    ) == 0
    assert events[-2:] == [("child", "2"), ("release", 2)]


def test_suite_child_failure_still_releases_its_slot():
    events: list[str] = []

    class FakeLock:
        slot = 1

        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self, *, wait):
            assert wait is True
            events.append("acquired")

        def release(self):
            events.append("released")

    def runner(command, **_kwargs):
        assert events == ["acquired"]
        return subprocess.CompletedProcess(command, 17)

    assert lease.run_with_slot_lease(SCRIPT, runner=runner, lock_factory=FakeLock) == 17
    assert events == ["acquired", "released"]


def test_suite_does_not_start_child_when_slot_acquisition_fails():
    child_started = False

    class BusyLock:
        slot = None

        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self, *, wait):
            assert wait is True
            raise RuntimeError("slot acquisition failed")

        def release(self):
            pytest.fail("an unacquired lock must not be released")

    def runner(*_args, **_kwargs):
        nonlocal child_started
        child_started = True
        return subprocess.CompletedProcess([], 0)

    with pytest.raises(RuntimeError, match="slot acquisition failed"):
        lease.run_with_slot_lease(SCRIPT, runner=runner, lock_factory=BusyLock)
    assert child_started is False


def test_suite_rejects_load_without_lease_and_preserves_approved_profile():
    source = SCRIPT.read_text(encoding="utf-8")
    guard = '[[ ${MULTIDETECT_SITL_LOCK_HELD:-} != 1 ]]'
    stress = 'stress-ng --cpu 12 --taskset "${LOAD_CPUS}" --timeout "${LOAD_SECONDS}s" --metrics-brief'

    assert source.count(guard) >= 3
    loaded_branch = source.index("run_condition()")
    assert source.index(guard, loaded_branch) < source.index('local condition_dir=', loaded_branch)
    assert source.index(stress) > source.index("if ((load)); then", loaded_branch)
    assert "LOAD_CPUS=0-11" in source
    assert "LOAD_SECONDS=${LOAD_SECONDS:-600}" in source
    assert "MULTIDETECT_SITL_SLOTS=3" in source
    assert "SLOT=${MULTIDETECT_SITL_SLOT:-0}" in source
    assert "CONTROL_PORT=$((14560 + SLOT * 10))" in source
    assert "TELEMETRY_PORT=$((14561 + SLOT * 10))" in source
    assert "OPERATOR_PORT=$((15480 + SLOT * 100))" in source
    assert "--worker-index \"${SLOT}\"" in source
    assert "artifacts/benchmarks/clock-sensor-load-20261108b" in source
    assert "clock-sensor-load-20261108/" not in source


def test_suite_shell_syntax_is_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

import json
from types import SimpleNamespace

import pytest

from scripts.polaris_sim.memwatch import (
    _set_pid,
    _stop_owned_container,
    check_once,
    parse_docker_usage,
    parse_memory_bytes,
)


def test_memory_quantity_parsing():
    assert parse_memory_bytes("16g") == 16 * 1024**3
    assert parse_memory_bytes("512MiB") == 512 * 1024**2
    assert parse_docker_usage("1.25GiB / 16GiB") == int(1.25 * 1024**3)


def test_watchdog_records_its_pid(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    _set_pid(state_file, 456)

    assert json.loads(state_file.read_text(encoding="utf-8"))["memwatch_pid"] == 456


def test_watchdog_stops_slot_unit_before_its_container(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scripts.polaris_sim.memwatch.subprocess.run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    _stop_owned_container("md-polaris-sim-slot-6")

    assert calls == [
        ["systemctl", "stop", "psim-sitl-slot-6"],
        ["docker", "stop", "--time", "5", "md-polaris-sim-slot-6"],
    ]


def test_watchdog_stops_only_owned_container_and_records_reason(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"phase": "running"}), encoding="utf-8")
    stopped = []
    usages = {
        "md-polaris-sim-slot-5": 15 * 1024**3,
        "md-polaris-sim-slot-6": int(16 * 1024**3 * 0.9),
    }

    events = check_once(
        tuple(usages),
        16 * 1024**3,
        state_file,
        usage_reader=usages.get,
        stopper=stopped.append,
        now=lambda: 123.0,
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert stopped == ["md-polaris-sim-slot-5"]
    assert events[0]["usage_bytes"] == 15 * 1024**3
    assert state["memory_watchdog_container"] == "md-polaris-sim-slot-5"
    assert "已停止该仿真容器" in state["memory_watchdog_reason"]
    assert state["phase"] == "running"


def test_watchdog_refuses_non_polaris_container(tmp_path):
    with pytest.raises(ValueError, match="unowned"):
        check_once(
            ("unrelated-container",),
            16 * 1024**3,
            tmp_path / "state.json",
            usage_reader=lambda _container: 16 * 1024**3,
            stopper=lambda _container: pytest.fail("must not stop"),
        )

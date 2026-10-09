from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vtol_sitl_attached_acceptance.py"
MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _module():
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("vtol_attached_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _container_record(module: object, *, label: str | None = None) -> dict[str, object]:
    return {
        "Id": "a" * 64,
        "Name": "/combined-sim-test",
        "Config": {
            "Labels": {
                "multidetect.purpose": label or module.EXPECTED_CONTAINER_LABEL,
            }
        },
        "State": {"Running": True},
    }


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeCollector:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.elapsed_s = 10.0
        self.messages: list[dict[str, object]] = [{"type": "HEARTBEAT"}]
        self.last_heartbeat = {
            "base_mode": 128,
            "vehicle_type": 22,
            "custom_mode": 0,
            "system_status": 4,
        }
        self.last_gps = {"fix_type": 3, "satellites_visible": 10, "lat": 1, "lon": 1}
        self.last_landed_state = 1  # MAV_LANDED_STATE_ON_GROUND
        self.armed_waits: list[tuple[bool, float]] = []

    def wait_for_heartbeat(self, _timeout_s: float) -> dict[str, object]:
        return dict(self.last_heartbeat)

    def wait_for_gps_fix(self, _timeout_s: float) -> dict[str, object]:
        return dict(self.last_gps)

    def wait_for_navigation_settle(self) -> None:
        return None

    def send_gcs_heartbeat(self, *, force: bool = False) -> None:
        assert force is True

    def collect(self, duration_s: float) -> None:
        self.elapsed_s += duration_s

    def wait_for_armed_state(self, armed: bool, timeout_s: float) -> None:
        assert timeout_s > 0.0
        self.armed_waits.append((armed, timeout_s))
        current = bool(
            self.last_heartbeat
            and self.last_heartbeat["base_mode"] & 128  # MAV_MODE_FLAG_SAFETY_ARMED
        )
        assert current is armed

    def wait_for_vtol_state(self, _state: int, _timeout_s: float) -> None:
        return None


def test_attach_gate_runs_only_bounded_evaluator_commands_and_keeps_runtime_readonly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    connection = _FakeConnection()
    collectors: list[_FakeCollector] = []

    def collector_factory(channel: _FakeConnection) -> _FakeCollector:
        collector = _FakeCollector(channel)
        collectors.append(collector)
        return collector

    command_calls: list[tuple[int, tuple[float, ...]]] = []

    def send_command(collector: _FakeCollector, command: int, *parameters: float) -> int:
        command_calls.append((command, parameters))
        if command == module.mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
            if parameters[0] == 0.0:
                collector.last_heartbeat["base_mode"] = 0
            else:
                collector.last_heartbeat["base_mode"] = 128
        return 0

    monkeypatch.setattr(
        module,
        "_container_inspect",
        lambda _docker, _name: _container_record(module),
    )
    monkeypatch.setattr(
        module.mavutil,
        "mavlink_connection",
        lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setattr(module, "TelemetryCollector", collector_factory)
    monkeypatch.setattr(module, "_send_command", send_command)
    monkeypatch.setattr(
        module,
        "_position_quality",
        lambda _collector: {
            "qualified": True,
            "false_geolocations": 0,
            "samples": 40,
            "gps_fix_type": 3,
            "satellites_visible": 10,
            "maximum_sample_jump_m": 0.01,
        },
    )
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "parameter set\n", ""),
    )

    evidence = tmp_path / "vtol-attached.json"
    ready = tmp_path / "fixed-wing-ready.json"
    result = module.run_attached_acceptance(
        MANIFEST,
        evidence,
        docker="docker.exe",
        container_name="combined-sim-test",
        evaluator_port=18778,
        fixed_wing_hold_s=0.0,
        multicopter_hold_s=0.0,
        fixed_wing_ready_file=ready,
    )

    assert result["gate_passed"] is True
    assert result["container"]["created_by_this_helper"] is False
    assert result["container"]["stopped_by_this_helper"] is False
    assert result["safety"]["product_runtime_flight_control_enabled"] is False
    assert result["safety"]["product_runtime_actuator_output_enabled"] is False
    assert result["safety"]["evaluator_command_count"] == 4
    assert [item[0] for item in command_calls] == [
        module.mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        module.mavutil.mavlink.MAV_CMD_DO_VTOL_TRANSITION,
        module.mavutil.mavlink.MAV_CMD_DO_VTOL_TRANSITION,
        module.mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
    ]
    assert result["scenario"]["truth_loaded_by_runtime"] is False
    assert result["scenario"]["expectations_loaded_by_runtime"] is False
    assert result["qgc"]["operator_visual_acceptance"] == "not_claimed"
    assert evidence.is_file()
    assert ready.is_file()
    assert collectors
    assert collectors[0].armed_waits == [
        (True, module.ARMED_STATE_TIMEOUT_SECONDS),
        (False, 12.0),
    ]
    assert connection.closed is True


def test_attach_gate_rejects_unowned_or_nonrunning_container_before_mavlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    opened = False

    def open_connection(*_args: object, **_kwargs: object) -> None:
        nonlocal opened
        opened = True

    record = _container_record(module, label="wrong-purpose")
    monkeypatch.setattr(module, "_container_inspect", lambda _docker, _name: record)
    monkeypatch.setattr(module.mavutil, "mavlink_connection", open_connection)

    with pytest.raises(module.VtolAcceptanceError, match="ownership label"):
        module.run_attached_acceptance(
            MANIFEST,
            tmp_path / "should-not-exist.json",
            docker="docker.exe",
            container_name="combined-sim-test",
            evaluator_port=18778,
        )
    assert opened is False


@pytest.mark.parametrize("port", [80, 14550, 65536])
def test_attach_gate_rejects_invalid_or_protected_port(
    port: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    inspected = False

    def inspect(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal inspected
        inspected = True
        return SimpleNamespace()

    monkeypatch.setattr(module, "_container_inspect", inspect)
    with pytest.raises(module.VtolAcceptanceError, match="cannot be 14550"):
        module.run_attached_acceptance(
            MANIFEST,
            tmp_path / "should-not-exist.json",
            docker="docker.exe",
            container_name="combined-sim-test",
            evaluator_port=port,
        )
    assert inspected is False


def test_attached_helper_source_keeps_lifecycle_and_ui_boundaries() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "docker run" not in text.lower()
    assert '"created_by_this_helper": False' in text
    assert '"stopped_by_this_helper": False' in text
    assert '"product_runtime_flight_control_enabled": False' in text
    assert '"operator_visual_acceptance": "not_claimed"' in text
    assert "MAV_CMD_COMPONENT_ARM_DISARM" in text
    assert "MAV_CMD_DO_VTOL_TRANSITION" in text

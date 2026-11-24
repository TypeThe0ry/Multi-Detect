from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from scripts import collect_sim_teacher_episodes as cste


class _FakeClock:
    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.now_ns = start_ns

    def monotonic_ns(self) -> int:
        return self.now_ns

    def monotonic(self) -> float:
        return self.now_ns / 1e9

    def sleep(self, dt: float) -> None:
        self.now_ns += int(dt * 1e9)


class _FakeState(dict):
    def __init__(self) -> None:
        super().__init__()
        self.command_acks: dict[int, tuple[SimpleNamespace, int]] = {}
        self.received_monotonic_ns: dict[str, int] = {}


class _FakeReader:
    def __init__(self) -> None:
        self.state = _FakeState()

    def raise_if_failed(self) -> None:
        return None


def _quat_for_pitch_deg(pitch_deg: float) -> list[float]:
    half = math.radians(pitch_deg) / 2.0
    return [math.cos(half), 0.0, math.sin(half), 0.0]


def test_wait_command_ack_accepts_ack_before_wait_starts_with_after_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock(10_000_000_000)
    monkeypatch.setattr(cste.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(cste.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cste.time, "sleep", clock.sleep)

    reader = _FakeReader()
    after_ns = clock.monotonic_ns()
    clock.now_ns += 500_000  # ACK arrives 0.5 ms after send
    reader.state.command_acks[1000] = (SimpleNamespace(command=1000, result=0), clock.now_ns)
    clock.now_ns += 500_000  # wait_command_ack starts 1 ms after send

    ack = cste._ReaderAckLink(SimpleNamespace(), reader).wait_command_ack(1000, 1.0, after_ns=after_ns)
    assert ack == {"command": 1000, "result": 0}


def test_wait_command_ack_default_behavior_unchanged_without_after_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock(20_000_000_000)
    monkeypatch.setattr(cste.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(cste.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cste.time, "sleep", clock.sleep)

    reader = _FakeReader()
    reader.state.command_acks[1000] = (SimpleNamespace(command=1000, result=0), clock.now_ns - 1_000_000)

    ack = cste._ReaderAckLink(SimpleNamespace(), reader).wait_command_ack(1000, 0.05)
    assert ack is None


def test_command_and_wait_gimbal_ack_missing_but_attitude_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock(30_000_000_000)
    monkeypatch.setattr(cste.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(cste.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cste.time, "sleep", clock.sleep)

    reader = _FakeReader()
    sent_commands: list[int] = []

    def _send(target_sys: int, target_comp: int, command: int, *args: float) -> None:
        sent_commands.append(int(command))
        if int(command) == cste.MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE:
            reader.state.command_acks[int(command)] = (SimpleNamespace(command=int(command), result=0), clock.now_ns)
            reader.state["GIMBAL_MANAGER_STATUS"] = SimpleNamespace(
                primary_control_sysid=cste.GCS_SOURCE_SYSTEM,
                primary_control_compid=cste.GCS_SOURCE_COMPONENT,
            )
            reader.state.received_monotonic_ns["GIMBAL_MANAGER_STATUS"] = clock.now_ns
        elif int(command) == cste.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW:
            # No COMMAND_ACK recorded, but gimbal attitude reaches -45 deg pitch.
            reader.state["GIMBAL_DEVICE_ATTITUDE_STATUS"] = SimpleNamespace(
                q=_quat_for_pitch_deg(-45.0),
                failure_flags=0,
            )
            reader.state.received_monotonic_ns["GIMBAL_DEVICE_ATTITUDE_STATUS"] = clock.now_ns

    conn = SimpleNamespace(
        mav=SimpleNamespace(command_long_send=_send),
        target_system=1,
        target_component=1,
    )
    result = cste._command_and_wait_gimbal(
        conn,
        reader,
        {"pitch": -45.0, "yaw": 0.0, "roll": 0.0},
        timeout_s=0.1,
        tolerance_deg=4.0,
    )
    assert result["status"] == "reached"
    assert "collector_gimbal_ack_missing_but_reached" in result
    assert sent_commands.count(cste.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW) == 1


def test_command_and_wait_gimbal_ack_missing_and_not_reached_resends_once_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(40_000_000_000)
    monkeypatch.setattr(cste.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(cste.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cste.time, "sleep", clock.sleep)

    reader = _FakeReader()
    sent_commands: list[int] = []

    def _send(target_sys: int, target_comp: int, command: int, *args: float) -> None:
        sent_commands.append(int(command))
        if int(command) == cste.MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE:
            reader.state.command_acks[int(command)] = (SimpleNamespace(command=int(command), result=0), clock.now_ns)
            reader.state["GIMBAL_MANAGER_STATUS"] = SimpleNamespace(
                primary_control_sysid=cste.GCS_SOURCE_SYSTEM,
                primary_control_compid=cste.GCS_SOURCE_COMPONENT,
            )
            reader.state.received_monotonic_ns["GIMBAL_MANAGER_STATUS"] = clock.now_ns
        elif int(command) == cste.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW:
            # Attitude stays at 0 deg (not reached) and no ACK arrives.
            reader.state["GIMBAL_DEVICE_ATTITUDE_STATUS"] = SimpleNamespace(
                q=_quat_for_pitch_deg(0.0),
                failure_flags=0,
            )
            reader.state.received_monotonic_ns["GIMBAL_DEVICE_ATTITUDE_STATUS"] = clock.now_ns

    conn = SimpleNamespace(
        mav=SimpleNamespace(command_long_send=_send),
        target_system=1,
        target_component=1,
    )
    result = cste._command_and_wait_gimbal(
        conn,
        reader,
        {"pitch": -45.0, "yaw": 0.0, "roll": 0.0},
        timeout_s=0.1,
        tolerance_deg=4.0,
    )
    assert result["status"] == "command_ack_timeout"
    assert sent_commands.count(cste.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW) == 2

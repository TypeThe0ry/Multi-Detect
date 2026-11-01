from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import collect_sim_teacher_episodes as collector


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def monotonic_ns(self) -> int:
        return int(self.now * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def _mavlink() -> SimpleNamespace:
    return SimpleNamespace(
        MAV_CMD_COMPONENT_ARM_DISARM=400,
        MAV_RESULT_ACCEPTED=0,
        MAV_RESULT_TEMPORARILY_REJECTED=1,
        MAV_RESULT_DENIED=2,
        MAV_RESULT_IN_PROGRESS=5,
        MAV_MODE_FLAG_SAFETY_ARMED=128,
        MAV_COMP_ID_AUTOPILOT1=1,
        MAV_TYPE_GCS=6,
        MAV_AUTOPILOT_INVALID=8,
    )


class FakeMav:
    srcSystem = 255
    srcComponent = 190

    def __init__(self) -> None:
        self.commands: list[tuple[object, ...]] = []

    def heartbeat_send(self, *_args: object) -> None:
        return None

    def command_long_send(self, *args: object) -> None:
        self.commands.append(args)


def test_arm_acceptance_waits_for_delayed_fresh_armed_heartbeat(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(collector, "time", clock)
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    mav = FakeMav()
    connection = SimpleNamespace(mav=mav, target_system=1, target_component=1)

    class Reader:
        state = SimpleNamespace(
            count=0,
            messages={
                "COMMAND_ACK": {"command": 400, "result": 0},
                "HEARTBEAT": {"base_mode": 0},
            },
            received_monotonic_ns={
                "COMMAND_ACK": clock.monotonic_ns(),
                "HEARTBEAT": clock.monotonic_ns(),
            },
        )

        def __init__(self) -> None:
            self.state.get = lambda name: self.state.messages.get(name)

        def raise_if_failed(self) -> None:
            if clock.monotonic() >= 105.5:
                self.state.messages["HEARTBEAT"] = {"base_mode": 128}
                self.state.received_monotonic_ns["HEARTBEAT"] = clock.monotonic_ns()

    result = collector._arm(
        connection,
        Reader(),
        SimpleNamespace(mavlink=_mavlink()),
        timeout_s=10.0,
    )

    assert result["armed"] is True
    assert result["attempts"] == 1
    assert len(mav.commands) == 1
    assert clock.monotonic() >= 105.5


@pytest.mark.parametrize("ack", [None, {"command": 400, "result": 1}])
def test_arm_without_acceptance_keeps_bounded_retry_behavior(monkeypatch, ack) -> None:
    clock = FakeClock()
    monkeypatch.setattr(collector, "time", clock)
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    mav = FakeMav()
    connection = SimpleNamespace(mav=mav, target_system=1, target_component=1)

    class Reader:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                messages={"COMMAND_ACK": ack, "HEARTBEAT": {"base_mode": 0}},
                received_monotonic_ns={},
            )
            self.state.get = lambda name: self.state.messages.get(name)

        def raise_if_failed(self) -> None:
            return None

    with pytest.raises(TimeoutError, match="6 bounded attempts"):
        collector._arm(
            connection,
            Reader(),
            SimpleNamespace(mavlink=_mavlink()),
            timeout_s=10.0,
        )
    assert len(mav.commands) == 6


def test_episode_watchdog_allows_overrun_while_vehicle_clock_advances() -> None:
    clock = FakeClock()
    reader = SimpleNamespace(state=SimpleNamespace(vehicle_time_boot_ms=100, count=1))
    watchdog = collector._EpisodeProgressWatchdog(
        started=90.0,
        maximum_s=10.0,
        reader=reader,
        stall_timeout_s=3.0,
        clock=clock.monotonic,
    )
    watchdog.check()
    clock.sleep(2.5)
    reader.state.vehicle_time_boot_ms += 100
    watchdog.check()
    clock.sleep(2.5)
    reader.state.vehicle_time_boot_ms += 100
    watchdog.check()


def test_episode_watchdog_uses_message_progress_without_vehicle_clock() -> None:
    clock = FakeClock()
    reader = SimpleNamespace(state=SimpleNamespace(vehicle_time_boot_ms=None, count=1))
    watchdog = collector._EpisodeProgressWatchdog(
        started=90.0,
        maximum_s=10.0,
        reader=reader,
        stall_timeout_s=3.0,
        clock=clock.monotonic,
    )
    watchdog.check()
    clock.sleep(2.5)
    reader.state.count += 1
    watchdog.check()
    clock.sleep(2.5)
    reader.state.count += 1
    watchdog.check()


def test_episode_watchdog_times_out_after_telemetry_stalls() -> None:
    clock = FakeClock()
    reader = SimpleNamespace(state=SimpleNamespace(vehicle_time_boot_ms=100, count=1))
    watchdog = collector._EpisodeProgressWatchdog(
        started=90.0,
        maximum_s=10.0,
        reader=reader,
        stall_timeout_s=3.0,
        clock=clock.monotonic,
    )
    clock.sleep(10.1)
    watchdog.check()
    clock.sleep(3.1)
    with pytest.raises(TimeoutError, match="telemetry stalled"):
        watchdog.check()


def test_progress_watchdog_has_a_hard_ceiling():
    from types import SimpleNamespace

    import pytest

    from scripts.collect_sim_teacher_episodes import _EpisodeProgressWatchdog

    now = [0.0]
    state = SimpleNamespace(count=0, vehicle_time_boot_ms=0)
    watchdog = _EpisodeProgressWatchdog(0.0, 100.0, SimpleNamespace(state=state), clock=lambda: now[0])
    for t in range(1, 700, 5):  # telemetry keeps advancing past the nominal budget
        now[0] = float(t)
        state.vehicle_time_boot_ms += 5000
        state.count += 10
        watchdog.check()
    now[0] = 701.0
    state.vehicle_time_boot_ms += 5000
    with pytest.raises(TimeoutError, match="hard wall-clock ceiling"):
        watchdog.check()

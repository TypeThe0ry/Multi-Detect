from __future__ import annotations

import pytest

from scripts import collect_sim_teacher_episodes as collector


class FakeClock:
    def __init__(self, on_sleep=None) -> None:
        self.now = 100.0
        self.on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class FakeState:
    def __init__(self, altitude_m: float) -> None:
        self.messages = {
            "HEARTBEAT": {"base_mode": 128, "custom_mode": "OFFBOARD"},
            "LOCAL_POSITION_NED": {"z": -altitude_m},
        }
        self.received_monotonic_ns = {"HEARTBEAT": 0, "EXTENDED_SYS_STATE": 0}

    def get(self, name: str):
        return self.messages.get(name)

    def mark_received(self, name: str, clock: FakeClock) -> None:
        self.received_monotonic_ns[name] = int(clock.monotonic() * 1_000_000_000)


class FakeReader:
    def __init__(self, state: FakeState) -> None:
        self.state = state

    def raise_if_failed(self) -> None:
        return None


class FakeConnection:
    def __init__(self, on_disarm=None) -> None:
        self.target_system = 1
        self.target_component = 1
        self.commands = []
        self.on_disarm = on_disarm
        self.mav = self

    def command_long_send(self, *args) -> None:
        self.commands.append(args)
        if args[2] == 400 and args[4] == 0.0 and self.on_disarm is not None:
            self.on_disarm()

    @staticmethod
    def mode_mapping() -> dict[str, tuple[int, int, int]]:
        return {"LAND": (29, 4, 6)}


class FakeStreamer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeExecutor:
    def __init__(self, on_land) -> None:
        self.on_land = on_land
        self.land_calls = 0

    def land(self) -> None:
        self.land_calls += 1
        self.on_land(self.land_calls)


def _record_heartbeat(state: FakeState, clock: FakeClock, *, mode: str | int, armed: bool) -> None:
    state.messages["HEARTBEAT"] = {
        "base_mode": 128 if armed else 0,
        "custom_mode": mode,
    }
    state.mark_received("HEARTBEAT", clock)


def test_land_acknowledged_promptly_stops_setpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    clock = FakeClock()
    state = FakeState(12.0)
    telemetry_ticks = [0]
    clock.on_sleep = lambda _seconds: (
        (
            _record_heartbeat(state, clock, mode=100925440, armed=True),
            state.mark_received("EXTENDED_SYS_STATE", clock),
            telemetry_ticks.__setitem__(0, telemetry_ticks[0] + 1),
        )
        if state.messages["HEARTBEAT"]["base_mode"] & 128
        else None
    )

    def land(_attempt: int) -> None:
        _record_heartbeat(state, clock, mode=100925440, armed=True)
        state.messages["COMMAND_ACK"] = {"command": 176, "result": 0}
        state.mark_received("COMMAND_ACK", clock)
        state.messages["EXTENDED_SYS_STATE"] = {"landed_state": 1}
        state.mark_received("EXTENDED_SYS_STATE", clock)

    executor = FakeExecutor(land)
    streamer = FakeStreamer()
    start_s = clock.monotonic()
    connection = FakeConnection(
        on_disarm=lambda: _record_heartbeat(state, clock, mode="LAND", armed=False)
    )
    result = collector._land_and_wait(
        executor,
        FakeReader(state),
        connection,
        streamer,
        stable_ground_s=0.5,
        auto_disarm_wait_s=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    assert streamer.stopped
    assert executor.land_calls == 1
    assert result["retries"] == 0
    assert result["land_mode_confirmed"]
    assert result["landed_state"] == 1
    assert result["land_ack_result"] == 0
    assert result["disarm_source"] == "collector"
    assert len(connection.commands) == 1
    assert connection.commands[0][2:5] == (400, 0, 0.0)
    assert clock.monotonic() - start_s <= 2.0
    assert telemetry_ticks[0] >= 2


def test_landed_aircraft_uses_px4_auto_disarm_without_collector_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    state = FakeState(0.0)
    clock = FakeClock()

    def auto_disarm(_seconds: float) -> None:
        if clock.now >= 102.0 and state.messages["HEARTBEAT"]["base_mode"] & 128:
            _record_heartbeat(state, clock, mode="LAND", armed=False)

    clock.on_sleep = auto_disarm

    def land(_attempt: int) -> None:
        _record_heartbeat(state, clock, mode="LAND", armed=True)
        state.messages["EXTENDED_SYS_STATE"] = {"landed_state": 1}
        state.mark_received("EXTENDED_SYS_STATE", clock)

    connection = FakeConnection()
    result = collector._land_and_wait(
        FakeExecutor(land),
        FakeReader(state),
        connection,
        None,
        stable_ground_s=0.5,
        auto_disarm_wait_s=5.0,
        disarm_confirm_timeout_s=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result["disarm_source"] == "px4_auto"
    assert result["armed"] is False
    assert connection.commands == []
    assert clock.monotonic() - 100.0 <= 5.5


def test_lost_land_request_is_resent_until_mode_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    clock = FakeClock()
    state = FakeState(8.0)

    def land(attempt: int) -> None:
        if attempt == 2:
            _record_heartbeat(state, clock, mode="LAND", armed=False)

    executor = FakeExecutor(land)
    result = collector._land_and_wait(
        executor,
        FakeReader(state),
        FakeConnection(),
        None,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    assert executor.land_calls == 2
    assert result["retries"] == 1
    assert result["land_mode_confirmed"]
    assert result["armed"] is False


def test_slow_descent_uses_altitude_derived_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    clock = FakeClock()
    state = FakeState(40.0)

    def land(_attempt: int) -> None:
        _record_heartbeat(state, clock, mode="LAND", armed=True)

    executor = FakeExecutor(land)
    connection = FakeConnection(
        on_disarm=lambda: _record_heartbeat(state, clock, mode="LAND", armed=False)
    )

    def descend(seconds: float) -> None:
        clock.sleep(seconds)
        position = state.messages["LOCAL_POSITION_NED"]
        position["z"] = min(0.0, position["z"] + 0.18 * seconds)
        if position["z"] == 0.0:
            state.messages["EXTENDED_SYS_STATE"] = {"landed_state": 1}
            state.mark_received("EXTENDED_SYS_STATE", clock)

    start_s = clock.monotonic()
    result = collector._land_and_wait(
        executor,
        FakeReader(state),
        connection,
        None,
        clock=clock.monotonic,
        sleep=descend,
    )

    assert result["timeout_s"] == collector._land_timeout_s(40.0)
    assert result["timeout_s"] > 120.0
    assert clock.monotonic() - start_s > 200.0
    assert result["landed_state"] == 1


def test_disarmed_heartbeat_confirms_landing_without_extended_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    clock = FakeClock()
    state = FakeState(3.0)

    def land(_attempt: int) -> None:
        _record_heartbeat(state, clock, mode="LAND", armed=False)

    executor = FakeExecutor(land)
    result = collector._land_and_wait(
        executor,
        FakeReader(state),
        FakeConnection(),
        None,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result["landed_state"] is None
    assert result["armed"] is False
    assert result["land_mode_confirmed"]


def test_preflight_on_ground_state_cannot_complete_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    clock = FakeClock()
    state = FakeState(4.0)
    state.messages["EXTENDED_SYS_STATE"] = {"landed_state": 1}
    executor = FakeExecutor(lambda _attempt: None)

    with pytest.raises(TimeoutError, match="timed out waiting for land"):
        collector._land_and_wait(
            executor,
            FakeReader(state),
            FakeConnection(),
            None,
            timeout_s=1.0,
            max_retries=0,
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

    assert executor.land_calls == 1


def test_land_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    events = []
    monkeypatch.setattr(
        collector,
        "_progress",
        lambda phase, **fields: events.append((phase, fields)),
    )
    clock = FakeClock()
    state = FakeState(10.0)
    executor = FakeExecutor(lambda _attempt: None)

    with pytest.raises(TimeoutError, match="retries=2"):
        collector._land_and_wait(
            executor,
            FakeReader(state),
            FakeConnection(),
            None,
            timeout_s=7.0,
            retry_interval_s=1.0,
            max_retries=2,
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

    assert executor.land_calls == 3
    assert events[-1][0] == "land_timeout"
    assert events[-1][1]["altitude_m"] == 10.0
    assert events[-1][1]["mode"] == "OFFBOARD"


def test_takeoff_budget_scales_with_altitude_like_landing():
    assert collector._takeoff_timeout_s(5.0) == collector.LAND_MIN_TIMEOUT_S
    assert collector._takeoff_timeout_s(22.0) == collector._land_timeout_s(22.0) > 60.0

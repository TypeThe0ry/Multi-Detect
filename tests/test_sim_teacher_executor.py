from __future__ import annotations

import pytest

from multidetect.sim_teacher import ActionPrimitive, CompletionCriterion, PymavlinkPrimitiveExecutor
from multidetect.simulator.isolation import SimulationIsolationError


class FakeMav:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def set_position_target_local_ned_send(self, *args):
        self.calls.append(args)
        self.state[:3] = list(args[5:8])
        self.state[3] = args[14] * 180.0 / 3.141592653589793


class FakeVehicle:
    target_system = 1
    target_component = 1

    def __init__(self):
        self.state = [0.0, 0.0, 0.0, 0.0]
        self.mav = FakeMav(self.state)
        self.modes = []

    def set_mode(self, mode):
        self.modes.append(mode)


def test_executor_streams_local_ned_and_completes_with_fake_vehicle():
    vehicle = FakeVehicle()
    executor = PymavlinkPrimitiveExecutor(
        vehicle,
        criterion=CompletionCriterion(timeout_s=1.0),
        position=lambda: tuple(vehicle.state[:3]),
        yaw_deg=lambda: vehicle.state[3],
        speed_mps=lambda: 0.0,
        clock=lambda: 0.0,
        sleep=lambda _: None,
    )
    result = executor.execute(ActionPrimitive.translate(2.0, 1.0, 0.5))
    assert result.status == "success"
    assert vehicle.mav.calls
    assert vehicle.state[:3] == pytest.approx([2.0, 1.0, -0.5])
    executor.rtl()
    assert vehicle.modes == ["RTL"]


def test_executor_achieved_delta_uses_measured_start_not_cumulative_setpoint():
    vehicle = FakeVehicle()
    vehicle.state[:3] = [0.0, 0.0, 0.8]
    sim = {"t": 0.0}
    executor = PymavlinkPrimitiveExecutor(
        vehicle,
        criterion=CompletionCriterion(timeout_s=0.01),
        position=lambda: (0.0, 0.0, 0.8),
        yaw_deg=lambda: 0.0,
        speed_mps=lambda: 0.0,
        clock=lambda: sim["t"],
        sleep=lambda seconds: sim.__setitem__("t", sim["t"] + seconds),
    )
    executor._commanded_position = (0.0, 0.0, -14.2)
    result = executor.execute(ActionPrimitive.translate(0.0, 0.0, 15.0))
    assert result.status == "timeout"
    assert result.achieved_delta_ned == pytest.approx((0.0, 0.0, 0.0))


def test_continue_waits_for_defined_sim_duration():
    sim = {"t": 0.0}

    def sleep(seconds: float) -> None:
        sim["t"] += seconds

    executor = PymavlinkPrimitiveExecutor(
        FakeVehicle(),
        position=lambda: (0.0, 0.0, -10.0),
        clock=lambda: sim["t"],
        sleep=sleep,
        stream_hz=10.0,
    )
    result = executor.execute(ActionPrimitive.continue_running())
    assert result.status == "success"
    assert result.elapsed_s == pytest.approx(0.5, abs=0.1)


def test_executor_rejects_remote_endpoint():
    with pytest.raises(SimulationIsolationError):
        PymavlinkPrimitiveExecutor(FakeVehicle(), endpoint="udp://192.168.1.2:14540")


def test_primitive_timeout_runs_on_the_given_clock_with_a_wall_bound() -> None:
    sim = {"t": 0.0}
    wall = {"t": 0.0}

    def sleep(seconds: float) -> None:
        wall["t"] += seconds
        sim["t"] += 0.2 * seconds  # the simulation runs at 0.2x real time

    executor = PymavlinkPrimitiveExecutor(
        FakeVehicle(),
        position=lambda: (0.0, 0.0, -10.0),  # never reaches the target
        clock=lambda: sim["t"],
        wall_clock=lambda: wall["t"],
        sleep=sleep,
    )
    result = executor.execute(ActionPrimitive.translate(15.0, 0.0, 0.0))
    # 20 s of simulation time took 100 s of wall time.
    assert result.status == "timeout" and result.elapsed_s >= 20.0 and wall["t"] >= 99.0

    sim["t"] = wall["t"] = 0.0
    bounded = PymavlinkPrimitiveExecutor(
        FakeVehicle(),
        position=lambda: (0.0, 0.0, -10.0),
        clock=lambda: sim["t"],
        wall_clock=lambda: wall["t"],
        sleep=lambda seconds: wall.__setitem__("t", wall["t"] + seconds),  # sim stalled
        max_wall_s=30.0,
    )
    result = bounded.execute(ActionPrimitive.translate(15.0, 0.0, 0.0))
    assert result.status == "timeout" and 30.0 <= wall["t"] < 31.0
    assert result.ack["wall_timeout"] is True


def test_linux_primitive_clock_stall_bound_is_shorter_without_changing_windows() -> None:
    from types import SimpleNamespace

    from scripts import collect_sim_teacher_episodes as collector

    assert collector.LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S == 45.0
    assert collector.PRIMITIVE_MAX_WALL_S == 180.0
    assert collector._primitive_max_wall_s("posix") == 45.0
    assert collector._primitive_max_wall_s("nt") == 180.0
    stalled = SimpleNamespace(ack={"wall_timeout": True})
    with pytest.raises(TimeoutError, match="PX4 simulation clock stalled during primitive"):
        collector._raise_if_px4_clock_stalled(stalled, 12, "posix")
    collector._raise_if_px4_clock_stalled(stalled, 12, "nt")


def test_linux_clock_stall_timeout_env_and_failure_diagnostics(monkeypatch) -> None:
    from types import SimpleNamespace

    from scripts import collect_sim_teacher_episodes as collector

    monkeypatch.setenv("LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S", "60")
    assert collector._primitive_max_wall_s("posix") == 60.0
    wall = {"t": 10.0}
    message = SimpleNamespace(time_boot_ms=1234)
    reader = SimpleNamespace(state={"LOCAL_POSITION_NED": message})
    clock = collector._SimClock(reader, wall=lambda: wall["t"])
    assert clock() == pytest.approx(10.0)
    wall["t"] = 70.0
    stalled = SimpleNamespace(ack={"wall_timeout": True})
    with pytest.raises(TimeoutError) as raised:
        collector._raise_if_px4_clock_stalled(stalled, 3, "posix", sim_clock=clock)
    assert raised.value.px4_clock_diagnostics == {
        "px4_clock_s": pytest.approx(1.234),
        "last_progress_at_monotonic_s": 10.0,
        "last_progress_px4_clock_s": pytest.approx(1.234),
        "clock_moved_again_after_45s": False,
    }


def test_linux_clock_stall_records_clock_recovery_after_default_deadline() -> None:
    from types import SimpleNamespace

    from scripts import collect_sim_teacher_episodes as collector

    wall = {"t": 0.0}
    message = SimpleNamespace(time_boot_ms=1000)
    reader = SimpleNamespace(state={"LOCAL_POSITION_NED": message})
    clock = collector._SimClock(reader, wall=lambda: wall["t"])
    clock()
    wall["t"] = 46.0
    message.time_boot_ms = 1001
    clock()
    assert clock.diagnostic_snapshot()["clock_moved_again_after_45s"] is True


def test_slow_simulation_progress_is_not_cut_short_by_total_wall_budget() -> None:
    sim = {"t": 0.0}
    wall = {"t": 0.0}

    def sleep(seconds: float) -> None:
        wall["t"] += seconds
        sim["t"] += 0.2 * seconds

    executor = PymavlinkPrimitiveExecutor(
        FakeVehicle(),
        position=lambda: (0.0, 0.0, -10.0),
        clock=lambda: sim["t"],
        wall_clock=lambda: wall["t"],
        sleep=sleep,
        max_wall_s=30.0,
    )
    result = executor.execute(ActionPrimitive.translate(15.0, 0.0, 0.0))

    # Twenty seconds of sim time take 100 seconds of wall time at 0.2x real
    # time, but the sim clock keeps progressing so the inactivity bound does
    # not abort the primitive.
    assert result.status == "timeout"
    assert result.elapsed_s >= 20.0
    assert wall["t"] >= 99.0
    assert "wall_timeout" not in result.ack


def test_a_hold_cut_short_by_the_wall_bound_is_a_timeout() -> None:
    wall = {"t": 0.0}
    executor = PymavlinkPrimitiveExecutor(
        FakeVehicle(),
        clock=lambda: 0.0,  # the simulation has stalled
        wall_clock=lambda: wall["t"],
        sleep=lambda seconds: wall.__setitem__("t", wall["t"] + seconds),
        max_wall_s=5.0,
    )
    result = executor.execute(ActionPrimitive.hold(2.0))
    assert result.status == "timeout" and result.ack.get("wall_timeout")

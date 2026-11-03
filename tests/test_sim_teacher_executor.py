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

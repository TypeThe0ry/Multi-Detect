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

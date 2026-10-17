from __future__ import annotations

from multidetect.sim_teacher.fw_executor import FixedWingVtolExecutor
from multidetect.autonomy.motion_intent import MotionCommand
from multidetect.sim_teacher.primitives import ActionPrimitive


class FakeVehicle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def arm(self) -> None:
        self.calls.append(("arm", (), {}))

    def fw_takeoff(self, *args, **kwargs):
        self.calls.append(("fw_takeoff", args, kwargs))
        return type("R", (), {"status": "sent", "ok": True})()

    def vtol_transition(self, *args, **kwargs):
        self.calls.append(("vtol_transition", args, kwargs))
        return type("R", (), {"status": "sent", "ok": True})()

    def reposition(self, *args, **kwargs):
        self.calls.append(("reposition", args, kwargs))
        return type("R", (), {"status": "sent", "ok": True})()

    def orbit(self, *args, **kwargs):
        self.calls.append(("orbit", args, kwargs))
        return type("R", (), {"status": "sent", "ok": True})()

    def fw_land(self, *args, **kwargs):
        self.calls.append(("fw_land", args, kwargs))
        return type("R", (), {"status": "sent", "ok": True})()


def test_fixed_wing_primitives_use_reposition_and_orbit() -> None:
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, position=lambda: (0.0, 0.0, -30.0))
    assert executor.execute(ActionPrimitive.translate(10, 2)).status == "success"
    assert fake.calls[-1][0] == "reposition"
    assert executor.execute(ActionPrimitive.turn(45)).status == "success"
    assert fake.calls[-1][0] == "reposition"
    assert executor.execute(ActionPrimitive.hold(1)).status == "success"
    assert fake.calls[-1][0] == "orbit"


def test_vtol_takeoff_and_landing_transition_sequence() -> None:
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, airframe="vtol")
    assert executor.arm_and_takeoff().status == "success"
    assert [name for name, _, _ in fake.calls[:3]] == ["arm", "fw_takeoff", "vtol_transition"]
    assert executor.land().status == "success"
    assert [name for name, _, _ in fake.calls[-2:]] == ["vtol_transition", "fw_land"]


def test_motion_command_loiter_and_transition_are_shared_with_runtime():
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, airframe="vtol")
    assert executor.execute_command(
        MotionCommand("loiter", target_ned=(1, 2, -30), radius_m=40, speed_mps=15, mode="FW")
    ).status == "success"
    assert fake.calls[-1][0] == "orbit"
    assert executor.execute_command(MotionCommand("transition", mode="MC")).status == "success"
    assert fake.calls[-1][0] == "vtol_transition"

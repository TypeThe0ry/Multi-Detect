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


def test_profile_alias_uses_vtol_sequence_and_nonzero_ack_timeout() -> None:
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, airframe="standard_vtol_mono_cam")
    assert executor.arm_and_takeoff().status == "success"
    assert [name for name, _, _ in fake.calls[:3]] == ["arm", "fw_takeoff", "vtol_transition"]
    assert fake.calls[1][2]["timeout_s"] > 0
    assert fake.calls[2][2]["timeout_s"] > 0


def test_state_wait_timeout_is_reported_with_phase() -> None:
    fake = FakeVehicle()
    waits: list[tuple[str, float]] = []

    def wait_for_state(phase: str, timeout_s: float) -> bool:
        waits.append((phase, timeout_s))
        return False

    executor = FixedWingVtolExecutor(fake, wait_for_state=wait_for_state)
    result = executor.arm_and_takeoff()
    assert result.status == "failed"
    assert result.ack["phase"] == "takeoff_state"
    assert result.ack["reason"] == "timeout"
    assert waits and waits[0][0] == "takeoff"


def test_command_rejection_does_not_advance_commanded_position() -> None:
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, position=lambda: (1.0, 2.0, -30.0))
    original = executor._commanded

    def reject(*_args, **_kwargs):
        fake.calls.append(("reposition", _args, _kwargs))
        return type("R", (), {"status": "failed", "ok": False, "detail": "no ack"})()

    fake.reposition = reject  # type: ignore[method-assign]
    result = executor.execute(ActionPrimitive.translate(10, 0))
    assert result.status == "failed"
    assert result.ack["phase"] == "reposition"
    assert executor._commanded == original


class RawMav:
    target_system = 1
    target_component = 1

    def __init__(self) -> None:
        self.commands: list[tuple] = []

    def command_long_send(self, *args) -> None:
        self.commands.append(args)


class RawVehicle:
    def __init__(self) -> None:
        self.mav = RawMav()

    def arm(self) -> None:
        return None


def test_raw_mavlink_fallback_emits_px4_command_long_sequence() -> None:
    vehicle = RawVehicle()
    executor = FixedWingVtolExecutor(vehicle, airframe="vtol")
    assert executor.arm_and_takeoff().status == "success"
    assert executor.execute(ActionPrimitive.hold(1)).status == "success"
    assert executor.land().status == "success"
    commands = vehicle.mav.commands
    # NAV_VTOL_TAKEOFF, DO_VTOL_TRANSITION(FW), DO_ORBIT,
    # DO_VTOL_TRANSITION(MC), DO_LAND_START, NAV_LAND.
    assert [row[2] for row in commands] == [84, 3000, 34, 3000, 189, 21]


def test_motion_command_loiter_and_transition_are_shared_with_runtime():
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, airframe="vtol")
    assert executor.execute_command(
        MotionCommand("loiter", target_ned=(1, 2, -30), radius_m=40, speed_mps=15, mode="FW")
    ).status == "success"
    assert fake.calls[-1][0] == "orbit"
    assert executor.execute_command(MotionCommand("transition", mode="MC")).status == "success"
    assert fake.calls[-1][0] == "vtol_transition"

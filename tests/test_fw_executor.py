from __future__ import annotations

import pytest

from multidetect.autonomy.motion_intent import MotionCommand
from multidetect.sim_teacher.fw_executor import FixedWingVtolExecutor
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts.collect_sim_teacher_episodes import (
    _apply_motion_wait_result,
    _fixed_wing_reposition_complete,
)
from multidetect.sim_teacher.executor import ExecutionResult


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
    home = ((47.0, 8.0, 500.0), (0.0, 0.0, 0.0))
    executor = FixedWingVtolExecutor(vehicle, airframe="vtol", geo_reference=lambda: home)
    assert executor.arm_and_takeoff().status == "success"
    assert executor.execute(ActionPrimitive.hold(1)).status == "success"
    assert executor.land().status == "success"
    commands = vehicle.mav.commands
    # NAV_VTOL_TAKEOFF, DO_VTOL_TRANSITION(FW), DO_ORBIT,
    # DO_VTOL_TRANSITION(MC), DO_LAND_START, NAV_LAND.
    assert [row[2] for row in commands] == [84, 3000, 34, 3000, 189, 21]
    # Positions go out as WGS84 around home with AMSL altitudes, never local metres.
    orbit = commands[2]
    assert abs(orbit[-3] - 47.0) < 1e-3 and abs(orbit[-2] - 8.0) < 1e-3
    assert orbit[-1] == pytest.approx(530.0)
    land = commands[-1]
    assert land[-1] == pytest.approx(500.0)


def test_raw_mavlink_fallback_refuses_guided_commands_without_a_geodetic_home() -> None:
    executor = FixedWingVtolExecutor(RawVehicle(), airframe="fixed_wing")
    result = executor.execute(ActionPrimitive.hold(1))
    assert result.status == "failed"
    assert "geodetic home" in str(result.ack)


def test_local_ned_to_global_offsets_from_home_and_reports_amsl() -> None:
    from multidetect.onboard_runtime.fw_vtol_commands import local_ned_to_global

    lat, lon, amsl = local_ned_to_global(
        (111.32, 0.0, -40.0), home_geodetic=(0.0, 0.0, 100.0), home_ned=(0.0, 0.0, -2.0)
    )
    assert lat == pytest.approx(0.001, abs=1e-6) and lon == pytest.approx(0.0)
    assert amsl == pytest.approx(138.0)


def test_motion_command_loiter_and_transition_are_shared_with_runtime():
    fake = FakeVehicle()
    executor = FixedWingVtolExecutor(fake, airframe="vtol")
    assert (
        executor.execute_command(
            MotionCommand("loiter", target_ned=(1, 2, -30), radius_m=40, speed_mps=15, mode="FW")
        ).status
        == "success"
    )
    assert fake.calls[-1][0] == "orbit"
    assert executor.execute_command(MotionCommand("transition", mode="MC")).status == "success"
    assert fake.calls[-1][0] == "vtol_transition"


def test_vtol_landing_over_command_long_uses_nav_land_not_the_mission_only_nav_vtol_land() -> None:
    # PX4 answers NAV_VTOL_LAND (85) as COMMAND_LONG with MAV_RESULT_UNSUPPORTED.
    vehicle = RawVehicle()
    home = ((47.0, 8.0, 500.0), (0.0, 0.0, 0.0))
    executor = FixedWingVtolExecutor(
        vehicle, airframe="vtol", landing_method="vtol", geo_reference=lambda: home
    )
    assert executor.arm_and_takeoff().status == "success"
    assert executor.land().status == "success"
    commands = [row[2] for row in vehicle.mav.commands]
    assert commands[-2:] == [3000, 21] and 85 not in commands


def test_motion_command_over_command_long_waits_for_the_ack() -> None:
    # A zero ACK timeout made every closed-loop reposition report failure although
    # PX4 accepted and flew it.
    class ReaderOwnedLink(RawVehicle):
        # Like the collector's _ReaderAckLink: the ACK is visible only to a wait > 0.
        def wait_command_ack(self, command, timeout_s):
            return {"command": command, "result": 0} if timeout_s > 0 else None

    vehicle = ReaderOwnedLink()
    executor = FixedWingVtolExecutor(
        vehicle, airframe="fixed_wing", geo_reference=lambda: ((47.0, 8.0, 500.0), (0.0, 0.0, 0.0))
    )
    result = executor.execute_command(
        MotionCommand("reposition", target_ned=(100.0, 0.0, -30.0), speed_mps=15.0)
    )
    assert result.status == "success"
    assert vehicle.mav.commands[-1][2] == 192


def test_fixed_wing_reposition_does_not_accept_cross_track_plane_crossing() -> None:
    # This is the logged Cessna failure shape: the target plane was crossed
    # along-track, but the aircraft was about 32 m off to the side.
    leg_n, leg_e = 28.5, -0.9
    dn, de = 1.2, 31.8
    distance = (dn**2 + de**2) ** 0.5
    assert not _fixed_wing_reposition_complete(
        dn=dn, de=de, leg_n=leg_n, leg_e=leg_e, distance_m=distance
    )


def test_fixed_wing_reposition_accepts_a_bounded_fly_through() -> None:
    leg_n, leg_e = 40.0, 0.0
    # Five metres beyond the target and three metres cross-track is a valid
    # fixed-wing pass even though it is not a point arrival.
    dn, de = -5.0, 3.0
    distance = (dn**2 + de**2) ** 0.5
    assert _fixed_wing_reposition_complete(
        dn=dn, de=de, leg_n=leg_n, leg_e=leg_e, distance_m=distance
    )


def test_fixed_wing_reposition_accepts_the_px4_loiter_capture_ring() -> None:
    # PX4 AUTO_LOITER keeps the Cessna about 80 m from the setpoint centre;
    # that ring is the fixed-wing equivalent of arrival.
    assert _fixed_wing_reposition_complete(
        dn=81.2, de=0.0, leg_n=25.0, leg_e=0.0, distance_m=81.2,
        loiter_radius_m=80.0,
        yaw_deg=90.0, centre_n=0.0, centre_e=0.0, angle_deg=90.0,
    )


def test_fixed_wing_motion_wait_timeout_is_not_recorded_as_success() -> None:
    result = ExecutionResult("success", 0.05, ack={})
    updated = _apply_motion_wait_result(
        result, {"waited": "timeout", "elapsed_s": 18.9, "distance_m": 81.4}
    )
    assert updated.status == "timeout"
    assert updated.elapsed_s == pytest.approx(18.95)

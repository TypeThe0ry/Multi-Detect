from __future__ import annotations

import threading

from multidetect.onboard_runtime.fw_vtol_commands import (
    MAV_CMD_DO_ORBIT,
    MAV_CMD_DO_REPOSITION,
    MAV_CMD_DO_VTOL_TRANSITION,
    custom_mode,
    do_orbit,
    do_reposition,
    do_vtol_transition,
    nav_loiter_time,
    parse_custom_mode,
)
from multidetect.onboard_runtime.vehicle import PymavlinkVehicle


def test_command_encoders_have_padded_wire_positions() -> None:
    reposition = do_reposition((31.2, 121.5), alt=120, ground_speed=18, radius=80, yaw_deg=90)
    assert (reposition.command, len(reposition.params)) == (MAV_CMD_DO_REPOSITION, 7)
    assert reposition.params[:3] == (18.0, 1.0, 80.0)
    assert reposition.params[3] == 1.5707963267948966
    assert reposition.params[4:] == (31.2, 121.5, 120.0)

    orbit = do_orbit((31.2, 121.5, 120), radius=80, speed=15, yaw_behavior=1)
    assert orbit.command == MAV_CMD_DO_ORBIT
    assert orbit.params == (80.0, 15.0, 1.0, 0.0, 31.2, 121.5, 120.0)
    assert nav_loiter_time((31.2, 121.5), seconds=10, radius=50, alt=100).params[2] == 50


def test_custom_modes_and_transition() -> None:
    assert custom_mode("AUTO_MISSION") == (4 << 16) | (4 << 24)
    assert parse_custom_mode(custom_mode("AUTO_LOITER")) == "AUTO_LOITER"
    assert parse_custom_mode(custom_mode("POSCTL")) == "POSCTL"
    assert do_vtol_transition("FW").command == MAV_CMD_DO_VTOL_TRANSITION


def test_vehicle_mode_and_vtol_state_decode() -> None:
    vehicle = PymavlinkVehicle("replay")
    vehicle.ingest({"type": "HEARTBEAT", "base_mode": 128,
                    "custom_mode": custom_mode("AUTO_MISSION")}, 1)
    vehicle.ingest({"type": "EXTENDED_SYS_STATE", "landed_state": 2,
                    "vtol_state": 4}, 2)
    state = vehicle.snapshot()
    assert state.mode == "AUTO_MISSION"
    assert state.vtol_state == 4
    assert state.landed_state == 2


def test_ack_timeout_and_acceptance_are_fail_closed() -> None:
    class Mav:
        def command_long_send(self, *args: object) -> None:
            self.last = args

    class Connection:
        target_system = 1
        target_component = 1

        def __init__(self) -> None:
            self.mav = Mav()

    connection = Connection()
    vehicle = PymavlinkVehicle("replay", connection=connection)
    timed_out = vehicle.reposition((31, 121), 100, timeout_s=0.001)
    assert timed_out.status == "timeout"

    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(
        vehicle.vtol_transition("MC", timeout_s=0.5)))
    worker.start()
    vehicle.ingest({"type": "COMMAND_ACK", "command": MAV_CMD_DO_VTOL_TRANSITION,
                    "result": 0}, 3)
    worker.join(1)
    assert result and result[0].status == "accepted"


def test_takeoff_preflight_branches() -> None:
    vehicle = PymavlinkVehicle("replay")
    hand = vehicle.check_takeoff_preflight(
        "hand_launch", {"FW_LAUN_DETCN_ON": 1, "FW_LAUN_AC_THLD": 30, "FW_LAUN_AC_T": .05})
    assert hand.ok
    assert not vehicle.check_takeoff_preflight("runway", {"RWTO_TKOFF": 0}).ok
    vtol = vehicle.check_takeoff_preflight(
        "vtol", {"VT_TYPE": 2, "VT_ARSP_TRANS": 15,
                  "VT_FW_MIN_ALT": 30, "VT_TRANS_TIMEOUT": 15})
    assert vtol.ok
    assert "VT_ARSP_TRANS" in vehicle.check_takeoff_preflight(
        "vtol", {"VT_TYPE": 2, "VT_FW_MIN_ALT": 30, "VT_TRANS_TIMEOUT": 15}).missing

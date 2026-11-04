from __future__ import annotations

import json
import math
import queue
import struct
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from multidetect.onboard_runtime.vehicle import (
    PositionSetpoint,
    PymavlinkVehicle,
    VehicleInterface,
)
from multidetect.sim_teacher.executor import SetpointStreamer


class FakeMav:
    def __init__(self) -> None:
        self.setpoints: list[tuple] = []
        self.commands: list[tuple] = []
        self.heartbeats: list[tuple] = []
        self._lock = threading.Lock()

    def set_position_target_local_ned_send(self, *args: object) -> None:
        assert self._lock.acquire(blocking=False), "concurrent MAVLink sender access"
        try:
            time.sleep(0.001)
            self.setpoints.append(args)
        finally:
            self._lock.release()

    def command_long_send(self, *args: object) -> None:
        assert self._lock.acquire(blocking=False), "concurrent MAVLink sender access"
        try:
            time.sleep(0.001)
            self.commands.append(args)
        finally:
            self._lock.release()

    def heartbeat_send(self, *args: object) -> None:
        self.heartbeats.append(args)

    def param_request_read_send(self, *args: object) -> None:
        self.commands.append(("read", *args))

    def param_set_send(self, *args: object) -> None:
        self.commands.append(("set", *args))


class FakeConnection:
    target_system = 1
    target_component = 1

    def __init__(self) -> None:
        self.mav = FakeMav()
        self.messages: queue.Queue[dict] = queue.Queue()
        self.closed = False

    def recv_match(self, *, blocking: bool, timeout: float) -> dict | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True


def test_telemetry_snapshot_units_freshness_and_immutability() -> None:
    vehicle = PymavlinkVehicle("replay")
    vehicle.ingest({"mavpackettype": "LOCAL_POSITION_NED", "x": 1, "y": 2, "z": -3,
                    "vx": 4, "vy": 5, "vz": -6}, 10)
    vehicle.ingest({"type": "ATTITUDE", "roll": 0, "pitch": math.pi / 2,
                    "yaw": -math.pi}, 10.1)
    vehicle.ingest({"message_type": "HEARTBEAT", "base_mode": 128,
                    "custom_mode": 6 << 16}, 10.2)
    vehicle.ingest({"type": "HOME_POSITION", "x": 7, "y": 8, "z": -9,
                    "latitude": 310000000, "longitude": 1210000000,
                    "altitude": 12345}, 10.3)
    vehicle.ingest({"type": "EXTENDED_SYS_STATE", "landed_state": 2}, 10.4)
    vehicle.ingest({"type": "BATTERY_STATUS", "battery_remaining": 78}, 10.5)
    state = vehicle.snapshot()
    assert isinstance(vehicle, VehicleInterface)
    assert state.position_ned == (1, 2, -3)
    assert state.velocity_ned == (4, 5, -6)
    assert state.attitude_deg == pytest.approx((0, 90, -180))
    assert state.home_ned == (7, 8, -9)
    assert state.home_geodetic == pytest.approx((31, 121, 12.345))
    assert state.landed is False
    assert state.armed is True
    assert state.mode == "OFFBOARD"
    assert state.battery_remaining == 78
    assert state.pose_monotonic == 10
    assert state.attitude_monotonic == 10.1
    assert state.heartbeat_monotonic == 10.2
    assert state.battery_monotonic == 10.5
    with pytest.raises(FrozenInstanceError):
        state.mode = "LAND"


def test_missing_invalid_and_old_telemetry_never_refreshes_pose() -> None:
    vehicle = PymavlinkVehicle("replay")
    assert vehicle.snapshot().position_ned is None
    vehicle.ingest({"type": "LOCAL_POSITION_NED", "x": 1, "y": 2, "z": -3}, 10)
    vehicle.ingest({"type": "LOCAL_POSITION_NED", "x": math.nan, "y": 2, "z": -3}, 11)
    vehicle.ingest({"type": "LOCAL_POSITION_NED", "x": 5, "y": 2, "z": -3}, 9)
    state = vehicle.snapshot()
    assert state.position_ned == (1, 2, -3)
    assert state.pose_monotonic == 10
    assert state.velocity_ned is None


def test_unknown_battery_and_landed_state_remain_unknown() -> None:
    vehicle = PymavlinkVehicle("replay")
    vehicle.ingest({"type": "SYS_STATUS", "battery_remaining": -1}, 10)
    vehicle.ingest({"type": "EXTENDED_SYS_STATE", "landed_state": 0}, 10)
    assert vehicle.snapshot().battery_remaining is None
    assert vehicle.snapshot().landed is None


def test_adapter_reuses_collector_wire_encoding_and_serializes_sends() -> None:
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("/dev/ttyTHS1", connection=connection, clock=lambda: 12.5)
    setpoint = PositionSetpoint(1, 2, -3, 90)
    threads = [threading.Thread(target=vehicle.send_setpoint, args=(setpoint,))
               for _ in range(8)]
    threads += [threading.Thread(target=vehicle.arm) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(connection.mav.setpoints) == 8
    assert len(connection.mav.commands) == 4
    args = connection.mav.setpoints[0]
    assert args[:8] == (12500, 1, 1, SetpointStreamer.FRAME_LOCAL_NED,
                        SetpointStreamer.TYPE_MASK, 1, 2, -3)
    assert args[14] == pytest.approx(math.pi / 2)
    assert len(connection.mav.heartbeats) == 1


def test_commands_encode_px4_modes_and_actions() -> None:
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("udpin:0.0.0.0:14540", connection=connection)
    vehicle.arm()
    vehicle.set_mode("OFFBOARD")
    vehicle.land()
    vehicle.rtl()
    assert connection.mav.commands[0] == (1, 1, 400, 0, 1, 0, 0, 0, 0, 0, 0)
    assert connection.mav.commands[1] == (1, 1, 176, 0, 1, 6, 0, 0, 0, 0, 0)
    assert [args[2] for args in connection.mav.commands[2:]] == [21, 20]
    with pytest.raises(ValueError, match="unsupported"):
        vehicle.set_mode("ACRO")


def test_heartbeat_selects_autopilot_and_rejects_other_peers() -> None:
    vehicle = PymavlinkVehicle("replay")
    vehicle.ingest({"type": "LOCAL_POSITION_NED", "_source_system": 43,
                    "_source_component": 1, "x": 9, "y": 9, "z": -9}, 0)
    assert vehicle.snapshot().position_ned is None
    vehicle.ingest({"type": "HEARTBEAT", "autopilot": 8, "_source_system": 255}, 1)
    assert vehicle.target_system is None
    vehicle.ingest({"type": "HEARTBEAT", "autopilot": 12,
                    "_source_system": 42, "_source_component": 1,
                    "base_mode": 128, "custom_mode": 6 << 16}, 2)
    vehicle.ingest({"type": "LOCAL_POSITION_NED", "_source_system": 43,
                    "_source_component": 1, "x": 9, "y": 9, "z": -9}, 3)
    vehicle.ingest({"type": "HEARTBEAT", "_source_system": 42,
                    "_source_component": 191, "base_mode": 0}, 3)
    assert vehicle.target_system == 42
    assert vehicle.snapshot().armed is True
    assert vehicle.snapshot().position_ned is None


def test_receiver_lifecycle_and_requests_without_hardware() -> None:
    connection = FakeConnection()
    connection.port = SimpleNamespace(write_timeout=None)
    connection.messages.put({"type": "HEARTBEAT", "autopilot": 12,
                             "base_mode": 0, "custom_mode": 4 << 16 | 3 << 24})
    connection.messages.put({"type": "LOCAL_POSITION_NED", "x": 1, "y": 2, "z": -3})
    vehicle = PymavlinkVehicle("COM4", connection=connection)
    assert vehicle.start() is vehicle
    assert connection.port.write_timeout == 0.05
    assert vehicle.start() is vehicle
    deadline = time.monotonic() + 1
    while vehicle.snapshot().position_ned is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert vehicle.snapshot().mode == "HOLD"
    assert vehicle.snapshot().position_ned == (1, 2, -3)
    assert all(args[2] == 511 for args in connection.mav.commands[:-1])
    assert any(args[4:6] == (285.0, 200_000.0) for args in connection.mav.commands)
    assert connection.mav.commands[-1][2:5] == (512, 0, 148.0)
    assert len(connection.mav.commands) == 10
    vehicle.close()
    vehicle.close()
    assert connection.closed
    with pytest.raises(RuntimeError, match="closed"):
        vehicle.arm()
    with pytest.raises(RuntimeError, match="closed"):
        vehicle.start()


def test_udp_alias_and_serial_baud_passed_to_lazy_pymavlink(monkeypatch) -> None:
    import sys

    opened: list[tuple] = []

    def connect(endpoint: str, **kwargs: object) -> FakeConnection:
        opened.append((endpoint, kwargs))
        return FakeConnection()

    monkeypatch.setitem(sys.modules, "pymavlink",
                        SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=connect)))
    with PymavlinkVehicle("udp://127.0.0.1:14540", baud=57600):
        pass
    with PymavlinkVehicle("/dev/ttyUSB0", baud=921600):
        pass
    assert opened == [("udpin:127.0.0.1:14540", {
        "baud": 57600, "source_system": 245, "source_component": 191}),
        ("/dev/ttyUSB0", {"baud": 921600, "source_system": 245, "source_component": 191})]


def test_unknown_peer_blocks_commands_and_nonfinite_setpoint() -> None:
    connection = FakeConnection()
    connection.target_system = connection.target_component = 0
    vehicle = PymavlinkVehicle("COM4", connection=connection)
    with pytest.raises(RuntimeError, match="heartbeat"):
        vehicle.arm()
    with pytest.raises(ValueError, match="finite"):
        PositionSetpoint(1, 2, math.inf, 4)


def test_sender_lock_deadline_does_not_block_the_caller_forever() -> None:
    vehicle = PymavlinkVehicle("COM4", connection=FakeConnection(), write_timeout_s=0.01)
    assert vehicle._send_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="write deadline"):
            vehicle.arm()
        assert time.monotonic() - started < 0.5
    finally:
        vehicle._send_lock.release()


def test_receiver_error_is_reported_to_runtime() -> None:
    connection = FakeConnection()

    def fail(**_: object) -> None:
        raise OSError("serial disconnected")

    connection.recv_match = fail
    vehicle = PymavlinkVehicle("COM4", connection=connection).start()
    try:
        assert vehicle._reader.done.wait(1)
        with pytest.raises(RuntimeError, match="serial disconnected"):
            vehicle.snapshot()
    finally:
        vehicle.close()


def test_gps_raw_int_eph_decodes_hdop_and_65535_is_unknown():
    vehicle = PymavlinkVehicle("replay")
    vehicle.ingest({"type": "GPS_RAW_INT", "fix_type": 3,
                    "satellites_visible": 8, "eph": 150}, 1)
    assert vehicle.snapshot().gps_hdop == pytest.approx(1.5)
    vehicle.ingest({"type": "GPS_RAW_INT", "fix_type": 3,
                    "satellites_visible": 8, "eph": 65535}, 2)
    assert vehicle.snapshot().gps_hdop is None


def test_param_int32_bit_cast_and_stale_values_are_ignored():
    vehicle = PymavlinkVehicle("replay", connection=FakeConnection())
    vehicle._parameter_pending_since["NAV_DLL_ACT"] = 10.0
    wire = struct.unpack("<f", struct.pack("<i", 2))[0]
    vehicle.ingest({"type": "PARAM_VALUE", "param_id": "NAV_DLL_ACT",
                    "param_value": wire, "param_type": 6}, 9.0)
    assert "NAV_DLL_ACT" not in vehicle._parameters
    vehicle.ingest({"type": "PARAM_VALUE", "param_id": "NAV_DLL_ACT",
                    "param_value": wire, "param_type": 6}, 11.0)
    assert vehicle._parameters["NAV_DLL_ACT"][0] == 2


def test_read_parameter_accepts_synchronous_zero_latency_reply() -> None:
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("replay", connection=connection, clock=lambda: 10.0)
    vehicle.target_system = vehicle.target_component = 1

    def reply_inside_send(*args: object) -> None:
        connection.mav.commands.append(("read", *args))
        vehicle.ingest({"type": "PARAM_VALUE", "param_id": "COM_OF_LOSS_T",
                        "param_value": 1.0, "param_type": 9})

    connection.mav.param_request_read_send = reply_inside_send
    assert vehicle.read_parameter("COM_OF_LOSS_T", timeout_s=0.05, retries=1) == 1.0


def test_param_value_before_heartbeat_is_not_discarded() -> None:
    vehicle = PymavlinkVehicle("replay", connection=FakeConnection(), clock=lambda: 2.0)
    vehicle.ingest({"type": "PARAM_VALUE", "param_id": "NAV_DLL_ACT",
                    "param_value": struct.unpack("<f", struct.pack("<i", 2))[0],
                    "param_type": 6, "_source_system": 1, "_source_component": 1}, 2.0)
    assert vehicle._parameters["NAV_DLL_ACT"][0] == 2


@pytest.mark.parametrize("encoding,raw,expected", [
    ("bytewise", struct.unpack("<f", struct.pack("<i", 2))[0], 2.0),
    ("c_cast", 2.0, 2.0),
])
def test_param_int32_encoding_decodes_both_protocol_forms(encoding, raw, expected) -> None:
    vehicle = PymavlinkVehicle("replay", connection=FakeConnection(), clock=lambda: 3.0)
    vehicle._parameter_encoding = encoding
    vehicle.ingest({"type": "PARAM_VALUE", "param_id": "NAV_DLL_ACT",
                    "param_value": raw, "param_type": 6}, 3.0)
    assert vehicle._parameters["NAV_DLL_ACT"][0] == expected


@pytest.mark.parametrize("encoding,wire,readback", [
    ("bytewise", struct.unpack("<f", struct.pack("<i", 2))[0],
     struct.unpack("<f", struct.pack("<i", 2))[0]),
    ("c_cast", 2.0, 2.0),
])
def test_write_parameter_int32_encodings(encoding, wire, readback) -> None:
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("replay", connection=connection, clock=lambda: 30.0)
    vehicle.target_system = vehicle.target_component = 1
    vehicle._parameter_encoding = encoding
    vehicle._parameters["NAV_DLL_ACT"] = (0.0, 6, 1.0)

    def reply_inside_send(system, component, param_id, sent_value, ptype):
        connection.mav.commands.append(("set", system, component, param_id, sent_value, ptype))
        vehicle.ingest({"type": "PARAM_VALUE", "param_id": "NAV_DLL_ACT",
                        "param_value": readback, "param_type": 6,
                        "_source_system": system, "_source_component": component})

    connection.mav.param_set_send = reply_inside_send
    assert vehicle.write_parameter("NAV_DLL_ACT", 2, timeout_s=0.05, retries=1) == 2.0
    assert connection.mav.commands[-1][4] == wire


def test_write_parameter_float_matches_float32_wire_roundtrip() -> None:
    connection = FakeConnection()
    ticks = iter((31.0, 31.1))
    vehicle = PymavlinkVehicle("replay", connection=connection, clock=lambda: next(ticks))
    vehicle.target_system = vehicle.target_component = 1
    expected_wire = struct.unpack("<f", struct.pack("<f", 123.456789))[0]

    def reply_inside_send(system, component, param_id, sent_value, ptype):
        connection.mav.commands.append(("set", system, component, param_id, sent_value, ptype))
        vehicle.ingest({"type": "PARAM_VALUE", "param_id": "GF_MAX_HOR_DIST",
                        "param_value": expected_wire, "param_type": 9,
                        "_source_system": system, "_source_component": component})

    connection.mav.param_set_send = reply_inside_send
    assert vehicle.write_parameter("GF_MAX_HOR_DIST", 123.456789,
                                  timeout_s=0.05, retries=1) == expected_wire
    assert connection.mav.commands[-1][4] == 123.456789


@pytest.mark.parametrize("capabilities,encoding", [(16, "bytewise"), (131072, "c_cast")])
def test_autopilot_version_selects_parameter_encoding(capabilities, encoding) -> None:
    vehicle = PymavlinkVehicle("replay", connection=FakeConnection())
    vehicle.ingest({"type": "AUTOPILOT_VERSION", "capabilities": capabilities})
    assert vehicle._parameter_encoding == encoding


def test_param_trace_records_send_and_acceptance(tmp_path, monkeypatch) -> None:
    trace_path = tmp_path / "params.jsonl"
    monkeypatch.setenv("MULTIDETECT_PARAM_TRACE", str(trace_path))
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("replay", connection=connection,
                               clock=iter((40.0, 40.1, 40.2, 40.3)).__next__)
    vehicle.target_system = vehicle.target_component = 1

    def reply_inside_send(*args):
        connection.mav.commands.append(("read", *args))
        vehicle.ingest({"type": "PARAM_VALUE", "param_id": "COM_OF_LOSS_T",
                        "param_value": 1.0, "param_type": 9,
                        "_source_system": 1, "_source_component": 1})

    connection.mav.param_request_read_send = reply_inside_send
    assert vehicle.read_parameter("COM_OF_LOSS_T", timeout_s=0.05, retries=1) == 1.0
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [row["kind"] for row in records] == ["PARAM_REQUEST_READ", "PARAM_VALUE"]
    assert records[0]["target_system"] == records[0]["target_component"] == 1
    assert records[1]["decision"] == "accepted"


def test_write_parameter_accepts_synchronous_zero_latency_readback() -> None:
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("replay", connection=connection, clock=lambda: 20.0)
    vehicle.target_system = vehicle.target_component = 1
    vehicle._parameters["GF_MAX_HOR_DIST"] = (10.0, 9, 1.0)

    def reply_inside_send(*args: object) -> None:
        connection.mav.commands.append(("set", *args))
        vehicle.ingest({"type": "PARAM_VALUE", "param_id": "GF_MAX_HOR_DIST",
                        "param_value": 12.5, "param_type": 9})

    connection.mav.param_set_send = reply_inside_send
    assert vehicle.write_parameter("GF_MAX_HOR_DIST", 12.5,
                                  timeout_s=0.05, retries=1) == 12.5


def test_param_set_uses_learned_type_and_wire_bit_cast():
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("replay", connection=connection)
    vehicle.target_system = vehicle.target_component = 1
    vehicle._parameters["NAV_DLL_ACT"] = (0.0, 6, 1.0)
    vehicle._parameters["GF_MAX_HOR_DIST"] = (10.0, 9, 1.0)
    vehicle.read_parameter = lambda name, **_: 2.0 if name == "NAV_DLL_ACT" else 12.5
    vehicle.write_parameter("NAV_DLL_ACT", 2, retries=1)
    vehicle.write_parameter("GF_MAX_HOR_DIST", 12.5, retries=1)
    sets = [row for row in connection.mav.commands if row[0] == "set"]
    assert sets[0][4] == pytest.approx(struct.unpack("<f", struct.pack("<i", 2))[0])
    assert sets[0][5] == 6
    assert sets[1][4] == pytest.approx(12.5)
    assert sets[1][5] == 9


def test_home_position_request_uses_request_message_command():
    connection = FakeConnection()
    vehicle = PymavlinkVehicle("replay", connection=connection)
    vehicle.target_system = vehicle.target_component = 1
    vehicle.request_home_position()
    assert connection.mav.commands[-1] == (1, 1, 512, 0, 242.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

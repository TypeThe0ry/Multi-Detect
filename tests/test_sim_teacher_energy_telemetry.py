from __future__ import annotations

from multidetect.sim_teacher.streams import (
    MavlinkReader,
    MavlinkState,
    SensorSample,
    build_observation,
)


def _sensor(stamp: int = 10_000_000_000) -> SensorSample:
    return SensorSample(
        rgb=None,
        width=1,
        height=1,
        depth=None,
        camera_info={},
        pose={},
        velocity=[0, 0, 0],
        sensor_stamp_ns=1_000_000_000,
        received_monotonic_ns=stamp,
        received_wall_time=1.0,
    )


def _state() -> MavlinkState:
    return MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"time_boot_ms": 1000, "x": 1, "y": 2, "z": -20},
            "ATTITUDE": {},
            "GLOBAL_POSITION_INT": {},
            "GPS_RAW_INT": {"fix_type": 3},
            "BATTERY_STATUS": {"current_battery": 1000, "battery_remaining": 80},
            "SYS_STATUS": {"battery_remaining": 81},
            "WIND_COV": {"wind_x": 2, "wind_y": 1},
            "HEARTBEAT": {"base_mode": 128},
        },
        received_monotonic_ns={
            "BATTERY_STATUS": 1_000_000_000,
            "SYS_STATUS": 2_000_000_000,
            "WIND_COV": 3_000_000_000,
            "HEARTBEAT": 4_000_000_000,
            "LOCAL_POSITION_NED": 5_000_000_000,
        },
    )


def test_observations_keep_wire_receive_times_and_skip_unstamped_samples() -> None:
    state = _state()
    first = build_observation(_sensor(), state)["energy_telemetry"]
    second = build_observation(_sensor(20_000_000_000), state)["energy_telemetry"]
    assert first == second
    assert [row["received_monotonic_s"] for row in first] == [1, 2, 3, 4, 5]
    assert first[0]["message"]["mavpackettype"] == "BATTERY_STATUS"
    assert "mavpackettype" not in state.messages["BATTERY_STATUS"]
    del state.received_monotonic_ns["BATTERY_STATUS"]
    rows = build_observation(_sensor(), state)["energy_telemetry"]
    assert all(row["message"]["mavpackettype"] != "BATTERY_STATUS" for row in rows)


def test_pose_alignment_preserves_latest_wire_state_for_energy() -> None:
    reader = MavlinkReader(object())
    reader.state = _state()
    reader.state.messages["LOCAL_POSITION_NED"] = {
        "time_boot_ms": 2000,
        "x": 30,
        "y": 0,
        "z": -20,
        "vx": 6,
    }
    reader._record_pose_message(
        "LOCAL_POSITION_NED",
        {"time_boot_ms": 1000, "x": 1, "y": 0, "z": -20, "vx": 0},
        900_000_000,
        1.0,
    )
    state = reader.aligned_state(1_000_000_000, timeout_s=0)
    observation = build_observation(_sensor(), state)
    assert observation["pose"]["local_ned_m"][0] == 1
    local = next(
        row
        for row in observation["energy_telemetry"]
        if row["message"]["mavpackettype"] == "LOCAL_POSITION_NED"
    )
    assert local["message"]["x"] == 30
    assert local["message"]["vx"] == 6
    assert local["received_monotonic_s"] == 5


def test_reader_captures_battery_and_wind_samples_with_receive_clocks() -> None:
    class Connection:
        def __init__(self) -> None:
            self.messages = iter(
                [
                    {"type": "BATTERY_STATUS", "current_battery": 1000},
                    {"type": "WIND_COV", "wind_x": 2, "wind_y": 1},
                ]
            )

        def recv_msg(self):
            message = next(self.messages, None)
            if message is None:
                reader._stop.set()
            return message

    clocks = iter([1_000_000_000, 2_000_000_000])
    reader = MavlinkReader(Connection(), clock=lambda: next(clocks))
    reader._run()
    reader.raise_if_failed()
    assert reader.state.get("BATTERY_STATUS")["current_battery"] == 1000
    assert reader.state.get("WIND_COV")["wind_x"] == 2
    assert reader.state.received_monotonic_ns == {
        "BATTERY_STATUS": 1_000_000_000,
        "WIND_COV": 2_000_000_000,
    }


def test_energy_snapshot_preserves_pymavlink_source_identity() -> None:
    class Message:
        def to_dict(self):
            return {"battery_remaining": 1}

        def get_srcSystem(self):
            return 2

        def get_srcComponent(self):
            return 7

    state = _state()
    state.messages["BATTERY_STATUS"] = Message()
    rows = build_observation(_sensor(), state)["energy_telemetry"]
    assert rows[0]["message"]["source_system"] == 2
    assert rows[0]["message"]["source_component"] == 7


def test_reader_stamps_energy_samples_with_the_vehicle_boot_clock() -> None:
    """SITL's lockstep clock, not host time, is the vehicle's own receipt clock."""
    host = iter(range(1_000_000_000, 100_000_000_000, 1_000_000_000))
    wire = [
        {"type": "BATTERY_STATUS", "battery_remaining": 90},  # before any boot time
        {"type": "ATTITUDE", "time_boot_ms": 200_000},
        {"type": "BATTERY_STATUS", "battery_remaining": 89},
        {"type": "LOCAL_POSITION_NED", "time_boot_ms": 200_250, "x": 0, "y": 0, "z": -5},
        {"type": "SYS_STATUS", "battery_remaining": 89},
    ]

    class Connection:
        def recv_msg(self):
            if wire:
                return wire.pop(0)
            reader._stop.set()
            return None

    reader = MavlinkReader(Connection(), clock=lambda: next(host))
    reader._run()
    assert reader.error is None
    assert reader.state.vehicle_time_boot_ms == 200_250
    rows = {
        row["message"]["mavpackettype"]: row
        for row in reader.aligned_state(200_250_000_000, timeout_s=0).energy_telemetry
    }
    # Host receipts are 1 s apart; on the vehicle clock only 0.25 s passed.
    assert rows["BATTERY_STATUS"]["received_monotonic_s"] == 3.0
    assert rows["BATTERY_STATUS"]["received_vehicle_s"] == 200.0
    assert rows["SYS_STATUS"]["received_vehicle_s"] == 200.25
    assert rows["LOCAL_POSITION_NED"]["received_vehicle_s"] == 200.25


def test_reader_reports_vehicle_clock_spans_with_no_telemetry() -> None:
    """Run9: a host stall overflowed the UDP buffer; PX4 time jumped 354.016 -> 361.096 s."""
    host = iter(range(1_000_000_000, 100_000_000_000, 1_000_000_000))
    wire = [
        {"type": "GLOBAL_POSITION_INT", "time_boot_ms": 353_996},
        {"type": "SYS_STATUS", "battery_remaining": 95},
        {"type": "GLOBAL_POSITION_INT", "time_boot_ms": 354_016},
        {"type": "GLOBAL_POSITION_INT", "time_boot_ms": 361_096},
        {"type": "ATTITUDE", "time_boot_ms": 1_000},  # a stray older clock
        {"type": "LOCAL_POSITION_NED", "time_boot_ms": 361_120, "x": 0, "y": 0, "z": -5},
    ]

    class Connection:
        def recv_msg(self):
            if wire:
                return wire.pop(0)
            reader._stop.set()
            return None

    reader = MavlinkReader(Connection(), clock=lambda: next(host))
    reader._run()
    assert reader.error is None
    assert reader.state.vehicle_gaps_ms == [(354_016, 361_096)]
    snapshot = _state()
    snapshot.vehicle_gaps_ms = reader.aligned_state(361_120_000_000, timeout_s=0).vehicle_gaps_ms
    observation = build_observation(_sensor(), snapshot)
    assert observation["vehicle_telemetry_gaps_s"] == [[354.016, 361.096]]

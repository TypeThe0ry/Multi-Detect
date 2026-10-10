from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from multidetect.sim_teacher.streams import MavlinkState, SensorSample, build_observation


def _collector_module():
    """Import the CLI module without starting Docker or a collection run."""
    from scripts import collect_sim_teacher_episodes

    return collect_sim_teacher_episodes


def test_build_observation_uses_live_mavlink_and_converts_rgb() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20, 30, 40, 50]),
        width=2,
        height=1,
        depth=np.ones((1, 2), dtype=np.float32),
        camera_info={"width": 2, "height": 1, "fx": 1.0, "fy": 1.0},
        pose={"lat_deg": 1.0, "lon_deg": 2.0, "alt_m": 3.0},
        velocity=[0.0, 0.0, 0.0],
        sensor_stamp_ns=10,
        received_monotonic_ns=100,
        received_wall_time=1.0,
    )
    state = MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"x": 1, "y": 2, "z": -3, "vx": 0, "vy": 0, "vz": 0},
            "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 1.0},
            "GLOBAL_POSITION_INT": {"lat": 10_000_000, "lon": 20_000_000, "relative_alt": 4000},
            "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        },
        received_monotonic_ns={"LOCAL_POSITION_NED": 90},
    )
    observation = build_observation(sensor, state)
    assert observation["sensor_status"] == "live"
    assert observation["pose"]["local_ned_m"] == [1.0, 2.0, -3.0]
    assert observation["rgb"].shape == (1, 2, 3)
    assert observation["frame_pose_skew_ns"] == 10


def test_setpoint_streamer_rate_target_and_encoding() -> None:
    import time

    from multidetect.sim_teacher.executor import SetpointStreamer

    class FakeMav:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def set_position_target_local_ned_send(self, *args: object) -> None:
            self.calls.append(args)

    mav = FakeMav()
    vehicle = type("Vehicle", (), {"mav": mav, "target_system": 1, "target_component": 1})()
    streamer = SetpointStreamer(vehicle, rate_hz=20.0)
    streamer.set_target(1.0, 2.0, -5.0, 30.0)
    started = time.monotonic()
    streamer.start()
    time.sleep(0.25)
    streamer.set_target(3.0, 4.0, -6.0, 45.0)
    time.sleep(0.25)
    streamer.stop()
    assert len(mav.calls) >= 8
    assert len(mav.calls) / (time.monotonic() - started) >= 15.0
    assert all(call[3] == 1 for call in mav.calls)
    assert all(call[4] == SetpointStreamer.TYPE_MASK for call in mav.calls)
    assert any(call[5:9] == (3.0, 4.0, -6.0, 0.0) for call in mav.calls)


def test_arm_uses_normal_command_encoding_and_same_connection() -> None:
    collector = _collector_module()

    class FakeMav:
        def __init__(self) -> None:
            self.heartbeats: list[tuple[object, ...]] = []
            self.commands: list[tuple[object, ...]] = []
            self.srcSystem = 255
            self.srcComponent = 190

        def heartbeat_send(self, *args: object) -> None:
            self.heartbeats.append(args)

        def command_long_send(self, *args: object) -> None:
            self.commands.append(args)

    class FakeConnection:
        target_system = 1
        target_component = 1

        def __init__(self) -> None:
            self.mav = FakeMav()

    class FakeReader:
        def __init__(self) -> None:
            self.heartbeat_reads = 0
            self.state = self

        def get(self, name: str):
            if name == "HEARTBEAT":
                self.heartbeat_reads += 1
                return {"base_mode": 128 if self.heartbeat_reads > 1 else 0}
            if name == "COMMAND_ACK":
                return {"command": 400, "result": 0}
            return None

        def raise_if_failed(self) -> None:
            return None

    mavlink = SimpleNamespace(
        MAV_CMD_COMPONENT_ARM_DISARM=400,
        MAV_RESULT_ACCEPTED=0,
        MAV_RESULT_TEMPORARILY_REJECTED=1,
        MAV_RESULT_IN_PROGRESS=5,
        MAV_MODE_FLAG_SAFETY_ARMED=128,
        MAV_COMP_ID_AUTOPILOT1=1,
        MAV_TYPE_GCS=6,
        MAV_AUTOPILOT_INVALID=8,
    )
    connection = FakeConnection()
    result = collector._arm(
        connection,
        FakeReader(),
        SimpleNamespace(mavlink=mavlink),
        timeout_s=1.0,
    )

    assert result["command_id"] == 400
    assert result["result"] == 0
    assert result["target_system"] == 1
    assert result["target_component"] == 1
    assert connection.mav.heartbeats, "GCS heartbeat must precede the arm request"
    assert len(connection.mav.commands) == 1
    command = connection.mav.commands[0]
    assert command[0:3] == (1, 1, 400)
    assert command[4] == 1.0
    assert command[5] == 0.0
    assert all(call[2] != 211 for call in connection.mav.commands)


def test_wait_prearm_health_decodes_failures_and_waits_for_clear(monkeypatch) -> None:
    collector = _collector_module()
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        collector,
        "_progress",
        lambda phase, **fields: events.append((phase, fields)),
    )

    class FakeReader:
        def __init__(self) -> None:
            self.state = MavlinkState(
                messages={"HEALTH_REPORT": {"arming_check_error_flags": 0x101080}}
            )

        def raise_if_failed(self) -> None:
            return None

    class FakeMav:
        def __init__(self, reader: FakeReader) -> None:
            self.reader = reader
            self.calls = 0

        def heartbeat_send(self, *args: object) -> None:
            self.calls += 1
            self.reader.state.messages["HEALTH_REPORT"] = {"arming_check_error_flags": 0}

    reader = FakeReader()
    connection = SimpleNamespace(mav=FakeMav(reader))
    mavlink = SimpleNamespace(
        MAV_TYPE_GCS=6,
        MAV_AUTOPILOT_INVALID=8,
    )
    snapshot = collector._wait_prearm_health(
        reader,
        timeout_s=1.0,
        heartbeat_connection=connection,
        mavutil_module=SimpleNamespace(mavlink=mavlink),
    )

    assert snapshot["ready"] is True
    assert snapshot["error_flags"] == 0
    failed = next(fields for phase, fields in events if phase == "prearm_health")
    assert failed["failed_checks"] == [
        "manual_control_input",
        "communication_links",
        "system",
    ]
    assert connection.mav.calls >= 1
    assert any(phase == "prearm_health_ok" for phase, _ in events)


def test_wait_prearm_health_times_out_with_named_failures(monkeypatch) -> None:
    import pytest

    collector = _collector_module()
    monkeypatch.setattr(collector, "_progress", lambda *args, **kwargs: None)
    reader = SimpleNamespace(
        state=MavlinkState(messages={"HEALTH_REPORT": {"arming_check_error_flags": 0x101080}}),
        raise_if_failed=lambda: None,
    )
    with pytest.raises(TimeoutError, match="manual_control_input, communication_links, system"):
        collector._wait_prearm_health(reader, timeout_s=0.02)


def test_mavlink_reader_drains_real_udp_mavlink2_burst() -> None:
    import socket
    import threading
    import time

    from pymavlink import mavutil
    from pymavlink.dialects.v20 import ardupilotmega as dialect

    receiver = mavutil.mavlink_connection("udpin:127.0.0.1:0", autoreconnect=False)
    # pymavlink does not expose the ephemeral port in the connection string;
    # bind a known port and recreate the connection for a deterministic test.
    receiver.close()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    receiver = mavutil.mavlink_connection(f"udpin:127.0.0.1:{port}", autoreconnect=False)

    from multidetect.sim_teacher.streams import MavlinkReader

    reader = MavlinkReader(receiver).start()
    sender_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    encoder = dialect.MAVLink(None, srcSystem=1, srcComponent=1)
    packets = [
        encoder.heartbeat_encode(6, 8, 0, 0, 4).pack(encoder),
        encoder.local_position_ned_encode(1, 1.0, 2.0, -3.0, 0.0, 0.0, 0.0).pack(encoder),
        encoder.attitude_encode(1, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0).pack(encoder),
    ]
    assert all(packet[0] == 0xFD for packet in packets)
    sent = [0]

    def blast() -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            for packet in packets:
                sender_socket.sendto(packet, ("127.0.0.1", port))
                sent[0] += 1
            # Three packets every 4 ms is 750 packets/s while leaving enough
            # scheduler time for the receiver thread on Windows.
            time.sleep(0.004)
        sender_socket.close()

    sender = threading.Thread(target=blast, name="mavlink-udp-blast")
    sender.start()
    sender.join(timeout=5.0)
    assert not sender.is_alive()
    time.sleep(0.2)
    reader.stop()
    receiver.close()

    assert reader.state.get("HEARTBEAT") is not None
    assert reader.state.count >= sent[0] * 0.90
    assert reader.parse_errors == 0
    assert reader.bad_crc == 0

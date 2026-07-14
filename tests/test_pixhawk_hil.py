from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("pymavlink")

from pymavlink import mavutil

from multidetect.pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from multidetect.pixhawk_hil import FixedWingTelemetryHilConfig, FixedWingTelemetryHilEmitter


class _RecordingMav:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        if not name.endswith("_send"):
            raise AttributeError(name)

        def record(*args: object) -> None:
            self.calls.append((name, args))

        return record


class _Connection:
    def __init__(self) -> None:
        self.mav = _RecordingMav()


def test_hil_emitter_sends_only_expected_telemetry_messages() -> None:
    connection = _Connection()
    emitter = FixedWingTelemetryHilEmitter(
        FixedWingTelemetryHilConfig(),
        connection=connection,
    )

    emitter.emit_cycle(elapsed_s=1.5)

    names = [name for name, _args in connection.mav.calls]
    assert names == [
        "heartbeat_send",
        "attitude_send",
        "global_position_int_send",
        "sys_status_send",
        "gps_raw_int_send",
        "mission_current_send",
    ]
    assert not any(
        token in name for name in names for token in ("command", "mission_item", "actuator")
    )
    heartbeat = connection.mav.calls[0][1]
    assert int(heartbeat[2]) & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
    position = connection.mav.calls[2][1]
    assert position[1] == 311_234_560
    assert position[2] == 1_216_543_210
    assert position[4] == 42_500
    assert position[5] == 1_700
    assert emitter.message_count == 6


@pytest.mark.parametrize(
    "changes",
    [
        {"rate_hz": 0.0},
        {"latitude_deg": 91.0},
        {"heading_deg": 360.0},
        {"mission_sequence": -1},
        {"armed": 1},
    ],
)
def test_hil_config_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="HIL telemetry"):
        FixedWingTelemetryHilConfig(**changes)


def test_real_pymavlink_udp_loopback_maps_fields_and_becomes_stale() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    receiver = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            f"udpin:127.0.0.1:{port}",
            stale_after_seconds=0.25,
            expected_system_id=1,
            expected_autopilot_id=mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            expected_vehicle_type_id=mavutil.mavlink.MAV_TYPE_FIXED_WING,
            require_operational_state=True,
        )
    )
    receiver.snapshot(now_s=time.monotonic())  # Bind before the first UDP datagram is emitted.
    emitter = FixedWingTelemetryHilEmitter(
        FixedWingTelemetryHilConfig(
            endpoint=f"udpout:127.0.0.1:{port}",
            rate_hz=20.0,
        )
    )
    thread = threading.Thread(target=emitter.run, kwargs={"duration_s": 0.6})
    thread.start()
    latest = None
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            latest = receiver.snapshot(now_s=time.monotonic())
            if latest.link_healthy is True and latest.position_healthy is True:
                break
            time.sleep(0.02)
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        latest = receiver.snapshot(now_s=time.monotonic())
        assert latest.link_healthy is True
        assert latest.position_healthy is True
        assert receiver.qualification.passed is True
        assert receiver.heartbeat_identity.to_document()["vehicle_type_name"] == (
            "MAV_TYPE_FIXED_WING"
        )
        assert latest.latitude_deg == pytest.approx(31.123456)
        assert latest.longitude_deg == pytest.approx(121.654321)
        assert latest.altitude_agl_m == pytest.approx(42.5)
        assert latest.ground_speed_mps == pytest.approx(17.0)
        assert latest.flight_mode == "AUTO"
        assert latest.mission_sequence == 3
        assert latest.in_allowed_zone is None
        assert latest.release_zone_clear is None

        time.sleep(0.30)
        stale = receiver.snapshot(now_s=time.monotonic())
        assert stale.link_healthy is False
        assert stale.position_healthy is False
    finally:
        receiver.close()
        thread.join(timeout=2.0)

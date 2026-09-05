from __future__ import annotations

import socket
import threading
import time

import pytest

from multidetect.simulator.control import SimulatorControlAuthority
from multidetect.simulator.control_gateway import (
    SimulatorControlGateway,
    SimulatorControlGatewayConfig,
    _decode_manual_control,
    build_manual_control_frame,
)
from multidetect.simulator.isolation import SimulationIsolationError
from multidetect.simulator.mavlink_control import (
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
)


class FakeMav:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(self, *args: int) -> None:
        self.sent.append(tuple(args))


class FakeConnection:
    def __init__(self) -> None:
        self.mav = FakeMav()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def make_gateway(
    *, ai: bool = False, watchdog_s: float = 0.5, manual_watchdog_s: float | None = None
) -> tuple[SimulatorControlGateway, FakeConnection]:
    authority = SimulatorControlAuthority()
    connection = FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(
            watchdog_s=watchdog_s, manual_watchdog_s=manual_watchdog_s
        ),
        connection=connection,
    )
    if ai:
        authority.enable_ai()
        authority.release_manual_control()
    return SimulatorControlGateway(adapter), connection


def test_v1_and_v2_base_frames_and_multi_datagram_stream() -> None:
    gateway, connection = make_gateway()
    v1 = build_manual_control_frame(pitch=0.25, roll=-0.5, yaw=0.1, throttle=0.75)
    v2 = build_manual_control_frame(version=2, pitch=-0.25, roll=0.5, yaw=-0.1, throttle=0.25)
    assert gateway.process_datagram(v1[:7]) == ()
    receipts = gateway.process_datagram(v1[7:] + v2)
    assert len(receipts) == 2
    assert connection.mav.sent == [(1, 250, -500, 750, 100, 0), (1, -250, 500, 250, -100, 0)]
    assert gateway.evidence()["manual_frames_accepted"] == 2


def test_v2_extensions_are_validated_and_partial_extensions_rejected() -> None:
    gateway, _ = make_gateway()
    # buttons2 (2 bytes), enabled_extensions=bit0, then s pitch extension.
    valid = build_manual_control_frame(
        version=2, pitch=0, roll=0, yaw=0, throttle=0.5, extensions=bytes([0, 0, 1, 0, 0])
    )
    assert len(gateway.process_datagram(valid)) == 1
    partial = build_manual_control_frame(
        version=2, pitch=0, roll=0, yaw=0, throttle=0.5, extensions=bytes([0, 0, 1])
    )
    assert gateway.process_datagram(partial) == ()
    assert gateway.evidence()["rejected_reasons"]["v2_manual_control_extension_truncated"] == 1


def test_source_target_message_and_axis_validation() -> None:
    gateway, _ = make_gateway()
    cases = [
        (
            build_manual_control_frame(pitch=0, roll=0, yaw=0, throttle=0.5, source_system=1),
            "source_system_mismatch",
        ),
        (
            build_manual_control_frame(pitch=0, roll=0, yaw=0, throttle=0.5, source_component=1),
            "source_component_mismatch",
        ),
        (
            build_manual_control_frame(pitch=0, roll=0, yaw=0, throttle=0.5, target_system=2),
            "manual_control_target_mismatch",
        ),
    ]
    for frame, _ in cases:
        gateway.process_datagram(frame)
    # Replace message id with heartbeat while preserving a valid frame shape.
    heartbeat = bytearray(build_manual_control_frame(pitch=0, roll=0, yaw=0, throttle=0.5))
    heartbeat[5] = 0
    gateway.process_datagram(bytes(heartbeat))
    assert gateway.evidence()["rejected_frames"] == 4
    assert gateway.evidence()["rejected_reasons"]["source_system_mismatch"] == 1
    assert gateway.evidence()["rejected_reasons"]["source_component_mismatch"] == 1


def test_decode_accepts_signed_z_and_rejects_invalid_sentinel() -> None:
    assert _decode_manual_control(bytes(10) + bytes([1]), version=1)[3] == 0.5
    with pytest.raises(ValueError, match="invalid_sentinel"):
        _decode_manual_control((32767).to_bytes(2, "little", signed=True) + bytes(9))


def test_non_loopback_and_datagram_size_are_counted() -> None:
    gateway, _ = make_gateway()
    gateway.process_datagram(b"bad", source=("192.0.2.1", 99))
    gateway.process_datagram(b"x" * 9000)
    evidence = gateway.evidence()
    assert evidence["non_loopback_datagrams_blocked"] == 1
    assert evidence["rejected_reasons"]["non_loopback_source"] == 1
    assert evidence["rejected_reasons"]["datagram_too_large"] == 1


def test_stop_shuts_down_ai_with_neutral_and_is_idempotent() -> None:
    gateway, connection = make_gateway(ai=True)
    gateway.adapter.submit_ai(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=1.0)
    gateway.stop()
    gateway.stop()
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)
    assert gateway.evidence()["shutdown_neutral_count"] == 1
    assert gateway.evidence()["stop_requested"] is True


def test_watchdog_transitions_to_failsafe() -> None:
    gateway, connection = make_gateway(ai=True, watchdog_s=0.5)
    gateway.adapter.submit_ai(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=1.0)
    receipt = gateway.poll_watchdog(now_s=1.6)
    assert receipt is not None
    assert receipt.source == "watchdog"
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)
    assert gateway.evidence()["authority"]["authority_state"] == "failsafe"


def test_gateway_manual_watchdog_neutralizes_stale_qgc_stream_once() -> None:
    gateway, connection = make_gateway(manual_watchdog_s=0.5)
    frame = build_manual_control_frame(pitch=0.1, roll=-0.2, yaw=0.0, throttle=0.6)

    receipts = gateway.process_datagram(frame, timestamp_s=10.0)
    assert len(receipts) == 1
    assert gateway.poll_watchdog(now_s=10.49) is None

    receipt = gateway.poll_watchdog(now_s=10.5)
    assert receipt is not None
    assert receipt.source == "manual_watchdog"
    assert receipt.state == "failsafe"
    assert connection.mav.sent[0] == (1, 100, -200, 600, 0, 0)
    assert connection.mav.sent[-1] == (1, 0, 0, 500, 0, 0)

    # The watchdog neutral is emitted by the adapter; the gateway must not
    # manufacture a second neutral on the same stale edge.
    assert len(connection.mav.sent) == 2
    assert gateway.poll_watchdog(now_s=11.0) is None
    evidence = gateway.evidence()
    assert evidence["manual_watchdog_s"] == pytest.approx(0.5)
    assert evidence["manual_watchdog_enabled"] is True
    assert evidence["authority"]["manual_watchdog_neutral_count"] == 1


def test_ports_are_loopback_and_isolated() -> None:
    with pytest.raises(SimulationIsolationError):
        SimulatorControlGatewayConfig(qgc_input_host="0.0.0.0")
    with pytest.raises(SimulationIsolationError):
        SimulatorControlGatewayConfig(qgc_input_port=14550)
    with pytest.raises(SimulationIsolationError):
        SimulatorControlGatewayConfig(px4_output_endpoint="udpout:example.test:14540")
    with pytest.raises(SimulationIsolationError):
        SimulatorControlGatewayConfig(qgc_input_port=14540)


def test_gateway_rejects_malformed_config_types_and_accepts_ipv6_loopback_output() -> None:
    with pytest.raises(ValueError, match="qgc_input_port"):
        SimulatorControlGatewayConfig(qgc_input_port="14650")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="maximum_datagram_bytes"):
        SimulatorControlGatewayConfig(maximum_datagram_bytes="8192")  # type: ignore[arg-type]
    config = SimulatorControlGatewayConfig(px4_output_endpoint="udpout:::1:14540")
    assert config.px4_output_endpoint == "udpout:::1:14540"


def test_gateway_rejects_nonfinite_duration_and_post_stop_input() -> None:
    gateway, _ = make_gateway()
    with pytest.raises(ValueError, match="finite"):
        gateway.run(duration_s=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        gateway.run(duration_s=float("inf"))
    gateway.stop()
    assert gateway.process_datagram(build_manual_control_frame(
        pitch=0, roll=0, yaw=0, throttle=0.5
    )) == ()
    assert gateway.evidence()["rejected_reasons"]["gateway_not_accepting"] == 1


def test_gateway_run_receives_and_rejects_oversized_datagram_without_truncation() -> None:
    gateway, _ = make_gateway()
    gateway.config = SimulatorControlGatewayConfig(
        qgc_input_port=14658,
        px4_output_endpoint=gateway.adapter.config.endpoint,
        maximum_datagram_bytes=64,
    )
    result: dict[str, object] = {}

    worker = threading.Thread(
        target=lambda: result.update(gateway.run(duration_s=2.0)),
        daemon=True,
    )
    worker.start()
    assert gateway.ready_event.wait(1.0)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.sendto(b"x" * 65, gateway.bound_endpoint)
        deadline = time.monotonic() + 1.0
        while gateway.evidence()["datagrams_received"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        sender.close()
        gateway.stop()
    worker.join(timeout=1.0)
    assert result["rejected_reasons"]["datagram_too_large"] == 1

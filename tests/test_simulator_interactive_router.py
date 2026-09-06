from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from multidetect.simulator.closed_loop import SimulatorFlightModelMav
from multidetect.simulator.flight_model import DeterministicFlightModel
from multidetect.simulator.interactive_router import (
    DEFAULT_PX4_INPUT_PORT,
    DEFAULT_PX4_OUTPUT_PORT,
    DEFAULT_QGC_INPUT_PORT,
    DEFAULT_QGC_OUTPUT_PORT,
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_DO_VTOL_TRANSITION,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MSG_MANUAL_CONTROL,
    MSG_MISSION_COUNT,
    MSG_MISSION_ITEM_INT,
    MSG_SET_MODE,
    SimulatorInteractiveRouter,
    SimulatorInteractiveRouterConfig,
    SimulatorInteractiveRouterError,
    _build_frame_for_cli,
    _decode_qgc_manual_control,
    _parser,
)
from multidetect.simulator.isolation import SimulationIsolationError


def _v1_frame(
    message_id: int,
    payload: bytes,
    *,
    source_system: int = 255,
    source_component: int = 190,
    sequence: int = 0,
) -> bytes:
    return (
        bytes(
            [
                0xFE,
                len(payload),
                sequence & 0xFF,
                source_system,
                source_component,
                message_id & 0xFF,
            ]
        )
        + payload
        + b"\x00\x00"
    )


def _v2_frame(
    message_id: int,
    payload: bytes,
    *,
    source_system: int = 255,
    source_component: int = 190,
    sequence: int = 0,
) -> bytes:
    return (
        bytes(
            [
                0xFD,
                len(payload),
                0,
                0,
                sequence & 0xFF,
                source_system,
                source_component,
                message_id & 0xFF,
                (message_id >> 8) & 0xFF,
                (message_id >> 16) & 0xFF,
            ]
        )
        + payload
        + b"\x00\x00"
    )


def _manual_frame(
    *,
    pitch: float = 0.0,
    roll: float = 0.0,
    yaw: float = 0.0,
    throttle: float = 0.5,
    source_system: int = 255,
    source_component: int = 190,
    target_system: int = 1,
) -> bytes:
    # QGC's established MANUAL_CONTROL wire scale is z=[0,1000].
    payload = struct.pack(
        "<hhhhHB",
        round(pitch * 1000),
        round(roll * 1000),
        round(throttle * 1000),
        round(yaw * 1000),
        0,
        target_system,
    )
    return _v1_frame(
        MSG_MANUAL_CONTROL,
        payload,
        source_system=source_system,
        source_component=source_component,
    )


def _command_long_frame(
    command: int,
    *,
    params: tuple[float, ...] = (),
    target_system: int = 1,
    target_component: int = 1,
    confirmation: int = 0,
) -> bytes:
    values = list(params) + [0.0] * (7 - len(params))
    assert len(values) == 7
    payload = struct.pack(
        "<7fHBBB",
        *values,
        command,
        target_system,
        target_component,
        confirmation,
    )
    return _v1_frame(76, payload)


def _set_mode_frame(*, custom_mode: int, target_system: int = 1, base_mode: int = 1) -> bytes:
    return _v1_frame(MSG_SET_MODE, struct.pack("<IBB", custom_mode, target_system, base_mode))


def _mission_count_frame(
    count: int, *, target_system: int = 1, target_component: int = 1, mission_type: int = 0
) -> bytes:
    return _v2_frame(
        MSG_MISSION_COUNT,
        struct.pack("<HBBB", count, target_system, target_component, mission_type),
    )


def _mission_item_int_frame(
    *,
    sequence: int = 0,
    command: int = 16,
    latitude: float = 31.2,
    longitude: float = 121.5,
    altitude: float = 50.0,
    target_system: int = 1,
    target_component: int = 1,
    frame: int = 6,
    current: int | None = None,
    autocontinue: int = 1,
    mission_type: int = 0,
    params: tuple[float, ...] = (),
) -> bytes:
    values = list(params) + [0.0] * (4 - len(params))
    assert len(values) == 4
    payload = struct.pack(
        "<ffffiifHHBBBBBB",
        *values,
        round(latitude * 10_000_000),
        round(longitude * 10_000_000),
        altitude,
        sequence,
        command,
        target_system,
        target_component,
        frame,
        (1 if sequence == 0 else 0) if current is None else current,
        autocontinue,
        mission_type,
    )
    if mission_type:
        return _v2_frame(MSG_MISSION_ITEM_INT, payload)
    return _v1_frame(MSG_MISSION_ITEM_INT, payload[:-1])


class FakeAdapter:
    def __init__(self, *, shutdown_receipt: object | None = None) -> None:
        self.manual: list[dict[str, float | None]] = []
        self.shutdown_calls = 0
        self.watchdog_calls = 0
        self.shutdown_receipt = shutdown_receipt

    def submit_manual(self, **kwargs: float | None) -> dict[str, object]:
        self.manual.append(kwargs)
        return {"source": "manual", **kwargs}

    def check_watchdog(self, *, now_s: float) -> None:
        del now_s
        self.watchdog_calls += 1
        return None

    def shutdown(self, *, timestamp_s: float) -> object | None:
        del timestamp_s
        self.shutdown_calls += 1
        return self.shutdown_receipt

    def evidence(self) -> dict[str, object]:
        return {
            "authority_state": "manual" if self.manual else "paused",
            "messages_sent": len(self.manual),
        }


def _ports() -> tuple[int, int, int, int]:
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4)]
    try:
        for channel in sockets:
            channel.bind(("127.0.0.1", 0))
        return tuple(channel.getsockname()[1] for channel in sockets)  # type: ignore[return-value]
    finally:
        for channel in sockets:
            channel.close()


def _config(ports: tuple[int, int, int, int], **kwargs: object) -> SimulatorInteractiveRouterConfig:
    qgc_input, qgc_output, px4_input, px4_output = ports
    return SimulatorInteractiveRouterConfig(
        qgc_input_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_input_port=px4_input,
        px4_output_endpoint=f"udpout:127.0.0.1:{px4_output}",
        **kwargs,
    )


def test_config_is_literal_loopback_and_ports_are_distinct() -> None:
    with pytest.raises(SimulationIsolationError, match="literal loopback"):
        SimulatorInteractiveRouterConfig(qgc_input_host="0.0.0.0")
    with pytest.raises(SimulationIsolationError, match="distinct"):
        SimulatorInteractiveRouterConfig(
            qgc_input_port=DEFAULT_QGC_INPUT_PORT,
            qgc_output_port=DEFAULT_QGC_INPUT_PORT,
        )
    with pytest.raises(SimulationIsolationError, match="udpout"):
        SimulatorInteractiveRouterConfig(px4_output_endpoint="udpout:example.test:14540")
    assert DEFAULT_QGC_INPUT_PORT != DEFAULT_QGC_OUTPUT_PORT
    assert DEFAULT_PX4_INPUT_PORT != DEFAULT_PX4_OUTPUT_PORT


def test_qgc_manual_and_read_only_frames_use_separate_paths() -> None:
    adapter = FakeAdapter()
    to_px4: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        px4_sender=lambda frame, destination: to_px4.append((frame, destination)),
    )

    receipts = router.process_qgc_datagram(
        _manual_frame(pitch=-0.25, roll=0.4, yaw=0.1, throttle=0.65),
        source=("127.0.0.1", 51321),
        timestamp_s=12.0,
    )
    assert len(receipts) == 1
    assert adapter.manual == [
        {"roll": 0.4, "pitch": -0.25, "yaw": 0.1, "throttle": 0.65, "timestamp_s": 12.0}
    ]
    assert to_px4 == []  # adapter owns canonical MANUAL_CONTROL emission

    read_request = _v1_frame(21, bytes([1, 1]))
    assert router.process_qgc_datagram(read_request, source=("127.0.0.1", 51321)) == ()
    assert to_px4 == [(read_request, router.config.px4_output_address)]
    evidence = router.evidence()
    assert evidence["qgc_manual_frames_accepted"] == 1
    assert evidence["qgc_read_only_frames_forwarded"] == 1
    assert evidence["qgc_frames_blocked"] == 0
    assert evidence["qgc_manual_control_via_shared_adapter"] is True


def test_qgc_source_and_message_policy_fail_closed() -> None:
    adapter = FakeAdapter()
    router = SimulatorInteractiveRouter(adapter, config=_config(_ports(), qgc_source_port=45678))
    router.process_qgc_datagram(_manual_frame(), source=("192.0.2.1", 45678))
    router.process_qgc_datagram(_manual_frame(), source=("127.0.0.1", 45679))
    router.process_qgc_datagram(_manual_frame(source_system=42), source=("127.0.0.1", 45678))
    router.process_qgc_datagram(_manual_frame(target_system=2), source=("127.0.0.1", 45678))
    # COMMAND_LONG/mission/control writes are not in the interactive QGC
    # read-only allowlist and must never reach PX4.
    router.process_qgc_datagram(_v1_frame(76, bytes(33)), source=("127.0.0.1", 45678))
    assert adapter.manual == []
    evidence = router.evidence()
    assert evidence["qgc_non_loopback_datagrams_blocked"] == 1
    assert evidence["qgc_unexpected_source_datagrams_blocked"] == 1
    assert evidence["qgc_rejected_reasons"]["qgc_source_system_mismatch"] == 1
    assert evidence["qgc_rejected_reasons"]["manual_control_target_mismatch"] == 1
    assert evidence["qgc_rejected_reasons"]["qgc_message_not_allowed"] == 1


def test_malformed_source_port_is_rejected_without_raising() -> None:
    adapter = FakeAdapter()
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports(), qgc_source_port=45678),
    )
    assert (
        router.process_qgc_datagram(
            _manual_frame(),
            source=("127.0.0.1", "not-a-port"),  # type: ignore[arg-type]
        )
        == ()
    )
    assert adapter.manual == []
    assert router.evidence()["qgc_rejected_reasons"]["unexpected_source_port"] == 1


def test_px4_telemetry_is_forwarded_byte_for_byte_and_identity_checked() -> None:
    adapter = FakeAdapter()
    to_qgc: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        qgc_sender=lambda frame, destination: to_qgc.append((frame, destination)),
    )
    heartbeat = _v1_frame(0, bytes(9), source_system=1, source_component=1)
    assert router.process_px4_datagram(heartbeat, source=("127.0.0.1", 14540)) == 1
    assert to_qgc == [(heartbeat, router.config.qgc_output_endpoint)]
    router.process_px4_datagram(
        _v1_frame(0, bytes(9), source_system=42, source_component=1),
        source=("127.0.0.1", 14540),
    )
    router.process_px4_datagram(
        heartbeat,
        source=("192.0.2.1", 14540),
    )
    evidence = router.evidence()
    assert evidence["px4_frames_forwarded_to_qgc"] == 1
    assert evidence["px4_frames_blocked"] == 1
    assert evidence["px4_rejected_reasons"]["px4_source_system_mismatch"] == 1
    assert evidence["px4_non_loopback_datagrams_blocked"] == 1
    assert evidence["telemetry_frames_rewritten"] is False


def test_px4_telemetry_sink_observes_after_forward_without_rewriting() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    observed: list[tuple[int, float, bytes]] = []

    def sink(frame: object, timestamp_s: float) -> None:
        # The production bridge receives the decoded frame object; retaining
        # its encoded bytes here proves the side-band observer sees the exact
        # payload that was forwarded to QGC.
        observed.append((frame.message_id, timestamp_s, frame.encoded))  # type: ignore[attr-defined]

    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        qgc_sender=lambda frame, destination: forwarded.append((frame, destination)),
        telemetry_sink=sink,
    )
    heartbeat = _v1_frame(0, bytes(9), source_system=1, source_component=1)
    assert router.process_px4_datagram(heartbeat, timestamp_s=7.25) == 1
    assert forwarded == [(heartbeat, router.config.qgc_output_endpoint)]
    assert observed == [(0, 7.25, heartbeat)]
    evidence = router.evidence()
    assert evidence["telemetry_sink_enabled"] is True
    assert evidence["px4_telemetry_sink_frames"] == 1
    assert evidence["px4_telemetry_sink_errors"] == 0
    assert evidence["px4_telemetry_sink_message_ids"] == {"0": 1}


def test_px4_telemetry_sink_failure_is_recorded_without_blocking_qgc() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []

    def failing_sink(_frame: object, _timestamp_s: float) -> None:
        raise RuntimeError("bridge parser exploded")

    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        qgc_sender=lambda frame, destination: forwarded.append((frame, destination)),
        telemetry_sink=failing_sink,
    )
    heartbeat = _v1_frame(0, bytes(9), source_system=1, source_component=1)
    assert router.process_px4_datagram(heartbeat, timestamp_s=8.0) == 1
    assert forwarded == [(heartbeat, router.config.qgc_output_endpoint)]
    evidence = router.evidence()
    assert evidence["px4_telemetry_sink_frames"] == 0
    assert evidence["px4_telemetry_sink_errors"] == 1
    assert "bridge parser exploded" in evidence["px4_telemetry_sink_last_error"]


def test_watchdog_without_sink_uses_adapter_fallback() -> None:
    adapter = FakeAdapter()
    router = SimulatorInteractiveRouter(adapter, config=_config(_ports()))

    assert router.poll_watchdog(now_s=12.5) is None
    assert adapter.watchdog_calls == 1
    evidence = router.evidence()
    assert evidence["watchdog_ticks"] == 1
    assert evidence["watchdog_sink_enabled"] is False
    assert evidence["watchdog_sink_calls"] == 0
    assert evidence["watchdog_sink_errors"] == 0


def test_watchdog_sink_owns_tick_without_duplicate_adapter_poll() -> None:
    adapter = FakeAdapter()
    observed: list[float] = []

    def sink(timestamp_s: float) -> str:
        observed.append(timestamp_s)
        return "bridge-decision"

    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        watchdog_sink=sink,
    )

    assert router.poll_watchdog(now_s=20.25) == "bridge-decision"
    assert observed == [20.25]
    assert adapter.watchdog_calls == 0
    evidence = router.evidence()
    assert evidence["watchdog_ticks"] == 1
    assert evidence["watchdog_sink_enabled"] is True
    assert evidence["watchdog_sink_calls"] == 1
    assert evidence["watchdog_sink_errors"] == 0


def test_watchdog_sink_failure_records_error_and_falls_back_to_adapter() -> None:
    adapter = FakeAdapter()

    def failing_sink(_timestamp_s: float) -> None:
        raise RuntimeError("live watchdog exploded")

    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        watchdog_sink=failing_sink,
    )

    assert router.poll_watchdog(now_s=30.5) is None
    assert adapter.watchdog_calls == 1
    evidence = router.evidence()
    assert evidence["watchdog_ticks"] == 1
    assert evidence["watchdog_sink_enabled"] is True
    assert evidence["watchdog_sink_calls"] == 0
    assert evidence["watchdog_sink_errors"] == 1
    assert "live watchdog exploded" in evidence["watchdog_sink_last_error"]


def test_split_frames_and_signed_throttle_modes() -> None:
    adapter = FakeAdapter()
    router = SimulatorInteractiveRouter(adapter, config=_config(_ports()))
    first = _manual_frame(throttle=0.25)
    second = _manual_frame(throttle=0.75)
    assert router.process_qgc_datagram(first[:5]) == ()
    assert len(router.process_qgc_datagram(first[5:] + second)) == 2
    assert [entry["throttle"] for entry in adapter.manual] == [0.25, 0.75]
    assert (
        _decode_qgc_manual_control(
            struct.pack("<hhhhHB", 0, 0, -1000, 0, 0, 1),
            version=1,
            throttle_wire_mode="auto",
        )[3]
        == 0.0
    )
    assert _decode_qgc_manual_control(
        struct.pack("<hhhhHB", 0, 0, -1, 0, 0, 1),
        version=1,
        throttle_wire_mode="qgc_legacy",
    )[3] == pytest.approx(0.4995)


def test_standalone_cli_exposes_all_throttle_wire_modes() -> None:
    parser = _parser()
    assert parser.parse_args([]).throttle_wire_mode == "auto"
    assert parser.parse_args(["--throttle-wire-mode", "qgc_legacy"]).throttle_wire_mode == (
        "qgc_legacy"
    )
    assert parser.parse_args(["--throttle-wire-mode", "signed"]).throttle_wire_mode == "signed"
    with pytest.raises(SystemExit):
        parser.parse_args(["--throttle-wire-mode", "invalid"])


def test_cli_fixture_builder_honors_signed_wire_mode() -> None:
    signed = _build_frame_for_cli(
        pitch=0.0,
        roll=0.0,
        yaw=0.0,
        throttle=0.25,
        throttle_wire_mode="signed",
    )
    payload = signed[6 : 6 + signed[1]]
    assert struct.unpack_from("<h", payload, 4)[0] == -500


def test_qgc_partial_frame_cannot_be_completed_by_another_source() -> None:
    adapter = FakeAdapter()
    router = SimulatorInteractiveRouter(adapter, config=_config(_ports()))
    frame = _manual_frame(throttle=0.7)
    router.process_qgc_datagram(frame[:5], source=("127.0.0.1", 41001))
    # The second loopback sender only contributes bytes after the source
    # boundary; those bytes must not complete the first sender's frame.
    assert router.process_qgc_datagram(frame[5:], source=("127.0.0.1", 41002)) == ()
    evidence = router.evidence()
    assert adapter.manual == []
    assert evidence["qgc_fragment_source_resets"] == 1
    assert evidence["qgc_rejected_reasons"]["fragment_source_changed"] == 1
    # A fresh complete frame from the new source is still accepted.
    assert len(router.process_qgc_datagram(frame, source=("127.0.0.1", 41002))) == 1


def test_px4_partial_frame_cannot_be_completed_by_another_source() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        qgc_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    telemetry = _v1_frame(0, bytes(9), source_system=1, source_component=1)
    router.process_px4_datagram(telemetry[:4], source=("127.0.0.1", 42001))
    assert router.process_px4_datagram(telemetry[4:], source=("127.0.0.1", 42002)) == 0
    evidence = router.evidence()
    assert forwarded == []
    assert evidence["px4_fragment_source_resets"] == 1
    assert evidence["px4_rejected_reasons"]["fragment_source_changed"] == 1
    assert router.process_px4_datagram(telemetry, source=("127.0.0.1", 42002)) == 1


def test_qgc_control_mode_defaults_read_only_and_blocks_flight_commands() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports()),
        px4_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    arm = _command_long_frame(MAV_CMD_COMPONENT_ARM_DISARM, params=(1.0, 0.0))
    assert router.process_qgc_datagram(arm) == ()
    assert forwarded == []
    evidence = router.evidence()
    assert evidence["qgc_control_mode"] == "read_only"
    assert evidence["qgc_rejected_reasons"]["qgc_message_not_allowed"] == 1


def test_sim_flight_forwards_bounded_arm_mode_transition_and_rtl() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports(), qgc_control_mode="sim_flight"),
        px4_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    frames = (
        _command_long_frame(MAV_CMD_COMPONENT_ARM_DISARM, params=(1.0, 0.0)),
        _set_mode_frame(custom_mode=1 << 16),
        _command_long_frame(MAV_CMD_DO_VTOL_TRANSITION, params=(4.0,)),
        _command_long_frame(MAV_CMD_NAV_RETURN_TO_LAUNCH),
    )
    for frame in frames:
        assert router.process_qgc_datagram(frame) == ()
    assert [item[0] for item in forwarded] == list(frames)
    evidence = router.evidence()
    assert evidence["qgc_control_mode"] == "sim_flight"
    assert evidence["qgc_sim_flight_frames_forwarded"] == 4
    assert evidence["qgc_flight_command_frames_forwarded"] == 4
    assert evidence["qgc_forwarded_message_ids"] == {"11": 1, "76": 3}


def test_sim_flight_lifecycle_sink_updates_local_model_without_changing_forwarding() -> None:
    model = DeterministicFlightModel()
    sink = SimulatorFlightModelMav(model)
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        FakeAdapter(),
        config=_config(_ports(), qgc_control_mode="sim_flight"),
        lifecycle_sink=sink,
        px4_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    arm = _command_long_frame(MAV_CMD_COMPONENT_ARM_DISARM, params=(1.0, 0.0))
    assert model.telemetry.armed is False
    router.process_qgc_datagram(arm)
    assert model.telemetry.armed is True
    assert forwarded == [(arm, router.config.px4_output_address)]
    evidence = router.evidence()
    assert evidence["lifecycle_sink_enabled"] is True
    assert evidence["lifecycle_sink_calls"] == 1
    assert evidence["lifecycle_sink_errors"] == 0


def test_sim_flight_rejects_unsafe_command_parameters_and_modes() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports(), qgc_control_mode="sim_flight"),
        px4_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    cases = (
        (
            _command_long_frame(MAV_CMD_COMPONENT_ARM_DISARM, params=(2.0, 0.0)),
            "arm_disarm_parameter_invalid",
        ),
        (
            _command_long_frame(MAV_CMD_DO_VTOL_TRANSITION, params=(9.0,)),
            "vtol_transition_parameter_invalid",
        ),
        (
            _command_long_frame(
                MAV_CMD_COMPONENT_ARM_DISARM,
                params=(1.0, 0.0),
                confirmation=1,
            ),
            "sim_flight_confirmation_not_zero",
        ),
        (_set_mode_frame(custom_mode=6 << 16), "set_mode_main_mode_not_allowed"),
        (_command_long_frame(176), "sim_flight_command_not_allowed"),
    )
    for frame, reason in cases:
        assert router.process_qgc_datagram(frame) == ()
        assert router.evidence()["qgc_rejected_reasons"][reason] == 1
    assert forwarded == []


def test_sim_flight_mission_count_and_item_are_bounded_and_targeted() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(_ports(), qgc_control_mode="sim_flight"),
        px4_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    # Item-before-count is rejected; a bounded count then accepts a valid INT
    # item and forwards the original bytes unchanged.
    item = _mission_item_int_frame()
    assert router.process_qgc_datagram(item) == ()
    assert router.evidence()["qgc_rejected_reasons"]["mission_item_without_count"] == 1
    count = _mission_count_frame(2)
    assert router.process_qgc_datagram(count) == ()
    assert router.process_qgc_datagram(item) == ()
    assert [frame for frame, _ in forwarded] == [count, item]
    evidence = router.evidence()
    assert evidence["qgc_mission_transactions"] == 1
    assert evidence["qgc_mission_expected_count"] == 2
    assert evidence["qgc_mission_items_seen"] == 1
    # Wrong sequence, target, command, coordinates and mission type all fail
    # closed without crossing the PX4 leg.
    invalid = (
        (_mission_item_int_frame(sequence=2), "mission_item_sequence_out_of_range"),
        (_mission_item_int_frame(target_system=2), "mission_item_target_system_mismatch"),
        (_mission_item_int_frame(command=400), "mission_command_not_allowed"),
        (_mission_item_int_frame(latitude=91.0), "mission_item_coordinate_invalid"),
        (_mission_item_int_frame(mission_type=1), "mission_type_not_mission"),
    )
    for frame, reason in invalid:
        assert router.process_qgc_datagram(frame) == ()
        assert router.evidence()["qgc_rejected_reasons"][reason] == 1
    assert len(forwarded) == 2


def test_sim_flight_ftp_read_only_target_uses_configured_system() -> None:
    adapter = FakeAdapter()
    forwarded: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        adapter,
        config=_config(
            _ports(),
            target_system=42,
            qgc_target_component_ids=frozenset({1}),
        ),
        px4_sender=lambda frame, destination: forwarded.append((frame, destination)),
    )
    payload = bytearray(7)
    payload[1], payload[2], payload[6] = 42, 1, 4  # OPEN_FILE_RO
    valid = _v2_frame(110, bytes(payload))
    wrong = _v2_frame(110, bytes(bytearray(payload[:1]) + bytes([1, 1]) + payload[3:]))
    assert router.process_qgc_datagram(valid) == ()
    assert forwarded == [(valid, router.config.px4_output_address)]
    assert router.process_qgc_datagram(wrong) == ()
    assert len(forwarded) == 1


def test_router_rejects_frames_after_stop_and_clears_bound_endpoints() -> None:
    ports = _ports()
    adapter = FakeAdapter()
    router = SimulatorInteractiveRouter(adapter, config=_config(ports))
    router.stop()
    assert router.process_qgc_datagram(_manual_frame()) == ()
    assert router.evidence()["qgc_rejected_reasons"]["router_not_accepting"] == 1
    # A later run resets the input gate, and the finalizer clears endpoint
    # evidence so a stopped router cannot look bound.
    router = SimulatorInteractiveRouter(adapter, config=_config(_ports()))
    result = router.run(duration_s=0.01)
    assert result["ready"] is False
    assert router.bound_endpoints == {}
    assert router.process_qgc_datagram(_manual_frame()) == ()
    assert router.evidence()["qgc_rejected_reasons"]["router_not_accepting"] == 1


def test_run_binds_both_legs_and_stop_neutral_is_once() -> None:
    ports = _ports()
    adapter = FakeAdapter(shutdown_receipt={"source": "shutdown"})
    router = SimulatorInteractiveRouter(adapter, config=_config(ports))
    result: dict[str, object] = {}
    failures: list[BaseException] = []

    def target() -> None:
        try:
            result.update(router.run(duration_s=2.0))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    assert router.ready_event.wait(1.0)
    qgc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    qgc_receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    px4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    qgc_receiver.bind(router.config.qgc_output_endpoint)
    qgc.settimeout(1.0)
    qgc_receiver.settimeout(1.0)
    try:
        qgc.sendto(_manual_frame(throttle=0.6), router.config.qgc_input_endpoint)
        deadline = time.monotonic() + 1.0
        while not adapter.manual and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter.manual and adapter.manual[0]["throttle"] == 0.6

        telemetry = _v1_frame(0, bytes(9), source_system=1, source_component=1)
        px4.sendto(telemetry, router.config.px4_input_endpoint)
        received, _ = qgc_receiver.recvfrom(8192)
        assert received == telemetry
    finally:
        qgc.close()
        qgc_receiver.close()
        px4.close()
        router.stop()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert failures == []
    assert adapter.shutdown_calls == 1
    assert result["qgc_frames_sent_to_px4"] == 0
    assert result["px4_frames_sent_to_qgc"] == 1
    assert result["shutdown_neutral_count"] == 1
    assert result["stop_requested"] is True


def test_run_rejects_second_concurrent_start() -> None:
    ports = _ports()
    router = SimulatorInteractiveRouter(FakeAdapter(), config=_config(ports))
    worker = threading.Thread(target=lambda: router.run(duration_s=0.4), daemon=True)
    worker.start()
    assert router.ready_event.wait(1.0)
    with pytest.raises(SimulatorInteractiveRouterError, match="already running"):
        router.run(duration_s=0.1)
    router.stop()
    worker.join(timeout=1.0)

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.px4_gazebo_qgc_router import (
    EVALUATOR_ALLOWED_MESSAGE_IDS,
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_DO_VTOL_TRANSITION,
    MAV_CMD_NAV_VTOL_TAKEOFF,
    MAV_CMD_NAV_WAYPOINT,
    MAV_FRAME_GLOBAL_RELATIVE_ALT,
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MAV_FRAME_MISSION,
    MAV_TYPE_GCS,
    MAXIMUM_CONFIGURED_ROUTER_DURATION_SECONDS,
    MAXIMUM_EVALUATOR_MISSION_ITEMS,
    MSG_MISSION_ACK,
    MSG_MISSION_COUNT,
    MSG_MISSION_ITEM,
    MSG_MISSION_ITEM_INT,
    MSG_MISSION_REQUEST,
    MSG_MISSION_REQUEST_INT,
    MSG_MISSION_REQUEST_LIST,
    MSG_MISSION_SET_CURRENT,
    MSG_SET_MODE,
    PX4_AUTO_MISSION_CUSTOM_MODE,
    EvaluatorFrameDisposition,
    Px4GazeboQgcRouterError,
    RouterSummary,
    _source_allowed,
    _validated_duration_seconds,
    _validated_ports,
    classify_evaluator_frame,
    main,
    run_router,
)
from multidetect.qgc_readonly_bridge import (
    MAV_CMD_SET_MESSAGE_INTERVAL,
    MSG_COMMAND_LONG,
    MSG_HEARTBEAT,
    MSG_PARAM_REQUEST_LIST,
    MavlinkFrameDecoder,
)

EVALUATOR_SYSTEM_ID = 42
EVALUATOR_COMPONENT_ID = 201


def _v2_frame(
    message_id: int,
    payload: bytes,
    *,
    source_system_id: int,
    source_component_id: int,
) -> bytes:
    return (
        bytes(
            [
                0xFD,
                len(payload),
                0,
                0,
                1,
                source_system_id,
                source_component_id,
                message_id & 0xFF,
                (message_id >> 8) & 0xFF,
                (message_id >> 16) & 0xFF,
            ]
        )
        + payload
        + b"\x00\x00"
    )


def _decode(encoded: bytes):
    frames = MavlinkFrameDecoder().feed(encoded)
    assert len(frames) == 1
    return frames[0]


def _heartbeat(
    *,
    source_system_id: int,
    source_component_id: int,
    mav_type: int = MAV_TYPE_GCS,
) -> bytes:
    payload = bytearray(9)
    payload[4] = mav_type
    payload[5] = 8  # MAV_AUTOPILOT_INVALID for a GCS producer.
    payload[8] = 3
    return _v2_frame(
        MSG_HEARTBEAT,
        bytes(payload),
        source_system_id=source_system_id,
        source_component_id=source_component_id,
    )


def _command_long(
    command_id: int,
    *,
    source_system_id: int,
    source_component_id: int,
    parameters: tuple[float, ...] = (),
    target_system: int = 1,
    target_component: int = 1,
    confirmation: int = 0,
) -> bytes:
    payload = bytearray(33)
    parameter_values = (*parameters, *(0.0 for _ in range(7 - len(parameters))))
    payload[0:28] = struct.pack("<7f", *parameter_values[:7])
    payload[28:30] = struct.pack("<H", command_id)
    payload[30] = target_system
    payload[31] = target_component
    payload[32] = confirmation
    return _v2_frame(
        MSG_COMMAND_LONG,
        bytes(payload),
        source_system_id=source_system_id,
        source_component_id=source_component_id,
    )


def _mission_count(
    count: int,
    *,
    source_system_id: int = EVALUATOR_SYSTEM_ID,
    source_component_id: int = EVALUATOR_COMPONENT_ID,
    target_system: int = 1,
    target_component: int = 1,
    mission_type: int | None = 0,
) -> bytes:
    payload = struct.pack("<HBB", count, target_system, target_component)
    if mission_type is not None:
        payload += bytes([mission_type])
    return _v2_frame(
        MSG_MISSION_COUNT,
        payload,
        source_system_id=source_system_id,
        source_component_id=source_component_id,
    )


def _mission_item(
    *,
    message_id: int,
    sequence: int,
    command: int,
    frame: int,
    latitude: float = 47.397742,
    longitude: float = 8.545594,
    altitude_m: float = 50.0,
    parameters: tuple[float, ...] = (),
    current: int | None = None,
    autocontinue: int = 1,
    target_system: int = 1,
    target_component: int = 1,
    mission_type: int | None = 0,
    source_system_id: int = EVALUATOR_SYSTEM_ID,
    source_component_id: int = EVALUATOR_COMPONENT_ID,
) -> bytes:
    parameter_values = (*parameters, *(0.0 for _ in range(4 - len(parameters))))
    current_value = (1 if sequence == 0 else 0) if current is None else current
    common_tail = (
        sequence,
        command,
        target_system,
        target_component,
        frame,
        current_value,
        autocontinue,
    )
    if message_id == MSG_MISSION_ITEM_INT:
        payload = struct.pack(
            "<ffffiifHHBBBBB",
            *parameter_values[:4],
            round(latitude * 10_000_000),
            round(longitude * 10_000_000),
            altitude_m,
            *common_tail,
        )
    elif message_id == MSG_MISSION_ITEM:
        payload = struct.pack(
            "<fffffffHHBBBBB",
            *parameter_values[:4],
            latitude,
            longitude,
            altitude_m,
            *common_tail,
        )
    else:  # pragma: no cover - helper misuse
        raise AssertionError(f"unsupported mission item message id: {message_id}")
    if mission_type is not None:
        payload += bytes([mission_type])
    return _v2_frame(
        message_id,
        payload,
        source_system_id=source_system_id,
        source_component_id=source_component_id,
    )


def _mission_request_list(
    *,
    target_system: int = 1,
    target_component: int = 1,
    mission_type: int | None = 0,
) -> bytes:
    payload = bytes([target_system, target_component])
    if mission_type is not None:
        payload += bytes([mission_type])
    return _v2_frame(
        MSG_MISSION_REQUEST_LIST,
        payload,
        source_system_id=EVALUATOR_SYSTEM_ID,
        source_component_id=EVALUATOR_COMPONENT_ID,
    )


def _mission_request(
    message_id: int,
    sequence: int,
    *,
    target_system: int = 1,
    target_component: int = 1,
    mission_type: int | None = 0,
) -> bytes:
    payload = struct.pack("<HBB", sequence, target_system, target_component)
    if mission_type is not None:
        payload += bytes([mission_type])
    return _v2_frame(
        message_id,
        payload,
        source_system_id=EVALUATOR_SYSTEM_ID,
        source_component_id=EVALUATOR_COMPONENT_ID,
    )


def _mission_ack(
    *,
    target_system: int = 1,
    target_component: int = 1,
    ack_type: int = 0,
    mission_type: int | None = 0,
) -> bytes:
    payload = bytes([target_system, target_component, ack_type])
    if mission_type is not None:
        payload += bytes([mission_type])
    return _v2_frame(
        MSG_MISSION_ACK,
        payload,
        source_system_id=EVALUATOR_SYSTEM_ID,
        source_component_id=EVALUATOR_COMPONENT_ID,
    )


def _mission_set_current(
    sequence: int,
    *,
    target_system: int = 1,
    target_component: int = 1,
) -> bytes:
    return _v2_frame(
        MSG_MISSION_SET_CURRENT,
        struct.pack("<HBB", sequence, target_system, target_component),
        source_system_id=EVALUATOR_SYSTEM_ID,
        source_component_id=EVALUATOR_COMPONENT_ID,
    )


def _set_mode(
    *,
    custom_mode: int = PX4_AUTO_MISSION_CUSTOM_MODE,
    target_system: int = 1,
    base_mode: int = 1,
) -> bytes:
    return _v2_frame(
        MSG_SET_MODE,
        struct.pack("<IBB", custom_mode, target_system, base_mode),
        source_system_id=EVALUATOR_SYSTEM_ID,
        source_component_id=EVALUATOR_COMPONENT_ID,
    )


def _with_payload(encoded: bytes, payload: bytes) -> bytes:
    frame = _decode(encoded)
    return _v2_frame(
        frame.message_id,
        payload,
        source_system_id=frame.source_system_id,
        source_component_id=frame.source_component_id,
    )


def _qgc_parameter_request() -> bytes:
    return _v2_frame(
        MSG_PARAM_REQUEST_LIST,
        bytes([1, 1]),
        source_system_id=255,
        source_component_id=190,
    )


def _bound_udp_socket() -> tuple[socket.socket, int]:
    channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    channel.bind(("127.0.0.1", 0))
    channel.settimeout(1.0)
    return channel, channel.getsockname()[1]


def _unused_udp_port(excluded: set[int]) -> int:
    while True:
        channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            channel.bind(("127.0.0.1", 0))
            port = channel.getsockname()[1]
        finally:
            channel.close()
        if port != 14550 and port not in excluded:
            return port


def test_evaluator_classifier_allows_identity_bound_bounded_control_and_mission() -> None:
    heartbeat = _decode(
        _heartbeat(
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
        )
    )
    arm = _decode(
        _command_long(
            MAV_CMD_COMPONENT_ARM_DISARM,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(1.0, 21196.0),
        )
    )
    transition = _decode(
        _command_long(
            MAV_CMD_DO_VTOL_TRANSITION,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(4.0,),
        )
    )
    wrong_command = _decode(
        _command_long(
            MAV_CMD_SET_MESSAGE_INTERVAL,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
        )
    )
    wrong_identity = _decode(
        _command_long(
            MAV_CMD_COMPONENT_ARM_DISARM,
            source_system_id=EVALUATOR_SYSTEM_ID + 1,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(1.0, 21196.0),
        )
    )
    autopilot_heartbeat = _decode(
        _heartbeat(
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            mav_type=2,
        )
    )

    def classify(frame):
        return classify_evaluator_frame(
            frame,
            evaluator_system_id=EVALUATOR_SYSTEM_ID,
            evaluator_component_id=EVALUATOR_COMPONENT_ID,
        )

    assert classify(heartbeat) is EvaluatorFrameDisposition.GCS_HEARTBEAT_TO_PX4
    assert classify(arm) is EvaluatorFrameDisposition.CONTROL_COMMAND_TO_PX4
    assert classify(transition) is EvaluatorFrameDisposition.CONTROL_COMMAND_TO_PX4
    assert classify(wrong_command) is EvaluatorFrameDisposition.BLOCKED
    assert classify(wrong_identity) is EvaluatorFrameDisposition.BLOCKED
    assert classify(autopilot_heartbeat) is EvaluatorFrameDisposition.BLOCKED

    # MAVLink 2 trims the final zero-valued COMMAND_LONG confirmation byte.
    # This is the exact canonical wire shape emitted by pymavlink for the
    # attached evaluator's ARM/DISARM and transition commands.
    canonical_arm_payload = arm.payload[:-1]
    assert len(canonical_arm_payload) == 32
    assert (
        classify(_decode(_with_payload(arm.encoded, canonical_arm_payload)))
        is EvaluatorFrameDisposition.CONTROL_COMMAND_TO_PX4
    )
    canonical_broadcast_arm_payload = canonical_arm_payload[:-1]
    assert len(canonical_broadcast_arm_payload) == 31
    assert (
        classify(_decode(_with_payload(arm.encoded, canonical_broadcast_arm_payload)))
        is EvaluatorFrameDisposition.CONTROL_COMMAND_TO_PX4
    )
    assert (
        classify(_decode(_with_payload(arm.encoded, canonical_broadcast_arm_payload[:-1])))
        is EvaluatorFrameDisposition.BLOCKED
    )

    mission_frames = (
        # MAVLink 2 permits mission_type to be omitted when it is the zero-valued
        # base mission type.  Target component 0 is the standard broadcast target.
        _mission_count(5, target_component=0, mission_type=None),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=MAV_CMD_NAV_VTOL_TAKEOFF,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            current=1,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM,
            sequence=1,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT,
            current=0,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=1,
            command=MAV_CMD_DO_VTOL_TRANSITION,
            frame=MAV_FRAME_MISSION,
            latitude=0.0,
            longitude=0.0,
            altitude_m=0.0,
            parameters=(4.0,),
            current=0,
        ),
        _mission_request_list(target_component=0),
        _mission_request(MSG_MISSION_REQUEST, 1),
        _mission_request(MSG_MISSION_REQUEST_INT, 1),
        _mission_ack(target_component=0),
        _mission_set_current(0, target_component=0),
        _set_mode(),
    )
    for encoded in mission_frames:
        assert classify(_decode(encoded)) is EvaluatorFrameDisposition.MISSION_PROTOCOL_TO_PX4

    # Exercise the actual MAVLink 2 zero-tail wire shape, not merely helper
    # payloads that retain explicit zero-valued component/type bytes.
    canonical_broadcast_frames = (
        _mission_count(5, target_component=0),
        _mission_request_list(target_component=0),
        _mission_request(MSG_MISSION_REQUEST, 1, target_component=0),
        _mission_request(MSG_MISSION_REQUEST_INT, 1, target_component=0),
        _mission_ack(target_component=0),
        _mission_set_current(0, target_component=0),
    )
    expected_payload_lengths = (3, 1, 3, 3, 1, 3)
    for encoded, expected_length in zip(
        canonical_broadcast_frames,
        expected_payload_lengths,
        strict=True,
    ):
        frame = _decode(encoded)
        canonical_payload = frame.payload.rstrip(b"\0")
        assert len(canonical_payload) == expected_length
        assert (
            classify(_decode(_with_payload(encoded, canonical_payload)))
            is EvaluatorFrameDisposition.MISSION_PROTOCOL_TO_PX4
        )

    canonical_component_one_ack = _mission_ack(target_component=1)
    canonical_component_one_ack_payload = _decode(canonical_component_one_ack).payload.rstrip(b"\0")
    assert len(canonical_component_one_ack_payload) == 2
    assert (
        classify(
            _decode(
                _with_payload(
                    canonical_component_one_ack,
                    canonical_component_one_ack_payload,
                )
            )
        )
        is EvaluatorFrameDisposition.MISSION_PROTOCOL_TO_PX4
    )


def test_evaluator_mission_classifier_rejects_wrong_target_type_and_truncation() -> None:
    def classify(encoded: bytes) -> EvaluatorFrameDisposition:
        return classify_evaluator_frame(
            _decode(encoded),
            evaluator_system_id=EVALUATOR_SYSTEM_ID,
            evaluator_component_id=EVALUATOR_COMPONENT_ID,
        )

    target_cases = (
        (_mission_count(5), 2, 3, 4),
        (
            _mission_item(
                message_id=MSG_MISSION_ITEM_INT,
                sequence=0,
                command=MAV_CMD_NAV_VTOL_TAKEOFF,
                frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            ),
            32,
            33,
            37,
        ),
        (
            _mission_item(
                message_id=MSG_MISSION_ITEM,
                sequence=0,
                command=MAV_CMD_NAV_WAYPOINT,
                frame=MAV_FRAME_GLOBAL_RELATIVE_ALT,
            ),
            32,
            33,
            37,
        ),
        (_mission_request_list(), 0, 1, 2),
        (_mission_request(MSG_MISSION_REQUEST, 0), 2, 3, 4),
        (_mission_request(MSG_MISSION_REQUEST_INT, 0), 2, 3, 4),
        (_mission_ack(), 0, 1, 3),
        (_mission_set_current(0), 2, 3, None),
    )
    for encoded, system_offset, component_offset, type_offset in target_cases:
        payload = bytearray(_decode(encoded).payload)
        assert classify(encoded) is EvaluatorFrameDisposition.MISSION_PROTOCOL_TO_PX4

        wrong_system = payload.copy()
        wrong_system[system_offset] = 2
        assert (
            classify(_with_payload(encoded, bytes(wrong_system)))
            is EvaluatorFrameDisposition.BLOCKED
        )

        wrong_component = payload.copy()
        wrong_component[component_offset] = 2
        assert (
            classify(_with_payload(encoded, bytes(wrong_component)))
            is EvaluatorFrameDisposition.BLOCKED
        )

        # Removing the target-system byte is always malformed.  A missing
        # trailing target-component byte can be the canonical MAVLink 2
        # representation of component 0 and is covered by the positive cases.
        assert (
            classify(_with_payload(encoded, bytes(payload[:system_offset])))
            is EvaluatorFrameDisposition.BLOCKED
        )

        if type_offset is not None:
            wrong_type = payload.copy()
            wrong_type[type_offset] = 1
            assert (
                classify(_with_payload(encoded, bytes(wrong_type)))
                is EvaluatorFrameDisposition.BLOCKED
            )

    wrong_identity = _mission_count(
        5,
        source_system_id=EVALUATOR_SYSTEM_ID + 1,
    )
    assert classify(wrong_identity) is EvaluatorFrameDisposition.BLOCKED


def test_evaluator_mission_classifier_rejects_unbounded_or_unsafe_values() -> None:
    def classify(encoded: bytes) -> EvaluatorFrameDisposition:
        return classify_evaluator_frame(
            _decode(encoded),
            evaluator_system_id=EVALUATOR_SYSTEM_ID,
            evaluator_component_id=EVALUATOR_COMPONENT_ID,
        )

    blocked_frames = (
        _mission_count(0),
        _mission_count(MAXIMUM_EVALUATOR_MISSION_ITEMS + 1),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=MAXIMUM_EVALUATOR_MISSION_ITEMS,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            current=0,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=183,  # MAV_CMD_DO_SET_SERVO is never a mission command here.
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_MISSION,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            latitude=91.0,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            altitude_m=float("inf"),
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=1,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            current=1,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=MAV_CMD_NAV_WAYPOINT,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            autocontinue=0,
        ),
        _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=1,
            command=MAV_CMD_DO_VTOL_TRANSITION,
            frame=MAV_FRAME_MISSION,
            latitude=0.0,
            longitude=0.0,
            altitude_m=0.0,
            parameters=(2.0,),
            current=0,
        ),
        _mission_request(
            MSG_MISSION_REQUEST_INT,
            MAXIMUM_EVALUATOR_MISSION_ITEMS,
        ),
        _mission_ack(ack_type=1),
        _mission_set_current(MAXIMUM_EVALUATOR_MISSION_ITEMS),
        _set_mode(custom_mode=0),
        _set_mode(target_system=2),
        _set_mode(base_mode=0),
        _with_payload(_set_mode(), _decode(_set_mode()).payload[:5]),
        _v2_frame(
            45,  # MISSION_CLEAR_ALL is deliberately absent from the allowlist.
            bytes([1, 1, 0]),
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
        ),
        _command_long(
            MAV_CMD_COMPONENT_ARM_DISARM,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(2.0,),
        ),
        _command_long(
            MAV_CMD_DO_VTOL_TRANSITION,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(4.0,),
            confirmation=1,
        ),
    )
    for encoded in blocked_frames:
        assert classify(encoded) is EvaluatorFrameDisposition.BLOCKED


def test_ports_duration_acknowledgement_and_sources_fail_closed() -> None:
    ports = (14669, 14667, 14671, 14670, 14668, 18570)
    _validated_ports(ports)
    with pytest.raises(Px4GazeboQgcRouterError, match="distinct"):
        _validated_ports((14669, 14669, 14671, 14670, 14668, 18570))
    with pytest.raises(Px4GazeboQgcRouterError, match="14550 is protected"):
        _validated_ports((14550, 14667, 14671, 14670, 14668, 18570))
    with pytest.raises(Px4GazeboQgcRouterError, match="1024..65535"):
        _validated_ports((1023, 14667, 14671, 14670, 14668, 18570))

    kwargs = {
        "qgc_port": 14669,
        "qgc_router_port": 14667,
        "evaluator_port": 14671,
        "evaluator_router_port": 14670,
        "px4_telemetry_port": 14668,
        "px4_input_port": 18570,
        "evaluator_system_id": EVALUATOR_SYSTEM_ID,
        "evaluator_component_id": EVALUATOR_COMPONENT_ID,
    }
    with pytest.raises(Px4GazeboQgcRouterError, match="acknowledgement"):
        run_router(**kwargs, duration_seconds=1.0)
    assert _validated_duration_seconds(1.0) == 1.0
    assert _validated_duration_seconds(600.0) == 600.0
    assert _validated_duration_seconds(
        1200.0,
        maximum=MAXIMUM_CONFIGURED_ROUTER_DURATION_SECONDS,
    ) == 1200.0
    with pytest.raises(Px4GazeboQgcRouterError, match="1..3600"):
        _validated_duration_seconds(
            MAXIMUM_CONFIGURED_ROUTER_DURATION_SECONDS + 1.0,
            maximum=MAXIMUM_CONFIGURED_ROUTER_DURATION_SECONDS,
        )
    with pytest.raises(Px4GazeboQgcRouterError, match="1..600"):
        run_router(
            **kwargs,
            duration_seconds=0.0,
            acknowledge_owned_disposable_sitl=True,
        )
    with pytest.raises(Px4GazeboQgcRouterError, match="1..600"):
        run_router(
            **kwargs,
            duration_seconds=601.0,
            acknowledge_owned_disposable_sitl=True,
        )
    with pytest.raises(Px4GazeboQgcRouterError, match="1..600"):
        _validated_duration_seconds(True)
    with pytest.raises(
        Px4GazeboQgcRouterError, match=r"ready_event must provide clear\(\) and set"
    ):
        run_router(
            **kwargs,
            duration_seconds=1.0,
            acknowledge_owned_disposable_sitl=True,
            ready_event=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(Px4GazeboQgcRouterError, match="stop_event must provide is_set"):
        run_router(
            **kwargs,
            duration_seconds=1.0,
            acknowledge_owned_disposable_sitl=True,
            stop_event=object(),  # type: ignore[arg-type]
        )

    summary = RouterSummary(started_monotonic_s=time.monotonic())
    assert not _source_allowed(
        ("192.0.2.1", 14669),
        expected_port=14669,
        summary=summary,
    )
    assert not _source_allowed(
        ("127.0.0.1", 14666),
        expected_port=14669,
        summary=summary,
    )
    assert summary.non_loopback_datagrams_blocked == 1
    assert summary.unexpected_udp_endpoint_datagrams_blocked == 1


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.usefixtures("require_loopback_sockets")
def test_router_mirrors_px4_and_enforces_independent_return_policies(trace) -> None:
    import hashlib

    from multidetect.clock import HighResolutionSystemClock

    clock = HighResolutionSystemClock(session_id="router-fixture") if trace else None
    qgc, qgc_port = _bound_udp_socket()
    evaluator, evaluator_port = _bound_udp_socket()
    px4_input, px4_input_port = _bound_udp_socket()
    excluded = {qgc_port, evaluator_port, px4_input_port, 14550}
    qgc_router_port = _unused_udp_port(excluded)
    excluded.add(qgc_router_port)
    evaluator_router_port = _unused_udp_port(excluded)
    excluded.add(evaluator_router_port)
    px4_telemetry_port = _unused_udp_port(excluded)
    result: dict[str, object] = {}
    failures: list[BaseException] = []
    ready_event = threading.Event()

    def target() -> None:
        try:
            result.update(
                run_router(
                    qgc_port=qgc_port,
                    qgc_router_port=qgc_router_port,
                    evaluator_port=evaluator_port,
                    evaluator_router_port=evaluator_router_port,
                    px4_telemetry_port=px4_telemetry_port,
                    px4_input_port=px4_input_port,
                    evaluator_system_id=EVALUATOR_SYSTEM_ID,
                    evaluator_component_id=EVALUATOR_COMPONENT_ID,
                    duration_seconds=1.0,
                    acknowledge_owned_disposable_sitl=True,
                    ready_event=ready_event,
                    observation_clock=clock,
                )
            )
        except BaseException as error:  # pragma: no cover - reported in main thread
            failures.append(error)

    router_thread = threading.Thread(target=target, daemon=True)
    router_thread.start()
    assert ready_event.wait(1.0)
    px4_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        px4_frame = _heartbeat(source_system_id=1, source_component_id=1, mav_type=2)
        px4_sender.sendto(px4_frame, ("127.0.0.1", px4_telemetry_port))
        assert qgc.recvfrom(8192)[0] == px4_frame
        assert evaluator.recvfrom(8192)[0] == px4_frame

        qgc_read = _qgc_parameter_request()
        qgc_blocked = _command_long(
            MAV_CMD_COMPONENT_ARM_DISARM,
            source_system_id=255,
            source_component_id=190,
            parameters=(1.0, 21196.0),
        )
        qgc_mission_blocked = _mission_count(
            2,
            source_system_id=255,
            source_component_id=190,
        )
        evaluator_heartbeat = _heartbeat(
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
        )
        evaluator_arm = _command_long(
            MAV_CMD_COMPONENT_ARM_DISARM,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(1.0, 21196.0),
        )
        evaluator_transition = _command_long(
            MAV_CMD_DO_VTOL_TRANSITION,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(4.0,),
        )
        evaluator_mission_count = _mission_count(2)
        evaluator_mission_item = _mission_item(
            message_id=MSG_MISSION_ITEM_INT,
            sequence=0,
            command=MAV_CMD_NAV_VTOL_TAKEOFF,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        )
        evaluator_blocked = _command_long(
            MAV_CMD_SET_MESSAGE_INTERVAL,
            source_system_id=EVALUATOR_SYSTEM_ID,
            source_component_id=EVALUATOR_COMPONENT_ID,
        )
        evaluator_wrong_identity = _command_long(
            MAV_CMD_COMPONENT_ARM_DISARM,
            source_system_id=EVALUATOR_SYSTEM_ID + 1,
            source_component_id=EVALUATOR_COMPONENT_ID,
            parameters=(1.0, 21196.0),
        )
        qgc.sendto(qgc_read, ("127.0.0.1", qgc_router_port))
        qgc.sendto(qgc_blocked, ("127.0.0.1", qgc_router_port))
        qgc.sendto(qgc_mission_blocked, ("127.0.0.1", qgc_router_port))
        for frame in (
            evaluator_heartbeat,
            evaluator_arm,
            evaluator_transition,
            evaluator_mission_count,
            evaluator_mission_item,
            evaluator_blocked,
            evaluator_wrong_identity,
        ):
            evaluator.sendto(frame, ("127.0.0.1", evaluator_router_port))

        forwarded = {px4_input.recvfrom(8192)[0] for _ in range(6)}
        assert forwarded == {
            qgc_read,
            evaluator_heartbeat,
            evaluator_arm,
            evaluator_transition,
            evaluator_mission_count,
            evaluator_mission_item,
        }
        px4_input.settimeout(0.2)
        with pytest.raises(socket.timeout):
            px4_input.recvfrom(8192)
    finally:
        px4_sender.close()
        px4_input.close()
        evaluator.close()
        qgc.close()

    router_thread.join(timeout=2.0)
    assert not router_thread.is_alive()
    assert failures == []
    assert result["px4_frames_received"] == 1
    if trace:
        receipt = result["telemetry_receipts"]
        assert receipt["host_clock_identity"] == clock.identity()
        assert receipt["samples_seen"] == 1
        assert receipt["samples_dropped"] == 0
        row = receipt["samples"][0]
        assert row["frame_sha256"] == hashlib.sha256(px4_frame).hexdigest()
        assert row["message_id"] == 0
        assert row["source_system"] == row["source_component"] == 1
        assert row["router_decoded_host_s"] <= row["router_forwarded_host_s"]
        assert receipt["source_send_timing_claimed"] is False
    else:
        assert "telemetry_receipts" not in result
    assert result["px4_frames_forwarded_to_qgc"] == 1
    assert result["px4_frames_forwarded_to_evaluator"] == 1
    assert result["qgc_read_only_frames_forwarded"] == 1
    assert result["qgc_forbidden_frames_blocked"] == 2
    assert result["evaluator_gcs_heartbeats_forwarded"] == 1
    assert result["evaluator_control_commands_forwarded"] == 2
    assert result["evaluator_mission_frames_forwarded"] == 2
    assert result["evaluator_mission_items_forwarded"] == 1
    assert result["evaluator_auto_mission_mode_changes_forwarded"] == 0
    assert result["evaluator_forbidden_frames_blocked"] == 2
    assert result["evaluator_blocked_message_ids"] == {str(MSG_COMMAND_LONG): 2}
    assert result["evaluator_forwarded_mission_message_ids"] == {
        str(MSG_MISSION_COUNT): 1,
        str(MSG_MISSION_ITEM_INT): 1,
    }
    assert result["evaluator_forwarded_mission_command_ids"] == {str(MAV_CMD_NAV_VTOL_TAKEOFF): 1}
    assert result["evaluator_forwarded_mission_item_sequences"] == {"0": 1}
    assert result["qgc_policy_reuses_classify_qgc_frame"] is True
    assert result["evaluator_allowed_command_ids"] == [
        MAV_CMD_COMPONENT_ARM_DISARM,
        MAV_CMD_DO_VTOL_TRANSITION,
    ]
    assert result["evaluator_allowed_message_ids"] == sorted(EVALUATOR_ALLOWED_MESSAGE_IDS)
    assert result["all_udp_endpoints_loopback"] is True
    assert result["all_ports_distinct"] is True
    assert result["protected_udp_14550_used"] is False
    assert result["px4_telemetry_frames_rewritten"] is False
    assert result["qgc_flight_commands_allowed"] is False
    assert result["qgc_mission_writes_allowed"] is False
    assert result["evaluator_bounded_mission_transaction_allowed"] is True
    assert result["evaluator_mission_clear_all_allowed"] is False
    assert result["evaluator_set_mode_restricted_to_px4_auto_mission"] is True
    assert result["evaluator_mission_target_component_ids"] == [0, 1]
    assert result["maximum_router_duration_seconds"] == 600
    assert result["evaluator_arbitrary_commands_allowed"] is False
    assert result["evaluator_identity_enforced"] is True
    assert result["acknowledge_owned_disposable_sitl"] is True
    assert result["software_only"] is True
    assert result["real_pixhawk_contacted"] is False
    assert result["physical_actuator_outputs_present"] is False
    assert result["termination_reason"] == "duration_elapsed"
    assert len(canonical_sha256(result)) == 64
    assert result["ready_barrier"] == {
        "reached": True,
        "ready_latency_seconds": result["ready_barrier"]["ready_latency_seconds"],
        "expected_udp_socket_count": 3,
        "bound_udp_socket_count": 3,
        "all_udp_sockets_bound_before_ready": True,
        "ready_event_supplied": True,
        "ready_event_set": True,
        "stop_event_supplied": False,
    }
    assert isinstance(result["ready_barrier"]["ready_latency_seconds"], float)
    assert result["ready_barrier"]["ready_latency_seconds"] >= 0.0


@pytest.mark.usefixtures("require_loopback_sockets")
def test_router_stop_event_terminates_after_ready_barrier() -> None:
    excluded = {14550}
    ports: list[int] = []
    for _ in range(6):
        port = _unused_udp_port(excluded)
        excluded.add(port)
        ports.append(port)
    ready_event = threading.Event()
    stop_event = threading.Event()
    result: dict[str, object] = {}
    failures: list[BaseException] = []

    def target() -> None:
        try:
            result.update(
                run_router(
                    qgc_port=ports[0],
                    qgc_router_port=ports[1],
                    evaluator_port=ports[2],
                    evaluator_router_port=ports[3],
                    px4_telemetry_port=ports[4],
                    px4_input_port=ports[5],
                    evaluator_system_id=EVALUATOR_SYSTEM_ID,
                    evaluator_component_id=EVALUATOR_COMPONENT_ID,
                    duration_seconds=30.0,
                    acknowledge_owned_disposable_sitl=True,
                    ready_event=ready_event,
                    stop_event=stop_event,
                )
            )
        except BaseException as error:  # pragma: no cover - reported below
            failures.append(error)

    router_thread = threading.Thread(target=target, daemon=True)
    router_thread.start()
    assert ready_event.wait(1.0)
    stop_requested_at = time.monotonic()
    stop_event.set()
    router_thread.join(timeout=1.0)

    assert not router_thread.is_alive()
    assert failures == []
    assert time.monotonic() - stop_requested_at < 1.0
    assert result["termination_reason"] == "stop_event_set"
    assert result["ready_barrier"]["reached"] is True
    assert result["ready_barrier"]["bound_udp_socket_count"] == 3
    assert result["ready_barrier"]["all_udp_sockets_bound_before_ready"] is True
    assert result["ready_barrier"]["ready_event_supplied"] is True
    assert result["ready_barrier"]["ready_event_set"] is True
    assert result["ready_barrier"]["stop_event_supplied"] is True


@pytest.mark.usefixtures("require_loopback_sockets")
def test_router_bind_failure_never_signals_ready_and_releases_prior_socket() -> None:
    occupied_qgc_router, occupied_qgc_router_port = _bound_udp_socket()
    excluded = {14550, occupied_qgc_router_port}
    ports: list[int] = []
    for _ in range(5):
        port = _unused_udp_port(excluded)
        excluded.add(port)
        ports.append(port)
    ready_event = threading.Event()
    ready_event.set()

    try:
        with pytest.raises(OSError):
            run_router(
                qgc_port=ports[0],
                qgc_router_port=occupied_qgc_router_port,
                evaluator_port=ports[1],
                evaluator_router_port=ports[2],
                px4_telemetry_port=ports[3],
                px4_input_port=ports[4],
                evaluator_system_id=EVALUATOR_SYSTEM_ID,
                evaluator_component_id=EVALUATOR_COMPONENT_ID,
                duration_seconds=1.0,
                acknowledge_owned_disposable_sitl=True,
                ready_event=ready_event,
            )
        assert not ready_event.is_set()

        rebound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rebound.bind(("127.0.0.1", ports[3]))
        finally:
            rebound.close()
    finally:
        occupied_qgc_router.close()


def test_cli_acknowledgement_gate_emits_complete_safe_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])

    assert exit_code == 2
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == 2
    assert document["event"] == "px4_gazebo_qgc_router_failed"
    assert document["acknowledge_owned_disposable_sitl"] is False
    assert document["software_only"] is True
    assert document["real_pixhawk_contacted"] is False
    assert document["physical_actuator_outputs_present"] is False
    assert "acknowledgement is required" in document["error"]

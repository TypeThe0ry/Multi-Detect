from __future__ import annotations

import struct

import pytest

from multidetect.px4_sitl_qgc_router import (
    JETSON_COMPONENT_ID,
    MSG_TUNNEL,
    OPERATOR_TUNNEL_PAYLOAD_TYPE,
    PX4_SYSTEM_ID,
    QgcFrameDisposition,
    SitlQgcRouterError,
    _validated_ports,
    classify_qgc_frame,
    tunnel_header,
)
from multidetect.qgc_readonly_bridge import (
    FTP_OPCODE_BURST_READ_FILE,
    FTP_OPCODE_CALC_FILE_CRC32,
    FTP_OPCODE_LIST_DIRECTORY,
    FTP_OPCODE_LIST_DIRECTORY_WITH_TIME,
    FTP_OPCODE_NONE,
    FTP_OPCODE_OPEN_FILE_RO,
    FTP_OPCODE_READ_FILE,
    FTP_OPCODE_RESET_SESSIONS,
    FTP_OPCODE_TERMINATE_SESSION,
    MAV_CMD_REQUEST_MESSAGE,
    MAV_CMD_RUN_PREARM_CHECKS,
    MSG_COMMAND_LONG,
    MSG_FILE_TRANSFER_PROTOCOL,
    MSG_PARAM_REQUEST_LIST,
    MSG_SYSTEM_TIME,
    MavlinkFrameDecoder,
)


def _v2_frame(
    message_id: int,
    payload: bytes,
    *,
    source_system_id: int = 255,
    source_component_id: int = 190,
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


def _command_long(command_id: int) -> bytes:
    payload = bytearray(33)
    payload[28:30] = struct.pack("<H", command_id)
    return _v2_frame(MSG_COMMAND_LONG, bytes(payload))


def _ftp_request(
    opcode: int,
    *,
    target_system_id: int = PX4_SYSTEM_ID,
    target_component_id: int = 1,
    truncate_to: int | None = None,
) -> bytes:
    payload = bytearray(15)
    payload[1] = target_system_id
    payload[2] = target_component_id
    payload[6] = opcode
    if truncate_to is not None:
        payload = payload[:truncate_to]
    return _v2_frame(MSG_FILE_TRANSFER_PROTOCOL, bytes(payload))


def test_operator_tunnel_is_local_only_and_never_forwarded_to_px4() -> None:
    payload = (
        OPERATOR_TUNNEL_PAYLOAD_TYPE.to_bytes(2, "little")
        + bytes([PX4_SYSTEM_ID, JETSON_COMPONENT_ID, 32])
        + bytes(32)
    )
    frame = _decode(_v2_frame(MSG_TUNNEL, payload))

    header = tunnel_header(frame)
    assert header is not None
    assert header.payload_type == OPERATOR_TUNNEL_PAYLOAD_TYPE
    assert header.target_component_id == JETSON_COMPONENT_ID
    assert classify_qgc_frame(frame) is QgcFrameDisposition.OPERATOR_TUNNEL_LOCAL_ONLY


def test_router_forwards_only_read_requests_and_blocks_control() -> None:
    parameter_read = _decode(_v2_frame(MSG_PARAM_REQUEST_LIST, bytes([PX4_SYSTEM_ID, 1])))
    request_message = _decode(_command_long(MAV_CMD_REQUEST_MESSAGE))
    arm = _decode(_command_long(400))
    wrong_source = _decode(
        _v2_frame(
            MSG_PARAM_REQUEST_LIST,
            bytes([PX4_SYSTEM_ID, 1]),
            source_system_id=42,
        )
    )

    assert classify_qgc_frame(parameter_read) is QgcFrameDisposition.READ_ONLY_TO_PX4
    assert classify_qgc_frame(request_message) is QgcFrameDisposition.READ_ONLY_TO_PX4
    assert classify_qgc_frame(arm) is QgcFrameDisposition.BLOCKED
    assert classify_qgc_frame(wrong_source) is QgcFrameDisposition.BLOCKED


def test_wrong_operator_tunnel_target_is_blocked() -> None:
    payload = (
        OPERATOR_TUNNEL_PAYLOAD_TYPE.to_bytes(2, "little")
        + bytes([PX4_SYSTEM_ID, 1, 32])
        + bytes(32)
    )

    assert (
        classify_qgc_frame(_decode(_v2_frame(MSG_TUNNEL, payload))) is QgcFrameDisposition.BLOCKED
    )


@pytest.mark.parametrize(
    "opcode",
    [
        FTP_OPCODE_NONE,
        FTP_OPCODE_TERMINATE_SESSION,
        FTP_OPCODE_RESET_SESSIONS,
        FTP_OPCODE_LIST_DIRECTORY,
        FTP_OPCODE_OPEN_FILE_RO,
        FTP_OPCODE_READ_FILE,
        FTP_OPCODE_CALC_FILE_CRC32,
        FTP_OPCODE_BURST_READ_FILE,
        FTP_OPCODE_LIST_DIRECTORY_WITH_TIME,
    ],
)
def test_router_allows_only_explicit_read_only_ftp_opcodes(opcode: int) -> None:
    frame = _decode(_ftp_request(opcode))

    assert frame.file_transfer_protocol_target == (PX4_SYSTEM_ID, 1)
    assert frame.file_transfer_protocol_opcode == opcode
    assert classify_qgc_frame(frame) is QgcFrameDisposition.READ_ONLY_TO_PX4


@pytest.mark.parametrize("opcode", [6, 7, 8, 9, 10, 11, 12, 13, 17, 128, 129, 255])
def test_router_blocks_every_file_mutating_or_unknown_ftp_opcode(opcode: int) -> None:
    assert classify_qgc_frame(_decode(_ftp_request(opcode))) is QgcFrameDisposition.BLOCKED


def test_router_blocks_malformed_or_wrong_target_ftp_requests() -> None:
    truncated_reset = _decode(_ftp_request(FTP_OPCODE_RESET_SESSIONS, truncate_to=7))
    malformed = _decode(_ftp_request(FTP_OPCODE_OPEN_FILE_RO, truncate_to=6))
    wrong_system = _decode(_ftp_request(FTP_OPCODE_OPEN_FILE_RO, target_system_id=42))
    wrong_component = _decode(_ftp_request(FTP_OPCODE_OPEN_FILE_RO, target_component_id=191))

    assert truncated_reset.file_transfer_protocol_opcode == FTP_OPCODE_RESET_SESSIONS
    assert classify_qgc_frame(truncated_reset) is QgcFrameDisposition.READ_ONLY_TO_PX4
    assert malformed.file_transfer_protocol_opcode is None
    assert classify_qgc_frame(malformed) is QgcFrameDisposition.BLOCKED
    assert classify_qgc_frame(wrong_system) is QgcFrameDisposition.BLOCKED
    assert classify_qgc_frame(wrong_component) is QgcFrameDisposition.BLOCKED


def test_router_records_but_never_forwards_qgc_system_time() -> None:
    frame = _decode(_v2_frame(MSG_SYSTEM_TIME, bytes(12)))

    assert classify_qgc_frame(frame) is QgcFrameDisposition.BLOCKED_HOUSEKEEPING


def test_router_allows_qgc_prearm_status_diagnostic_but_not_arm() -> None:
    frame = _decode(_command_long(MAV_CMD_RUN_PREARM_CHECKS))
    arm = _decode(_command_long(400))

    assert classify_qgc_frame(frame) is QgcFrameDisposition.READ_ONLY_TO_PX4
    assert classify_qgc_frame(arm) is QgcFrameDisposition.BLOCKED


def test_router_ports_are_distinct_and_never_use_protected_14550() -> None:
    _validated_ports((14669, 14667, 14668, 18570))

    with pytest.raises(SitlQgcRouterError, match="distinct"):
        _validated_ports((14669, 14669, 14668, 18570))
    with pytest.raises(SitlQgcRouterError, match="14550 is protected"):
        _validated_ports((14550, 14667, 14668, 18570))

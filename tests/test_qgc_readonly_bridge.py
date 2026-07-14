from __future__ import annotations

import struct
from pathlib import Path

from multidetect.qgc_readonly_bridge import (
    MAV_CMD_REQUEST_MESSAGE,
    MSG_COMMAND_LONG,
    MSG_FILE_TRANSFER_PROTOCOL,
    MSG_HEARTBEAT,
    MSG_PARAM_REQUEST_LIST,
    MSG_PARAM_SET,
    READ_ONLY_FTP_OPCODES,
    MavlinkFrameDecoder,
    _recv_qgc_datagram,
    qgc_frame_is_read_only,
)


def _v1_frame(message_id: int, payload: bytes = b"") -> bytes:
    return bytes([0xFE, len(payload), 1, 255, 190, message_id]) + payload + b"\x00\x00"


def _v2_frame(message_id: int, payload: bytes = b"", *, signed: bool = False) -> bytes:
    incompatibility_flags = 1 if signed else 0
    header = bytes(
        [
            0xFD,
            len(payload),
            incompatibility_flags,
            0,
            1,
            255,
            190,
            message_id & 0xFF,
            (message_id >> 8) & 0xFF,
            (message_id >> 16) & 0xFF,
        ]
    )
    signature = bytes(range(13)) if signed else b""
    return header + payload + b"\x00\x00" + signature


def _command_long(command_id: int) -> bytes:
    payload = bytearray(33)
    payload[28:30] = struct.pack("<H", command_id)
    return _v2_frame(MSG_COMMAND_LONG, bytes(payload))


def _ftp_request(
    opcode: int,
    *,
    payload_length: int = 15,
    target_system: int = 1,
    target_component: int = 1,
) -> bytes:
    payload = bytearray(15)
    payload[1:3] = bytes([target_system, target_component])
    payload[6] = opcode
    return _v2_frame(MSG_FILE_TRANSFER_PROTOCOL, bytes(payload[:payload_length]))


def test_decoder_preserves_fragmented_v1_and_signed_v2_frames() -> None:
    first = _v1_frame(MSG_HEARTBEAT, b"123456789")
    second = _v2_frame(MSG_PARAM_REQUEST_LIST, b"\x01\x01", signed=True)
    decoder = MavlinkFrameDecoder()

    assert decoder.feed(b"noise" + first[:4]) == []
    frames = decoder.feed(first[4:] + second)

    assert [frame.encoded for frame in frames] == [first, second]
    assert [frame.version for frame in frames] == [1, 2]
    assert [frame.message_id for frame in frames] == [MSG_HEARTBEAT, MSG_PARAM_REQUEST_LIST]
    assert [(frame.source_system_id, frame.source_component_id) for frame in frames] == [
        (255, 190),
        (255, 190),
    ]
    assert decoder.discarded_bytes == 5


def test_read_only_policy_allows_reads_and_blocks_parameter_writes() -> None:
    decoder = MavlinkFrameDecoder()
    request, parameter_write = decoder.feed(
        _v2_frame(MSG_PARAM_REQUEST_LIST, b"\x01\x01") + _v2_frame(MSG_PARAM_SET, bytes(23))
    )

    assert qgc_frame_is_read_only(request) is True
    assert qgc_frame_is_read_only(parameter_write) is False


def test_read_only_policy_allows_request_message_but_blocks_arm_command() -> None:
    decoder = MavlinkFrameDecoder()
    request_message, arm = decoder.feed(_command_long(MAV_CMD_REQUEST_MESSAGE) + _command_long(400))

    assert request_message.command_long_id == MAV_CMD_REQUEST_MESSAGE
    assert qgc_frame_is_read_only(request_message) is True
    assert arm.command_long_id == 400
    assert qgc_frame_is_read_only(arm) is False


def test_read_only_policy_parses_ftp_and_blocks_all_mutating_unknown_or_malformed_opcodes() -> None:
    decoder = MavlinkFrameDecoder()

    for opcode in READ_ONLY_FTP_OPCODES:
        frame = decoder.feed(_ftp_request(opcode))[0]
        assert frame.file_transfer_protocol_target == (1, 1)
        assert frame.file_transfer_protocol_opcode == opcode
        assert qgc_frame_is_read_only(frame) is True

    for opcode in (6, 7, 8, 9, 10, 11, 12, 13, 17, 128, 129, 255):
        frame = decoder.feed(_ftp_request(opcode))[0]
        assert qgc_frame_is_read_only(frame) is False

    truncated_but_valid = decoder.feed(_ftp_request(2, payload_length=7))[0]
    assert truncated_but_valid.file_transfer_protocol_opcode == 2
    assert qgc_frame_is_read_only(truncated_but_valid) is True

    malformed = decoder.feed(_ftp_request(4, payload_length=6))[0]
    assert malformed.file_transfer_protocol_target is None
    assert malformed.file_transfer_protocol_opcode is None
    assert qgc_frame_is_read_only(malformed) is False

    wrong_target = decoder.feed(_ftp_request(4, target_system=2, target_component=1))[0]
    assert wrong_target.file_transfer_protocol_target == (2, 1)
    assert qgc_frame_is_read_only(wrong_target) is False


def test_qgc_udp_connection_reset_is_nonfatal() -> None:
    class _ResettingSocket:
        def recvfrom(self, size: int):
            assert size == 8192
            raise ConnectionResetError(10054, "QGC is not listening")

    assert _recv_qgc_datagram(_ResettingSocket()) is None  # type: ignore[arg-type]


def test_launcher_prefers_the_standalone_custom_qgc_deployment() -> None:
    launcher = (
        Path(__file__).resolve().parents[1] / "scripts" / "start_qgc_gr01_readonly.ps1"
    ).read_text(encoding="utf-8")

    assert "QGroundControl-MultiDetect\\build-multidetect-release\\staging" in launcher
    assert "bin\\MultiDetectGCS.exe" in launcher
    assert "[IO.Path]::GetFileNameWithoutExtension($QgcPath)" in launcher
    assert "-WorkingDirectory (Split-Path -Parent $QgcPath)" in launcher

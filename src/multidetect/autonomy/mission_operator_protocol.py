"""Version 2 mission bodies for the authenticated operator TUNNEL protocol.

The outer framing and key remain unchanged. Version 1 message numbers 1..27
retain their exact wire representation; only mission messages 28..37 use v2.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

MISSION_PROTOCOL_VERSION = 2
MAX_DOCUMENT_BYTES = 65_536
MAX_COMMAND_DOCUMENT_BYTES = 8_192
CHUNK_DATA_BYTES = 44
MAX_CHUNKS = (MAX_DOCUMENT_BYTES + CHUNK_DATA_BYTES - 1) // CHUNK_DATA_BYTES
CHUNK = struct.Struct(">QHHI32s")
ACK = struct.Struct(">QBHB")
CONTROL = struct.Struct(">QQ")
TIGHTEN = struct.Struct(">QQBd")
TELEMETRY_ACK = struct.Struct(">QH")
MISSION_TYPES = frozenset(range(28, 38))
REQUEST_TYPES = frozenset((28, 30, 31, 32, 33, 34, 37))


def mission_token(mission_id: str) -> int:
    """Stable mission binding, in network byte order, shared with the GCS."""
    return int.from_bytes(hashlib.sha256(mission_id.encode("utf-8")).digest()[:8], "big")


def validate_mission_body(message_type: int, body: bytes) -> None:
    if message_type not in MISSION_TYPES or not isinstance(body, bytes):
        raise ValueError("invalid mission message type or body")
    if message_type in (28, 35, 37):
        if not CHUNK.size < len(body) <= CHUNK.size + CHUNK_DATA_BYTES:
            raise ValueError("invalid mission chunk size")
        token, index, count, total, _ = CHUNK.unpack_from(body)
        expected_count = (total + CHUNK_DATA_BYTES - 1) // CHUNK_DATA_BYTES
        maximum = MAX_COMMAND_DOCUMENT_BYTES if message_type == 37 else MAX_DOCUMENT_BYTES
        if not token or not 0 < total <= maximum or count != expected_count:
            raise ValueError("invalid mission transfer bounds")
        if not 0 <= index < count:
            raise ValueError("invalid mission chunk index")
        if len(body) - CHUNK.size != min(CHUNK_DATA_BYTES, total - index * CHUNK_DATA_BYTES):
            raise ValueError("invalid mission chunk data length")
    elif message_type == 29:
        if not ACK.size <= len(body) <= ACK.size + 80:
            raise ValueError("invalid mission acknowledgement size")
        token, request_type, index, accepted = ACK.unpack_from(body)
        if not token or request_type not in REQUEST_TYPES or accepted not in (0, 1):
            raise ValueError("invalid mission acknowledgement")
        if request_type not in (28, 37) and index:
            raise ValueError("control acknowledgement index must be zero")
        if index >= MAX_CHUNKS:
            raise ValueError("acknowledgement index exceeds transfer bounds")
        body[ACK.size :].decode("utf-8", errors="strict")
    elif message_type == 36:
        if len(body) != TELEMETRY_ACK.size:
            raise ValueError("invalid telemetry acknowledgement")
        token, index = TELEMETRY_ACK.unpack(body)
        if not token or index >= MAX_CHUNKS:
            raise ValueError("invalid telemetry acknowledgement")
    elif message_type == 33:
        if len(body) != TIGHTEN.size:
            raise ValueError("invalid mission policy size")
        mission, request, policy, timeout = TIGHTEN.unpack(body)
        if not mission or not request or policy > 4 or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("invalid mission policy")
    else:
        if len(body) != CONTROL.size or not all(CONTROL.unpack(body)):
            raise ValueError("invalid mission control")


@dataclass(frozen=True, slots=True)
class MissionWireMessage:
    message_type: int
    body: bytes

    def __post_init__(self) -> None:
        validate_mission_body(self.message_type, self.body)


def document_chunks(document: bytes, token: int, *, message_type: int = 28):
    """Yield canonical fixed-size chunks; the digest binds the complete bytes."""
    if not document or len(document) > MAX_DOCUMENT_BYTES:
        raise ValueError("invalid mission document size")
    digest = hashlib.sha256(document).digest()
    count = (len(document) + CHUNK_DATA_BYTES - 1) // CHUNK_DATA_BYTES
    for index in range(count):
        body = (
            CHUNK.pack(token, index, count, len(document), digest)
            + document[index * CHUNK_DATA_BYTES : (index + 1) * CHUNK_DATA_BYTES]
        )
        yield MissionWireMessage(message_type, body)

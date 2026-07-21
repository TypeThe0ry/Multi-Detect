from __future__ import annotations

import hashlib
import hmac
import socket
import struct
import time
import zlib
from dataclasses import dataclass

from .metric_depth import MetricDepthGrid

DEPTH_GRID_MAGIC = b"MDPD"
DEPTH_GRID_VERSION = 1
DEPTH_GRID_AUTH_TAG_BYTES = 16
DEPTH_GRID_HEADER = struct.Struct("!4sBBHIQHHHHIIIIIH")


@dataclass(frozen=True, slots=True)
class DepthGridFragment:
    sequence: int
    sent_at_ms: int
    width: int
    height: int
    fragment_index: int
    fragment_count: int
    minimum_depth_mm: int
    maximum_depth_mm: int
    logarithmic_encoding: bool
    uncompressed_size: int
    compressed_size: int
    raw_crc32: int
    payload: bytes


class DepthGridDatagramCodec:
    """Authenticated fragmented wire format for the low-rate depth side channel."""

    def __init__(self, hmac_key: bytes, *, maximum_datagram_bytes: int = 1200) -> None:
        if len(hmac_key) < 32:
            raise ValueError("depth-grid HMAC key must contain at least 32 bytes")
        minimum_datagram_bytes = DEPTH_GRID_HEADER.size + DEPTH_GRID_AUTH_TAG_BYTES + 1
        if maximum_datagram_bytes < minimum_datagram_bytes or maximum_datagram_bytes > 65_507:
            raise ValueError("invalid depth-grid maximum datagram size")
        self._hmac_key = bytes(hmac_key)
        self.maximum_datagram_bytes = maximum_datagram_bytes

    @property
    def maximum_fragment_payload_bytes(self) -> int:
        return (
            self.maximum_datagram_bytes
            - DEPTH_GRID_HEADER.size
            - DEPTH_GRID_AUTH_TAG_BYTES
        )

    def encode(
        self,
        grid: MetricDepthGrid,
        *,
        sequence: int,
        sent_at_ms: int | None = None,
    ) -> tuple[bytes, ...]:
        raw = bytes(grid.quantized_depth)
        expected_size = grid.width * grid.height
        if len(raw) != expected_size:
            raise ValueError("depth-grid payload size does not match its geometry")
        if not 1 <= grid.width <= 4096 or not 1 <= grid.height <= 4096:
            raise ValueError("depth-grid geometry is outside the protocol limit")
        # Prefix the zlib stream with its big-endian raw size so Qt's
        # qUncompress can decode it without adding a second compression library
        # to the QGC custom build.
        compressed = struct.pack("!I", len(raw)) + zlib.compress(raw, level=1)
        maximum_payload = self.maximum_fragment_payload_bytes
        fragment_count = max(1, (len(compressed) + maximum_payload - 1) // maximum_payload)
        if fragment_count > 4096:
            raise ValueError("depth-grid frame requires too many fragments")
        emitted_at_ms = int(time.time() * 1000.0) if sent_at_ms is None else sent_at_ms
        minimum_depth_mm = max(0, min(0xFFFFFFFF, round(grid.minimum_depth_m * 1000.0)))
        maximum_depth_mm = max(0, min(0xFFFFFFFF, round(grid.maximum_depth_m * 1000.0)))
        raw_crc32 = zlib.crc32(raw) & 0xFFFFFFFF
        datagrams: list[bytes] = []
        for fragment_index in range(fragment_count):
            start = fragment_index * maximum_payload
            payload = compressed[start : start + maximum_payload]
            header = DEPTH_GRID_HEADER.pack(
                DEPTH_GRID_MAGIC,
                DEPTH_GRID_VERSION,
                int(grid.encoding == "logarithmic"),
                DEPTH_GRID_HEADER.size,
                sequence & 0xFFFFFFFF,
                emitted_at_ms & 0xFFFFFFFFFFFFFFFF,
                grid.width,
                grid.height,
                fragment_index,
                fragment_count,
                minimum_depth_mm,
                maximum_depth_mm,
                len(raw),
                len(compressed),
                raw_crc32,
                len(payload),
            )
            authenticated = header + payload
            tag = hmac.new(self._hmac_key, authenticated, hashlib.sha256).digest()[
                :DEPTH_GRID_AUTH_TAG_BYTES
            ]
            datagrams.append(authenticated + tag)
        return tuple(datagrams)

    def decode_fragment(self, datagram: bytes) -> DepthGridFragment:
        minimum_size = DEPTH_GRID_HEADER.size + DEPTH_GRID_AUTH_TAG_BYTES
        if len(datagram) < minimum_size:
            raise ValueError("depth-grid datagram is truncated")
        authenticated = datagram[:-DEPTH_GRID_AUTH_TAG_BYTES]
        observed_tag = datagram[-DEPTH_GRID_AUTH_TAG_BYTES:]
        expected_tag = hmac.new(self._hmac_key, authenticated, hashlib.sha256).digest()[
            :DEPTH_GRID_AUTH_TAG_BYTES
        ]
        if not hmac.compare_digest(observed_tag, expected_tag):
            raise ValueError("depth-grid authentication failed")
        fields = DEPTH_GRID_HEADER.unpack_from(authenticated)
        (
            magic,
            version,
            flags,
            header_size,
            sequence,
            sent_at_ms,
            width,
            height,
            fragment_index,
            fragment_count,
            minimum_depth_mm,
            maximum_depth_mm,
            uncompressed_size,
            compressed_size,
            raw_crc32,
            payload_size,
        ) = fields
        if magic != DEPTH_GRID_MAGIC or version != DEPTH_GRID_VERSION:
            raise ValueError("unsupported depth-grid protocol")
        if flags & ~0x01:
            raise ValueError("unsupported depth-grid encoding flags")
        if header_size != DEPTH_GRID_HEADER.size:
            raise ValueError("invalid depth-grid header size")
        payload = authenticated[header_size:]
        if len(payload) != payload_size:
            raise ValueError("depth-grid fragment length mismatch")
        if fragment_count < 1 or fragment_count > 4096 or fragment_index >= fragment_count:
            raise ValueError("invalid depth-grid fragment coordinates")
        if width < 1 or height < 1 or width * height != uncompressed_size:
            raise ValueError("invalid depth-grid frame geometry")
        if compressed_size < 5 or compressed_size > width * height * 2 + 4:
            raise ValueError("invalid depth-grid compressed size")
        return DepthGridFragment(
            sequence=sequence,
            sent_at_ms=sent_at_ms,
            width=width,
            height=height,
            fragment_index=fragment_index,
            fragment_count=fragment_count,
            minimum_depth_mm=minimum_depth_mm,
            maximum_depth_mm=maximum_depth_mm,
            logarithmic_encoding=bool(flags & 0x01),
            uncompressed_size=uncompressed_size,
            compressed_size=compressed_size,
            raw_crc32=raw_crc32,
            payload=payload,
        )


class DepthGridUdpPublisher:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        hmac_key: bytes,
        maximum_datagram_bytes: int = 1200,
        local_port: int = 14_583,
    ) -> None:
        if not host.strip():
            raise ValueError("depth-grid UDP host is required")
        if not 1024 <= port <= 65535:
            raise ValueError("depth-grid UDP port is outside the application range")
        if not 1024 <= local_port <= 65535 or local_port == port:
            raise ValueError("invalid depth-grid local UDP port")
        self._destination = (socket.gethostbyname(host.strip()), port)
        self._codec = DepthGridDatagramCodec(
            hmac_key,
            maximum_datagram_bytes=maximum_datagram_bytes,
        )
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("0.0.0.0", local_port))
        self._socket.setblocking(False)
        self._sequence = 0
        self._closed = False
        self.published_frames = 0
        self.published_datagrams = 0

    def publish(self, grid: MetricDepthGrid) -> int:
        if self._closed:
            raise RuntimeError("depth-grid UDP publisher is closed")
        self._drain_keepalives()
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        datagrams = self._codec.encode(grid, sequence=self._sequence)
        sent = 0
        for datagram in datagrams:
            self._socket.sendto(datagram, self._destination)
            sent += 1
        self.published_frames += 1
        self.published_datagrams += sent
        return sent

    def _drain_keepalives(self) -> None:
        while True:
            try:
                self._socket.recvfrom(256)
            except BlockingIOError:
                return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._socket.close()

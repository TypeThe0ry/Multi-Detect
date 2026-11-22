"""Annex-B access-unit parsing, timestamp SEI and RTP/H.264 packetization."""

from __future__ import annotations

import base64
import re
import secrets
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")
LATENCY_UUID = bytes.fromhex("8b4d5d49d9fc4dc7a1a855c6f372a120")


class AnnexBAccessUnitParser:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.access_unit: list[bytes] = []

    def feed(self, chunk: bytes) -> list[list[bytes]]:
        self.buffer.extend(chunk)
        starts = list(START_CODE.finditer(self.buffer))
        if len(starts) < 2:
            return []
        nals = [
            bytes(self.buffer[start.end() : next_start.start()]).rstrip(b"\x00")
            for start, next_start in zip(starts, starts[1:], strict=False)
        ]
        del self.buffer[: starts[-1].start()]
        return self._group(nal for nal in nals if nal)

    def finish(self) -> Iterator[list[bytes]]:
        starts = list(START_CODE.finditer(self.buffer))
        if starts:
            nal = bytes(self.buffer[starts[-1].end() :]).rstrip(b"\x00")
            if nal:
                yield from self._group((nal,))
        if self.access_unit:
            yield self.access_unit
            self.access_unit = []

    def _group(self, nals: Iterable[bytes]) -> list[list[bytes]]:
        complete: list[list[bytes]] = []
        for nal in nals:
            if not nal:
                continue
            if nal[0] & 0x1F == 9 and self.access_unit:
                complete.append(self.access_unit)
                self.access_unit = []
            self.access_unit.append(nal)
        return complete


def sps_pps(annex_b: bytes) -> tuple[bytes | None, bytes | None]:
    matches = list(START_CODE.finditer(annex_b))
    values: dict[int, bytes] = {}
    for index, start in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(annex_b)
        nal = annex_b[start.end() : end].rstrip(b"\x00")
        if nal:
            kind = nal[0] & 0x1F
            if kind in (7, 8):
                values[kind] = nal
    return values.get(7), values.get(8)


def make_latency_sei(sequence: int, simulation_time_ns: int, captured_monotonic_ns: int) -> bytes:
    """Encode source timing in an H.264 user-data-unregistered SEI NAL."""
    payload = LATENCY_UUID + struct.pack(
        "!QQQ", sequence, simulation_time_ns, captured_monotonic_ns
    )
    rbsp = bytearray((5,))
    size = len(payload)
    while size >= 255:
        rbsp.append(255)
        size -= 255
    rbsp.append(size)
    rbsp.extend(payload)
    rbsp.append(0x80)
    escaped = bytearray()
    zeros = 0
    for value in rbsp:
        if zeros >= 2 and value <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return b"\x06" + bytes(escaped)


def parse_latency_sei(nal: bytes) -> tuple[int, int, int] | None:
    if not nal or nal[0] & 0x1F != 6:
        return None
    rbsp = bytearray()
    zeros = 0
    for value in nal[1:]:
        if zeros >= 2 and value == 3:
            zeros = 0
            continue
        rbsp.append(value)
        zeros = zeros + 1 if value == 0 else 0
    cursor = 0
    while cursor < len(rbsp):
        if rbsp[cursor] == 0x80:
            return None
        payload_type = 0
        while cursor < len(rbsp) and rbsp[cursor] == 255:
            payload_type += 255
            cursor += 1
        if cursor >= len(rbsp):
            break
        payload_type += rbsp[cursor]
        cursor += 1
        payload_size = 0
        while cursor < len(rbsp) and rbsp[cursor] == 255:
            payload_size += 255
            cursor += 1
        if cursor >= len(rbsp):
            break
        payload_size += rbsp[cursor]
        cursor += 1
        payload = bytes(rbsp[cursor : cursor + payload_size])
        cursor += payload_size
        if payload_type == 5 and payload.startswith(LATENCY_UUID):
            values = payload[len(LATENCY_UUID) :]
            if len(values) == 24:
                return struct.unpack("!QQQ", values)
    return None


@dataclass(slots=True)
class RtpPacketizer:
    payload_type: int = 96
    maximum_payload_bytes: int = 1200
    sequence: int = 0
    timestamp: int = 0
    ssrc: int = 0

    def __post_init__(self) -> None:
        self.sequence = secrets.randbelow(65536)
        self.timestamp = secrets.randbits(32)
        self.ssrc = secrets.randbits(32)

    def packetize(self, nals: list[bytes], timestamp: int) -> list[bytes]:
        payloads: list[bytes] = []
        for nal in nals:
            if len(nal) <= self.maximum_payload_bytes:
                payloads.append(nal)
                continue
            indicator = (nal[0] & 0xE0) | 28
            nal_type = nal[0] & 0x1F
            body = nal[1:]
            maximum = self.maximum_payload_bytes - 2
            for offset in range(0, len(body), maximum):
                part = body[offset : offset + maximum]
                start = offset == 0
                end = offset + len(part) == len(body)
                header = nal_type | (0x80 if start else 0) | (0x40 if end else 0)
                payloads.append(bytes((indicator, header)) + part)
        packets: list[bytes] = []
        for index, payload in enumerate(payloads):
            marker = 0x80 if index == len(payloads) - 1 else 0
            second = marker | (self.payload_type & 0x7F)
            header = struct.pack(
                "!BBHII", 0x80, second, self.sequence, timestamp & 0xFFFFFFFF, self.ssrc
            )
            packets.append(header + payload)
            self.sequence = (self.sequence + 1) & 0xFFFF
        return packets


def make_sdp(sps: bytes, pps: bytes, width: int, height: int, fps: int) -> str:
    profile = sps[1:4].hex() if len(sps) >= 4 else "42e01f"
    sets = f"{base64.b64encode(sps).decode('ascii')},{base64.b64encode(pps).decode('ascii')}"
    return (
        "v=0\r\n"
        "o=- 0 0 IN IP4 0.0.0.0\r\n"
        "s=Polaris SITL camera\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\n"
        f"m=video 0 RTP/AVP 96\r\n"
        f"a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 packetization-mode=1;profile-level-id={profile};sprop-parameter-sets={sets}\r\n"
        f"a=framesize:96 {width}-{height}\r\n"
        f"a=framerate:{fps}\r\n"
        "a=control:trackID=0\r\n"
    )

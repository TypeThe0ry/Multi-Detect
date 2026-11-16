#!/usr/bin/env python3
"""Measure RTSP frame latency from the Gazebo monotonic capture SEI."""

from __future__ import annotations

import argparse
import json
import math
import select
import socket
import statistics
import struct
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.polaris_sim.video.h264 import parse_latency_sei  # noqa: E402


class RtspTcpClient:
    def __init__(self, host: str, port: int, mount: str, timeout: float) -> None:
        self.host, self.port = host, port
        self.mount = "/" + mount.strip("/")
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.cseq = 0
        self.session = ""

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        base = f"rtsp://{self.host}:{self.port}{quote(self.mount, safe='/')}"
        self._request("OPTIONS", base)
        describe = self._request("DESCRIBE", base, {"Accept": "application/sdp"})
        if describe[0] != 200:
            raise RuntimeError(f"DESCRIBE failed: RTSP {describe[0]}")
        setup = self._request(
            "SETUP",
            base + "/trackID=0",
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        if setup[0] != 200:
            raise RuntimeError(f"SETUP failed: RTSP {setup[0]}")
        self.session = setup[1].get("session", "").split(";", 1)[0]
        play = self._request("PLAY", base, {"Session": self.session, "Range": "npt=0.000-"})
        if play[0] != 200:
            raise RuntimeError(f"PLAY failed: RTSP {play[0]}")
        self.sock.settimeout(None)

    def _request(
        self, method: str, uri: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str]]:
        assert self.sock is not None
        self.cseq += 1
        rows = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}"]
        rows.extend(f"{name}: {value}" for name, value in (headers or {}).items())
        self.sock.sendall(("\r\n".join(rows) + "\r\n\r\n").encode("ascii"))
        header = _recv_until(self.sock, b"\r\n\r\n")
        lines = header.decode("ascii", errors="replace").split("\r\n")
        status = int(lines[0].split(" ")[1])
        parsed: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                parsed[name.strip().lower()] = value.strip()
        body_size = int(parsed.get("content-length", "0"))
        if body_size:
            _recv_exact(self.sock, body_size)
        return status, parsed

    def packets(self, duration: float):
        assert self.sock is not None
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            readable, _, _ = select.select(
                [self.sock], [], [], max(0.0, deadline - time.monotonic())
            )
            if not readable:
                break
            marker = self.sock.recv(1)
            if not marker:
                break
            if marker != b"$":
                continue
            header = _recv_exact(self.sock, 3)
            channel, size = header[0], struct.unpack("!H", header[1:])[0]
            packet = _recv_exact(self.sock, size)
            if channel == 0:
                yield packet

    def close(self) -> None:
        if self.sock is not None:
            try:
                base = f"rtsp://{self.host}:{self.port}{self.mount}"
                self._request("TEARDOWN", base, {"Session": self.session})
            except (OSError, ValueError, IndexError):
                pass
            self.sock.close()
            self.sock = None


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = sock.recv(size - len(value))
        if not chunk:
            raise EOFError("RTSP connection closed")
        value.extend(chunk)
    return bytes(value)


def _recv_until(sock: socket.socket, delimiter: bytes) -> bytes:
    value = bytearray()
    while not value.endswith(delimiter):
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError("RTSP connection closed during response")
        value.extend(chunk)
    return bytes(value)


def measure(host: str, port: int, mount: str, duration: float, timeout: float) -> dict[str, object]:
    client = RtspTcpClient(host, port, mount, timeout)
    client.connect()
    started = time.monotonic()
    ended = started + duration
    latencies_ms: list[float] = []
    sequences: list[int] = []
    previous_rtp_sequence: int | None = None
    rtp_missing = 0
    frame_sequence: int | None = None
    try:
        for packet in client.packets(duration):
            if time.monotonic() >= ended:
                break
            if len(packet) < 12:
                continue
            first, second = packet[0], packet[1]
            csrc_count = first & 0x0F
            offset = 12 + 4 * csrc_count
            if first & 0x10:
                if len(packet) < offset + 4:
                    continue
                offset += 4 + 4 * struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
            rtp_sequence = struct.unpack("!H", packet[2:4])[0]
            if previous_rtp_sequence is not None:
                gap = (rtp_sequence - previous_rtp_sequence - 1) & 0xFFFF
                if gap < 32768:
                    rtp_missing += gap
            previous_rtp_sequence = rtp_sequence
            payload = packet[offset:]
            if not payload:
                continue
            nal_type = payload[0] & 0x1F
            if nal_type == 6:
                value = parse_latency_sei(payload)
                if value:
                    frame_sequence = value[0]
                    capture_ns = value[2]
            elif nal_type == 28 and len(payload) > 2:
                nal_type = payload[1] & 0x1F
                if nal_type == 6 and payload[1] & 0x80:
                    frame_sequence = None
            else:
                value = None
            if second & 0x80:
                if frame_sequence is not None:
                    sequences.append(frame_sequence)
                    latencies_ms.append((time.monotonic_ns() - capture_ns) / 1_000_000)
                frame_sequence = None
        elapsed = max(1e-9, min(time.monotonic(), ended) - started)
    finally:
        client.close()
    sorted_latency = sorted(latencies_ms)
    percentile = (
        sorted_latency[min(len(sorted_latency) - 1, math.ceil(len(sorted_latency) * 0.95) - 1)]
        if sorted_latency
        else None
    )
    span = max(sequences) - min(sequences) + 1 if sequences else 0
    dropped = max(0, span - len(set(sequences)))
    return {
        "schema": "multidetect.polaris-sim.video-latency",
        "schema_version": 1,
        "host": host,
        "mount": "/" + mount.strip("/"),
        "transport": "rtp-over-tcp",
        "measurement_clock": "host-monotonic",
        "frames_received": len(sequences),
        "frame_sequence_span": span,
        "dropped_frame_estimate": dropped,
        "dropped_frame_rate": dropped / span if span else None,
        "rtp_packets_missing": rtp_missing,
        "actual_fps": len(sequences) / elapsed,
        "latency_ms_p50": statistics.median(sorted_latency) if sorted_latency else None,
        "latency_ms_p95": percentile,
        "elapsed_s": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8554)
    parser.add_argument("--mount", default="/v0/cam")
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = measure(args.host, args.port, args.mount, args.duration, args.timeout)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["frames_received"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Software-only RTSP fan-out bench for the replay publisher.

The publisher keeps a non-shared GstRTSPMediaFactory for the normal
single-client/reconnect path and switches to GStreamer's shared media fan-out
when the requested client minimum is greater than one.  This bench proves the
complementary invariant: while multiple RTSP sessions are active, one
validated BGR24 frame is accepted by every requested client.  The result is
deliberately labelled software-only; it never promotes a local RTSP/RTP run to
aircraft, GR01, HIL or REAL evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

IPC_MAGIC = b"MDRTSP1\0"
IPC_VERSION = 1
IPC_HEADER_BYTES = 88
IPC_FRAME = 1
IPC_EOS = 2
PIXEL_FORMAT_BGR24 = 1
EVENT_SCHEMA = "multidetect.replay-rtsp.event"
EVENT_SCHEMA_VERSION = 1
HEADER = struct.Struct("<8sHHIQQIIIIQ32s")


class BenchError(RuntimeError):
    """A deterministic bench failure with an actionable message."""


class EventReader:
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.events: queue.Queue[Mapping[str, Any] | BaseException | None] = queue.Queue()
        self.thread = threading.Thread(
            target=self._run,
            name="multidetect-rtsp-bench-events",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            while True:
                line = self._stream.readline(64 * 1024 + 1)
                if not line:
                    self.events.put(None)
                    return
                if len(line) > 64 * 1024 or not line.endswith(b"\n"):
                    raise BenchError("publisher emitted an oversized or unterminated JSON event")
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise BenchError("publisher event was not a JSON object")
                self.events.put(value)
        except BaseException as exc:  # the main thread reports the cause
            self.events.put(exc)

    def wait(self, event_name: str, timeout_s: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise BenchError(f"timed out waiting for publisher event {event_name}")
            try:
                item = self.events.get(timeout=remaining)
            except queue.Empty as exc:
                raise BenchError(f"timed out waiting for publisher event {event_name}") from exc
            if item is None:
                raise BenchError(f"publisher stdout closed before event {event_name}")
            if isinstance(item, BaseException):
                raise BenchError(f"publisher emitted invalid stdout: {item}") from item
            if (
                item.get("schema") != EVENT_SCHEMA
                or item.get("schema_version") != EVENT_SCHEMA_VERSION
                or item.get("version") != IPC_VERSION
            ):
                raise BenchError("publisher event schema/version does not match RTSP IPC v1")
            if item.get("event") == "error":
                raise BenchError(
                    f"publisher error {item.get('code', 'unknown')}: {item.get('message', '')}"
                )
            if item.get("event") != event_name:
                raise BenchError(
                    "publisher event order mismatch: "
                    f"expected {event_name}, got {item.get('event')!r}"
                )
            return item


class RawRtspClient:
    """Small RTSP-over-TCP client used when a full GStreamer client is unavailable.

    The helper's media pipeline is live.  The normal single-client path is
    non-shared, while a multi-client bench requests shared media so all RTSP
    sessions can complete DESCRIBE/SETUP/PLAY without waiting on one another.
    This client therefore performs the handshake in its own thread and keeps
    counting interleaved RTP packets after PLAY.  It validates transport and
    RTP framing, not decoded pixel content; the evidence remains software-only.
    """

    def __init__(self, index: int, url: str, timeout_s: float) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "rtsp" or parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise BenchError("raw RTSP client requires an exact loopback rtsp:// URL")
        self.index = index
        self.url = url.rstrip("/")
        self._host = parsed.hostname
        self._port = parsed.port
        self._timeout_s = max(0.5, float(timeout_s))
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.finished = threading.Event()
        self._condition = threading.Condition()
        self._error: str | None = None
        self._packet_count = 0
        self._rtp_bytes = 0
        self._last_sequence: int | None = None
        self._response_codes: list[int] = []
        self._sdp = ""
        self.thread = threading.Thread(
            target=self._run,
            name=f"multidetect-rtsp-raw-client-{index}",
            daemon=True,
        )

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def packet_count(self) -> int:
        with self._condition:
            return self._packet_count

    @property
    def response_codes(self) -> list[int]:
        with self._condition:
            return list(self._response_codes)

    @property
    def sdp(self) -> str:
        with self._condition:
            return self._sdp

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._condition:
            self._condition.notify_all()

    def join(self, timeout_s: float = 3.0) -> None:
        self.thread.join(timeout=max(0.1, timeout_s))

    def wait_ready(self, timeout_s: float) -> None:
        if not self.ready.wait(timeout=max(0.1, timeout_s)):
            if self.error:
                raise BenchError(f"raw RTSP client {self.index} failed: {self.error}")
            raise BenchError(f"raw RTSP client {self.index} did not complete SETUP/PLAY")
        if self.error:
            raise BenchError(f"raw RTSP client {self.index} failed: {self.error}")

    def wait_for_packets(self, minimum_count: int, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.1, timeout_s)
        with self._condition:
            while self._packet_count < minimum_count:
                if self._error is not None:
                    raise BenchError(f"raw RTSP client {self.index} failed: {self._error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise BenchError(
                        f"raw RTSP client {self.index} did not receive an RTP packet "
                        f"(wanted >= {minimum_count}, got {self._packet_count})"
                    )
                self._condition.wait(timeout=remaining)

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                "index": self.index,
                "ready": self.ready.is_set(),
                "finished": self.finished.is_set(),
                "packet_count": self._packet_count,
                "rtp_bytes": self._rtp_bytes,
                "last_rtp_sequence": self._last_sequence,
                "response_codes": list(self._response_codes),
                "error": self._error,
                "sdp_has_h264": "H264/90000" in self._sdp,
                "sdp_has_positive_framerate": "a=framerate:" in self._sdp
                and "a=framerate:0" not in self._sdp,
            }

    def _set_error(self, error: str) -> None:
        with self._condition:
            if self._error is None:
                self._error = error
            self._condition.notify_all()

    def _send(
        self,
        method: str,
        uri: str,
        cseq: int,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        sock = self._socket
        if sock is None:
            raise BenchError("raw RTSP socket is not connected")
        headers = {"CSeq": str(cseq), "User-Agent": "MultiDetectRTSPBench/1"}
        if extra_headers:
            headers.update(extra_headers)
        request = (
            f"{method} {uri} RTSP/1.0\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            + "\r\n"
        )
        sock.sendall(request.encode("ascii"))

    def _recv(self) -> None:
        sock = self._socket
        if sock is None:
            raise BenchError("raw RTSP socket is not connected")
        chunk = sock.recv(64 * 1024)
        if not chunk:
            raise BenchError("RTSP peer closed the control connection")
        self._buffer.extend(chunk)

    def _consume_interleaved(self) -> bool:
        if not self._buffer or self._buffer[0] != 0x24:  # '$'
            return False
        while len(self._buffer) < 4:
            self._recv()
        channel = self._buffer[1]
        payload_bytes = int.from_bytes(self._buffer[2:4], "big")
        if payload_bytes > 4 * 1024 * 1024:
            raise BenchError("interleaved RTSP payload exceeds the 4 MiB bench limit")
        frame_bytes = 4 + payload_bytes
        while len(self._buffer) < frame_bytes:
            self._recv()
        packet = bytes(self._buffer[4:frame_bytes])
        del self._buffer[:frame_bytes]
        if channel % 2 == 0:
            self._record_rtp(packet)
        return True

    def _record_rtp(self, packet: bytes) -> None:
        if len(packet) < 12 or packet[0] >> 6 != 2:
            raise BenchError("interleaved channel 0 did not contain a valid RTPv2 packet")
        csrc_count = packet[0] & 0x0F
        header_bytes = 12 + csrc_count * 4
        if len(packet) < header_bytes:
            raise BenchError("RTP CSRC list exceeds packet length")
        if packet[0] & 0x10:
            if len(packet) < header_bytes + 4:
                raise BenchError("RTP extension header is truncated")
            extension_words = int.from_bytes(packet[header_bytes + 2 : header_bytes + 4], "big")
            header_bytes += 4 + extension_words * 4
            if len(packet) < header_bytes:
                raise BenchError("RTP extension payload exceeds packet length")
        payload_type = packet[1] & 0x7F
        if payload_type != 96:
            raise BenchError(f"unexpected RTP payload type {payload_type}; expected H.264 PT 96")
        sequence = int.from_bytes(packet[2:4], "big")
        with self._condition:
            self._packet_count += 1
            self._rtp_bytes += len(packet)
            self._last_sequence = sequence
            self._condition.notify_all()

    def _read_response(self) -> tuple[int, dict[str, str], bytes]:
        while True:
            if self._consume_interleaved():
                continue
            marker = self._buffer.find(b"\r\n\r\n")
            if marker < 0:
                if len(self._buffer) > 64 * 1024:
                    raise BenchError("RTSP response headers exceed the 64 KiB bench limit")
                self._recv()
                continue
            header_bytes = bytes(self._buffer[:marker])
            del self._buffer[: marker + 4]
            lines = header_bytes.decode("iso-8859-1").split("\r\n")
            if not lines or not lines[0].startswith("RTSP/1.0 "):
                raise BenchError("RTSP response status line is malformed")
            try:
                status = int(lines[0].split(" ", 2)[1])
            except (IndexError, ValueError) as exc:
                raise BenchError("RTSP response status code is malformed") from exc
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    raise BenchError("RTSP response contains a malformed header")
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
            try:
                body_bytes = int(headers.get("content-length", "0"))
            except ValueError as exc:
                raise BenchError("RTSP Content-Length is not an integer") from exc
            if body_bytes < 0 or body_bytes > 4 * 1024 * 1024:
                raise BenchError("RTSP response body exceeds the 4 MiB bench limit")
            while len(self._buffer) < body_bytes:
                self._recv()
            body = bytes(self._buffer[:body_bytes])
            del self._buffer[:body_bytes]
            with self._condition:
                self._response_codes.append(status)
            if status != 200:
                raise BenchError(f"RTSP request returned status {status}")
            return status, headers, body

    def _read_stream(self) -> None:
        while not self._stop.is_set():
            if self._consume_interleaved():
                continue
            try:
                self._recv()
            except TimeoutError:
                continue

    def _run(self) -> None:
        try:
            self._socket = socket.create_connection((self._host, self._port), timeout=3.0)
            self._socket.settimeout(min(1.0, self._timeout_s))
            self._send("OPTIONS", self.url, 1)
            self._read_response()
            self._send("DESCRIBE", self.url, 2, {"Accept": "application/sdp"})
            _, _, sdp = self._read_response()
            self._sdp = sdp.decode("utf-8", errors="replace")
            if "m=video" not in self._sdp or "H264/90000" not in self._sdp:
                raise BenchError("DESCRIBE SDP did not advertise an H.264 video stream")
            if "a=framerate:" not in self._sdp or "a=framerate:0" in self._sdp:
                raise BenchError("DESCRIBE SDP did not advertise a positive frame rate")
            stream_url = self.url + "/stream=0"
            self._send(
                "SETUP",
                stream_url,
                3,
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
            )
            _, setup_headers, _ = self._read_response()
            session = setup_headers.get("session", "").split(";", 1)[0].strip()
            if not session:
                raise BenchError("SETUP response did not include a session identifier")
            self._send("PLAY", self.url + "/", 4, {"Session": session})
            self._read_response()
            self.ready.set()
            self._read_stream()
        except (BenchError, OSError, TimeoutError) as exc:
            # Once PLAY completed, the helper is allowed to close the RTSP
            # control socket immediately after EOS.  Preserve the successful
            # handshake/RTP evidence instead of misclassifying that expected
            # peer shutdown as a client setup failure.
            if not self._stop.is_set() and not self.ready.is_set():
                self._set_error(str(exc))
        finally:
            self.finished.set()
            self.stop()


def _pack_header(
    *,
    message_type: int,
    sequence: int,
    captured_at_ns: int,
    width: int,
    height: int,
    payload: bytes,
) -> bytes:
    stride = width * 3
    payload_bytes = len(payload) if message_type == IPC_FRAME else 0
    digest = hashlib.sha256(payload).digest() if message_type == IPC_FRAME else b"\0" * 32
    return HEADER.pack(
        IPC_MAGIC,
        IPC_VERSION,
        message_type,
        IPC_HEADER_BYTES,
        sequence,
        captured_at_ns,
        width,
        height,
        stride,
        PIXEL_FORMAT_BGR24,
        payload_bytes,
        digest,
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _infer_gst_root(repo_root: Path, explicit: Path | None) -> Path | None:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.extend(
        [
            repo_root
            / "ground-control"
            / "MultiDetectGCS"
            / ".cache"
            / "CPM"
            / "gstreamer-win-x86_64-1.28.4"
            / "sdk",
            Path(os.environ.get("GStreamer_ROOT_DIR", "")),
            Path(os.environ.get("GSTREAMER_1_0_ROOT_MSVC_X86_64", "")),
        ]
    )
    for candidate in candidates:
        if candidate and (candidate / "bin" / "gst-launch-1.0.exe").is_file():
            return candidate.resolve()
    return None


def _find_decoder(gst_root: Path) -> str:
    inspect = gst_root / "bin" / "gst-inspect-1.0.exe"
    if not inspect.is_file():
        return "avdec_h264"
    env = os.environ.copy()
    env["PATH"] = str(gst_root / "bin") + os.pathsep + env.get("PATH", "")
    env["GST_PLUGIN_PATH"] = str(gst_root / "lib" / "gstreamer-1.0")
    for decoder in ("avdec_h264", "openh264dec"):
        result = subprocess.run(
            [str(inspect), decoder],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return decoder
    raise BenchError("GStreamer SDK has neither avdec_h264 nor openh264dec")


def _event_int(event: Mapping[str, Any], name: str, *, minimum: int = 0) -> int:
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BenchError(f"publisher event field {name!r} is not an integer >= {minimum}")
    return value


def _write_json(path: Path | None, result: Mapping[str, Any]) -> None:
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")


def run(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    helper = Path(args.helper).expanduser().resolve(strict=True)
    if not helper.is_file():
        raise BenchError(f"RTSP helper is not a regular file: {helper}")
    if args.clients < 2:
        raise BenchError("--clients must be at least 2 for a multi-client bench")
    if args.frames < 1:
        raise BenchError("--frames must be positive")
    if args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
        raise BenchError("width and height must be positive even H.264 dimensions")
    gst_root = _infer_gst_root(
        repo_root,
        Path(args.gst_root).expanduser() if args.gst_root else None,
    )
    gst_launch = gst_root / "bin" / "gst-launch-1.0.exe" if gst_root else None
    decoder: str | None = None
    decoder_error: str | None = None
    if gst_root:
        try:
            decoder = _find_decoder(gst_root)
        except BenchError as exc:
            # Decoder discovery is useful metadata; raw RTSP/RTP framing is
            # sufficient for this transport fan-out proof.
            decoder_error = str(exc)
    port = args.port or _reserve_loopback_port()
    mount = "/multidetect-replay-multiclient"
    url = f"rtsp://127.0.0.1:{port}{mount}"
    frame_bytes = args.width * args.height * 3
    client_timeout_ms = max(1, math.ceil(args.timeout_s * 1000.0))
    env = os.environ.copy()
    if gst_root:
        env["PATH"] = str(gst_root / "bin") + os.pathsep + env.get("PATH", "")
        env["GST_PLUGIN_PATH"] = str(gst_root / "lib" / "gstreamer-1.0")

    helper_process: subprocess.Popen[bytes] | None = None
    clients: list[RawRtspClient] = []
    reader: EventReader | None = None
    frame_results: list[dict[str, Any]] = []
    warmup_result: dict[str, Any] | None = None
    helper_stderr = ""
    client_stats: list[dict[str, Any]] = []
    gate_passed = False
    failure: str | None = None
    started_at = time.time()
    summary: Mapping[str, Any] | None = None
    try:
        helper_process = subprocess.Popen(
            [
                str(helper),
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--mount",
                mount,
                "--width",
                str(args.width),
                "--height",
                str(args.height),
                "--client-timeout-ms",
                str(client_timeout_ms),
                "--minimum-client-count",
                str(args.clients),
            ],
            cwd=str(helper.parent),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if (
            helper_process.stdin is None
            or helper_process.stdout is None
            or helper_process.stderr is None
        ):
            raise BenchError("publisher did not expose all required pipes")
        reader = EventReader(helper_process.stdout)
        reader.start()
        server = reader.wait("server_ready", args.timeout_s)
        if server.get("url") != url or server.get("mount") != mount:
            raise BenchError(
                "publisher server_ready URL identity did not match the requested loopback URL"
            )
        expected_shared_media = args.clients > 1
        if server.get("shared_media") is not expected_shared_media:
            raise BenchError(
                "publisher server_ready shared_media mode did not match the "
                f"multi-client request (expected={expected_shared_media})"
            )

        clients = [RawRtspClient(index, url, args.timeout_s) for index in range(args.clients)]
        # Start together: the helper selects shared media for this multi-client
        # request, so every DESCRIBE/SETUP/PLAY handshake can progress while
        # the frame fan-out barrier is being measured.
        for client in clients:
            client.start()
        ready = reader.wait("client_ready", args.timeout_s)
        if _event_int(ready, "client_count", minimum=args.clients) < args.clients:
            raise BenchError("publisher client_ready did not include every requested client")
        if _event_int(ready, "appsrc_count", minimum=1) < 1:
            raise BenchError("publisher client_ready did not expose an initial appsrc")

        def submit_frame(sequence: int) -> tuple[Mapping[str, Any], int]:
            if helper_process is None or helper_process.stdin is None or reader is None:
                raise BenchError("publisher stdin disappeared before frame submission")
            payload = bytes((sequence * 29 + index * 7) % 256 for index in range(frame_bytes))
            captured_at_ns = 1_000_000_000 + sequence * 100_000_000
            helper_process.stdin.write(
                _pack_header(
                    message_type=IPC_FRAME,
                    sequence=sequence,
                    captured_at_ns=captured_at_ns,
                    width=args.width,
                    height=args.height,
                    payload=payload,
                )
            )
            helper_process.stdin.write(payload)
            helper_process.stdin.flush()
            return reader.wait("frame_accepted", args.timeout_s), captured_at_ns

        # Warm-up frame 0 establishes the live encoder path.  It is still
        # required to fan out to every requested client before it is
        # acknowledged, but is excluded from the steady-state RTP deltas.
        warmup_event, warmup_captured_at_ns = submit_frame(0)
        warmup_result = {
            "sequence": 0,
            "captured_at_ns": warmup_captured_at_ns,
            "fanout_count": _event_int(warmup_event, "fanout_count", minimum=args.clients),
            "client_count": _event_int(warmup_event, "client_count", minimum=args.clients),
            "fanout_attempts": _event_int(warmup_event, "fanout_attempts", minimum=1),
            "generation_retry_count": _event_int(
                warmup_event,
                "generation_retry_count",
                minimum=0,
            ),
        }
        for client in clients:
            client.wait_ready(args.timeout_s)

        for index in range(args.frames):
            sequence = index + 1
            before = [client.packet_count for client in clients]
            event, captured_at_ns = submit_frame(sequence)
            deltas: list[int] = []
            for client, previous_count in zip(clients, before, strict=True):
                client.wait_for_packets(previous_count + 1, args.timeout_s)
                deltas.append(client.packet_count - previous_count)
            frame_results.append(
                {
                    "sequence": sequence,
                    "captured_at_ns": captured_at_ns,
                    "fanout_count": _event_int(event, "fanout_count", minimum=args.clients),
                    "client_count": _event_int(event, "client_count", minimum=args.clients),
                    "fanout_attempts": _event_int(event, "fanout_attempts", minimum=1),
                    "generation_retry_count": _event_int(
                        event,
                        "generation_retry_count",
                        minimum=0,
                    ),
                    "rtp_packets_delta": deltas,
                }
            )

        if helper_process.stdin is None or reader is None:
            raise BenchError("publisher stdin disappeared before EOS")
        last_sequence = args.frames
        last_captured_at_ns = 1_000_000_000 + last_sequence * 100_000_000
        helper_process.stdin.write(
            _pack_header(
                message_type=IPC_EOS,
                sequence=last_sequence + 1,
                captured_at_ns=last_captured_at_ns,
                width=args.width,
                height=args.height,
                payload=b"",
            )
        )
        helper_process.stdin.flush()
        summary = reader.wait("summary", args.timeout_s)
        helper_process.stdin.close()
        helper_process.stdin = None
        helper_exit = helper_process.wait(timeout=args.timeout_s)
        if helper_exit != 0:
            raise BenchError(f"publisher exited with {helper_exit} after EOS")
        if _event_int(summary, "frame_count", minimum=0) != args.frames + 1:
            raise BenchError(
                "publisher summary frame_count did not match warmup plus requested frame count"
            )
        gate_passed = bool(
            warmup_result["fanout_count"] >= args.clients
            and warmup_result["client_count"] >= args.clients
            and frame_results
            and all(
                row["fanout_count"] >= args.clients
                and row["client_count"] >= args.clients
                and all(delta >= 1 for delta in row["rtp_packets_delta"])
                for row in frame_results
            )
        )
        if not gate_passed:
            raise BenchError(
                "one or more frames were not accepted and received by every requested RTSP client "
                f"(requested={args.clients}, results={frame_results})"
            )
    except (BenchError, OSError, subprocess.SubprocessError, ValueError) as exc:
        failure = str(exc)
    finally:
        for client in clients:
            client.stop()
        for client in clients:
            client.join(timeout_s=2.0)
            client_stats.append(client.stats())
        if helper_process is not None and helper_process.stdin is not None:
            try:
                helper_process.stdin.close()
            except OSError:
                pass
        if helper_process is not None and helper_process.poll() is None:
            helper_process.terminate()
            try:
                helper_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                helper_process.kill()
                helper_process.wait(timeout=2.0)
        if helper_process is not None and helper_process.stderr is not None:
            try:
                helper_stderr = helper_process.stderr.read(64 * 1024).decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                helper_stderr = ""

    result: dict[str, Any] = {
        "schema": "multidetect.replay-rtsp.multiclient-bench",
        "schema_version": 1,
        "version": 1,
        "generated_at_unix_s": started_at,
        "source_kind": "software_only_local_rtsp",
        "hardware_claim": "none",
        "helper": str(helper),
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "client_transport": "rtsp_interleaved_tcp_raw",
        "shared_media": args.clients > 1,
        "gstreamer_root": str(gst_root) if gst_root else None,
        "gst_launch": str(gst_launch) if gst_launch else None,
        "decoder": decoder,
        "decoder_error": decoder_error,
        "url": url,
        "bind": "127.0.0.1",
        "mount": mount,
        "width": args.width,
        "height": args.height,
        "clients_requested": args.clients,
        "frames_requested": args.frames,
        "warmup_frame": warmup_result,
        "frame_results": frame_results,
        "gate_passed": gate_passed and failure is None,
        "failure": failure,
        "helper_stderr": helper_stderr[-4096:],
        "client_stats": client_stats,
        "release_impact": "does_not_close_hil_or_real_release_gates",
    }
    _write_json(Path(args.out).expanduser() if args.out else None, result)
    return 0 if result["gate_passed"] else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--helper",
        required=True,
        help="path to MultiDetectReplayRtspPublisher(.exe)",
    )
    parser.add_argument(
        "--gst-root",
        default="",
        help="optional GStreamer SDK root recorded as environment metadata",
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=2,
        help="simultaneous RTSP clients (minimum 2)",
    )
    parser.add_argument("--frames", type=int, default=3, help="frames to fan out")
    parser.add_argument("--width", type=int, default=64)
    parser.add_argument("--height", type=int, default=48)
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback RTSP port; 0 chooses an ephemeral port",
    )
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--out", default="", help="optional JSON evidence output path")
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(_parser().parse_args()))
    except BenchError as exc:
        print(f"RTSP multiclient bench failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

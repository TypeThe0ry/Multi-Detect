"""Minimal H.264 RTSP publisher with UDP and TCP-interleaved RTP transports."""

from __future__ import annotations

import heapq
import logging
import re
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from .frames import Frame
from .h264 import RtpPacketizer, make_latency_sei, make_sdp

LOG = logging.getLogger(__name__)


class NetemShaper(Protocol):
    """Structural contract supplied by the Sim Gateway lane."""

    def shape(self, direction: str, payload: bytes) -> list[tuple[float, bytes]]: ...


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass(eq=False, slots=True)
class RtspSession:
    server: RtspServer
    stream: H264Stream
    connection: socket.socket
    peer_host: str
    transport: str
    session_id: str = field(default_factory=lambda: secrets.token_hex(8))
    interleaved_channel: int = 0
    client_rtp_port: int = 0
    client_rtcp_port: int = 0
    rtp_socket: socket.socket | None = None
    rtcp_socket: socket.socket | None = None
    packetizer: RtpPacketizer = field(default_factory=RtpPacketizer)
    playing: bool = False
    closed: bool = False
    _send_lock: threading.Lock = field(default_factory=threading.Lock)

    def deliver(self, nals: list[bytes], timestamp: int) -> None:
        if not self.playing or self.closed:
            return
        for packet in self.packetizer.packetize(nals, timestamp):
            self.server.dispatch(self, packet)

    def send_rtp(self, packet: bytes) -> None:
        if not self.playing or self.closed:
            return
        try:
            if self.transport == "tcp":
                interleaved = (
                    b"$"
                    + bytes((self.interleaved_channel,))
                    + len(packet).to_bytes(2, "big")
                    + packet
                )
                with self._send_lock:
                    self.connection.sendall(interleaved)
            else:
                assert self.rtp_socket is not None
                self.rtp_socket.sendto(packet, (self.peer_host, self.client_rtp_port))
            self.server._metric("rtp_packets_sent")
            self.server._metric("rtp_bytes_sent", len(packet))
        except OSError:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.playing = False
        self.stream.detach(self)
        for udp_socket in (self.rtp_socket, self.rtcp_socket):
            if udp_socket is not None:
                try:
                    udp_socket.close()
                except OSError:
                    pass
        self.server._unregister_session(self)
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class H264Stream:
    def __init__(self, mount: str, *, width: int, height: int, fps: int) -> None:
        if not mount.startswith("/") or mount.endswith("/") or ".." in mount.split("/"):
            raise ValueError("mount must be a normalized absolute RTSP path")
        if width < 2 or height < 2 or width % 2 or height % 2:
            raise ValueError("H.264 dimensions must be positive even numbers")
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.mount = mount
        self.width, self.height, self.fps = width, height, fps
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._sessions: set[RtspSession] = set()
        self._lock = threading.RLock()
        self._frame_index = 0
        self._timestamp_base = secrets.randbits(32)
        self._server: RtspServer | None = None

    def attach(self, session: RtspSession) -> None:
        with self._lock:
            self._sessions.add(session)

    def detach(self, session: RtspSession) -> None:
        with self._lock:
            self._sessions.discard(session)

    def sdp(self) -> str | None:
        with self._lock:
            if self._sps is None or self._pps is None:
                return None
            return make_sdp(self._sps, self._pps, self.width, self.height, self.fps)

    def publish(self, nals: list[bytes], frame: Frame) -> None:
        self.publish_access_unit(
            nals,
            sequence=frame.sequence,
            simulation_time_ns=frame.simulation_time_ns,
            captured_monotonic_ns=frame.captured_monotonic_ns,
        )

    def publish_access_unit(
        self,
        nals: list[bytes],
        *,
        sequence: int,
        simulation_time_ns: int,
        captured_monotonic_ns: int,
    ) -> None:
        if not nals:
            return
        with self._lock:
            for nal in nals:
                kind = nal[0] & 0x1F
                if kind == 7:
                    self._sps = nal
                elif kind == 8:
                    self._pps = nal
        sei = make_latency_sei(sequence, simulation_time_ns, captured_monotonic_ns)
        aud_at = 1 if nals[0][0] & 0x1F == 9 else 0
        packet_nals = [*nals[:aud_at], sei, *nals[aud_at:]]
        with self._lock:
            timestamp = (self._timestamp_base + self._frame_index * 90_000 // self.fps) & 0xFFFFFFFF
            self._frame_index += 1
            sessions = tuple(self._sessions)
        for session in sessions:
            session.deliver(packet_nals, timestamp)


class RtspServer:
    """Read-only live RTSP service. ``shaper`` receives outgoing RTP packets."""

    def __init__(
        self,
        host: str = "192.168.1.138",
        port: int = 8554,
        *,
        shaper: NetemShaper | None = None,
    ) -> None:
        self.host, self.port, self.shaper = host, port, shaper
        self.streams: dict[str, H264Stream] = {}
        self._server: _ThreadingServer | None = None
        self._serve_thread: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None
        self._schedule_cv = threading.Condition()
        self._scheduled: list[tuple[float, int, RtspSession, bytes]] = []
        self._schedule_sequence = 0
        self._stopping = threading.Event()
        self._metrics_lock = threading.Lock()
        self.metrics: dict[str, int] = {}
        self._sessions: set[RtspSession] = set()
        self._sessions_lock = threading.Lock()

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return int(self._server.server_address[1])

    def add_stream(self, stream: H264Stream) -> None:
        if stream.mount in self.streams:
            raise ValueError(f"duplicate RTSP mount: {stream.mount}")
        stream._server = self
        self.streams[stream.mount] = stream

    def start(self) -> None:
        if self._server is not None:
            return
        self._stopping.clear()
        parent = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                _RtspConnection(parent, self.request, self.client_address[0], self.rfile).run()

        self._server = _ThreadingServer((self.host, self.port), Handler)
        self._serve_thread = threading.Thread(
            target=self._server.serve_forever, name="polaris-rtsp", daemon=True
        )
        self._scheduler = threading.Thread(
            target=self._schedule_loop, name="polaris-rtp-netem", daemon=True
        )
        self._serve_thread.start()
        self._scheduler.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._sessions_lock:
            sessions = tuple(self._sessions)
        for session in sessions:
            session.close()
        with self._schedule_cv:
            self._scheduled.clear()
            self._schedule_cv.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for thread in (self._serve_thread, self._scheduler):
            if thread is not None:
                thread.join(timeout=2)
        self._serve_thread = self._scheduler = None

    def dispatch(self, session: RtspSession, packet: bytes) -> None:
        if self.shaper is None:
            session.send_rtp(packet)
            return
        shaped = self.shaper.shape("down", packet)
        self._metric("rtp_packets_shaped")
        if not shaped:
            self._metric("rtp_packets_dropped")
            return
        now = time.monotonic()
        with self._schedule_cv:
            for delay_s, payload in shaped:
                delay = max(0.0, float(delay_s))
                self._schedule_sequence += 1
                heapq.heappush(
                    self._scheduled,
                    (now + delay, self._schedule_sequence, session, bytes(payload)),
                )
            self._schedule_cv.notify()

    def _register_session(self, session: RtspSession) -> None:
        with self._sessions_lock:
            self._sessions.add(session)

    def _unregister_session(self, session: RtspSession) -> None:
        with self._sessions_lock:
            self._sessions.discard(session)

    def _schedule_loop(self) -> None:
        while not self._stopping.is_set():
            with self._schedule_cv:
                while not self._scheduled and not self._stopping.is_set():
                    self._schedule_cv.wait(timeout=0.2)
                if self._stopping.is_set():
                    return
                due, _, session, packet = self._scheduled[0]
                remaining = due - time.monotonic()
                if remaining > 0:
                    self._schedule_cv.wait(timeout=remaining)
                    continue
                heapq.heappop(self._scheduled)
            session.send_rtp(packet)

    def _metric(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self.metrics[name] = self.metrics.get(name, 0) + amount


class _RtspConnection:
    def __init__(
        self,
        server: RtspServer,
        connection: socket.socket,
        peer_host: str,
        reader: object,
    ) -> None:
        self.server, self.connection, self.peer_host, self.reader = (
            server,
            connection,
            peer_host,
            reader,
        )
        self.session: RtspSession | None = None
        self._send_lock = threading.Lock()

    def run(self) -> None:
        try:
            while True:
                request = self._read_request()
                if request is None:
                    return
                method, target, version, headers, body = request
                if not self._handle(method, target, version, headers, body):
                    return
        except (OSError, ValueError, UnicodeError):
            LOG.debug("RTSP client disconnected", exc_info=True)
        finally:
            if self.session is not None:
                self.session.close()

    def _read_request(self) -> tuple[str, str, str, dict[str, str], bytes] | None:
        first = self.reader.read(1)  # type: ignore[attr-defined]
        if not first:
            return None
        if first == b"$":
            header = self.reader.read(3)  # type: ignore[attr-defined]
            if len(header) != 3:
                return None
            size = int.from_bytes(header[1:3], "big")
            self.reader.read(size)  # type: ignore[attr-defined]
            return self._read_request()
        line = first + self.reader.readline()  # type: ignore[attr-defined]
        request_line = line.decode("ascii").strip().split(" ")
        if len(request_line) != 3:
            raise ValueError("malformed RTSP request line")
        headers: dict[str, str] = {}
        while True:
            line = self.reader.readline()  # type: ignore[attr-defined]
            if line in (b"\r\n", b"\n", b""):
                break
            name, separator, value = line.decode("ascii").partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = self.reader.read(length) if length else b""  # type: ignore[attr-defined]
        return request_line[0].upper(), request_line[1], request_line[2], headers, body

    def _handle(
        self, method: str, target: str, version: str, headers: dict[str, str], body: bytes
    ) -> bool:
        del version, body
        cseq = headers.get("cseq", "1")
        path = urlsplit(target).path.rstrip("/")
        if path.endswith("/trackID=0"):
            path = path.removesuffix("/trackID=0")
        stream = self.server.streams.get(path)
        if method == "OPTIONS":
            self._respond(
                cseq,
                200,
                "OK",
                {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER"},
            )
            return True
        if stream is None:
            self._respond(cseq, 404, "Not Found")
            return True
        if method == "DESCRIBE":
            sdp = stream.sdp()
            ready_deadline = time.monotonic() + 2.0
            while sdp is None and time.monotonic() < ready_deadline:
                time.sleep(0.02)
                sdp = stream.sdp()
            if sdp is None:
                self._respond(cseq, 503, "Stream Not Ready", {"Retry-After": "1"})
            else:
                body_bytes = sdp.encode("ascii")
                self._respond(
                    cseq,
                    200,
                    "OK",
                    {
                        "Content-Type": "application/sdp",
                        "Content-Base": target.rstrip("/") + "/",
                        "Content-Length": str(len(body_bytes)),
                    },
                    body_bytes,
                )
            return True
        if method == "SETUP":
            return self._setup(cseq, target, stream, headers)
        if method in {"PLAY", "PAUSE", "TEARDOWN", "GET_PARAMETER"}:
            if (
                self.session is None
                or headers.get("session", "").split(";", 1)[0] != self.session.session_id
            ):
                self._respond(cseq, 454, "Session Not Found")
                return True
            if method == "PLAY":
                self._respond(
                    cseq,
                    200,
                    "OK",
                    {
                        "Session": self.session.session_id,
                        "Range": "npt=0.000-",
                    },
                )
                self.session.playing = True
                self.session.stream.attach(self.session)
                return True
            if method == "PAUSE":
                self.session.playing = False
                self.session.stream.detach(self.session)
                self._respond(cseq, 200, "OK", {"Session": self.session.session_id})
                return True
            if method == "GET_PARAMETER":
                self._respond(cseq, 200, "OK", {"Session": self.session.session_id})
                return True
            self._respond(cseq, 200, "OK", {"Session": self.session.session_id})
            self.session.close()
            self.session = None
            return False
        self._respond(
            cseq,
            405,
            "Method Not Allowed",
            {"Allow": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN"},
        )
        return True

    def _setup(self, cseq: str, target: str, stream: H264Stream, headers: dict[str, str]) -> bool:
        transport = headers.get("transport", "")
        if not transport:
            self._respond(cseq, 461, "Unsupported Transport")
            return True
        if "RTP/AVP/TCP" in transport.upper():
            match = re.search(r"interleaved=(\d+)(?:-(\d+))?", transport, re.IGNORECASE)
            channel = int(match.group(1)) if match else 0
            session = RtspSession(
                self.server,
                stream,
                self.connection,
                self.peer_host,
                "tcp",
                interleaved_channel=channel,
            )
            response_transport = f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"
        else:
            match = re.search(r"client_port=(\d+)(?:-(\d+))?", transport, re.IGNORECASE)
            if match is None:
                self._respond(cseq, 461, "Unsupported Transport")
                return True
            client_rtp = int(match.group(1))
            client_rtcp = int(match.group(2) or client_rtp + 1)
            if not 1 <= client_rtp < 65535 or not 1 <= client_rtcp < 65536:
                self._respond(cseq, 461, "Unsupported Transport")
                return True
            local_host = self.connection.getsockname()[0]
            rtp_socket, rtcp_socket = _allocate_udp_pair(local_host)
            session = RtspSession(
                self.server,
                stream,
                self.connection,
                self.peer_host,
                "udp",
                client_rtp_port=client_rtp,
                client_rtcp_port=client_rtcp,
                rtp_socket=rtp_socket,
                rtcp_socket=rtcp_socket,
            )
            server_rtp = int(rtp_socket.getsockname()[1])
            response_transport = (
                f"RTP/AVP;unicast;client_port={client_rtp}-{client_rtcp};"
                f"server_port={server_rtp}-{server_rtp + 1};"
                f"source={self.connection.getsockname()[0]}"
            )
        if self.session is not None:
            self.session.close()
        session._send_lock = self._send_lock
        self.session = session
        self.server._register_session(session)
        self._respond(
            cseq,
            200,
            "OK",
            {"Transport": response_transport, "Session": session.session_id + ";timeout=60"},
        )
        return True

    def _respond(
        self,
        cseq: str,
        code: int,
        reason: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        all_headers = {"CSeq": cseq, "Server": "PolarisSim/1.0", "Connection": "keep-alive"}
        if headers:
            all_headers.update(headers)
        if body and "Content-Length" not in all_headers:
            all_headers["Content-Length"] = str(len(body))
        response = (
            f"RTSP/1.0 {code} {reason}\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in all_headers.items())
            + "\r\n"
        )
        with self._send_lock:
            self.connection.sendall(response.encode("ascii") + body)


def _allocate_udp_pair(host: str) -> tuple[socket.socket, socket.socket]:
    for _ in range(100):
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        first.bind((host, 0))
        port = int(first.getsockname()[1])
        if port % 2:
            first.close()
            second.close()
            continue
        try:
            second.bind((host, port + 1))
            return first, second
        except OSError:
            first.close()
            second.close()
    raise OSError("could not allocate an adjacent RTP/RTCP UDP port pair")

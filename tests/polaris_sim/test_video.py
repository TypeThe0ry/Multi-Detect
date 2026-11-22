from __future__ import annotations

import hashlib
import json
import socket
import struct
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest

from multidetect.polaris_sim.camera import get_camera_profile
from multidetect.polaris_sim.gateway.netem import NetemConfig, NetemShaper
from multidetect.polaris_sim.recording import split_mavlink_frames
from multidetect.polaris_sim.replay import H264FileReplay, RecordingReplay, iter_tlog
from multidetect.polaris_sim.video.frames import (
    FRAME_HEADER,
    Frame,
    GazeboTopicSource,
    read_frame,
)
from multidetect.polaris_sim.video.h264 import (
    AnnexBAccessUnitParser,
    RtpPacketizer,
    make_latency_sei,
    parse_latency_sei,
)
from multidetect.polaris_sim.video.pipeline import VideoPipeline, VideoService, VideoSettings
from multidetect.polaris_sim.video.rtsp import H264Stream, RtspServer, _RtspConnection

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = Path("/usr/bin/ffmpeg")
FFPROBE = Path("/usr/bin/ffprobe")


def test_camera_profile_defaults_are_used_for_rtsp_gazebo_sources():
    settings = VideoSettings(**get_camera_profile("lite").video_settings())
    service = VideoService("127.0.0.1", 0, default_settings=settings)
    pipeline = service.add_gazebo_camera(
        0, "cam", "md-polaris-sim-slot-5", "rw_fixture", "x500_mono_cam_0"
    )

    assert pipeline.settings.width == 640
    assert pipeline.settings.height == 480
    assert pipeline.settings.fps == 10
    assert pipeline.source.max_fps == 10


def test_gimbal_source_uses_merged_camera_link_image_topic():
    source = GazeboTopicSource(
        "md-polaris-sim-slot-5", "rw_suburb_davis", "x500_gimbal_0", max_fps=10
    )

    assert source.topic == (
        "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/image"
    )


class DropShaper:
    def shape(self, direction: str, payload: bytes) -> float | None:
        assert direction == "down"
        assert payload
        return None


class SyntheticSource:
    def __init__(
        self,
        *,
        count: int | None = 80,
        width: int = 320,
        height: int = 240,
        interval: float = 0.025,
    ):
        self.count, self.width, self.height, self.interval = count, width, height, interval
        self.stopped = threading.Event()

    def __iter__(self):
        sequence = 0
        while self.count is None or sequence < self.count:
            if self.stopped.is_set():
                return
            value = (sequence * 7) & 0xFF
            pixels = bytes((value, 60, (220 - value) & 0xFF)) * self.width * self.height
            yield Frame(
                sequence,
                self.width,
                self.height,
                sequence * 25_000_000,
                time.monotonic_ns(),
                pixels,
            )
            time.sleep(self.interval)
            sequence += 1

    def stop(self) -> None:
        self.stopped.set()


def _response(reader) -> tuple[int, dict[str, str], bytes]:
    first = reader.readline().decode("ascii").strip()
    status = int(first.split(" ")[1])
    headers: dict[str, str] = {}
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.lower()] = value.strip()
    body = reader.read(int(headers.get("content-length", "0")))
    return status, headers, body


def _request(sock: socket.socket, reader, method: str, uri: str, cseq: int, headers=None):
    rows = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}"]
    rows.extend(f"{name}: {value}" for name, value in (headers or {}).items())
    sock.sendall(("\r\n".join(rows) + "\r\n\r\n").encode("ascii"))
    return _response(reader)


def _open_rtsp_session(server: RtspServer, *, tcp: bool = True):
    sock = socket.create_connection(("127.0.0.1", server.bound_port), timeout=2)
    sock.settimeout(2)
    reader = sock.makefile("rb")
    base = f"rtsp://127.0.0.1:{server.bound_port}/v0/cam"
    assert _request(sock, reader, "OPTIONS", base, 1)[0] == 200
    assert _request(sock, reader, "DESCRIBE", base, 2, {"Accept": "application/sdp"})[0] == 200
    if tcp:
        status, headers, _ = _request(
            sock,
            reader,
            "SETUP",
            base + "/trackID=0",
            3,
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        assert status == 200
        session = headers["session"].split(";", 1)[0]
        assert _request(sock, reader, "PLAY", base, 4, {"Session": session})[0] == 200
        return sock, reader, session, None

    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("127.0.0.1", 0))
    rtp.settimeout(2)
    client_port = rtp.getsockname()[1]
    status, headers, _ = _request(
        sock,
        reader,
        "SETUP",
        base + "/trackID=0",
        3,
        {"Transport": f"RTP/AVP;unicast;client_port={client_port}-{client_port + 1}"},
    )
    assert status == 200
    session = headers["session"].split(";", 1)[0]
    assert _request(sock, reader, "PLAY", base, 4, {"Session": session})[0] == 200
    return sock, reader, session, rtp


def _send_test_access_unit(stream: H264Stream) -> None:
    stream.publish_access_unit(
        [b"\x09\xf0", b"\x67\x42\xe0\x1f", b"\x68\xce\x06\xe2", b"\x65\x88\x84\x21"],
        sequence=1,
        simulation_time_ns=1_000_000,
        captured_monotonic_ns=time.monotonic_ns(),
    )


def test_rtsp_tcp_interleaved_unit_contract() -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def shutdown(self, how: int) -> None:
            del how

    stream = H264Stream("/v0/cam", width=320, height=240, fps=30)
    _send_test_access_unit(stream)
    server = RtspServer("127.0.0.1", 0)
    server.add_stream(stream)
    fake = FakeConnection()
    incoming = BytesIO(
        b"$\x01\x00\x02okGET_PARAMETER rtsp://127.0.0.1/v0/cam RTSP/1.0\r\n"
        b"CSeq: 5\r\nSession: placeholder\r\n\r\n"
    )
    connection = _RtspConnection(server, fake, "local", incoming)
    base = "rtsp://127.0.0.1:8554/v0/cam"
    try:
        assert connection._handle("OPTIONS", base, "RTSP/1.0", {"cseq": "1"}, b"")
        assert connection._handle("DESCRIBE", base, "RTSP/1.0", {"cseq": "2"}, b"")
        assert connection._handle(
            "SETUP",
            base + "/trackID=0",
            "RTSP/1.0",
            {"cseq": "3", "transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
            b"",
        )
        assert connection.session is not None
        session = connection.session.session_id
        assert connection._handle("PLAY", base, "RTSP/1.0", {"cseq": "4", "session": session}, b"")
        play_response_index = next(
            index for index, payload in enumerate(fake.sent) if b"CSeq: 4" in payload
        )
        _send_test_access_unit(stream)
        interleaved = [packet for packet in fake.sent if packet.startswith(b"$")]
        assert interleaved
        first_packet_index = next(
            index for index, payload in enumerate(fake.sent) if payload.startswith(b"$")
        )
        assert play_response_index < first_packet_index
        assert interleaved[0][1] == 0
        packet_size = int.from_bytes(interleaved[0][2:4], "big")
        assert len(interleaved[0]) == packet_size + 4
        assert interleaved[0][4] >> 6 == 2

        request = connection._read_request()
        assert request is not None and request[0] == "GET_PARAMETER" and request[3]["cseq"] == "5"
        request = (
            request[0],
            request[1],
            request[2],
            {**request[3], "session": session},
            request[4],
        )
        assert connection._handle(*request)
    finally:
        if connection.session is not None:
            connection.session.close()
        server.stop()


def test_frame_wire_sei_parser_and_rtp_markers() -> None:
    frame = Frame(9, 2, 2, 1234, 5678, bytes(range(12)))
    wire = (
        FRAME_HEADER.pack(
            b"PSVF",
            frame.sequence,
            frame.width,
            frame.height,
            frame.simulation_time_ns,
            frame.captured_monotonic_ns,
            7,
        )
        + frame.pixels
    )
    assert read_frame(BytesIO(wire)) == frame
    assert read_frame(BytesIO(b"")) is None

    sei = make_latency_sei(9, 1234, 5678)
    assert parse_latency_sei(sei) == (9, 1234, 5678)
    parser = AnnexBAccessUnitParser()
    access_units = parser.feed(
        b"\x00\x00\x00\x01\x09\xf0\x00\x00\x01\x65\xaa\x00\x00\x01\x09\xf0\x00\x00\x01\x06\x80"
    )
    assert access_units == [[b"\x09\xf0", b"\x65\xaa"]]

    packets = RtpPacketizer(maximum_payload_bytes=8).packetize([b"\x65" + b"x" * 40], 77)
    assert len(packets) > 1
    assert all((packet[1] & 0x7F) == 96 for packet in packets)
    assert not (packets[0][1] & 0x80) and packets[-1][1] & 0x80


def test_tlog_frame_roundtrip(tmp_path: Path) -> None:
    frame = b"\xfe\x00\x01\x01\x01\x01\x00\x00"
    path = tmp_path / "sample.tlog"
    path.write_bytes(struct.pack(">Q", 1_234_567) + frame + struct.pack(">Q", 1_234_890) + frame)
    assert split_mavlink_frames(frame + frame) == [frame, frame]
    assert list(iter_tlog(path)) == [(1_234_567, frame), (1_234_890, frame)]


def test_rtsp_tcp_interleaved_and_udp_rtp(require_loopback_sockets) -> None:
    stream = H264Stream("/v0/cam", width=320, height=240, fps=30)
    _send_test_access_unit(stream)
    server = RtspServer("127.0.0.1", 0)
    server.add_stream(stream)
    server.start()
    clients = []
    try:
        tcp, reader, _, _ = _open_rtsp_session(server)
        clients.append((tcp, reader, None))
        _send_test_access_unit(stream)
        assert reader.read(1) == b"$"
        interleaved_header = reader.read(3)
        assert interleaved_header[0] == 0
        size = int.from_bytes(interleaved_header[1:], "big")
        rtp_packet = reader.read(size)
        assert len(rtp_packet) == size and rtp_packet[0] >> 6 == 2

        udp_control, udp_reader, _, rtp = _open_rtsp_session(server, tcp=False)
        clients.append((udp_control, udp_reader, rtp))
        _send_test_access_unit(stream)
        udp_packet, _ = rtp.recvfrom(2048)
        assert udp_packet[0] >> 6 == 2
    finally:
        for control, reader, rtp in clients:
            reader.close()
            control.close()
            if rtp is not None:
                rtp.close()
        server.stop()


def test_netem_drops_outgoing_video_packets(require_loopback_sockets) -> None:
    stream = H264Stream("/v0/cam", width=320, height=240, fps=30)
    _send_test_access_unit(stream)
    server = RtspServer("127.0.0.1", 0, shaper=DropShaper())
    server.add_stream(stream)
    server.start()
    sock = reader = None
    try:
        sock, reader, _, _ = _open_rtsp_session(server)
        _send_test_access_unit(stream)
        time.sleep(0.05)
        assert server.metrics["rtp_packets_dropped"] >= 1
        sock.settimeout(0.05)
        with pytest.raises((TimeoutError, socket.timeout)):
            reader.read(1)
    finally:
        if reader:
            reader.close()
        if sock:
            sock.close()
        server.stop()


def test_rtsp_dispatch_uses_gateway_netem_shaper_contract() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def send_rtp(self, packet: bytes) -> None:
            self.sent.append(packet)

    session = FakeSession()
    shaper = NetemShaper(down=NetemConfig(delay_ms=25), seed=7)
    server = RtspServer("127.0.0.1", 0, shaper=shaper)

    server.dispatch(session, b"rtp-packet")

    assert len(server._scheduled) == 1
    due, _, scheduled_session, packet = server._scheduled[0]
    assert scheduled_session is session
    assert packet == b"rtp-packet"
    assert due > time.monotonic()
    assert server.metrics["rtp_packets_shaped"] == 1

    dropped = RtspServer(
        "127.0.0.1", 0, shaper=NetemShaper(down=NetemConfig(loss_pct=100), seed=7)
    )
    dropped.dispatch(session, b"dropped-packet")
    assert not dropped._scheduled
    assert dropped.metrics["rtp_packets_dropped"] == 1


def test_synthetic_source_pixels_remain_valid_bytes_for_long_stream() -> None:
    source = SyntheticSource(count=40, width=4, height=4, interval=0)

    frames = list(source)

    assert len(frames) == 40
    assert all(frame.pixels and max(frame.pixels) <= 255 for frame in frames)


@pytest.mark.skipif(
    not FFMPEG.is_file() or not FFPROBE.is_file(), reason="ffmpeg/ffprobe unavailable"
)
def test_ffmpeg_pipeline_and_file_replay_h264_without_sockets(tmp_path: Path) -> None:
    stream = H264Stream("/v0/cam", width=320, height=240, fps=30)
    source = SyntheticSource(count=24, width=320, height=240, interval=0.002)
    encoded_path = tmp_path / "encoded.h264"
    encoded = encoded_path.open("wb")
    source_frames: list[Frame] = []

    def capture_encoded(nals, frame):
        source_frames.append(frame)
        for nal in nals:
            encoded.write(b"\x00\x00\x00\x01" + nal)
        encoded.flush()

    stream.publish = capture_encoded
    pipeline = VideoPipeline(
        source,
        stream,
        VideoSettings(width=320, height=240, fps=30, bitrate_kbps=700, profile="baseline"),
    )
    pipeline.start()
    assert pipeline._producer is not None
    pipeline._producer.join(timeout=8)
    pipeline.stop()
    encoded.close()
    assert pipeline.error is None
    assert pipeline.frames_encoded >= 10
    assert len(source_frames) == pipeline.frames_encoded

    probe = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-f",
            "h264",
            "-show_entries",
            "stream=codec_name,profile,width,height",
            "-of",
            "json",
            str(encoded_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=4,
    )
    info = json.loads(probe.stdout)["streams"][0]
    assert info["codec_name"] == "h264"
    assert "Baseline" in info["profile"]
    assert (info["width"], info["height"]) == (320, 240)

    replay_stream = H264Stream("/v0/cam", width=320, height=240, fps=30)
    replayed: list[list[bytes]] = []
    replay_stream.publish_access_unit = lambda nals, **_: replayed.append(nals)
    replay = H264FileReplay(encoded_path, replay_stream, speed=2, loop=False)
    replay.start()
    assert replay.thread is not None
    replay.thread.join(timeout=8)
    replay.stop()
    assert replay.error is None
    assert replay.frames_sent >= 10
    assert len(replayed) == replay.frames_sent


@pytest.mark.skipif(
    not FFMPEG.is_file() or not FFPROBE.is_file(), reason="ffmpeg/ffprobe unavailable"
)
def test_ffmpeg_h264_rtsp_latency_and_recording_replay(
    tmp_path: Path, require_loopback_sockets
) -> None:
    service = VideoService("127.0.0.1", 0)
    pipeline = service.add_camera(
        0,
        "cam",
        SyntheticSource(count=None, width=320, height=240, interval=0.03),
        settings=VideoSettings(width=320, height=240, fps=30, bitrate_kbps=700, profile="baseline"),
    )
    service.start()
    try:
        uri = f"rtsp://127.0.0.1:{service.server.bound_port}/v0/cam"
        probe = subprocess.run(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-rw_timeout",
                "4000000",
                "-read_intervals",
                "%+0.7",
                "-show_entries",
                "stream=codec_name,profile,width,height,r_frame_rate",
                "-of",
                "json",
                uri,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=6,
        )
        stream_info = json.loads(probe.stdout)["streams"][0]
        assert stream_info["codec_name"] == "h264"
        assert "Baseline" in stream_info["profile"]
        assert (stream_info["width"], stream_info["height"]) == (320, 240)
        assert stream_info["r_frame_rate"] == "30/1"

        from scripts.polaris_sim.measure_video_latency import measure

        latency = measure("127.0.0.1", service.server.bound_port, "/v0/cam", 3.0, 4)
        assert latency["frames_received"] >= 5, f"latency sample was too short: {latency}"
        assert latency["latency_ms_p95"] is not None, f"no latency samples: {latency}"
        assert latency["latency_ms_p95"] < 400, f"latency exceeded budget: {latency}"
        assert pipeline.frames_encoded >= 10, (
            f"too few encoded frames ({pipeline.frames_encoded}): {latency}"
        )

        video_path = tmp_path / "capture.mkv"
        record = subprocess.Popen(
            [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                uri,
                "-t",
                "2.5",
                "-an",
                "-c:v",
                "copy",
                "-f",
                "matroska",
                str(video_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _, stderr = record.communicate(timeout=10)
        assert record.returncode == 0, stderr.decode(errors="replace")
        tlog_path = tmp_path / "mavlink.tlog"
        mavlink_frame = b"\xfe\x00\x01\x01\x01\x01\x00\x00"
        tlog_path.write_bytes(struct.pack(">Q", 1_000_000) + mavlink_frame)
        manifest = {
            "schema": "multidetect.polaris-sim.recording",
            "schema_version": 1,
            "vehicles": [{"index": 0, "slot": 5, "sysid": 1, "world": "rw_fixture"}],
            "files": [
                {
                    "path": video_path.name,
                    "kind": "video",
                    "vehicle_index": 0,
                    "camera": "cam",
                    "bytes": video_path.stat().st_size,
                    "sha256": hashlib.sha256(video_path.read_bytes()).hexdigest(),
                },
                {
                    "path": tlog_path.name,
                    "kind": "mavlink_tlog",
                    "bytes": tlog_path.stat().st_size,
                    "sha256": hashlib.sha256(tlog_path.read_bytes()).hexdigest(),
                },
            ],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert list(iter_tlog(tlog_path)) == [(1_000_000, mavlink_frame)]
        replay = RecordingReplay(
            tmp_path,
            host="127.0.0.1",
            rtsp_port=0,
            mavlink_host="127.0.0.1",
            mavlink_port=0,
            speed=1,
            loop=True,
        )
        replay.start()
        try:
            replay_uri = f"rtsp://127.0.0.1:{replay.server.bound_port}/v0/cam"
            replay_probe = subprocess.run(
                [
                    str(FFPROBE),
                    "-v",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-rw_timeout",
                    "3000000",
                    "-read_intervals",
                    "%+0.4",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "json",
                    replay_uri,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            replay_info = json.loads(replay_probe.stdout)["streams"][0]
            assert replay_info["codec_name"] == "h264"
            assert (replay_info["width"], replay_info["height"]) == (320, 240)
            assert replay.mavlink is not None
            gcs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            gcs.bind(("127.0.0.1", 0))
            gcs.settimeout(2)
            heartbeat = b"\xfe\x09\x01\xff\xbe\x00" + bytes(9) + b"\x00\x00"
            gcs.sendto(heartbeat, ("127.0.0.1", replay.mavlink.port))
            outbound, _ = gcs.recvfrom(2048)
            assert outbound == mavlink_frame
            gcs.close()
        finally:
            replay.stop()
    finally:
        service.stop()

"""Replay recorded H.264 and MAVLink on configurable RTSP/UDP endpoints."""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .recording import split_mavlink_frames
from .video.h264 import AnnexBAccessUnitParser
from .video.rtsp import H264Stream, RtspServer

LOG = logging.getLogger(__name__)


def iter_tlog(path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield (Unix microseconds, raw MAVLink frame) from a standard tlog."""
    with path.open("rb") as stream:
        while True:
            stamp = stream.read(8)
            if not stamp:
                return
            if len(stamp) != 8:
                raise ValueError("truncated tlog timestamp")
            magic = stream.read(1)
            if not magic:
                raise ValueError("tlog ended before a MAVLink frame")
            if magic == b"\xfe":
                header_tail = stream.read(5)
                if len(header_tail) != 5:
                    raise ValueError("truncated MAVLink v1 header in tlog")
                length = header_tail[0] + 8
                frame = magic + header_tail
                rest = stream.read(length - len(frame))
            elif magic == b"\xfd":
                header_tail = stream.read(9)
                if len(header_tail) != 9:
                    raise ValueError("truncated MAVLink v2 header in tlog")
                length = 12 + header_tail[0] + (13 if header_tail[1] & 1 else 0)
                frame = magic + header_tail
                rest = stream.read(length - len(frame))
            else:
                raise ValueError(f"unknown MAVLink framing byte in tlog: {magic.hex()}")
            if len(rest) != length - len(frame):
                raise ValueError("truncated MAVLink message in tlog")
            yield struct.unpack(">Q", stamp)[0], frame + rest


class MavlinkTlogReplay:
    def __init__(
        self,
        path: Path,
        *,
        bind_host: str = "192.168.1.138",
        port: int = 14550,
        speed: float = 1.0,
        loop: bool = False,
    ) -> None:
        if not math.isfinite(speed) or speed not in {0.5, 1.0, 2.0}:
            raise ValueError("replay speed must be 0.5, 1, or 2")
        self.path, self.bind_host, self.port = path, bind_host, port
        self.speed, self.loop = speed, loop
        self.stop_event = threading.Event()
        self.client_seen = threading.Event()
        self.thread: threading.Thread | None = None
        self.socket: socket.socket | None = None
        self.clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self.error: str | None = None
        self.messages_sent = 0

    def start(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.bind_host, self.port))
        self.port = int(self.socket.getsockname()[1])
        self.socket.settimeout(0.25)
        self.thread = threading.Thread(target=self._run, name="polaris-mavlink-replay", daemon=True)
        self.thread.start()
        threading.Thread(target=self._listen, name="polaris-mavlink-register", daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _listen(self) -> None:
        assert self.socket is not None
        while not self.stop_event.is_set():
            try:
                payload, address = self.socket.recvfrom(65_535)
            except (OSError, TimeoutError):
                continue
            if any(_is_heartbeat(frame) for frame in split_mavlink_frames(payload)):
                with self._clients_lock:
                    self.clients.add((address[0], address[1]))
                self.client_seen.set()

    def _run(self) -> None:
        try:
            self.client_seen.wait()
            if self.stop_event.is_set():
                return
            anchor = time.monotonic()
            while not self.stop_event.is_set():
                entries = iter_tlog(self.path)
                first_entry = next(entries, None)
                if first_entry is None:
                    raise ValueError("tlog contains no MAVLink records")
                first = first_entry[0]
                last = first
                for stamp, frame in itertools.chain((first_entry,), entries):
                    if stamp < last:
                        raise ValueError("tlog timestamps are not monotonic")
                    last = stamp
                    due = anchor + (stamp - first) / 1_000_000 / self.speed
                    while not self.stop_event.is_set():
                        remaining = due - time.monotonic()
                        if remaining <= 0:
                            break
                        self.stop_event.wait(min(remaining, 0.05))
                    if self.stop_event.is_set():
                        return
                    with self._clients_lock:
                        clients = tuple(self.clients)
                    if self.socket is not None:
                        for client in clients:
                            self.socket.sendto(frame, client)
                        self.messages_sent += len(clients)
                if not self.loop:
                    return
                anchor += max(1_000, last - first + 1_000_000) / 1_000_000 / self.speed
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            LOG.error("MAVLink replay failed: %s", exc)


def _is_heartbeat(frame: bytes) -> bool:
    if frame[0] == 0xFE:
        return len(frame) >= 8 and frame[5] == 0
    return len(frame) >= 12 and int.from_bytes(frame[7:10], "little") == 0


class H264FileReplay:
    def __init__(
        self,
        path: Path,
        stream: H264Stream,
        *,
        speed: float = 1.0,
        loop: bool = False,
        ffmpeg: str = "/usr/bin/ffmpeg",
    ) -> None:
        if speed not in {0.5, 1.0, 2.0}:
            raise ValueError("replay speed must be 0.5, 1, or 2")
        self.path, self.stream, self.speed, self.loop, self.ffmpeg = (
            path,
            stream,
            speed,
            loop,
            ffmpeg,
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.frames_sent = 0
        self.error: str | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, name=f"polaris-video-replay-{self.stream.mount}", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _run(self) -> None:
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if self.loop:
            command += ["-stream_loop", "-1"]
        command += [
            "-readrate",
            str(self.speed),
            "-i",
            str(self.path),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb,h264_metadata=aud=insert",
            "-f",
            "h264",
            "pipe:1",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            assert self.process.stdout is not None
            parser = AnnexBAccessUnitParser()
            sequence = 0

            def publish(access_units: list[list[bytes]]) -> None:
                nonlocal sequence
                for nals in access_units:
                    now = time.monotonic_ns()
                    self.stream.publish_access_unit(
                        nals,
                        sequence=sequence,
                        simulation_time_ns=sequence * 1_000_000_000 // self.stream.fps,
                        captured_monotonic_ns=now,
                    )
                    sequence += 1
                    self.frames_sent += 1

            while not self.stop_event.is_set():
                chunk = self.process.stdout.read(64 * 1024)
                if not chunk:
                    break
                publish(parser.feed(chunk))
            publish(list(parser.finish()))
            code = self.process.wait(timeout=2)
            if code and not self.stop_event.is_set():
                self.error = f"ffmpeg exited with status {code}"
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.error = str(exc)


class RecordingReplay:
    """Load the recording manifest and serve its H.264 and MAVLink files."""

    def __init__(
        self,
        recording_dir: Path,
        *,
        host: str = "192.168.1.138",
        rtsp_port: int = 8554,
        mavlink_host: str = "192.168.1.138",
        mavlink_port: int = 14550,
        speed: float = 1.0,
        loop: bool = False,
    ) -> None:
        self.root = recording_dir
        self.manifest = json.loads((recording_dir / "manifest.json").read_text(encoding="utf-8"))
        self.server = RtspServer(host, rtsp_port)
        self.videos: list[H264FileReplay] = []
        self.mavlink: MavlinkTlogReplay | None = None
        self.speed, self.loop = speed, loop
        root = recording_dir.resolve(strict=True)
        for item in self.manifest.get("files", []):
            if item.get("kind") != "video":
                continue
            path = _verified_asset_path(root, item)
            metadata = _probe_h264(path)
            index = int(item["vehicle_index"])
            kind = item["camera"]
            stream = H264Stream(
                f"/v{index}/{kind}",
                width=metadata["width"],
                height=metadata["height"],
                fps=metadata["fps"],
            )
            self.server.add_stream(stream)
            self.videos.append(H264FileReplay(path, stream, speed=speed, loop=loop))
        tlog = next(
            (row for row in self.manifest.get("files", []) if row.get("kind") == "mavlink_tlog"),
            None,
        )
        if tlog:
            path = _verified_asset_path(root, tlog)
            self.mavlink = MavlinkTlogReplay(
                path, bind_host=mavlink_host, port=mavlink_port, speed=speed, loop=loop
            )

    def start(self) -> None:
        self.server.start()
        for video in self.videos:
            video.start()
        if self.mavlink is not None:
            self.mavlink.start()

    def stop(self) -> None:
        for video in self.videos:
            video.stop()
        if self.mavlink is not None:
            self.mavlink.stop()
        self.server.stop()


def _probe_h264(path: Path) -> dict[str, int]:
    result = subprocess.run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    if stream.get("codec_name") != "h264":
        raise ValueError(f"recording video is not H.264: {path}")
    numerator, denominator = stream.get("avg_frame_rate", "30/1").split("/", 1)
    fps = max(1, round(int(numerator) / max(1, int(denominator))))
    return {"width": int(stream["width"]), "height": int(stream["height"]), "fps": fps}


def _verified_asset_path(root: Path, row: dict[str, Any]) -> Path:
    relative = Path(str(row.get("path", "")))
    if relative.is_absolute():
        raise ValueError("recording manifest asset path must be relative")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("recording manifest asset path escapes the recording directory")
    digest_builder = hashlib.sha256()
    with path.open("rb") as asset:
        for chunk in iter(lambda: asset.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    expected_hash = row.get("sha256")
    expected_bytes = row.get("bytes")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("recording manifest asset is missing a SHA-256")
    if type(expected_bytes) is not int or expected_bytes < 1:
        raise ValueError("recording manifest asset is missing its byte count")
    if expected_hash != digest:
        raise ValueError(f"recording asset SHA-256 mismatch: {relative}")
    if expected_bytes != path.stat().st_size:
        raise ValueError(f"recording asset size mismatch: {relative}")
    return path

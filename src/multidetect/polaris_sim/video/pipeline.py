"""Gazebo raw-frame ingestion and CPU-only x264 publishing."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .frames import Frame, GazeboTopicSource
from .h264 import AnnexBAccessUnitParser
from .rtsp import H264Stream, NetemShaper, RtspServer

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoSettings:
    width: int = 1280
    height: int = 720
    fps: int = 20
    bitrate_kbps: int = 2500
    profile: str = "baseline"
    encoder_threads: int = 2
    ffmpeg: str = "/usr/bin/ffmpeg"

    def __post_init__(self) -> None:
        if self.width < 2 or self.height < 2 or self.width % 2 or self.height % 2:
            raise ValueError("H.264 output width and height must be positive even numbers")
        if not 1 <= self.fps <= 120:
            raise ValueError("fps must be in the range 1..120")
        if not 64 <= self.bitrate_kbps <= 100_000:
            raise ValueError("bitrate_kbps must be in the range 64..100000")
        if self.profile not in {"baseline", "main"}:
            raise ValueError("H.264 profile must be baseline or main")
        if not 1 <= self.encoder_threads <= 4:
            raise ValueError("encoder_threads must be in the range 1..4")
        if not (shutil.which(self.ffmpeg) or self.ffmpeg == "/usr/bin/ffmpeg"):
            raise ValueError(f"ffmpeg executable not found: {self.ffmpeg}")


class FrameSource(Protocol):
    def __iter__(self) -> Iterable[Frame]: ...

    def stop(self) -> None: ...


class VideoPipeline:
    """Encode one camera source and publish its H.264 access units to RTSP."""

    def __init__(self, source: FrameSource, stream: H264Stream, settings: VideoSettings) -> None:
        self.source, self.stream, self.settings = source, stream, settings
        self._stop = threading.Event()
        self._producer: threading.Thread | None = None
        self._consumer: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._source_dimensions: tuple[int, int, str] | None = None
        self._timestamps: deque[Frame] = deque()
        self._timestamp_lock = threading.Lock()
        self.error: str | None = None
        self.frames_ingested = 0
        self.frames_encoded = 0

    def start(self) -> None:
        if self._producer is not None:
            return
        self._stop.clear()
        self._producer = threading.Thread(
            target=self._produce, name=f"video-input-{self.stream.mount}", daemon=True
        )
        self._producer.start()

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()
        if self._producer is not None:
            self._producer.join(timeout=8)
        if self._process is not None and self._process.poll() is None:
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
        if self._consumer is not None:
            self._consumer.join(timeout=5)
        self._producer = self._consumer = None
        self._process = None

    def _start_encoder(self, frame: Frame) -> None:
        self._source_dimensions = frame.width, frame.height, frame.pixel_format
        fps = self.settings.fps
        command = [
            self.settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "rawvideo",
            "-pixel_format",
            frame.pixel_format,
            "-video_size",
            f"{frame.width}x{frame.height}",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            f"scale={self.settings.width}:{self.settings.height}:flags=fast_bilinear",
            "-c:v",
            "libx264",
            "-threads",
            str(self.settings.encoder_threads),
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            self.settings.profile,
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{self.settings.bitrate_kbps}k",
            "-maxrate",
            f"{self.settings.bitrate_kbps}k",
            "-bufsize",
            f"{self.settings.bitrate_kbps}k",
            "-g",
            str(fps),
            "-keyint_min",
            str(fps),
            "-sc_threshold",
            "0",
            "-x264-params",
            f"aud=1:repeat-headers=1:scenecut=0:keyint={fps}:min-keyint={fps}",
            "-f",
            "h264",
            "pipe:1",
        ]
        LOG.info("starting CPU H.264 encoder for %s", self.stream.mount)
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._consumer = threading.Thread(
            target=self._consume, name=f"video-h264-{self.stream.mount}", daemon=True
        )
        self._consumer.start()

    def _produce(self) -> None:
        try:
            for frame in self.source:
                if self._stop.is_set():
                    break
                if self._process is None:
                    self._start_encoder(frame)
                elif (frame.width, frame.height, frame.pixel_format) != self._source_dimensions:
                    raise ValueError(
                        "Gazebo camera geometry or pixel format changed during the stream"
                    )
                assert self._process.stdin is not None
                with self._timestamp_lock:
                    self._timestamps.append(frame)
                self._process.stdin.write(frame.pixels)
                self.frames_ingested += 1
        except (BrokenPipeError, OSError, RuntimeError, ValueError) as exc:
            self.error = str(exc)
            LOG.error("video capture failed for %s: %s", self.stream.mount, exc)
        finally:
            if self._process is not None and self._process.stdin is not None:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass
            if self._process is not None:
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    self._process.wait(timeout=2)

    def _consume(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        parser = AnnexBAccessUnitParser()

        def publish(access_units: list[list[bytes]]) -> None:
            for nals in access_units:
                with self._timestamp_lock:
                    frame = self._timestamps.popleft() if self._timestamps else None
                if frame is None:
                    self.error = "H.264 encoder emitted an access unit without a source frame"
                    continue
                self.stream.publish(nals, frame)
                self.frames_encoded += 1

        try:
            while True:
                chunk = self._process.stdout.read(64 * 1024)
                if not chunk:
                    break
                publish(parser.feed(chunk))
            publish(list(parser.finish()))
        except (OSError, ValueError) as exc:
            self.error = str(exc)


class VideoService:
    """Embeddable RTSP service for the Sim Gateway process."""

    def __init__(
        self,
        host: str = "192.168.1.138",
        port: int = 8554,
        *,
        shaper: NetemShaper | None = None,
        default_settings: VideoSettings | None = None,
    ) -> None:
        self.server = RtspServer(host, port, shaper=shaper)
        self.pipelines: list[VideoPipeline] = []
        self.default_settings = default_settings or VideoSettings()

    def add_camera(
        self,
        vehicle_index: int,
        kind: str,
        source: FrameSource,
        *,
        settings: VideoSettings | None = None,
    ) -> VideoPipeline:
        if vehicle_index not in (0, 1, 2):
            raise ValueError("vehicle_index must be 0, 1, or 2")
        if kind not in {"cam", "gimbal"}:
            raise ValueError("camera kind must be cam or gimbal")
        config = settings or self.default_settings
        stream = H264Stream(
            f"/v{vehicle_index}/{kind}",
            width=config.width,
            height=config.height,
            fps=config.fps,
        )
        self.server.add_stream(stream)
        pipeline = VideoPipeline(source, stream, config)
        self.pipelines.append(pipeline)
        return pipeline

    def add_gazebo_camera(
        self,
        vehicle_index: int,
        kind: str,
        container: str,
        world: str,
        model: str,
        *,
        topic: str | None = None,
        settings: VideoSettings | None = None,
    ) -> VideoPipeline:
        return self.add_camera(
            vehicle_index,
            kind,
            GazeboTopicSource(
                container,
                world,
                model,
                topic=topic,
                max_fps=(settings or self.default_settings).fps,
            ),
            settings=settings,
        )

    def start(self) -> None:
        self.server.start()
        for pipeline in self.pipelines:
            pipeline.start()

    def stop(self) -> None:
        for pipeline in reversed(self.pipelines):
            pipeline.stop()
        self.server.stop()

"""Bounded binary bridge for Gazebo camera frames emitted inside SITL."""

from __future__ import annotations

import logging
import struct
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

LOG = logging.getLogger(__name__)
FRAME_MAGIC = b"PSVF"
FRAME_HEADER = struct.Struct("!4sQIIQQB")
MAX_FRAME_BYTES = 64 * 1024 * 1024
PIXEL_FORMATS = {1: ("gray", 1), 3: ("rgb24", 3), 4: ("rgba", 4), 7: ("bgr24", 3), 8: ("bgra", 4)}


@dataclass(frozen=True, slots=True)
class Frame:
    sequence: int
    width: int
    height: int
    simulation_time_ns: int
    captured_monotonic_ns: int
    pixels: bytes
    pixel_format: str = "bgr24"


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if chunks:
                raise EOFError("truncated Gazebo camera frame record")
            return b""
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(stream: BinaryIO) -> Frame | None:
    """Read one validated frame from the container stdout wire."""
    header = _read_exact(stream, FRAME_HEADER.size)
    if not header:
        return None
    magic, sequence, width, height, simulation_time_ns, captured_ns, format_code = (
        FRAME_HEADER.unpack(header)
    )
    if magic != FRAME_MAGIC:
        raise ValueError("Gazebo frame wire magic mismatch")
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise ValueError("Gazebo frame dimensions are outside the supported range")
    if format_code not in PIXEL_FORMATS:
        raise ValueError(f"unsupported Gazebo pixel format code: {format_code}")
    pixel_format, channels = PIXEL_FORMATS[format_code]
    size = width * height * channels
    if size > MAX_FRAME_BYTES:
        raise ValueError("Gazebo frame exceeds the 64 MiB limit")
    pixels = _read_exact(stream, size)
    if len(pixels) != size:
        raise EOFError("truncated Gazebo camera pixel payload")
    return Frame(sequence, width, height, simulation_time_ns, captured_ns, pixels, pixel_format)


class GazeboTopicSource:
    """Run a tiny gz-transport subscriber inside a leased SITL container.

    The script is passed to ``python -c`` so the bridge creates no files in
    the container and moves raw frames over Docker's stdout pipe, not via
    repeated image-file polling.
    """

    _CONTAINER_SCRIPT = r"""
import queue, struct, sys, threading, time
from gz.msgs10.image_pb2 import Image
from gz.transport13 import Node

world, model, topic = sys.argv[1:4]
header = struct.Struct("!4sQIIQQB")
latest = queue.Queue(maxsize=1)
stopping = threading.Event()
sequence_lock = threading.Lock()
source_sequence = 0
failure = []

def stamp_ns(message):
    stamp = message.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nsec)

def bgr24(message):
    formats = {1: 1, 3: 3, 4: 4, 7: 3, 8: 4}
    code = int(message.pixel_format_type)
    channels = formats.get(code, 0)
    width, height, step = int(message.width), int(message.height), int(message.step)
    data = memoryview(message.data)
    if not channels or step < width * channels or len(data) < step * height:
        raise ValueError("unsupported Gazebo image layout")
    row_bytes = width * channels
    payload = bytes(data[:row_bytes * height]) if step == row_bytes else b"".join(
        data[y * step:y * step + row_bytes] for y in range(height)
    )
    return width, height, code, payload

def on_image(message):
    global source_sequence
    try:
        width, height, format_code, pixels = bgr24(message)
    except Exception as exc:
        failure.append(str(exc))
        print("Gazebo image callback failed: " + str(exc), file=sys.stderr, flush=True)
        stopping.set()
        return
    with sequence_lock:
        sequence = source_sequence
        source_sequence += 1
    row = (sequence, width, height, stamp_ns(message), time.monotonic_ns(), format_code, pixels)
    try:
        latest.put_nowait(row)
    except queue.Full:
        try: latest.get_nowait()
        except queue.Empty: pass
        try: latest.put_nowait(row)
        except queue.Full: pass

node = Node()
if not node.subscribe(Image, topic, on_image):
    raise SystemExit("Gazebo image subscription failed: " + topic)
out = sys.stdout.buffer
while not stopping.is_set():
    try: sequence, width, height, sim_ns, mono_ns, format_code, pixels = latest.get(timeout=0.5)
    except queue.Empty: continue
    out.write(header.pack(b"PSVF", sequence, width, height, sim_ns, mono_ns, format_code))
    out.write(pixels)
    out.flush()
if failure:
    raise SystemExit(2)
"""

    def __init__(
        self,
        container: str,
        world: str,
        model: str,
        *,
        topic: str | None = None,
        docker: str = "docker",
    ) -> None:
        if not container or any(char.isspace() for char in container):
            raise ValueError("container must be a non-empty Docker container name")
        if not world or not model:
            raise ValueError("world and model are required")
        self.container = container
        self.world = world
        self.model = model
        # SITL containers always run the selected world sdf as Gazebo world "default".
        self.topic = topic or (f"/world/default/model/{model}/link/camera_link/sensor/camera/image")
        self.docker = docker
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            [
                self.docker,
                "exec",
                self.container,
                "python3",
                "-u",
                "-c",
                self._CONTAINER_SCRIPT,
                self.world,
                self.model,
                self.topic,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert self._process.stderr is not None

        def drain() -> None:
            assert self._process is not None and self._process.stderr is not None
            for line in iter(self._process.stderr.readline, b""):
                LOG.warning(
                    "Gazebo capture [%s]: %s",
                    self.container,
                    line.decode(errors="replace").rstrip(),
                )

        self._stderr_thread = threading.Thread(target=drain, name="gz-camera-stderr", daemon=True)
        self._stderr_thread.start()

    def __iter__(self) -> Iterator[Frame]:
        self.start()
        assert self._process is not None and self._process.stdout is not None
        while True:
            try:
                frame = read_frame(self._process.stdout)
            except (EOFError, ValueError) as exc:
                code = self._process.poll()
                raise RuntimeError(
                    f"Gazebo camera bridge failed for {self.container} ({code}): {exc}"
                ) from exc
            if frame is None:
                code = self._process.poll()
                if code not in (None, 0):
                    raise RuntimeError(f"Gazebo camera bridge exited with status {code}")
                return
            yield frame

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

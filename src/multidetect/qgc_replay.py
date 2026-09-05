"""Synchronized Replay/live-SIM video publication for the QGC operator surface.

The E4 bridge deliberately sits *around* the canonical
:class:`~multidetect.operator_bridge.LiveOperatorBridge`.  The production
``LiveMissionRunner`` still owns inference, tracking, safety logic and signed
operator metadata.  This module only adds a strict, versioned BGR24 stream to
the local RTSP helper and a wall-clock presentation pacer.

The synchronization barrier for every frame is:

``pace on an independent wall clock -> write complete BGR frame -> receive the
helper's matching frame_accepted ACK -> send signed type-23 RuntimeStatus ->
QGC adopts the fresh status into its native controller state -> receive and
authenticate QGC's correlated signed type-24 RuntimeStatusAck``.

Replay's :class:`~multidetect.clock.ManualClock` is never advanced here.  The
helper and QGC links are loopback-only and this path exposes no Pixhawk or
actuator interface.  Live SIM sources use a validation-only pacer because
their capture loop has already established presentation timing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import queue
import re
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from .operator_bridge import (
    LiveOperatorBridge,
    OperatorBridgeResult,
    RuntimeStatusAcceptanceTransport,
)
from .operator_tracking import OperatorTargetLock

REPLAY_RTSP_IPC_MAGIC = b"MDRTSP1\0"
REPLAY_RTSP_IPC_VERSION = 1
REPLAY_RTSP_IPC_HEADER_BYTES = 88
REPLAY_RTSP_MESSAGE_FRAME = 1
REPLAY_RTSP_MESSAGE_EOS = 2
REPLAY_RTSP_PIXEL_FORMAT_BGR24 = 1
REPLAY_RTSP_EVENT_SCHEMA = "multidetect.replay-rtsp.event"

_HEADER = struct.Struct("<8sHHIQQIIIIQ32s")
if _HEADER.size != REPLAY_RTSP_IPC_HEADER_BYTES:  # pragma: no cover - import drift guard
    raise RuntimeError("Replay RTSP IPC header layout is not 88 bytes")

_MAX_HELPER_EVENT_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_SAFE_MOUNT = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")
_MIN_WALL_SLEEP_S = 0.001


@dataclass(frozen=True, slots=True)
class ReplayVideoGeometry:
    width: int
    height: int
    stride: int
    pixel_format: int = REPLAY_RTSP_PIXEL_FORMAT_BGR24

    def __post_init__(self) -> None:
        for value, name in ((self.width, "width"), (self.height, "height")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Replay video {name} must be a positive integer")
        if self.stride != self.width * 3:
            raise ValueError("Replay BGR24 stride must equal width * 3")
        if self.pixel_format != REPLAY_RTSP_PIXEL_FORMAT_BGR24:
            raise ValueError("Replay video pixel format must be BGR24")

    @classmethod
    def bgr24(cls, width: int, height: int) -> ReplayVideoGeometry:
        return cls(width=width, height=height, stride=width * 3)

    @property
    def payload_bytes(self) -> int:
        return self.stride * self.height


@dataclass(frozen=True, slots=True)
class ReplayRtspFrameAck:
    sequence: int
    captured_at_ns: int
    payload_bytes: int
    acknowledged_at_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class ReplayRtspPublisherStats:
    frame_count: int
    payload_bytes: int
    first_captured_at_ns: int | None
    last_captured_at_ns: int | None
    maximum_ack_latency_ms: float
    eos_sent: bool
    summary_received: bool
    helper_exit_code: int | None

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WallClockPacerStats:
    paced_frame_count: int
    slept_seconds: float
    maximum_lag_seconds: float
    playback_rate: float

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


class FramePacer(Protocol):
    """Presentation pacing contract shared by Replay and live SIM frames."""

    def pace(self, captured_at_s: float) -> None: ...

    @property
    def stats(self) -> WallClockPacerStats: ...


@dataclass(frozen=True, slots=True)
class SynchronizedReplayEvidence:
    """Machine-readable synchronized-QGC acceptance evidence.

    ``metadata_acceptance_ack_count`` is the canonical count of fully
    validated, post-adoption type-24 ACKs. ``metadata_barrier_count`` remains
    only as a legacy compatibility alias and is always serialized from that
    canonical count; it is never maintained as an independent counter.
    """

    expected_frame_count: int
    video_frame_count: int
    metadata_barrier_count: int
    maximum_ack_to_metadata_skew_ms: float
    allowed_ack_to_metadata_skew_ms: float
    qgc_peer_host: str | None
    qgc_peer_port: int | None
    complete: bool
    ground_truth_isolated: bool = True
    hardware_outputs_present: bool = False
    pixhawk_attached: bool = False
    actuator_outputs_present: bool = False
    metadata_send_count: int = 0
    metadata_acceptance_ack_count: int = 0
    maximum_metadata_acceptance_ack_latency_ms: float = 0.0
    maximum_frame_to_acceptance_latency_ms: float = 0.0
    allowed_metadata_acceptance_ack_latency_ms: float = 0.0
    acceptance_ack_peer_host: str | None = None
    acceptance_ack_peer_port: int | None = None
    first_acknowledged_runtime_sequence: int | None = None
    last_acknowledged_runtime_sequence: int | None = None
    unique_acknowledged_runtime_sequence_count: int = 0
    acknowledged_runtime_run_hash64_hex: str | None = None
    first_sent_runtime_sequence: int | None = None
    last_sent_runtime_sequence: int | None = None
    unique_sent_runtime_sequence_count: int = 0
    sent_runtime_sequences_contiguous: bool = False
    acknowledged_runtime_sequences_contiguous: bool = False
    metadata_barrier_count_semantics: str = (
        "legacy_alias_of_canonical_post_adoption_type24_ack_count"
    )

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


# Keep the proven E4 evidence schema stable while exposing a mode-neutral name
# to SIM callers.  Both names intentionally refer to the exact same dataclass
# and therefore serialize to the same mapping.
SynchronizedQgcEvidence = SynchronizedReplayEvidence


class ReplayRtspPublisher(Protocol):
    """Small publisher surface used by the synchronized operator bridge."""

    def start(self) -> None: ...

    def publish_frame(self, image_bgr: Any, *, captured_at_s: float) -> ReplayRtspFrameAck: ...

    def close(self) -> None: ...

    @property
    def stats(self) -> ReplayRtspPublisherStats: ...


class WallClockReplayPacer:
    """Present Replay frames at a selected rate without touching Replay time.

    ``wall_monotonic`` and ``sleep`` are injectable so deterministic tests can
    prove that a ``ManualClock`` is not involved.  Falling behind does not
    rewrite timestamps or drop frames; it is captured as presentation lag.
    """

    def __init__(
        self,
        *,
        playback_rate: float = 1.0,
        wall_monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(playback_rate) or playback_rate <= 0.0:
            raise ValueError("Replay playback_rate must be finite and positive")
        self.playback_rate = float(playback_rate)
        self._wall_monotonic = wall_monotonic
        self._sleep = sleep
        self._first_captured_at_s: float | None = None
        self._wall_started_at_s: float | None = None
        self._last_captured_at_s: float | None = None
        self._paced_frame_count = 0
        self._slept_seconds = 0.0
        self._maximum_lag_seconds = 0.0

    def pace(self, captured_at_s: float) -> None:
        if not math.isfinite(captured_at_s) or captured_at_s < 0.0:
            raise ValueError("Replay captured_at_s must be finite and non-negative")
        if self._last_captured_at_s is not None and captured_at_s <= self._last_captured_at_s:
            raise ValueError("Replay presentation timestamps must be strictly increasing")
        now_s = self._wall_monotonic()
        if not math.isfinite(now_s):
            raise RuntimeError("wall presentation clock returned a non-finite value")
        if self._first_captured_at_s is None:
            self._first_captured_at_s = captured_at_s
            self._wall_started_at_s = now_s
        else:
            if self._wall_started_at_s is None:  # pragma: no cover - state invariant
                raise RuntimeError("Replay pacer lost its wall-clock anchor")
            target_s = self._wall_started_at_s + (
                (captured_at_s - self._first_captured_at_s) / self.playback_rate
            )
            remaining_s = target_s - now_s
            while remaining_s > 0.0:
                before_sleep_s = now_s
                # Windows timer resolution can leave ``time.monotonic`` at
                # the same tick when a high-rate Gazebo frame asks for a
                # sub-millisecond sleep.  Keep the injected clock contract
                # strict, but request one timer quantum so the real clock has
                # a chance to advance instead of falsely failing the replay.
                sleep_s = max(remaining_s, _MIN_WALL_SLEEP_S)
                self._sleep(sleep_s)
                self._slept_seconds += sleep_s
                now_s = self._wall_monotonic()
                if not math.isfinite(now_s):
                    raise RuntimeError("wall presentation clock returned a non-finite value")
                if now_s <= before_sleep_s:
                    raise RuntimeError("wall presentation clock did not advance after sleep")
                remaining_s = target_s - now_s
            self._maximum_lag_seconds = max(self._maximum_lag_seconds, max(0.0, now_s - target_s))
        self._last_captured_at_s = captured_at_s
        self._paced_frame_count += 1

    @property
    def stats(self) -> WallClockPacerStats:
        return WallClockPacerStats(
            paced_frame_count=self._paced_frame_count,
            slept_seconds=self._slept_seconds,
            maximum_lag_seconds=self._maximum_lag_seconds,
            playback_rate=self.playback_rate,
        )


class LiveFramePacer:
    """Validate live-source frame time without adding a second wall-clock delay.

    Gazebo and other live SIM sources have already paced frame delivery.  A
    second sleep here would add latency and break the same-frame RTSP/metadata
    barrier.  This pacer consequently performs no wall-clock reads or sleeps;
    it only rejects non-finite and non-increasing source timestamps.
    """

    def __init__(self) -> None:
        self._last_captured_at_s: float | None = None
        self._paced_frame_count = 0

    def pace(self, captured_at_s: float) -> None:
        if isinstance(captured_at_s, bool) or not isinstance(captured_at_s, (int, float)):
            raise ValueError("Live captured_at_s must be finite")
        timestamp_s = float(captured_at_s)
        if not math.isfinite(timestamp_s):
            raise ValueError("Live captured_at_s must be finite")
        if self._last_captured_at_s is not None and timestamp_s <= self._last_captured_at_s:
            raise ValueError("Live presentation timestamps must be strictly increasing")
        self._last_captured_at_s = timestamp_s
        self._paced_frame_count += 1

    @property
    def stats(self) -> WallClockPacerStats:
        return WallClockPacerStats(
            paced_frame_count=self._paced_frame_count,
            slept_seconds=0.0,
            maximum_lag_seconds=0.0,
            playback_rate=1.0,
        )


class ReplayRtspFrameWriterAdapter:
    """Encode and acknowledge the fixed 88-byte little-endian IPC v1 stream."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        geometry: ReplayVideoGeometry,
        wait_for_event: Callable[[str, float], Mapping[str, object]],
        acknowledgement_timeout_s: float,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not callable(getattr(stream, "write", None)) or not callable(
            getattr(stream, "flush", None)
        ):
            raise TypeError("Replay RTSP IPC stream must be writable and flushable")
        if not isinstance(geometry, ReplayVideoGeometry):
            raise TypeError("geometry must be ReplayVideoGeometry")
        if not math.isfinite(acknowledgement_timeout_s) or acknowledgement_timeout_s <= 0.0:
            raise ValueError("Replay frame acknowledgement timeout must be finite and positive")
        self.stream = stream
        self.geometry = geometry
        self._wait_for_event = wait_for_event
        self.acknowledgement_timeout_s = float(acknowledgement_timeout_s)
        self._monotonic_ns = monotonic_ns
        self._sequence = 0
        self._payload_bytes = 0
        self._first_captured_at_ns: int | None = None
        self._last_captured_at_ns: int | None = None
        self._maximum_ack_latency_ms = 0.0
        self._eos_sent = False
        self._summary_received = False

    def write_frame(self, image_bgr: Any, *, captured_at_s: float) -> ReplayRtspFrameAck:
        if self._eos_sent:
            raise RuntimeError("Replay RTSP IPC stream is already at EOS")
        captured_at_ns = _captured_at_ns(captured_at_s)
        if self._last_captured_at_ns is not None and captured_at_ns <= self._last_captured_at_ns:
            raise ValueError(
                "Replay RTSP IPC timestamps must be strictly increasing in nanoseconds"
            )
        payload = _bgr24_payload(image_bgr, self.geometry)
        digest = hashlib.sha256(payload).digest()
        header = _pack_header(
            message_type=REPLAY_RTSP_MESSAGE_FRAME,
            sequence=self._sequence,
            captured_at_ns=captured_at_ns,
            geometry=self.geometry,
            payload_bytes=len(payload),
            sha256=digest,
        )
        started_ns = self._monotonic_ns()
        try:
            self.stream.write(header)
            self.stream.write(payload)
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError("Replay RTSP helper closed its stdin while writing a frame") from exc
        event = self._wait_for_event("frame_accepted", self.acknowledgement_timeout_s)
        acknowledged_ns = self._monotonic_ns()
        if acknowledged_ns < started_ns:
            raise RuntimeError("Replay RTSP acknowledgement clock moved backwards")
        _require_int_field(event, "sequence", expected=self._sequence)
        _require_int_field(event, "captured_at_ns", expected=captured_at_ns)
        _require_int_field(event, "payload_bytes", expected=len(payload))
        latency_ms = max(0, acknowledged_ns - started_ns) / 1_000_000.0
        self._maximum_ack_latency_ms = max(self._maximum_ack_latency_ms, latency_ms)
        if self._first_captured_at_ns is None:
            self._first_captured_at_ns = captured_at_ns
        self._last_captured_at_ns = captured_at_ns
        self._payload_bytes += len(payload)
        acknowledgement = ReplayRtspFrameAck(
            sequence=self._sequence,
            captured_at_ns=captured_at_ns,
            payload_bytes=len(payload),
            acknowledged_at_monotonic_ns=acknowledged_ns,
        )
        self._sequence += 1
        return acknowledgement

    def write_eos(self) -> Mapping[str, object]:
        if self._eos_sent:
            raise RuntimeError("Replay RTSP IPC EOS was already sent")
        captured_at_ns = self._last_captured_at_ns or 0
        header = _pack_header(
            message_type=REPLAY_RTSP_MESSAGE_EOS,
            sequence=self._sequence,
            captured_at_ns=captured_at_ns,
            geometry=self.geometry,
            payload_bytes=0,
            sha256=b"\0" * 32,
        )
        try:
            self.stream.write(header)
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError("Replay RTSP helper closed its stdin before EOS") from exc
        self._eos_sent = True
        summary = self._wait_for_event("summary", self.acknowledgement_timeout_s)
        _require_int_field(summary, "frame_count", expected=self._sequence)
        _require_int_field(summary, "bytes", expected=self._payload_bytes)
        if self._sequence:
            _require_int_field(
                summary,
                "first_captured_at_ns",
                expected=self._first_captured_at_ns,
            )
            _require_int_field(
                summary,
                "last_captured_at_ns",
                expected=self._last_captured_at_ns,
            )
        self._summary_received = True
        return summary

    def stats(self, *, helper_exit_code: int | None) -> ReplayRtspPublisherStats:
        return ReplayRtspPublisherStats(
            frame_count=self._sequence,
            payload_bytes=self._payload_bytes,
            first_captured_at_ns=self._first_captured_at_ns,
            last_captured_at_ns=self._last_captured_at_ns,
            maximum_ack_latency_ms=self._maximum_ack_latency_ms,
            eos_sent=self._eos_sent,
            summary_received=self._summary_received,
            helper_exit_code=helper_exit_code,
        )


class SubprocessReplayRtspPublisher:
    """Own ``MultiDetectReplayRtspPublisher`` and its strict IPC lifecycle."""

    def __init__(
        self,
        executable: str | Path,
        *,
        bind_host: str,
        port: int,
        mount: str,
        geometry: ReplayVideoGeometry,
        server_ready_timeout_s: float = 10.0,
        client_ready_timeout_s: float = 15.0,
        acknowledgement_timeout_s: float = 2.0,
        process_exit_timeout_s: float = 5.0,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        helper_path = Path(executable).expanduser().resolve(strict=True)
        if not helper_path.is_file():
            raise ValueError("Replay RTSP helper executable must be a regular file")
        if bind_host != "127.0.0.1":
            raise ValueError("Replay RTSP helper must bind exactly to 127.0.0.1")
        _require_port(port, "Replay RTSP helper port")
        if _SAFE_MOUNT.fullmatch(mount) is None or any(
            component in {".", ".."} for component in mount.split("/")
        ):
            raise ValueError("Replay RTSP mount must be an absolute contained URL path")
        if not isinstance(geometry, ReplayVideoGeometry):
            raise TypeError("geometry must be ReplayVideoGeometry")
        _require_h264_geometry(geometry)
        for value, name in (
            (server_ready_timeout_s, "server-ready timeout"),
            (client_ready_timeout_s, "client-ready timeout"),
            (acknowledgement_timeout_s, "frame acknowledgement timeout"),
            (process_exit_timeout_s, "helper exit timeout"),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"Replay RTSP {name} must be finite and positive")
        self.executable = helper_path
        self.bind_host = bind_host
        self.port = port
        self.mount = mount
        self.geometry = geometry
        self.server_ready_timeout_s = float(server_ready_timeout_s)
        self.client_ready_timeout_s = float(client_ready_timeout_s)
        self.acknowledgement_timeout_s = float(acknowledgement_timeout_s)
        self.process_exit_timeout_s = float(process_exit_timeout_s)
        self._popen_factory = popen_factory
        self._monotonic = monotonic
        self._monotonic_ns = monotonic_ns
        self._process: subprocess.Popen[bytes] | None = None
        self._adapter: ReplayRtspFrameWriterAdapter | None = None
        self._event_queue: queue.Queue[Mapping[str, object] | BaseException | None] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_chunks: list[bytes] = []
        self._stderr_bytes = 0
        self._started = False
        self._closed = False
        self._failed = False
        self._helper_exit_code: int | None = None

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://{self.bind_host}:{self.port}{self.mount}"

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("Replay RTSP publisher can only be started once")
        command = [
            str(self.executable),
            "--bind",
            self.bind_host,
            "--port",
            str(self.port),
            "--mount",
            self.mount,
            "--width",
            str(self.geometry.width),
            "--height",
            str(self.geometry.height),
            "--client-timeout-ms",
            str(max(1, math.ceil(self.client_ready_timeout_s * 1_000.0))),
        ]
        try:
            process = self._popen_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.executable.parent),
                shell=False,
                bufsize=0,
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise RuntimeError("Replay RTSP helper did not expose all required pipes")
            self._process = process
            self._stdout_thread = threading.Thread(
                target=self._read_stdout,
                args=(process.stdout,),
                name="multidetect-replay-rtsp-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(process.stderr,),
                name="multidetect-replay-rtsp-stderr",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            server = self._wait_for_event("server_ready", self.server_ready_timeout_s)
            self._validate_server_ready(server)
            client = self._wait_for_event("client_ready", self.client_ready_timeout_s)
            if _require_int_field(client, "client_count", minimum=1) < 1:  # pragma: no cover
                raise RuntimeError("Replay RTSP helper reported no connected QGC client")
            self._adapter = ReplayRtspFrameWriterAdapter(
                process.stdin,
                geometry=self.geometry,
                wait_for_event=self._wait_for_event,
                acknowledgement_timeout_s=self.acknowledgement_timeout_s,
                monotonic_ns=self._monotonic_ns,
            )
            self._started = True
        except BaseException:
            self._failed = True
            self._terminate_process()
            self._join_reader_threads()
            raise

    def publish_frame(self, image_bgr: Any, *, captured_at_s: float) -> ReplayRtspFrameAck:
        if not self._started or self._closed or self._adapter is None:
            raise RuntimeError("Replay RTSP publisher is not active")
        try:
            return self._adapter.write_frame(image_bgr, captured_at_s=captured_at_s)
        except BaseException:
            self._failed = True
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        process = self._process
        if self._started and not self._failed and self._adapter is not None:
            try:
                self._adapter.write_eos()
                if process is not None and process.stdin is not None:
                    process.stdin.close()
                if process is not None:
                    self._helper_exit_code = process.wait(timeout=self.process_exit_timeout_s)
                    if self._helper_exit_code != 0:
                        raise RuntimeError(
                            "Replay RTSP helper exited non-zero after EOS: "
                            f"{self._helper_exit_code}; stderr={self.stderr_text!r}"
                        )
            except BaseException as exc:
                self._failed = True
                if isinstance(exc, subprocess.TimeoutExpired):
                    failure = TimeoutError(
                        "timed out waiting for Replay RTSP helper to exit after EOS"
                    )
                    failure.__cause__ = exc
                else:
                    failure = exc
        self._terminate_process()
        self._join_reader_threads()
        if failure is not None:
            raise failure

    @property
    def stats(self) -> ReplayRtspPublisherStats:
        if self._adapter is None:
            return ReplayRtspPublisherStats(
                frame_count=0,
                payload_bytes=0,
                first_captured_at_ns=None,
                last_captured_at_ns=None,
                maximum_ack_latency_ms=0.0,
                eos_sent=False,
                summary_received=False,
                helper_exit_code=self._helper_exit_code,
            )
        return self._adapter.stats(helper_exit_code=self._helper_exit_code)

    @property
    def stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    def _validate_server_ready(self, event: Mapping[str, object]) -> None:
        if event.get("schema_version") != 1:
            raise RuntimeError("Replay RTSP helper server_ready schema_version must be 1")
        _require_int_field(event, "version", expected=REPLAY_RTSP_IPC_VERSION)
        if event.get("bind") != self.bind_host:
            raise RuntimeError("Replay RTSP helper server_ready bind does not match")
        _require_int_field(event, "port", expected=self.port)
        if event.get("mount") != self.mount or event.get("url") != self.rtsp_url:
            raise RuntimeError("Replay RTSP helper server_ready URL identity does not match")
        _require_int_field(event, "width", expected=self.geometry.width)
        _require_int_field(event, "height", expected=self.geometry.height)

    def _wait_for_event(self, expected_event: str, timeout_s: float) -> Mapping[str, object]:
        deadline = self._monotonic() + timeout_s
        while True:
            remaining_s = deadline - self._monotonic()
            if remaining_s <= 0.0:
                raise TimeoutError(f"timed out waiting for Replay RTSP {expected_event}")
            try:
                item = self._event_queue.get(timeout=remaining_s)
            except queue.Empty as exc:
                raise TimeoutError(
                    f"timed out waiting for Replay RTSP {expected_event}"
                ) from exc
            if item is None:
                return_code = self._process.poll() if self._process is not None else None
                raise RuntimeError(
                    "Replay RTSP helper closed stdout before "
                    f"{expected_event}; exit={return_code}; stderr={self.stderr_text!r}"
                )
            if isinstance(item, BaseException):
                raise RuntimeError("Replay RTSP helper emitted invalid stdout") from item
            if (
                item.get("schema") != REPLAY_RTSP_EVENT_SCHEMA
                or item.get("schema_version") != 1
                or item.get("version") != REPLAY_RTSP_IPC_VERSION
            ):
                raise RuntimeError("Replay RTSP helper event schema/version does not match IPC v1")
            event_name = item.get("event")
            if event_name == "error":
                raise RuntimeError(
                    "Replay RTSP helper error: "
                    f"{item.get('code', item.get('error_type', 'unknown'))}: "
                    f"{item.get('message', '')}"
                )
            if event_name != expected_event:
                raise RuntimeError(
                    "Replay RTSP helper event order mismatch: "
                    f"expected {expected_event}, received {event_name!r}"
                )
            return item

    def _read_stdout(self, stream: BinaryIO) -> None:
        try:
            while True:
                line = stream.readline(_MAX_HELPER_EVENT_BYTES + 1)
                if not line:
                    self._event_queue.put(None)
                    return
                if len(line) > _MAX_HELPER_EVENT_BYTES or not line.endswith(b"\n"):
                    raise ValueError("Replay RTSP helper stdout event exceeds the line limit")
                raw = json.loads(line.decode("utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("event"), str):
                    raise ValueError("Replay RTSP helper stdout must contain JSON object events")
                self._event_queue.put(raw)
        except BaseException as exc:
            self._event_queue.put(exc)

    def _read_stderr(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(4_096)
                if not chunk:
                    return
                remaining = _MAX_STDERR_BYTES - self._stderr_bytes
                if remaining > 0:
                    retained = chunk[:remaining]
                    self._stderr_chunks.append(retained)
                    self._stderr_bytes += len(retained)
        except OSError:
            return

    def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        return_code = process.poll()
        if return_code is None:
            try:
                process.terminate()
                return_code = process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    return_code = process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    return_code = process.poll()
        if return_code is not None:
            self._helper_exit_code = int(return_code)

    def _join_reader_threads(self) -> None:
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.0)


class SynchronizedQgcOperatorBridge(LiveOperatorBridge):
    """Publish a same-frame RTSP ACK barrier before signed QGC metadata.

    The bridge is source-mode neutral: Replay supplies
    :class:`WallClockReplayPacer`, while an already paced live SIM source uses
    :class:`LiveFramePacer`.  Both retain the production
    :class:`LiveOperatorBridge` metadata path.
    """

    _diagnostic_mode = "QGC"
    _frame_limit_subject = "the configured frame count"

    def __init__(
        self,
        transport: RuntimeStatusAcceptanceTransport,
        target_lock: OperatorTargetLock,
        *,
        frame_publisher: ReplayRtspPublisher,
        pacer: FramePacer,
        expected_frame_count: int,
        qgc_peer_ready_timeout_s: float = 15.0,
        maximum_ack_to_metadata_skew_ms: float = 100.0,
        metadata_acceptance_timeout_s: float = 1.0,
        wall_monotonic: Callable[[], float] = time.monotonic,
        wall_monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
        **bridge_options: Any,
    ) -> None:
        if isinstance(expected_frame_count, bool) or not isinstance(expected_frame_count, int):
            raise ValueError("expected_frame_count must be a positive integer")
        if expected_frame_count <= 0:
            raise ValueError("expected_frame_count must be a positive integer")
        if not math.isfinite(qgc_peer_ready_timeout_s) or qgc_peer_ready_timeout_s <= 0.0:
            raise ValueError("QGC peer-ready timeout must be finite and positive")
        if (
            not math.isfinite(maximum_ack_to_metadata_skew_ms)
            or maximum_ack_to_metadata_skew_ms <= 0.0
        ):
            raise ValueError("ACK-to-metadata skew limit must be finite and positive")
        if (
            not math.isfinite(metadata_acceptance_timeout_s)
            or metadata_acceptance_timeout_s <= 0.0
        ):
            raise ValueError("metadata acceptance timeout must be finite and positive")
        if not callable(getattr(pacer, "pace", None)) or not hasattr(pacer, "stats"):
            raise TypeError("pacer must implement FramePacer")
        if frame_publisher is None:
            raise TypeError("frame_publisher is required")
        super().__init__(transport, target_lock, **bridge_options)
        self.frame_publisher = frame_publisher
        self.pacer = pacer
        self.expected_frame_count = expected_frame_count
        self.qgc_peer_ready_timeout_s = float(qgc_peer_ready_timeout_s)
        self.maximum_ack_to_metadata_skew_ms = float(maximum_ack_to_metadata_skew_ms)
        self.metadata_acceptance_timeout_s = float(metadata_acceptance_timeout_s)
        self._wall_monotonic = wall_monotonic
        self._wall_monotonic_ns = wall_monotonic_ns
        self._sleep = sleep
        self._synchronized_started = False
        self._closed_synchronized = False
        self._ready_peer: tuple[str, int] | None = None
        self._video_frame_count = 0
        self._metadata_send_count = 0
        self._metadata_acceptance_ack_count = 0
        self._maximum_observed_skew_ms = 0.0
        self._maximum_metadata_acceptance_ack_latency_ms = 0.0
        self._maximum_frame_to_acceptance_latency_ms = 0.0
        self._acceptance_ack_peer: tuple[str, int] | None = None
        self._sent_runtime_sequences: list[int] = []
        self._acknowledged_runtime_sequences: list[int] = []
        self._acknowledged_runtime_run_hash64: int | None = None
        self._runtime_run_hash64: int | None = None
        self._last_runtime_sequence: int | None = None
        self._current_video_acknowledgement: ReplayRtspFrameAck | None = None

    def start(self) -> None:
        if self._synchronized_started or self._closed_synchronized:
            raise RuntimeError(
                f"synchronized {self._diagnostic_mode} operator bridge can only be started once"
            )
        super().start()
        try:
            self.frame_publisher.start()
            self._ready_peer = self._wait_for_qgc_peer()
            self._synchronized_started = True
        except BaseException:
            try:
                self.frame_publisher.close()
            finally:
                super().close()
            raise

    def process_frame(self, **frame: Any) -> OperatorBridgeResult:
        if not self._synchronized_started or self._closed_synchronized:
            raise RuntimeError(
                f"synchronized {self._diagnostic_mode} operator bridge is not active"
            )
        if self._video_frame_count >= self.expected_frame_count:
            raise RuntimeError(
                f"synchronized {self._diagnostic_mode} produced more frames than "
                f"{self._frame_limit_subject}"
            )
        image_bgr = frame.get("image_bgr")
        if image_bgr is None:
            raise ValueError(
                f"synchronized {self._diagnostic_mode} requires the complete same-frame BGR image"
            )
        captured_at_s = frame.get("captured_at_s")
        if isinstance(captured_at_s, bool) or not isinstance(captured_at_s, (int, float)):
            raise ValueError(
                f"synchronized {self._diagnostic_mode} captured_at_s must be numeric"
            )
        runtime_status = frame.get("runtime_status")
        if runtime_status is None:
            raise ValueError(
                f"synchronized {self._diagnostic_mode} requires a signed runtime status barrier "
                "for every frame"
            )
        runtime_sequence = getattr(runtime_status, "sequence", None)
        runtime_run_hash64 = getattr(runtime_status, "run_hash64", None)
        if (
            isinstance(runtime_sequence, bool)
            or not isinstance(runtime_sequence, int)
            or not 0 <= runtime_sequence <= 0xFFFFFFFF
        ):
            raise ValueError("synchronized runtime status sequence must be uint32")
        if (
            isinstance(runtime_run_hash64, bool)
            or not isinstance(runtime_run_hash64, int)
            or not 1 <= runtime_run_hash64 <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("synchronized runtime status run hash must be non-zero uint64")
        if (
            self._last_runtime_sequence is not None
            and runtime_sequence <= self._last_runtime_sequence
        ):
            raise RuntimeError(
                "synchronized runtime status sequence is duplicate or out of order"
            )
        if self._runtime_run_hash64 is not None and runtime_run_hash64 != self._runtime_run_hash64:
            raise RuntimeError("synchronized runtime status run hash changed during the session")
        peer = self._require_active_qgc_peer()
        if self._ready_peer is None or peer != self._ready_peer:
            raise RuntimeError(
                f"authenticated QGC peer changed during synchronized {self._diagnostic_mode}"
            )

        self.pacer.pace(float(captured_at_s))
        peer_after_pacing = self._require_active_qgc_peer()
        if peer_after_pacing != self._ready_peer:
            raise RuntimeError(
                "authenticated QGC peer changed while pacing synchronized "
                f"{self._diagnostic_mode}"
            )
        acknowledgement = self.frame_publisher.publish_frame(
            image_bgr,
            captured_at_s=float(captured_at_s),
        )
        if acknowledgement.sequence != self._video_frame_count:
            raise RuntimeError("Replay RTSP helper ACK sequence is not frame-aligned")
        self._video_frame_count += 1
        self._current_video_acknowledgement = acknowledgement
        self._runtime_run_hash64 = runtime_run_hash64
        self._last_runtime_sequence = runtime_sequence
        try:
            result = super().process_frame(**frame)
        finally:
            self._current_video_acknowledgement = None
        if self.active_peer != self._ready_peer:
            raise RuntimeError("authenticated QGC heartbeat lease expired before metadata send")
        if result.transport_errors:
            raise RuntimeError(
                "signed QGC metadata pipeline reported an error after the frame ACK: "
                + ", ".join(result.transport_errors)
            )
        if result.published_runtime_statuses != (runtime_status,):
            raise RuntimeError(
                "signed QGC runtime status did not receive a matching post-adoption ACK"
            )
        return result

    def _runtime_status_due(self, status: Any) -> bool:
        """Force one existing signed runtime-status packet after every video ACK.

        The base bridge normally rate-limits unchanged runtime status.  The
        synchronized QGC path uses the same status type and signing path as a
        per-frame barrier marker, so an ACKed frame can never be counted as
        synchronized merely because ordinary metadata heartbeats were not due.
        """

        return True

    def _publish_runtime_status(
        self,
        status: Any,
        peer: tuple[str, int],
        errors: list[str],
    ) -> bool:
        """Send type 23, then require QGC's signed post-adoption type-24 ACK."""

        publish = getattr(self.transport, "publish_runtime_status_for_acceptance", None)
        wait = getattr(self.transport, "wait_runtime_status_ack", None)
        if not callable(publish) or not callable(wait):
            errors.append("RuntimeStatusAcceptanceAckTransportUnavailable")
            return False
        video_ack = self._current_video_acknowledgement
        if video_ack is None:
            errors.append("RuntimeStatusAcceptanceAckMissingVideoBarrier")
            return False
        try:
            send_receipt = publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(f"RuntimeStatusSend{type(exc).__name__}")
            return False
        self._metadata_send_count += 1
        self._sent_runtime_sequences.append(status.sequence)
        try:
            send_sequence = send_receipt.acknowledged_runtime_sequence
            send_run_hash64 = send_receipt.run_hash64
            send_peer = send_receipt.remote
            send_at_ns = send_receipt.sent_at_monotonic_ns
            receipt = wait(
                send_receipt,
                timeout_s=self.metadata_acceptance_timeout_s,
            )
            acknowledgement = receipt.acknowledgement
            acknowledged_sequence = acknowledgement.acknowledged_runtime_sequence
            acknowledged_run_hash64 = acknowledgement.run_hash64
            receipt_peer = receipt.remote
            sent_at_ns = receipt.sent_at_monotonic_ns
            acknowledged_at_ns = receipt.acknowledged_at_monotonic_ns
        except (AttributeError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            errors.append(f"RuntimeStatusAcceptanceAck{type(exc).__name__}")
            return False
        if (
            isinstance(send_sequence, bool)
            or not isinstance(send_sequence, int)
            or not 0 <= send_sequence <= 0xFFFFFFFF
            or isinstance(send_run_hash64, bool)
            or not isinstance(send_run_hash64, int)
            or not 1 <= send_run_hash64 <= 0xFFFFFFFFFFFFFFFF
            or send_sequence != status.sequence
            or send_run_hash64 != status.run_hash64
            or send_peer != peer
        ):
            errors.append("RuntimeStatusAcceptanceAckSendReceiptMismatch")
            return False
        if receipt_peer != peer or receipt_peer != self._ready_peer:
            errors.append("RuntimeStatusAcceptanceAckPeerMismatch")
            return False
        if acknowledged_sequence != status.sequence:
            errors.append("RuntimeStatusAcceptanceAckSequenceMismatch")
            return False
        if acknowledged_run_hash64 != status.run_hash64:
            errors.append("RuntimeStatusAcceptanceAckRunMismatch")
            return False
        video_acknowledged_at_ns = video_ack.acknowledged_at_monotonic_ns
        if (
            isinstance(acknowledged_sequence, bool)
            or not isinstance(acknowledged_sequence, int)
            or not 0 <= acknowledged_sequence <= 0xFFFFFFFF
            or isinstance(acknowledged_run_hash64, bool)
            or not isinstance(acknowledged_run_hash64, int)
            or not 1 <= acknowledged_run_hash64 <= 0xFFFFFFFFFFFFFFFF
            or isinstance(video_acknowledged_at_ns, bool)
            or not isinstance(video_acknowledged_at_ns, int)
            or isinstance(sent_at_ns, bool)
            or not isinstance(sent_at_ns, int)
            or isinstance(acknowledged_at_ns, bool)
            or not isinstance(acknowledged_at_ns, int)
            or isinstance(send_at_ns, bool)
            or not isinstance(send_at_ns, int)
            or send_at_ns != sent_at_ns
            or not 0 <= video_acknowledged_at_ns <= 0xFFFFFFFFFFFFFFFF
            or not 0 <= sent_at_ns <= 0xFFFFFFFFFFFFFFFF
            or not 0 <= acknowledged_at_ns <= 0xFFFFFFFFFFFFFFFF
            or sent_at_ns < video_acknowledged_at_ns
            or acknowledged_at_ns < sent_at_ns
        ):
            errors.append("RuntimeStatusAcceptanceAckClockMismatch")
            return False
        if self._acceptance_ack_peer is not None and receipt_peer != self._acceptance_ack_peer:
            errors.append("RuntimeStatusAcceptanceAckPeerChanged")
            return False

        skew_ns = sent_at_ns - video_acknowledged_at_ns
        ack_latency_ns = acknowledged_at_ns - sent_at_ns
        frame_to_acceptance_ns = (
            acknowledged_at_ns - video_acknowledged_at_ns
        )
        skew_ms = skew_ns / 1_000_000.0
        ack_latency_ms = ack_latency_ns / 1_000_000.0
        frame_to_acceptance_ms = frame_to_acceptance_ns / 1_000_000.0
        if skew_ms > self.maximum_ack_to_metadata_skew_ms:
            errors.append(
                "runtime-status frame ACK-to-metadata skew exceeded "
                f"({skew_ms:.3f} ms > {self.maximum_ack_to_metadata_skew_ms:.3f} ms)"
            )
            return False
        if ack_latency_ms > self.metadata_acceptance_timeout_s * 1000.0:
            errors.append("RuntimeStatusAcceptanceAckLatencyExceeded")
            return False
        if (
            self._acknowledged_runtime_run_hash64 is not None
            and acknowledged_run_hash64 != self._acknowledged_runtime_run_hash64
        ):
            errors.append("RuntimeStatusAcceptanceAckRunChanged")
            return False

        # Commit canonical acceptance evidence only after every peer, sequence,
        # run, clock, skew, and latency check above has passed.
        self._metadata_acceptance_ack_count += 1
        self._acceptance_ack_peer = receipt_peer
        self._acknowledged_runtime_sequences.append(acknowledged_sequence)
        self._acknowledged_runtime_run_hash64 = acknowledged_run_hash64
        self._maximum_observed_skew_ms = max(self._maximum_observed_skew_ms, skew_ms)
        self._maximum_metadata_acceptance_ack_latency_ms = max(
            self._maximum_metadata_acceptance_ack_latency_ms,
            ack_latency_ms,
        )
        self._maximum_frame_to_acceptance_latency_ms = max(
            self._maximum_frame_to_acceptance_latency_ms,
            frame_to_acceptance_ms,
        )
        return True

    def close(self) -> None:
        if self._closed_synchronized:
            return
        self._closed_synchronized = True
        failure: BaseException | None = None
        try:
            self.frame_publisher.close()
        except BaseException as exc:
            failure = exc
        try:
            super().close()
            # ``LiveOperatorBridge.close`` intentionally does nothing before
            # start.  This bridge can own a bound UDP socket before a session
            # builder fails, so close it explicitly in that case.
            if not self._synchronized_started:
                self.transport.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            raise failure

    def require_complete(self) -> SynchronizedQgcEvidence:
        evidence = self.evidence
        stats = self.frame_publisher.stats
        if not self._closed_synchronized:
            raise RuntimeError(
                f"synchronized {self._diagnostic_mode} evidence is unavailable before close"
            )
        if not evidence.complete:
            raise RuntimeError(
                f"synchronized {self._diagnostic_mode} did not complete every "
                "video/metadata barrier: "
                f"video={evidence.video_frame_count}, sent={evidence.metadata_send_count}, "
                f"accepted={evidence.metadata_acceptance_ack_count}, "
                f"legacy_metadata_alias={evidence.metadata_barrier_count}, "
                f"expected={evidence.expected_frame_count}"
            )
        if not stats.eos_sent or not stats.summary_received or stats.helper_exit_code != 0:
            raise RuntimeError("Replay RTSP helper did not close with a verified EOS summary")
        return evidence

    @property
    def evidence(self) -> SynchronizedQgcEvidence:
        publisher_count = self.frame_publisher.stats.frame_count
        unique_sent_sequences = len(set(self._sent_runtime_sequences))
        unique_sequences = len(set(self._acknowledged_runtime_sequences))
        sent_sequences_contiguous = _sequences_are_contiguous(self._sent_runtime_sequences)
        acknowledged_sequences_contiguous = _sequences_are_contiguous(
            self._acknowledged_runtime_sequences
        )
        complete = (
            self._video_frame_count == self.expected_frame_count
            and self._metadata_send_count == self.expected_frame_count
            and self._metadata_acceptance_ack_count == self.expected_frame_count
            and publisher_count == self.expected_frame_count
            and len(self._sent_runtime_sequences) == self.expected_frame_count
            and unique_sent_sequences == self.expected_frame_count
            and len(self._acknowledged_runtime_sequences) == self.expected_frame_count
            and unique_sequences == self.expected_frame_count
            and self._sent_runtime_sequences == self._acknowledged_runtime_sequences
            and sent_sequences_contiguous
            and acknowledged_sequences_contiguous
            and self._acknowledged_runtime_run_hash64 == self._runtime_run_hash64
            and self._acceptance_ack_peer == self._ready_peer
            and self._maximum_observed_skew_ms <= self.maximum_ack_to_metadata_skew_ms
            and self._maximum_metadata_acceptance_ack_latency_ms
            <= self.metadata_acceptance_timeout_s * 1000.0
        )
        return SynchronizedQgcEvidence(
            expected_frame_count=self.expected_frame_count,
            video_frame_count=self._video_frame_count,
            # Compatibility-only alias of the canonical post-adoption ACK
            # count. Keeping one source value makes divergence impossible.
            metadata_barrier_count=self._metadata_acceptance_ack_count,
            maximum_ack_to_metadata_skew_ms=self._maximum_observed_skew_ms,
            allowed_ack_to_metadata_skew_ms=self.maximum_ack_to_metadata_skew_ms,
            qgc_peer_host=self._ready_peer[0] if self._ready_peer is not None else None,
            qgc_peer_port=self._ready_peer[1] if self._ready_peer is not None else None,
            complete=complete,
            metadata_send_count=self._metadata_send_count,
            metadata_acceptance_ack_count=self._metadata_acceptance_ack_count,
            maximum_metadata_acceptance_ack_latency_ms=(
                self._maximum_metadata_acceptance_ack_latency_ms
            ),
            maximum_frame_to_acceptance_latency_ms=(
                self._maximum_frame_to_acceptance_latency_ms
            ),
            allowed_metadata_acceptance_ack_latency_ms=(
                self.metadata_acceptance_timeout_s * 1000.0
            ),
            acceptance_ack_peer_host=(
                self._acceptance_ack_peer[0]
                if self._acceptance_ack_peer is not None
                else None
            ),
            acceptance_ack_peer_port=(
                self._acceptance_ack_peer[1]
                if self._acceptance_ack_peer is not None
                else None
            ),
            first_acknowledged_runtime_sequence=(
                self._acknowledged_runtime_sequences[0]
                if self._acknowledged_runtime_sequences
                else None
            ),
            last_acknowledged_runtime_sequence=(
                self._acknowledged_runtime_sequences[-1]
                if self._acknowledged_runtime_sequences
                else None
            ),
            unique_acknowledged_runtime_sequence_count=unique_sequences,
            acknowledged_runtime_run_hash64_hex=(
                f"{self._acknowledged_runtime_run_hash64:016x}"
                if self._acknowledged_runtime_run_hash64 is not None
                else None
            ),
            first_sent_runtime_sequence=(
                self._sent_runtime_sequences[0] if self._sent_runtime_sequences else None
            ),
            last_sent_runtime_sequence=(
                self._sent_runtime_sequences[-1] if self._sent_runtime_sequences else None
            ),
            unique_sent_runtime_sequence_count=unique_sent_sequences,
            sent_runtime_sequences_contiguous=sent_sequences_contiguous,
            acknowledged_runtime_sequences_contiguous=acknowledged_sequences_contiguous,
        )

    def _wait_for_qgc_peer(self) -> tuple[str, int]:
        deadline = self._wall_monotonic() + self.qgc_peer_ready_timeout_s
        while True:
            error = self.transport.poll_error()
            if error is not None:
                raise RuntimeError(
                    "operator transport failed before QGC heartbeat readiness"
                ) from error
            peer = self._active_heartbeat_peer()
            if peer is not None:
                _require_loopback_peer(peer, diagnostic_mode=self._diagnostic_mode)
                return peer
            remaining_s = deadline - self._wall_monotonic()
            if remaining_s <= 0.0:
                raise TimeoutError("timed out waiting for an authenticated QGC heartbeat peer")
            self._sleep(min(0.01, remaining_s))

    def _require_active_qgc_peer(self) -> tuple[str, int]:
        peer = self._active_heartbeat_peer()
        if peer is None:
            raise RuntimeError("authenticated QGC heartbeat peer is not active")
        _require_loopback_peer(peer, diagnostic_mode=self._diagnostic_mode)
        return peer

    def _active_heartbeat_peer(self) -> tuple[str, int] | None:
        heartbeat_peer = getattr(self.transport, "active_heartbeat_peer", None)
        if callable(heartbeat_peer):
            return heartbeat_peer()
        # Compatibility for small test transports and older custom transports;
        # the production UDP transport implements the strict heartbeat method.
        return self.transport.active_metadata_peer()


class SynchronizedReplayOperatorBridge(SynchronizedQgcOperatorBridge):
    """Backward-compatible Replay facade with the original diagnostics."""

    _diagnostic_mode = "Replay"
    _frame_limit_subject = "the recording"

    def __init__(
        self,
        transport: RuntimeStatusAcceptanceTransport,
        target_lock: OperatorTargetLock,
        *,
        frame_publisher: ReplayRtspPublisher,
        pacer: WallClockReplayPacer,
        expected_frame_count: int,
        qgc_peer_ready_timeout_s: float = 15.0,
        maximum_ack_to_metadata_skew_ms: float = 100.0,
        metadata_acceptance_timeout_s: float = 1.0,
        wall_monotonic: Callable[[], float] = time.monotonic,
        wall_monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
        **bridge_options: Any,
    ) -> None:
        if not isinstance(pacer, WallClockReplayPacer):
            raise TypeError("pacer must be WallClockReplayPacer")
        super().__init__(
            transport,
            target_lock,
            frame_publisher=frame_publisher,
            pacer=pacer,
            expected_frame_count=expected_frame_count,
            qgc_peer_ready_timeout_s=qgc_peer_ready_timeout_s,
            maximum_ack_to_metadata_skew_ms=maximum_ack_to_metadata_skew_ms,
            metadata_acceptance_timeout_s=metadata_acceptance_timeout_s,
            wall_monotonic=wall_monotonic,
            wall_monotonic_ns=wall_monotonic_ns,
            sleep=sleep,
            **bridge_options,
        )


def recording_video_geometry(recording: Any) -> ReplayVideoGeometry:
    """Preflight fixed geometry and nanosecond timestamp representability."""

    frames = getattr(recording, "frames", None)
    if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes, bytearray)):
        raise TypeError("raw Replay recording must expose a frame sequence")
    if not frames:
        raise ValueError("raw Replay recording contains no frames")
    first_image = getattr(frames[0], "image", None)
    geometry = ReplayVideoGeometry.bgr24(
        _positive_int(getattr(first_image, "width", None), "recording frame width"),
        _positive_int(getattr(first_image, "height", None), "recording frame height"),
    )
    _require_h264_geometry(geometry)
    previous_ns: int | None = None
    for index, frame in enumerate(frames):
        image = getattr(frame, "image", None)
        observed = (
            _positive_int(getattr(image, "width", None), f"frame {index} width"),
            _positive_int(getattr(image, "height", None), f"frame {index} height"),
        )
        if observed != (geometry.width, geometry.height):
            raise ValueError("raw Replay QGC publication requires fixed frame geometry")
        captured_ns = _captured_at_ns(getattr(frame, "captured_at_s", None))
        if previous_ns is not None and captured_ns <= previous_ns:
            raise ValueError(
                "raw Replay QGC publication requires strictly increasing nanosecond timestamps"
            )
        previous_ns = captured_ns
    return geometry


def _pack_header(
    *,
    message_type: int,
    sequence: int,
    captured_at_ns: int,
    geometry: ReplayVideoGeometry,
    payload_bytes: int,
    sha256: bytes,
) -> bytes:
    if message_type not in {REPLAY_RTSP_MESSAGE_FRAME, REPLAY_RTSP_MESSAGE_EOS}:
        raise ValueError("unsupported Replay RTSP IPC message type")
    for value, name in (
        (sequence, "sequence"),
        (captured_at_ns, "captured_at_ns"),
        (payload_bytes, "payload_bytes"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError(f"Replay RTSP IPC {name} must fit uint64")
    if not isinstance(sha256, bytes) or len(sha256) != 32:
        raise ValueError("Replay RTSP IPC SHA-256 must contain exactly 32 bytes")
    return _HEADER.pack(
        REPLAY_RTSP_IPC_MAGIC,
        REPLAY_RTSP_IPC_VERSION,
        message_type,
        REPLAY_RTSP_IPC_HEADER_BYTES,
        sequence,
        captured_at_ns,
        geometry.width,
        geometry.height,
        geometry.stride,
        geometry.pixel_format,
        payload_bytes,
        sha256,
    )


def _bgr24_payload(image_bgr: Any, geometry: ReplayVideoGeometry) -> memoryview:
    shape = getattr(image_bgr, "shape", None)
    if not isinstance(shape, Sequence) or tuple(shape) != (
        geometry.height,
        geometry.width,
        3,
    ):
        raise ValueError("Replay RTSP frame shape does not match fixed BGR geometry")
    dtype = getattr(getattr(image_bgr, "dtype", None), "name", None)
    if dtype != "uint8":
        raise ValueError("Replay RTSP BGR frame dtype must be uint8")
    flags = getattr(image_bgr, "flags", None)
    if flags is None or not bool(getattr(flags, "c_contiguous", False)):
        raise ValueError("Replay RTSP BGR frame must be C-contiguous")
    strides = getattr(image_bgr, "strides", None)
    if not isinstance(strides, Sequence) or tuple(strides) != (geometry.stride, 3, 1):
        raise ValueError("Replay RTSP BGR frame strides do not match packed BGR24")
    try:
        payload = memoryview(image_bgr).cast("B")
    except (TypeError, ValueError) as exc:
        raise ValueError("Replay RTSP BGR frame does not expose a byte buffer") from exc
    if payload.nbytes != geometry.payload_bytes:
        raise ValueError("Replay RTSP BGR payload size does not match fixed geometry")
    return payload


def _captured_at_ns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Replay captured_at_s must be numeric")
    captured_at_s = float(value)
    if not math.isfinite(captured_at_s) or captured_at_s < 0.0:
        raise ValueError("Replay captured_at_s must be finite and non-negative")
    captured_at_ns = int(math.floor(captured_at_s * 1_000_000_000.0 + 0.5))
    if not 0 <= captured_at_ns <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Replay captured_at_s cannot be represented as uint64 nanoseconds")
    return captured_at_ns


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _sequences_are_contiguous(sequences: Sequence[int]) -> bool:
    """Return true only for a non-empty, gap-free increasing sequence."""

    return bool(sequences) and all(
        current == previous + 1
        for previous, current in zip(sequences, sequences[1:], strict=False)
    )


def _require_int_field(
    event: Mapping[str, object],
    name: str,
    *,
    expected: int | None = None,
    minimum: int | None = None,
) -> int:
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Replay RTSP helper {name} must be an integer")
    if expected is not None and value != expected:
        raise RuntimeError(
            f"Replay RTSP helper {name} mismatch: expected {expected}, observed {value}"
        )
    if minimum is not None and value < minimum:
        raise RuntimeError(f"Replay RTSP helper {name} must be at least {minimum}")
    return value


def _require_port(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ValueError(f"{name} must be in [1, 65535]")
    return value


def _require_h264_geometry(geometry: ReplayVideoGeometry) -> None:
    if geometry.width % 2 != 0 or geometry.height % 2 != 0:
        raise ValueError("Replay RTSP H.264 width and height must both be even")


def _require_loopback_peer(peer: tuple[str, int], *, diagnostic_mode: str = "Replay") -> None:
    if not isinstance(peer, tuple) or len(peer) != 2:
        raise RuntimeError("QGC metadata peer must be a host/port pair")
    host, port = peer
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError("QGC metadata peer host is not an IP address") from exc
    if not address.is_loopback:
        raise RuntimeError(
            f"synchronized {diagnostic_mode} QGC metadata peer must be loopback-only"
        )
    _require_port(port, "QGC metadata peer port")


__all__ = [
    "FramePacer",
    "LiveFramePacer",
    "REPLAY_RTSP_IPC_HEADER_BYTES",
    "REPLAY_RTSP_IPC_MAGIC",
    "REPLAY_RTSP_IPC_VERSION",
    "REPLAY_RTSP_EVENT_SCHEMA",
    "REPLAY_RTSP_MESSAGE_EOS",
    "REPLAY_RTSP_MESSAGE_FRAME",
    "REPLAY_RTSP_PIXEL_FORMAT_BGR24",
    "ReplayRtspFrameAck",
    "ReplayRtspFrameWriterAdapter",
    "ReplayRtspPublisherStats",
    "ReplayVideoGeometry",
    "SubprocessReplayRtspPublisher",
    "SynchronizedReplayEvidence",
    "SynchronizedReplayOperatorBridge",
    "SynchronizedQgcEvidence",
    "SynchronizedQgcOperatorBridge",
    "WallClockPacerStats",
    "WallClockReplayPacer",
    "recording_video_geometry",
]

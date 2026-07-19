from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .evidence_session import normalize_evidence_session_id
from .video_evidence import VideoEvidenceProbe, probe_video_evidence, video_evidence_document


@dataclass(frozen=True, slots=True)
class RtspEvidenceRecordingConfig:
    source_env: str
    session_id: str
    output_video: Path
    manifest_out: Path
    duration_s: float = 30.0
    latency_ms: int = 100
    finalize_timeout_s: float = 5.0
    overwrite: bool = False

    def __post_init__(self) -> None:
        if not self.source_env.strip():
            raise ValueError("RTSP evidence source environment name cannot be empty")
        object.__setattr__(self, "session_id", normalize_evidence_session_id(self.session_id))
        if self.output_video.suffix.lower() not in {".mkv", ".matroska"}:
            raise ValueError("RTSP evidence output must use .mkv or .matroska")
        if self.output_video == self.manifest_out:
            raise ValueError("RTSP evidence video and manifest paths must differ")
        if not math.isfinite(self.duration_s) or not 1.0 <= self.duration_s <= 86_400.0:
            raise ValueError("RTSP evidence duration must be in [1, 86400] seconds")
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or not 0 <= self.latency_ms <= 10_000
        ):
            raise ValueError("RTSP evidence latency must be an integer in [0, 10000] ms")
        if not math.isfinite(self.finalize_timeout_s) or not 1.0 <= self.finalize_timeout_s <= 60.0:
            raise ValueError("RTSP evidence finalize timeout must be in [1, 60] seconds")
        if not isinstance(self.overwrite, bool):
            raise ValueError("RTSP evidence overwrite must be a boolean")


@dataclass(frozen=True, slots=True)
class StreamCopyResult:
    actual_duration_s: float
    eos_received: bool
    started_at_monotonic_s: float
    ended_at_monotonic_s: float
    stream_copy_no_decode_or_reencode: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.actual_duration_s) or self.actual_duration_s <= 0.0:
            raise ValueError("stream-copy actual duration must be finite and positive")
        if not all(
            math.isfinite(value)
            for value in (self.started_at_monotonic_s, self.ended_at_monotonic_s)
        ):
            raise ValueError("stream-copy monotonic timestamps must be finite")
        if self.started_at_monotonic_s < 0.0:
            raise ValueError("stream-copy start timestamp must be non-negative")
        if self.ended_at_monotonic_s <= self.started_at_monotonic_s:
            raise ValueError("stream-copy end timestamp must be later than start")


@dataclass(frozen=True, slots=True)
class RtspEvidenceRecordingReport:
    session_id: str
    output_video: Path
    manifest_out: Path
    requested_duration_s: float
    actual_duration_s: float
    started_at_monotonic_s: float
    ended_at_monotonic_s: float
    output_bytes: int
    output_sha256: str
    video_probe: VideoEvidenceProbe
    eos_received: bool
    passed: bool
    source_uri_recorded: bool = False
    stream_copy_no_decode_or_reencode: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False


def record_rtsp_evidence(
    config: RtspEvidenceRecordingConfig,
    *,
    environ: Mapping[str, str] | None = None,
    stream_copy_runner: Callable[[str, Path, float, int, float], StreamCopyResult] | None = None,
    video_probe: Callable[[Path], VideoEvidenceProbe] | None = None,
) -> RtspEvidenceRecordingReport:
    environment = os.environ if environ is None else environ
    source = environment.get(config.source_env)
    if source is None or not source.strip():
        raise ValueError(
            f"RTSP evidence source environment variable is missing: {config.source_env}"
        )
    if not source.lower().startswith("rtsp://"):
        raise ValueError("RTSP evidence source must be an rtsp:// URL")
    existing = tuple(path for path in (config.output_video, config.manifest_out) if path.exists())
    if existing and not config.overwrite:
        raise ValueError("RTSP evidence output already exists; explicit overwrite is required")
    config.output_video.parent.mkdir(parents=True, exist_ok=True)
    config.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    temporary_video = config.output_video.with_name(
        f".{config.output_video.stem}.{uuid4().hex}.tmp{config.output_video.suffix}"
    )
    runner = stream_copy_runner or _record_h265_rtsp_stream_copy
    try:
        copy_result = runner(
            source,
            temporary_video,
            config.duration_s,
            config.latency_ms,
            config.finalize_timeout_s,
        )
        if not temporary_video.is_file() or temporary_video.stat().st_size <= 0:
            raise RuntimeError("RTSP stream-copy recorder produced no output bytes")
        os.replace(temporary_video, config.output_video)
    finally:
        temporary_video.unlink(missing_ok=True)

    probe = (video_probe or probe_video_evidence)(config.output_video)
    report = RtspEvidenceRecordingReport(
        session_id=config.session_id,
        output_video=config.output_video,
        manifest_out=config.manifest_out,
        requested_duration_s=config.duration_s,
        actual_duration_s=copy_result.actual_duration_s,
        started_at_monotonic_s=copy_result.started_at_monotonic_s,
        ended_at_monotonic_s=copy_result.ended_at_monotonic_s,
        output_bytes=config.output_video.stat().st_size,
        output_sha256=_sha256_file(config.output_video),
        video_probe=probe,
        eos_received=copy_result.eos_received,
        stream_copy_no_decode_or_reencode=(copy_result.stream_copy_no_decode_or_reencode),
        passed=(
            copy_result.eos_received
            and copy_result.stream_copy_no_decode_or_reencode
            and probe.passed
        ),
    )
    _atomic_write_text(
        config.manifest_out,
        json.dumps(
            rtsp_evidence_recording_document(report),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n",
    )
    return report


def rtsp_evidence_recording_document(
    report: RtspEvidenceRecordingReport,
) -> dict[str, object]:
    return {
        "event": "rtsp_tracking_evidence_recording_completed",
        "schema_version": 2,
        "session_id": report.session_id,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_description": "RTSP source",
        "source_uri_recorded": report.source_uri_recorded,
        "transport": "tcp",
        "codec": "h265",
        "container": "matroska",
        "requested_duration_s": report.requested_duration_s,
        "actual_duration_s": report.actual_duration_s,
        "started_at_monotonic_s": report.started_at_monotonic_s,
        "ended_at_monotonic_s": report.ended_at_monotonic_s,
        "output_video": str(report.output_video),
        "output_bytes": report.output_bytes,
        "output_sha256": report.output_sha256,
        "eos_received": report.eos_received,
        "stream_copy_no_decode_or_reencode": report.stream_copy_no_decode_or_reencode,
        "video_probe": video_evidence_document(report.video_probe),
        "passed": report.passed,
        "flight_control_enabled": report.flight_control_enabled,
        "physical_release_enabled": report.physical_release_enabled,
    }


def _record_h265_rtsp_stream_copy(
    source: str,
    output_video: Path,
    duration_s: float,
    latency_ms: int,
    finalize_timeout_s: float,
) -> StreamCopyResult:
    Gst, GstRtsp = _require_gstreamer()
    pipeline = Gst.Pipeline.new("multidetect-rtsp-evidence")
    if pipeline is None:
        raise RuntimeError("GStreamer failed to create the evidence pipeline")
    source_element = _gst_element(Gst, "rtspsrc", "source")
    depay = _gst_element(Gst, "rtph265depay", "depay")
    parser = _gst_element(Gst, "h265parse", "parser")
    muxer = _gst_element(Gst, "matroskamux", "muxer")
    sink = _gst_element(Gst, "filesink", "sink")
    source_element.set_property("location", source)
    source_element.set_property("protocols", GstRtsp.RTSPLowerTrans.TCP)
    source_element.set_property("latency", latency_ms)
    parser.set_property("config-interval", -1)
    sink.set_property("location", str(output_video))
    for element in (source_element, depay, parser, muxer, sink):
        pipeline.add(element)
    if not depay.link(parser) or not parser.link(muxer) or not muxer.link(sink):
        raise RuntimeError("GStreamer failed to link the H.265 stream-copy pipeline")
    dynamic_pad_failures: list[str] = []

    def on_pad_added(_source: Any, pad: Any) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or "H265" not in caps.to_string().upper():
            return
        sink_pad = depay.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        result = pad.link(sink_pad)
        if result != Gst.PadLinkReturn.OK:
            dynamic_pad_failures.append("RTSP source pad could not link to H.265 depayloader")

    source_element.connect("pad-added", on_pad_added)
    bus = pipeline.get_bus()
    started_at_s = time.monotonic()
    eos_received = False
    try:
        state_result = pipeline.set_state(Gst.State.PLAYING)
        if state_result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer RTSP evidence pipeline could not enter PLAYING")
        deadline_s = started_at_s + duration_s
        message_mask = Gst.MessageType.ERROR | Gst.MessageType.EOS
        while True:
            remaining_s = deadline_s - time.monotonic()
            if remaining_s <= 0.0:
                break
            message = bus.timed_pop_filtered(
                int(min(remaining_s, 0.25) * Gst.SECOND),
                message_mask,
            )
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                error, _debug = message.parse_error()
                raise RuntimeError("GStreamer RTSP stream-copy failed with " + type(error).__name__)
            if message.type == Gst.MessageType.EOS:
                eos_received = True
                break
        if dynamic_pad_failures:
            raise RuntimeError(dynamic_pad_failures[0])
        if not eos_received:
            if not pipeline.send_event(Gst.Event.new_eos()):
                raise RuntimeError("GStreamer RTSP evidence pipeline rejected EOS")
            finalize_deadline_s = time.monotonic() + finalize_timeout_s
            while time.monotonic() < finalize_deadline_s:
                message = bus.timed_pop_filtered(
                    int(min(0.25, finalize_deadline_s - time.monotonic()) * Gst.SECOND),
                    message_mask,
                )
                if message is None:
                    continue
                if message.type == Gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    raise RuntimeError(
                        "GStreamer RTSP stream-copy finalization failed with "
                        + type(error).__name__
                    )
                if message.type == Gst.MessageType.EOS:
                    eos_received = True
                    break
        if not eos_received:
            raise RuntimeError("GStreamer RTSP evidence pipeline did not finalize before timeout")
    finally:
        pipeline.set_state(Gst.State.NULL)
    ended_at_s = time.monotonic()
    return StreamCopyResult(
        actual_duration_s=max(0.0, ended_at_s - started_at_s),
        eos_received=eos_received,
        started_at_monotonic_s=started_at_s,
        ended_at_monotonic_s=ended_at_s,
    )


def _require_gstreamer() -> tuple[Any, Any]:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstRtsp", "1.0")
        from gi.repository import Gst, GstRtsp
    except (ImportError, ValueError) as exc:
        raise RuntimeError("Python GStreamer bindings with GstRtsp are required") from exc
    Gst.init(None)
    return Gst, GstRtsp


def _gst_element(Gst: Any, factory: str, name: str) -> Any:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element is unavailable: {factory}")
    return element


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "RtspEvidenceRecordingConfig",
    "RtspEvidenceRecordingReport",
    "StreamCopyResult",
    "record_rtsp_evidence",
    "rtsp_evidence_recording_document",
]

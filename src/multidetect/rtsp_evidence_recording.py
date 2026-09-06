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

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .evidence_session import normalize_evidence_session_id
from .video_evidence import VideoEvidenceProbe, probe_video_evidence, video_evidence_document

RTSP_EVIDENCE_SCHEMA_VERSION = 3


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
    started_at_utc: str
    ended_at_utc: str
    recorded_at_utc: str
    output_bytes: int
    output_sha256: str
    video_probe: VideoEvidenceProbe
    eos_received: bool
    passed: bool
    source_uri_recorded: bool = False
    stream_copy_no_decode_or_reencode: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False


@dataclass(frozen=True, slots=True)
class VerifiedRtspEvidenceRecording:
    manifest_path: Path
    manifest_sha256: str
    manifest_bytes: int
    canonical_hash: str
    session_id: str
    output_video: Path
    output_bytes: int
    output_sha256: str
    started_at_utc: str
    ended_at_utc: str
    actual_duration_s: float
    video_probe: VideoEvidenceProbe


def record_rtsp_evidence(
    config: RtspEvidenceRecordingConfig,
    *,
    environ: Mapping[str, str] | None = None,
    stream_copy_runner: Callable[[str, Path, float, int, float], StreamCopyResult] | None = None,
    video_probe: Callable[[Path], VideoEvidenceProbe] | None = None,
    utc_now: Callable[[], datetime] | None = None,
) -> RtspEvidenceRecordingReport:
    environment = os.environ if environ is None else environ
    source = environment.get(config.source_env)
    if source is None or not source.strip():
        raise ValueError(
            f"RTSP evidence source environment variable is missing: {config.source_env}"
        )
    if not source.lower().startswith("rtsp://"):
        raise ValueError("RTSP evidence source must be an rtsp:// URL")
    output_video = config.output_video.expanduser().resolve()
    manifest_out = config.manifest_out.expanduser().resolve()
    if output_video == manifest_out:
        raise ValueError("RTSP evidence video and manifest paths must differ")
    existing = tuple(path for path in (output_video, manifest_out) if path.exists())
    if existing and not config.overwrite:
        raise ValueError("RTSP evidence output already exists; explicit overwrite is required")
    output_video.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    temporary_video = output_video.with_name(
        f".{output_video.stem}.{uuid4().hex}.tmp{output_video.suffix}"
    )
    runner = stream_copy_runner or _record_h265_rtsp_stream_copy
    clock = utc_now or (lambda: datetime.now(timezone.utc))
    started_at_utc = _require_utc_clock_value(clock(), "RTSP capture start")
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
        ended_at_utc = _require_utc_clock_value(clock(), "RTSP capture end")
        if ended_at_utc <= started_at_utc:
            raise RuntimeError("RTSP capture UTC end must be later than its UTC start")
        os.replace(temporary_video, output_video)
    finally:
        temporary_video.unlink(missing_ok=True)

    probe = (video_probe or probe_video_evidence)(output_video)
    report = RtspEvidenceRecordingReport(
        session_id=config.session_id,
        output_video=output_video,
        manifest_out=manifest_out,
        requested_duration_s=config.duration_s,
        actual_duration_s=copy_result.actual_duration_s,
        started_at_monotonic_s=copy_result.started_at_monotonic_s,
        ended_at_monotonic_s=copy_result.ended_at_monotonic_s,
        started_at_utc=_format_utc(started_at_utc),
        ended_at_utc=_format_utc(ended_at_utc),
        recorded_at_utc=_format_utc(ended_at_utc),
        output_bytes=output_video.stat().st_size,
        output_sha256=_sha256_file(output_video),
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
        manifest_out,
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
    document: dict[str, object] = {
        "event": "rtsp_tracking_evidence_recording_completed",
        "schema_version": RTSP_EVIDENCE_SCHEMA_VERSION,
        "session_id": report.session_id,
        "recorded_at_utc": report.recorded_at_utc,
        "started_at_utc": report.started_at_utc,
        "ended_at_utc": report.ended_at_utc,
        "utc_clock": "system_utc_enclosing_stream_copy",
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
    document["canonical_hash"] = canonical_sha256(document)
    return document


def verify_rtsp_evidence_recording_manifest(
    manifest: str | Path,
    *,
    expected_video: str | Path | None = None,
    video_probe: Callable[[Path], VideoEvidenceProbe] | None = None,
) -> VerifiedRtspEvidenceRecording:
    """Reopen a v3 RTSP manifest and its exact container fail-closed."""

    manifest_path = Path(manifest).expanduser()
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AutonomyContractError("RTSP evidence manifest must be a regular non-symlink file")
    manifest_path = manifest_path.resolve(strict=True)
    payload = manifest_path.read_bytes()
    raw = strict_json_loads(payload, name="RTSP evidence manifest")
    value = strict_object(
        raw,
        name="RtspEvidenceRecordingManifest",
        required=frozenset(
            {
                "event",
                "schema_version",
                "session_id",
                "recorded_at_utc",
                "started_at_utc",
                "ended_at_utc",
                "utc_clock",
                "source_description",
                "source_uri_recorded",
                "transport",
                "codec",
                "container",
                "requested_duration_s",
                "actual_duration_s",
                "started_at_monotonic_s",
                "ended_at_monotonic_s",
                "output_video",
                "output_bytes",
                "output_sha256",
                "eos_received",
                "stream_copy_no_decode_or_reencode",
                "video_probe",
                "passed",
                "flight_control_enabled",
                "physical_release_enabled",
                "canonical_hash",
            }
        ),
    )
    require_schema_version(value["schema_version"], expected=RTSP_EVIDENCE_SCHEMA_VERSION)
    if value["event"] != "rtsp_tracking_evidence_recording_completed":
        raise AutonomyContractError("RTSP evidence manifest event is invalid")
    session_id = normalize_evidence_session_id(require_text(value["session_id"], "session_id"))
    expected_constants = {
        "utc_clock": "system_utc_enclosing_stream_copy",
        "source_description": "RTSP source",
        "source_uri_recorded": False,
        "transport": "tcp",
        "codec": "h265",
        "container": "matroska",
        "eos_received": True,
        "stream_copy_no_decode_or_reencode": True,
        "passed": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }
    for field, expected in expected_constants.items():
        observed = value[field]
        if isinstance(expected, bool):
            observed = require_bool(observed, field)
        if observed != expected:
            raise AutonomyContractError(f"RTSP evidence manifest {field} is invalid")

    started_at_utc = _parse_utc(value["started_at_utc"], "started_at_utc")
    ended_at_utc = _parse_utc(value["ended_at_utc"], "ended_at_utc")
    recorded_at_utc = _parse_utc(value["recorded_at_utc"], "recorded_at_utc")
    if ended_at_utc <= started_at_utc:
        raise AutonomyContractError("RTSP evidence UTC window is not positive")
    if recorded_at_utc != ended_at_utc:
        raise AutonomyContractError("recorded_at_utc must equal ended_at_utc")

    require_real(
        value["requested_duration_s"], "requested_duration_s", minimum=1.0, maximum=86_400.0
    )
    actual_duration_s = require_real(
        value["actual_duration_s"], "actual_duration_s", minimum=0.000_001
    )
    started_monotonic = require_real(
        value["started_at_monotonic_s"], "started_at_monotonic_s", minimum=0.0
    )
    ended_monotonic = require_real(
        value["ended_at_monotonic_s"], "ended_at_monotonic_s", minimum=0.0
    )
    if ended_monotonic <= started_monotonic:
        raise AutonomyContractError("RTSP evidence monotonic window is not positive")
    if abs((ended_monotonic - started_monotonic) - actual_duration_s) > 0.25:
        raise AutonomyContractError(
            "RTSP evidence actual duration contradicts monotonic timestamps"
        )
    stored_video = Path(require_text(value["output_video"], "output_video"))
    candidate = stored_video if stored_video.is_absolute() else manifest_path.parent / stored_video
    if candidate.is_symlink() or not candidate.is_file():
        raise AutonomyContractError("RTSP evidence video must be a regular non-symlink file")
    output_video = candidate.resolve(strict=True)
    if expected_video is not None:
        expected_path = Path(expected_video).expanduser()
        if expected_path.is_symlink() or not expected_path.is_file():
            raise AutonomyContractError("expected RTSP evidence video must be a regular file")
        if expected_path.resolve(strict=True) != output_video:
            raise AutonomyContractError("RTSP evidence manifest points at a different video")
    output_bytes = require_int(value["output_bytes"], "output_bytes", minimum=1)
    if output_video.stat().st_size != output_bytes:
        raise AutonomyContractError("RTSP evidence video byte count changed")
    output_sha256 = require_sha256(value["output_sha256"], "output_sha256")
    if _sha256_file(output_video) != output_sha256:
        raise AutonomyContractError("RTSP evidence video SHA-256 changed")

    observed_probe = (video_probe or probe_video_evidence)(output_video)
    probe_document = strict_object(
        value["video_probe"],
        name="video_probe",
        required=frozenset(
            {
                "decoded_frame_count",
                "declared_frame_count",
                "fps",
                "width",
                "height",
                "duration_s",
                "full_frame_scan_completed",
                "stable_dimensions",
                "passed",
                "failure_reasons",
            }
        ),
    )
    if dict(probe_document) != video_evidence_document(observed_probe):
        raise AutonomyContractError("RTSP evidence video probe changed")
    if not observed_probe.passed:
        raise AutonomyContractError("RTSP evidence video probe did not pass")

    canonical_hash = require_sha256(value["canonical_hash"], "canonical_hash")
    unhashed = dict(value)
    unhashed.pop("canonical_hash")
    if canonical_sha256(unhashed) != canonical_hash:
        raise AutonomyContractError("RTSP evidence manifest canonical_hash mismatch")
    return VerifiedRtspEvidenceRecording(
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        manifest_bytes=len(payload),
        canonical_hash=canonical_hash,
        session_id=session_id,
        output_video=output_video,
        output_bytes=output_bytes,
        output_sha256=output_sha256,
        started_at_utc=_format_utc(started_at_utc),
        ended_at_utc=_format_utc(ended_at_utc),
        actual_duration_s=actual_duration_s,
        video_probe=observed_probe,
    )


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


def _require_utc_clock_value(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError(f"{name} clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(f"{name} clock must return timezone-aware UTC")
    if value.utcoffset().total_seconds() != 0:
        raise RuntimeError(f"{name} clock must return UTC")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, name: str) -> datetime:
    text = require_text(value, name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} must be RFC3339 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomyContractError(f"{name} must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise AutonomyContractError(f"{name} must use UTC")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
    "RTSP_EVIDENCE_SCHEMA_VERSION",
    "RtspEvidenceRecordingConfig",
    "RtspEvidenceRecordingReport",
    "StreamCopyResult",
    "VerifiedRtspEvidenceRecording",
    "record_rtsp_evidence",
    "rtsp_evidence_recording_document",
    "verify_rtsp_evidence_recording_manifest",
]

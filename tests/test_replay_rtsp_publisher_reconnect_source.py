from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = (
    ROOT
    / "ground-control"
    / "MultiDetectGCS"
    / "custom"
    / "tools"
    / "MultiDetectReplayRtspPublisher.cc"
)
GENERATION = PUBLISHER.with_name("MultiDetectReplayRtspGeneration.h")


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unterminated function body for {signature}")


def test_rtsp_client_close_is_not_a_permanent_process_global_latch() -> None:
    source = PUBLISHER.read_text(encoding="utf-8")
    closed = _function_body(source, "static void clientClosedCallback")

    assert "_clientLost" not in source
    assert "shutdownAppSrc" not in closed
    assert "_clientCount" in closed
    assert "gst_app_src_end_of_stream" in closed
    assert "_appSrcs" in closed
    assert "for (GstAppSrc* source : sources)" in closed


def test_rtsp_unprepared_cleanup_is_generation_identity_safe() -> None:
    source = PUBLISHER.read_text(encoding="utf-8")
    unprepared = _function_body(source, "static void mediaUnpreparedCallback")
    generation = GENERATION.read_text(encoding="utf-8")

    assert "MediaBinding" in source
    assert "_sharedFactory = _options.minimumClientCount > 1" in source
    assert "gst_rtsp_media_factory_set_shared(factory, _sharedFactory ? TRUE : FALSE)" in source
    assert "binding->publisher->retireAppSrc(binding->generation)" in unprepared
    assert "generation != _current" in generation
    assert "_current = ++_lastPublished" in generation
    assert re.search(r"waitForAppSrcAfter\s*\(", source)


def test_rtsp_reconnect_keeps_strict_stdout_frame_barrier() -> None:
    source = PUBLISHER.read_text(encoding="utf-8")
    publish = _function_body(source, "publishAppSrc(GstAppSrc* source)")
    reader = _function_body(source, "void readerLoop()")

    # client_ready is emitted once for Python start(); reconnect generations
    # remain transparent until an actual appsrc push produces frame_accepted.
    assert "emitInitialClientReadyIfReady" in publish
    assert "ready_for_minimum" in source
    assert "generation_retry_count" in reader
    assert 'JsonEvent accepted("frame_accepted")' in reader
    assert reader.count('JsonEvent accepted("frame_accepted")') == 1
    assert "gst_app_src_push_buffer" in reader
    assert "gst_buffer_ref(frameBuffer)" in reader
    assert "deliveredGenerations" in reader
    assert (
        'accepted.number("fanout_count", _sharedFactory ? lastClientCount : '
        "deliveredGenerations.size())"
    ) in reader
    assert 'accepted.boolean("shared_media", _sharedFactory)' in reader
    assert '"block", FALSE' in source
    assert reader.index("gst_app_src_push_buffer") < reader.index(
        'JsonEvent accepted("frame_accepted")'
    )


def test_rtsp_push_retry_is_bounded_and_remains_fail_closed() -> None:
    source = PUBLISHER.read_text(encoding="utf-8")
    reader = _function_body(source, "void readerLoop()")

    assert "kMaximumGenerationRetriesPerMessage" in reader
    assert 'fail("client_timeout"' in reader
    assert 'fail("push_buffer"' in reader
    assert "waitForAppSrcAfter(" in reader
    assert "failedPushes" in reader
    assert "successful generations are excluded" in reader
    assert "_clientAvailabilityGeneration" in source
    assert "_clientCount > 0" in source


def test_rtsp_frame_cadence_remains_source_pts_authoritative() -> None:
    source = PUBLISHER.read_text(encoding="utf-8")
    make_frame = _function_body(source, "GstBuffer* makeFrameBuffer")

    # Replay, SIM and hardware captures do not share one fixed frame rate.
    # The positive caps value exists only so RTSP can build SDP before the
    # first buffer; preserve the exact captured-at PTS for the media clock.
    assert "do-timestamp=false" in source
    assert "framerate=(fraction)30/1" in source
    assert "RTSP SDP requires a concrete, positive nominal frame rate" in source
    assert "GST_BUFFER_PTS(buffer) = ptsNs" in make_frame
    assert "GST_BUFFER_DTS(buffer) = ptsNs" in make_frame
    assert "GST_BUFFER_DURATION(buffer) = GST_CLOCK_TIME_NONE" in make_frame
    assert "kNominalFrameRate" not in source
    assert "nominal_frame_rate_hz" not in source

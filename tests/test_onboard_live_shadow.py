"""Camera/IPC fixtures exercise wiring, not semantic inference or flight."""

from __future__ import annotations

import hashlib
import io
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from test_gazebo_sim_backend import _wire_frame

from multidetect.autonomy.contracts import PromptEnvelope
from multidetect.autonomy.onboard_live_shadow import OnboardLiveShadowBridge
from multidetect.autonomy.onboard_live_vlm_contract import serialize_onboard_live_vlm_messages
from multidetect.autonomy.onboard_vlm_runtime_contract import parse_onboard_vlm_runtime_response
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmWorkerResult
from multidetect.backends.gazebo_sim import (
    GazeboSimFrameProvenance,
    GazeboSimFrameTelemetryProvenance,
    GazeboSimTelemetryProvider,
    GazeboSimWireTelemetryProvenance,
    read_gazebo_sim_wire_frame,
)
from multidetect.clock import HighResolutionSystemClock


def source(sequence, *, identity=None, stamp=None):
    stamp = (sequence + 1) * 1_000_000_000 if stamp is None else stamp
    packet = _wire_frame(sequence, stamp_ns=stamp)
    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=sequence,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    captured = time.perf_counter()
    provider = GazeboSimTelemetryProvider()
    provider.update(wire, observed_at_s=captured)
    image = np.frombuffer(wire.payload_bgr24, dtype=np.uint8).reshape(1, 2, 3)
    p = GazeboSimFrameProvenance(
        frame_id=f"gazebo-sim-{sequence:06d}",
        sequence=sequence,
        captured_at_s=captured,
        captured_at_clock="host_perf_counter",
        sensor_stamp_ns=stamp,
        camera_info_stamp_ns=stamp - 3,
        navsat_stamp_ns=stamp - 2,
        odometry_stamp_ns=stamp - 1,
        camera_info_source_skew_ns=3,
        navsat_source_skew_ns=2,
        odometry_source_skew_ns=1,
        record_sha256=wire.record_sha256,
        payload_sha256=hashlib.sha256(wire.payload_bgr24).hexdigest(),
        width=2,
        height=1,
        stride=6,
        payload_bytes=6,
        flags=wire.flags,
        pixel_format="BGR24",
        wire_unsigned_header_hex=wire.unsigned_header.hex(),
        wire_telemetry=GazeboSimWireTelemetryProvenance.from_wire(wire),
        frame_telemetry=GazeboSimFrameTelemetryProvenance.from_telemetry(
            provider.snapshot(now_s=captured)
        ),
        host_clock_identity=tuple(
            (identity or HighResolutionSystemClock(session_id="fixture").identity()).items()
        ),
    )
    return SimpleNamespace(image_bgr=image), p


class FixtureWorker:
    def __init__(self, *, blocked=False):
        self.state, self.error, self.last_attempt = "idle", None, None
        self.requests = []
        self.blocked = blocked
        self.pending = None

    def start(self):
        pass

    def submit(self, request, *, observed_now_s):
        request.validate_binding()
        request.model_input.validate_freshness(observed_now_s)
        self.requests.append(request)
        self.pending, self.state = request, "pending"
        return len(self.requests)

    def take_result(self):
        if self.pending is None or self.blocked:
            return None
        request = self.pending
        raw = json.dumps(
            {
                "schema_version": 2,
                "contract": "multidetect.onboard-vlm-runtime-intent-v2",
                "frame_ids": [request.model_input.frames[-1].frame_id],
                "evidence_ids": [],
                "output": {"response": "hold", "reason": "fixture not model", "confidence": 0.5},
            }
        )
        now = time.perf_counter()
        response = parse_onboard_vlm_runtime_response(
            raw,
            request,
            current_scope=request.model_input.scope,
            completed_at_s=now,
            inference_latency_ms=0,
        )
        self.pending, self.state = None, "idle"
        return OnboardVlmWorkerResult(
            len(self.requests), request.canonical_hash, response, raw, now, 0, "{}"
        )

    def cancel(self, reason):
        self.state, self.error = "cancelled", reason


def bridge(tmp_path, worker=None, **kwargs):
    prompt = PromptEnvelope(
        "prompt", "查找可安全观察的目标", "zh", "2026-10-07T00:00:00Z", "operator", "sim"
    )
    value = OnboardLiveShadowBridge(
        worker or FixtureWorker(),
        output_directory=tmp_path / "episode",
        prompt=prompt,
        prompt_text_original="  查找可安全观察的目标\n",
        **kwargs,
    )
    value.start()
    return value


def wait_for(value, key, count=1, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value.evidence()[key] >= count or value.evidence()["error"]:
            return
        time.sleep(0.005)
    raise AssertionError(value.evidence())


def test_actual_callback_partial_state_and_verbatim_prompt_reach_messages(tmp_path):
    worker = FixtureWorker()
    value = bridge(tmp_path, worker)
    frame, p = source(0)
    assert value.on_sensor_frame(frame, p)
    wait_for(value, "strict_decisions")
    report = value.finish(timeout_s=1)
    assert report["closed"] and report["error"] is None
    request = worker.requests[0]
    assert request.model_input.frames[0].image_bytes == bytes((3, 2, 1, 6, 5, 4))
    assert request.model_input.frames[0].context is None
    messages = serialize_onboard_live_vlm_messages(request)
    texts = "\n".join(
        item["text"] for row in messages for item in row["content"] if item["type"] == "text"
    )
    assert "47.397971" in texts and "查找可安全观察的目标\n" in texts
    assert report["model_actuated_flight"] is report["execution_authority"] is False


def test_long_episode_does_not_stop_at_four_frames_or_deduplicate_same_pixels(tmp_path):
    value = bridge(tmp_path)
    for sequence in range(10):
        value.on_sensor_frame(*source(sequence))
        wait_for(value, "recorded_frames", sequence + 1)
        wait_for(value, "strict_decisions", sequence + 1)
    report = value.finish(timeout_s=1)
    assert report["error"] is None and report["recorded_frames"] == 10
    assert report["strict_decisions"] == 10
    assert all(len(row.model_input.frames) <= 2 for row in value.worker.requests)
    assert len({row.model_input.scope.canonical_hash for row in value.worker.requests}) == 1


def test_callback_does_not_wait_for_pending_inference_and_no_hidden_restart(tmp_path):
    worker = FixtureWorker(blocked=True)
    value = bridge(tmp_path, worker)
    value.on_sensor_frame(*source(0))
    wait_for(value, "submitted_requests")
    began = time.monotonic()
    value.on_sensor_frame(*source(1))
    assert time.monotonic() - began < 0.2
    wait_for(value, "recorded_frames", 2)
    report = value.finish(timeout_s=0.03)
    assert report["closed"] and report["error"]
    assert report["strict_decisions"] == 0
    assert len(worker.requests) == 1 and worker.state == "cancelled"


@pytest.mark.parametrize("mutation", ["bytes", "shape", "dtype", "provenance"])
def test_callback_rejects_unbound_camera_payload(tmp_path, mutation):
    value = bridge(tmp_path)
    frame, p = source(0)
    if mutation == "bytes":
        frame.image_bgr = np.zeros((1, 2, 3), dtype=np.uint8)
    elif mutation == "shape":
        frame.image_bgr = np.zeros((2, 2, 3), dtype=np.uint8)
    elif mutation == "dtype":
        frame.image_bgr = frame.image_bgr.astype(np.float32)
    else:
        p = p.to_mapping()
    try:
        with pytest.raises((TypeError, ValueError)):
            value.on_sensor_frame(frame, p)
    finally:
        value.finish(timeout_s=1)


@pytest.mark.parametrize(
    "mutation", ["clock", "sequence", "sensor_stamp", "future", "wire", "legacy"]
)
def test_bad_source_closes_partial_episode_without_action(tmp_path, mutation):
    value = bridge(tmp_path)
    value.on_sensor_frame(*source(0))
    wait_for(value, "strict_decisions")
    frame, p = source(1)
    if mutation == "clock":
        p = replace(
            p,
            host_clock_identity=tuple(
                HighResolutionSystemClock(session_id="other").identity().items()
            ),
        )
    elif mutation == "sequence":
        p = replace(p, sequence=0)
    elif mutation == "sensor_stamp":
        frame, p = source(1, stamp=1_000_000_000)
    elif mutation == "future":
        p = replace(p, captured_at_s=time.perf_counter() + 100)
    elif mutation == "wire":
        p = replace(p, record_sha256="0" * 64)
    else:
        p = replace(p, captured_at_clock="host_monotonic", host_clock_identity=None)
    value.on_sensor_frame(frame, p)
    report = value.finish(timeout_s=1)
    assert report["closed"] and report["error"]
    assert report["model_actuated_flight"] is False


def test_fresh_output_and_one_use_lifecycle(tmp_path):
    value = bridge(tmp_path)
    with pytest.raises(RuntimeError):
        value.start()
    value.on_sensor_frame(*source(0))
    value.finish(timeout_s=1)
    with pytest.raises(RuntimeError):
        value.on_sensor_frame(*source(1))
    with pytest.raises(FileExistsError):
        bridge(tmp_path)


def test_failed_submit_is_not_recorded_as_submitted(tmp_path):
    class RefusingWorker(FixtureWorker):
        def submit(self, request, *, observed_now_s):
            raise RuntimeError("fixture mailbox rejected")

    value = bridge(tmp_path, RefusingWorker())
    value.on_sensor_frame(*source(0))
    report = value.finish(timeout_s=1)
    assert report["error"] and report["submitted_requests"] == 0
    events = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (tmp_path / "episode/events").glob("*.json")
    ]
    assert all(row["kind"] != "request_submitted" for row in events)


def test_worker_strict_rejection_keeps_raw_in_partial_episode(tmp_path):
    class RejectingWorker(FixtureWorker):
        def take_result(self):
            if self.pending is not None:
                self.last_attempt = {
                    "request_sha256": self.pending.canonical_hash,
                    "raw_output": "not-json fixture",
                    "completed_at_s": time.perf_counter(),
                    "inference_latency_ms": 0,
                }
                self.state, self.error = "failed", "host_validation:ValueError"
                self.pending = None
            return None

    value = bridge(tmp_path, RejectingWorker())
    value.on_sensor_frame(*source(0))
    report = value.finish(timeout_s=1)
    assert report["error"] and report["strict_decisions"] == 0
    events = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (tmp_path / "episode/events").glob("*.json")
    ]
    rejected = next(row for row in events if row["kind"] == "decision_rejected")
    raw = rejected["data"]["raw_response_original"]
    assert (tmp_path / "episode" / raw["path"]).read_bytes() == b"not-json fixture"


def test_mailbox_success_is_counted_even_when_episode_write_fails(tmp_path, monkeypatch):
    from multidetect.autonomy.onboard_live_episode import OnboardLiveEpisodeRecorder

    def failed(*_a, **_kw):
        raise OSError("fixture disk write failure")

    monkeypatch.setattr(OnboardLiveEpisodeRecorder, "record_request", failed)
    value = bridge(tmp_path)
    value.on_sensor_frame(*source(0))
    report = value.finish(timeout_s=1)
    assert report["error"] and report["submitted_requests"] == 1
    assert len(value.worker.requests) == 1 and value.worker.state == "cancelled"

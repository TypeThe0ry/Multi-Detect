from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import time
from dataclasses import dataclass, replace

import pytest
from test_onboard_multimodal import _input

from multidetect.autonomy.onboard_vlm_backend import QwenVlmBackendFactory
from multidetect.autonomy.onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    serialize_onboard_vlm_messages,
)
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmProcessWorker


@dataclass(frozen=True)
class FixtureBackendFactory:
    mode: str = "normal"

    def __call__(self):
        if self.mode == "load_hang":
            time.sleep(30)
        return FixtureBackend(self.mode)


class FixtureBackend:
    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    def infer(self, request):
        self.calls += 1
        if self.mode == "hang":
            time.sleep(30)
        if self.mode == "crash":
            os._exit(7)
        if self.mode == "oversize":
            return {"raw_output": "x" * 1_048_577, "metrics": {}}
        if self.mode == "metrics_oversize":
            return {"raw_output": "{}", "metrics": {"x": "x" * 262_144}}
        if self.mode == "bad_json":
            return {"raw_output": "not-json", "metrics": {"fixture_only": True}}
        messages = serialize_onboard_vlm_messages(request)
        image_bytes = b"".join(
            item["image"].tobytes()
            for message in messages
            for item in message["content"]
            if item["type"] == "image"
        )
        return {
            "raw_output": json.dumps(
                {
                    "schema_version": 2,
                    "contract": "multidetect.onboard-vlm-runtime-intent-v2",
                    "frame_ids": [request.model_input.frames[-1].frame_id],
                    "evidence_ids": [],
                    "output": {"response": "hold", "reason": "fixture", "confidence": 0.6},
                }
            ),
            "metrics": {
                "fixture_only": True,
                "calls": self.calls,
                "input_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "prompt_received": request.model_input.prompt.text,
            },
        }


def _request(deadline=2.0):
    value = _input()
    return OnboardVlmRuntimeRequest(replace(value, deadline_at_s=value.requested_at_s + deadline))


def _wait(worker, states, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if worker.state in states:
            return worker.state
        time.sleep(0.01)
    pytest.fail(f"worker did not reach {states}: {worker.state} {worker.error}")


def _warm(worker):
    worker.start()
    assert _wait(worker, {"idle", "failed", "timed_out"}) == "idle"


def test_real_image_serializer_runs_in_owned_persistent_process_and_consumes_once():
    with OnboardVlmProcessWorker(FixtureBackendFactory(), startup_timeout_s=8) as worker:
        assert _wait(worker, {"idle"}) == "idle"
        pid = worker.process_id
        request = _request()
        started = time.monotonic()
        generation = worker.submit(request, observed_now_s=request.model_input.requested_at_s)
        assert time.monotonic() - started < 0.1
        assert _wait(worker, {"ready", "failed"}) == "ready"
        first = worker.take_result()
        assert first.generation == generation
        assert first.request_sha256 == request.canonical_hash
        assert first.response.output.reason == "fixture"
        assert first.to_mapping()["execution_authority"] is False
        metrics = first.to_mapping()["backend_metrics"]
        assert metrics["calls"] == 1
        assert metrics["prompt_received"] == request.model_input.prompt.text
        assert (
            metrics["input_image_sha256"]
            == hashlib.sha256(
                b"".join(frame.image_bytes for frame in request.model_input.frames)
            ).hexdigest()
        )
        assert worker.take_result() is None
        generation2 = worker.submit(request, observed_now_s=request.model_input.requested_at_s)
        assert generation2 > generation
        assert _wait(worker, {"ready", "failed"}) == "ready"
        assert worker.take_result().to_mapping()["backend_metrics"]["calls"] == 2
        assert worker.process_id == pid
    assert pid not in {process.pid for process in mp.active_children()}


@pytest.mark.parametrize("mode", ["hang", "crash", "oversize", "metrics_oversize", "bad_json"])
def test_backend_faults_cannot_block_poll_or_create_ready_intent(mode):
    with OnboardVlmProcessWorker(FixtureBackendFactory(mode), startup_timeout_s=8) as worker:
        assert _wait(worker, {"idle"}) == "idle"
        request = _request(deadline=0.25)
        worker.submit(request, observed_now_s=request.model_input.requested_at_s)
        started = time.monotonic()
        assert worker.poll() is None
        assert time.monotonic() - started < 0.05
        state = _wait(worker, {"failed", "timed_out"}, timeout=3)
        assert worker.take_result() is None
        if mode == "hang":
            assert state == "timed_out"
            assert worker.error == "inference_deadline_exceeded"
        if mode == "bad_json":
            assert worker.last_attempt["raw_output"] == "not-json"
    assert worker.process_id not in {process.pid for process in mp.active_children()}


def test_independent_watchdog_kills_startup_that_never_becomes_ready():
    with OnboardVlmProcessWorker(FixtureBackendFactory("load_hang"), startup_timeout_s=0.1) as w:
        assert _wait(w, {"timed_out"}, timeout=2) == "timed_out"
        assert w.error == "startup_deadline_exceeded"
        assert w.poll() is None
    assert w.process_id not in {p.pid for p in mp.active_children()}


def test_cancel_is_immediate_retires_late_result_and_requires_explicit_prewarm():
    with OnboardVlmProcessWorker(FixtureBackendFactory("hang"), startup_timeout_s=8) as w:
        assert _wait(w, {"idle"}) == "idle"
        request = _request()
        w.submit(request, observed_now_s=10)
        started = time.monotonic()
        w.cancel("scope_changed")
        assert time.monotonic() - started < 0.05
        assert w.state == "cancelled"
        assert w.take_result() is None
        with pytest.raises(RuntimeError, match="prewarmed"):
            w.submit(request, observed_now_s=10)


def test_explicit_prewarm_after_cancel_uses_new_owned_process():
    with OnboardVlmProcessWorker(FixtureBackendFactory(), startup_timeout_s=8) as w:
        assert _wait(w, {"idle"}) == "idle"
        old_pid = w.process_id
        w.cancel("new_scope")
        end = time.monotonic() + 3
        while w._thread.is_alive() and time.monotonic() < end:
            time.sleep(0.01)
        w.start()
        assert _wait(w, {"idle"}) == "idle"
        assert w.process_id != old_pid
        w.submit(_request(), observed_now_s=10)
        assert _wait(w, {"ready", "failed"}) == "ready"
        assert w.take_result() is not None
    assert old_pid not in {p.pid for p in mp.active_children()}


def test_ready_result_expires_even_if_caller_never_polls():
    with OnboardVlmProcessWorker(FixtureBackendFactory(), startup_timeout_s=8) as w:
        assert _wait(w, {"idle"}) == "idle"
        w.submit(_request(deadline=0.5), observed_now_s=10)
        assert _wait(w, {"ready", "failed"}) == "ready"
        assert _wait(w, {"timed_out"}, timeout=2) == "timed_out"
        assert w.take_result() is None
        assert w.last_attempt is not None


def test_unprewarmed_busy_stale_and_out_of_epoch_submissions_reject():
    w = OnboardVlmProcessWorker(FixtureBackendFactory("hang"), startup_timeout_s=8)
    request = _request()
    with pytest.raises(RuntimeError, match="prewarmed"):
        w.submit(request, observed_now_s=10)
    _warm(w)
    try:
        with pytest.raises(ValueError):
            w.submit(request, observed_now_s=9.9)
        with pytest.raises(ValueError):
            w.submit(request, observed_now_s=12.01)
        w.submit(request, observed_now_s=10)
        with pytest.raises(RuntimeError, match="prewarmed"):
            w.submit(request, observed_now_s=10)
    finally:
        w.close()
    with pytest.raises(RuntimeError, match="closed"):
        w.start()


@pytest.mark.parametrize(
    "values",
    [
        {"snapshot_path": "relative", "model_sha256": "b" * 64},
        {"snapshot_path": "D:/snapshot", "model_sha256": "bad"},
        {"snapshot_path": "D:/snapshot", "model_sha256": "b" * 64, "max_input_tokens": True},
        {"snapshot_path": "D:/snapshot", "model_sha256": "b" * 64, "max_new_tokens": 1025},
    ],
)
def test_native_backend_requires_bounded_pinned_local_configuration(values):
    with pytest.raises((TypeError, ValueError)):
        QwenVlmBackendFactory(**values)

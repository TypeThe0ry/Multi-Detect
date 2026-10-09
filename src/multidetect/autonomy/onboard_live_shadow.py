"""Nonblocking actual-camera VLM branch and durable long-episode observations.

The camera callback never waits for CUDA, model loading, or telemetry. It copies
validated immutable source bytes into a bounded queue. One host thread owns the
worker mailbox and recorder. No MAVLink receiver or flight command writer exists
here; an independently flying baseline is not this Prompt's executed action.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..backends.gazebo_sim import GazeboSimFrameProvenance
from ..clock import validate_host_identity
from .contracts import PromptEnvelope
from .onboard_live_episode import OnboardLiveEpisodeRecorder
from .onboard_live_vlm_contract import (
    OnboardLiveMultimodalInput,
    OnboardLiveVlmRequest,
    gazebo_sensor_fact_json,
)
from .onboard_multimodal import (
    MissionEvidenceLedger,
    OnboardImageFrame,
    OnboardMultimodalScope,
)
from .serialization import canonical_json, canonical_sha256, require_int, require_real


@dataclass(frozen=True, slots=True)
class _SourceFrame:
    bgr_bytes: bytes
    provenance: GazeboSimFrameProvenance
    received_at_s: float


class OnboardLiveShadowBridge:
    """Latest-window inference, with every admitted raw frame kept on disk.

    Host ``clock`` is explicitly perf-counter in the actual camera host epoch.
    Source simulator nanosecond stamps are retained, never converted to host or
    UTC timestamps. New observations do not cancel an already running request.
    A worker failure closes a partial episode; no hidden fallback or restart.
    """

    def __init__(
        self,
        worker,
        *,
        output_directory: Path,
        prompt: PromptEnvelope,
        prompt_text_original: str,
        clock: Callable[[], float] = time.perf_counter,
        history_frames: int = 2,
        queue_frames: int = 16,
        deadline_s: float = 30.0,
        source_max_age_s: float = 10.0,
        max_total_pixels: int = 200_704,
    ):
        if not isinstance(prompt, PromptEnvelope):
            raise TypeError("prompt must be PromptEnvelope")
        if type(prompt_text_original) is not str or prompt_text_original.strip() != prompt.text:
            raise ValueError("verbatim Prompt differs from its envelope")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.output_directory = Path(output_directory).resolve()
        if self.output_directory.exists():
            raise FileExistsError("live episode output must be new")
        self.worker, self.prompt, self.original, self.clock = (
            worker,
            prompt,
            prompt_text_original,
            clock,
        )
        self.history_frames = require_int(history_frames, "history_frames", minimum=1, maximum=4)
        self.deadline_s = require_real(deadline_s, "deadline_s", minimum=0.05, maximum=120)
        self.source_max_age_s = require_real(
            source_max_age_s, "source_max_age_s", minimum=0.05, maximum=30
        )
        self.max_total_pixels = require_int(
            max_total_pixels, "max_total_pixels", minimum=4, maximum=1_605_632
        )
        self._queue = queue.Queue(
            maxsize=require_int(queue_frames, "queue_frames", minimum=1, maximum=32)
        )
        self._history = deque(maxlen=self.history_frames)
        self._scope = None
        self._clock_identity = None
        self._recorder = None
        self._pending = None
        self._last_submitted = None
        self._last_source_sequence = -1
        self._last_source_time = -1.0
        self._last_sensor_stamp_ns = -1
        self._thread = None
        self._inputs_closed = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._error = None
        self._stats = {
            "queued_frames": 0,
            "queue_overflow_frames": 0,
            "recorded_frames": 0,
            "submitted_requests": 0,
            "strict_decisions": 0,
            "expired_window_frames": 0,
        }

    def start(self) -> None:
        if self._thread is not None or self._inputs_closed.is_set():
            raise RuntimeError("live bridge is one-use")
        self.worker.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="onboard-live-shadow")
        self._thread.start()

    def on_sensor_frame(self, frame, provenance, _depth=None) -> bool:
        """Actual Gazebo callback: copy/enqueue only, including unchanged classes."""
        if self._thread is None or self._inputs_closed.is_set() or self._done.is_set():
            raise RuntimeError("live bridge is not accepting frames")
        if not isinstance(provenance, GazeboSimFrameProvenance):
            raise TypeError("actual typed Gazebo provenance required")
        import numpy as np

        image = getattr(frame, "image_bgr", None)
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.shape != (provenance.height, provenance.width, 3)
            or provenance.pixel_format != "BGR24"
            or provenance.stride != provenance.width * 3
        ):
            raise ValueError("camera BGR layout differs from source provenance")
        payload = image.tobytes(order="C")
        if len(payload) != provenance.payload_bytes or (
            hashlib.sha256(payload).hexdigest() != provenance.payload_sha256
        ):
            raise ValueError("actual BGR source bytes differ from wire provenance")
        item = _SourceFrame(payload, provenance, self.clock())
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._stats["queue_overflow_frames"] += 1
            return False
        with self._lock:
            self._stats["queued_frames"] += 1
        return True

    def _record_frame(self, source: _SourceFrame) -> None:
        p = source.provenance
        if p.captured_at_clock != "host_perf_counter" or p.host_clock_identity is None:
            raise ValueError("live camera must declare its exact perf-counter host clock")
        identity = validate_host_identity(dict(p.host_clock_identity))
        now = self.clock()
        if not 0 <= p.captured_at_s <= source.received_at_s <= now:
            raise ValueError("source/receive/host times are future or reversed")
        if (
            p.sequence <= self._last_source_sequence
            or p.captured_at_s <= self._last_source_time
            or p.sensor_stamp_ns <= self._last_sensor_stamp_ns
        ):
            raise ValueError("camera sequence or host observation time regressed")
        if self._scope is None:
            self._clock_identity = canonical_json(identity)
            epoch = "gazebo-host-perf-counter:" + canonical_sha256(identity)
            self._scope = OnboardMultimodalScope(
                session_id=str(uuid4()),
                prompt_sha256=self.prompt.canonical_hash,
                aircraft_id=self.prompt.aircraft_id,
                selected_task_id="live-shadow-unresolved-prompt",
                clock_epoch=epoch,
            )
        elif canonical_json(identity) != self._clock_identity:
            raise ValueError("camera host clock identity changed within episode")
        import numpy as np

        rgb = (
            np.frombuffer(source.bgr_bytes, dtype=np.uint8)
            .reshape(p.height, p.width, 3)[:, :, ::-1]
            .tobytes(order="C")
        )
        observation = OnboardImageFrame(
            self._scope,
            p.frame_id,
            p.captured_at_s,
            rgb,
            hashlib.sha256(rgb).hexdigest(),
            p.width,
            p.height,
            "rgb8",
            None,
        )
        facts = gazebo_sensor_fact_json(observation, p.to_mapping())
        # Reopen/reconstruct the wire before admitting this observation. The
        # helper above only builds an envelope, not its verification result.
        OnboardLiveMultimodalInput(
            scope=self._scope,
            prompt=self.prompt,
            frames=(observation,),
            ledger=MissionEvidenceLedger(self._scope, (observation,)),
            requested_at_s=now,
            deadline_at_s=now + self.deadline_s,
            max_frame_age_s=max(now - observation.captured_at_s, self.source_max_age_s)
            + self.deadline_s,
            prompt_text_original=self.original,
            factual_sensor_state_json=(facts,),
        )
        if self._recorder is None:
            self._recorder = OnboardLiveEpisodeRecorder.create(
                self.output_directory,
                scope=self._scope,
                prompt=self.prompt,
                prompt_text_original=self.original,
                host_clock_id=self._scope.clock_epoch,
                created_at_s=self.clock(),
            )
        self._recorder.record_observation(
            observation,
            received_at_s=source.received_at_s,
            processed_at_s=self.clock(),
            source_clock_id="gazebo-sensor-sim-time-ns",
            source_timestamp_s=None,
            source_provenance=canonical_json(p.to_mapping()).encode("utf-8"),
        )
        self._history.append((observation, facts))
        # Source-byte cap is independent of resized model pixels.
        while sum(len(row[0].image_bytes) for row in self._history) > 16 * 1024 * 1024:
            self._history.popleft()
        self._last_source_sequence, self._last_source_time = p.sequence, p.captured_at_s
        self._last_sensor_stamp_ns = p.sensor_stamp_ns
        with self._lock:
            self._stats["recorded_frames"] += 1

    def _poll_and_submit(self) -> None:
        if self._pending is not None:
            result = self.worker.take_result()
            if result is not None:
                if result.request_sha256 != self._pending.canonical_hash:
                    raise ValueError("worker result belongs to a different request")
                self._recorder.record_decision(
                    self._pending.canonical_hash,
                    result.raw_output,
                    completed_at_s=result.completed_at_s,
                    inference_latency_ms=result.inference_latency_ms,
                    received_at_s=self.clock(),
                    backend_metrics_json=result.backend_metrics_json,
                )
                self._pending = None
                with self._lock:
                    self._stats["strict_decisions"] += 1
        state = self.worker.state
        if state in {"failed", "cancelled", "timed_out", "closed"}:
            attempt = self.worker.last_attempt
            if (
                state == "failed"
                and str(self.worker.error).startswith("host_validation:")
                and self._pending is not None
                and isinstance(attempt, dict)
                and attempt.get("request_sha256") == self._pending.canonical_hash
                and isinstance(attempt.get("raw_output"), str)
            ):
                # The worker already rejected this raw response. Reopen the
                # same request in the recorder so rejected bytes survive in
                # the episode, not just in the final run's native_attempt.
                self._recorder.record_decision(
                    self._pending.canonical_hash,
                    attempt["raw_output"],
                    completed_at_s=attempt["completed_at_s"],
                    inference_latency_ms=attempt["inference_latency_ms"],
                    received_at_s=self.clock(),
                    backend_metrics_json=canonical_json(attempt.get("backend_metrics", {})),
                )
            raise RuntimeError("VLM worker failed: " + str(self.worker.error or state))
        if state != "idle" or self._pending is not None:
            return
        now = self.clock()
        while self._history and now - self._history[0][0].captured_at_s > self.source_max_age_s:
            self._history.popleft()
            with self._lock:
                self._stats["expired_window_frames"] += 1
        if not self._history or self._history[-1][0].frame_id == self._last_submitted:
            return
        frames = tuple(item[0] for item in self._history)
        facts = tuple(item[1] for item in self._history)
        model_input = OnboardLiveMultimodalInput(
            scope=self._scope,
            prompt=self.prompt,
            frames=frames,
            ledger=MissionEvidenceLedger(self._scope, frames),
            requested_at_s=now,
            deadline_at_s=now + self.deadline_s,
            max_frame_age_s=self.source_max_age_s + self.deadline_s,
            max_gap_s=30.0,
            prompt_text_original=self.original,
            factual_sensor_state_json=facts,
        )
        request = OnboardLiveVlmRequest(model_input, max_total_pixels=self.max_total_pixels)
        submitted_at = self.clock()
        self.worker.submit(request, observed_now_s=submitted_at)
        self._pending = request
        self._last_submitted = frames[-1].frame_id
        with self._lock:
            self._stats["submitted_requests"] += 1
        # Publish a confirmed submission only after mailbox acceptance. The
        # timestamp is the call's start, not ACK time: a fast worker can finish
        # before submit returns, without any intervening recorder event.
        self._recorder.record_request(request, submitted_at_s=submitted_at)

    def _run(self) -> None:
        try:
            while True:
                # Leave inference supervision/polling responsive during a burst.
                for _ in range(8):
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._record_frame(item)
                self._poll_and_submit()
                if self._inputs_closed.is_set() and self._queue.empty() and self._pending is None:
                    break
                self._inputs_closed.wait(0.005) if not self._inputs_closed.is_set() else time.sleep(
                    0.005
                )
        except Exception as exc:
            self._error = type(exc).__name__ + ": " + str(exc)
            try:
                self.worker.cancel("live shadow input/worker failure")
            except Exception as cancel_error:
                self._error += "; worker cancel failed: " + str(cancel_error)
        finally:
            if self._recorder is not None:
                try:
                    incomplete = self._error is not None or self._stats["queue_overflow_frames"] > 0
                    self._recorder.close(
                        status="failed_partial" if incomplete else "closed",
                        reason=self._error
                        or ("camera queue overflow" if incomplete else "capture ended"),
                        closed_at_s=self.clock(),
                    )
                except Exception as exc:
                    self._error = self._error or ("episode close failed: " + str(exc))
            self._done.set()

    def finish(self, *, timeout_s: float = 35.0) -> dict:
        timeout = require_real(timeout_s, "timeout_s", minimum=0.01, maximum=180)
        self._inputs_closed.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.worker.cancel("live shadow finish deadline exceeded")
                self._thread.join(2.0)
                if self._thread.is_alive():
                    self._error = "live shadow host thread did not finish"
        return self.evidence()

    def evidence(self) -> dict:
        with self._lock:
            stats = dict(self._stats)
        return {
            "contract": "multidetect.onboard-live-shadow-bridge-v1",
            **stats,
            "closed": self._done.is_set(),
            "error": self._error,
            "episode_directory": str(self.output_directory),
            "scope": None if self._scope is None else self._scope.to_mapping(),
            "native_attempt": self.worker.last_attempt,
            "model_receives_partial_sensor_facts": True,
            "complete_safety_context_provided": False,
            "model_actuated_flight": False,
            "execution_authority": False,
            "flight_action_training_performed": False,
            "training_ready": False,
        }

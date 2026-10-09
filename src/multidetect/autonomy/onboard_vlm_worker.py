"""Process-isolated VLM inference, without Mission or actuator authority.

Prewarm with ``start`` outside the Mission event-loop lock. Submissions and
polls never wait for model loading/generation. A host watchdog retires and
kills the owned process on deadline/cancellation; Transformers ``max_time``
is only a secondary guard. A retired worker needs explicit prewarming again.
This is a development source-bound seam, not a flight-qualified deployment.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    OnboardVlmRuntimeResponse,
    parse_onboard_vlm_runtime_response,
)
from .serialization import require_real, require_text


@dataclass(frozen=True, slots=True)
class OnboardVlmWorkerResult:
    generation: int
    request_sha256: str
    response: OnboardVlmRuntimeResponse
    raw_output: str
    completed_at_s: float
    inference_latency_ms: float
    backend_metrics_json: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.onboard-vlm-worker-result-v1",
            "generation": self.generation,
            "request_sha256": self.request_sha256,
            "response": self.response.to_mapping(),
            "raw_output": self.raw_output,
            "completed_at_s": self.completed_at_s,
            "inference_latency_ms": self.inference_latency_ms,
            "backend_metrics": json.loads(self.backend_metrics_json),
            "timing_semantics": "host_monotonic_elapsed_from_caller_observed_request_clock",
            "execution_authority": False,
        }


def _worker_main(factory: Callable, inbox, outbox) -> None:
    """Only this process constructs a backend or touches CUDA."""
    try:
        backend = factory()
        outbox.put({"kind": "ready"})
        while True:
            item = inbox.get()
            if item is None:
                return
            generation, request = item
            raw = backend.infer(request)
            if not isinstance(raw, dict) or set(raw) != {"raw_output", "metrics"}:
                raise ValueError("backend result must contain raw_output and metrics")
            output = raw["raw_output"]
            if type(output) is not str or not 0 < len(output.encode("utf-8")) <= 1_048_576:
                raise ValueError("backend output exceeds the bounded text contract")
            # Bound output before it crosses IPC. Do not send tensors, image
            # objects, model instances or exception tracebacks to the host.
            metrics = json.dumps(raw["metrics"], sort_keys=True, allow_nan=False)
            if len(metrics.encode("utf-8")) > 262_144:
                raise ValueError("backend metrics exceed the bounded IPC contract")
            outbox.put(
                {
                    "kind": "result",
                    "generation": generation,
                    "raw_output": output,
                    "metrics": metrics,
                }
            )
    except Exception as exc:
        outbox.put({"kind": "error", "error_type": type(exc).__name__})


class OnboardVlmProcessWorker:
    """A one-request/one-result mailbox supervised by a host watchdog.

    ``observed_now_s`` must be the caller's current time in the input's
    explicitly declared clock epoch. The watchdog independently measures
    elapsed time with ``monotonic``; it does not infer a sensor/host mapping.
    No implicit restart or fallback is selected after failure. Backend
    factories must be spawn-picklable, trusted local code without transport.
    """

    def __init__(self, backend_factory: Callable, *, startup_timeout_s: float = 120.0):
        if not callable(backend_factory):
            raise TypeError("backend_factory must be callable")
        self._factory = backend_factory
        self._startup_timeout_s = require_real(
            startup_timeout_s, "startup_timeout_s", minimum=0.05, maximum=300.0
        )
        self._mp = mp.get_context("spawn")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._retire = threading.Event()
        self._process = None
        self._inbox = None
        self._outbox = None
        self._thread = None
        self._state = "idle"
        self._error = None
        self._generation = 0
        self._pending = None
        self._result = None
        self._started = 0.0
        self._attempt_json = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def process_id(self) -> int | None:
        with self._lock:
            return None if self._process is None else self._process.pid

    @property
    def last_attempt(self) -> dict[str, object] | None:
        """Retain native raw output even when strict parsing rejects it."""
        with self._lock:
            return None if self._attempt_json is None else json.loads(self._attempt_json)

    def start(self) -> None:
        """Explicit loading phase; call outside flight/telemetry locks."""
        with self._lock:
            if self._state == "closed":
                raise RuntimeError("worker is closed")
            if self._thread is not None and self._thread.is_alive():
                if self._state in {"starting", "idle", "pending", "ready"}:
                    return
                raise RuntimeError("retired process is still being reaped")
            self._stop.clear()
            self._retire.clear()
            self._inbox = self._mp.Queue(maxsize=1)
            self._outbox = self._mp.Queue(maxsize=1)
            self._process = self._mp.Process(
                target=_worker_main,
                args=(self._factory, self._inbox, self._outbox),
                daemon=True,
            )
            self._pending = self._result = None
            self._state = "starting"
            self._error = None
            self._started = time.monotonic()
            try:
                self._process.start()
            except Exception:
                self._state, self._error = "failed", "backend_factory_spawn_failed"
                for mailbox in (self._inbox, self._outbox):
                    mailbox.cancel_join_thread()
                    mailbox.close()
                self._process = None
                raise
            self._thread = threading.Thread(target=self._supervise, daemon=True)
            self._thread.start()

    def submit(self, request: OnboardVlmRuntimeRequest, *, observed_now_s: float) -> int:
        if not isinstance(request, OnboardVlmRuntimeRequest):
            raise TypeError("request must be OnboardVlmRuntimeRequest")
        now = require_real(observed_now_s, "observed_now_s", minimum=0)
        request.model_input.validate_freshness(now)
        if now < request.model_input.requested_at_s:
            raise ValueError("host time predates the sealed request")
        remaining = request.model_input.deadline_at_s - now
        if remaining <= 0:
            raise ValueError("request has no remaining deadline")
        digest = request.canonical_hash
        with self._lock:
            if self._state != "idle" or self._process is None:
                raise RuntimeError("worker must be prewarmed, idle and have no unconsumed result")
            self._generation += 1
            generation = self._generation
            started = time.monotonic()
            self._pending = (generation, request, digest, now, started, started + remaining)
            self._attempt_json = None
            self._state = "pending"
            try:
                self._inbox.put_nowait((generation, request))
            except queue.Full:
                self._fail("failed", "inbox_full")
                raise RuntimeError("worker mailbox is full") from None
            return generation

    def poll(self) -> OnboardVlmWorkerResult | None:
        """Ready-only: never waits for preprocessing, CUDA or model generation."""
        with self._lock:
            if self._state != "ready" or not self._pending:
                return None
            if time.monotonic() >= self._pending[5]:
                self._fail("timed_out", "result_expired_before_consumption")
                return None
            return self._result

    def take_result(self) -> OnboardVlmWorkerResult | None:
        with self._lock:
            result = self.poll()
            if result is not None:
                self._result = self._pending = None
                self._state = "idle"
            return result

    def cancel(self, reason: str) -> None:
        reason = require_text(reason, "cancellation reason")
        with self._lock:
            if self._state == "closed":
                return
            self._generation += 1
            self._fail("cancelled", reason)

    def _fail(self, state: str, error: str) -> None:
        self._state, self._error = state, error
        self._pending = self._result = None
        self._retire.set()

    def _supervise(self) -> None:
        try:
            while not self._stop.wait(0.005):
                with self._lock:
                    if self._retire.is_set():
                        break
                    now = time.monotonic()
                    if self._state == "starting" and (
                        now - self._started >= self._startup_timeout_s
                    ):
                        self._fail("timed_out", "startup_deadline_exceeded")
                        break
                    if self._pending and now >= self._pending[5]:
                        if self._state == "ready":
                            self._fail("timed_out", "result_expired_before_consumption")
                            break
                        else:
                            self._fail("timed_out", "inference_deadline_exceeded")
                            break
                    try:
                        message = self._outbox.get_nowait()
                    except queue.Empty:
                        if not self._process.is_alive():
                            self._fail("failed", "worker_process_exited")
                            break
                        continue
                    if message.get("kind") == "error":
                        self._fail("failed", message.get("error_type", "backend_error"))
                        break
                    if message.get("kind") == "ready" and self._state == "starting":
                        self._state = "idle"
                    elif message.get("kind") == "result":
                        pending = self._pending
                        if not pending or message["generation"] != pending[0]:
                            self._fail("failed", "retired_or_unbound_worker_result")
                            break
                        generation, request, digest, clock_at_submit, started, deadline = pending
                        finished = time.monotonic()
                        if finished >= deadline:
                            self._fail("timed_out", "late_worker_result")
                            break
                        elapsed = finished - started
                        completed_at = clock_at_submit + elapsed
                        self._attempt_json = json.dumps(
                            {
                                "generation": generation,
                                "request_sha256": digest,
                                "input_sha256": request.model_input.canonical_hash,
                                "scope_sha256": request.model_input.scope.canonical_hash,
                                "raw_output": message["raw_output"],
                                "backend_metrics": json.loads(message["metrics"]),
                                "completed_at_s": completed_at,
                                "inference_latency_ms": elapsed * 1000,
                                "execution_authority": False,
                            },
                            sort_keys=True,
                            allow_nan=False,
                        )
                        response = parse_onboard_vlm_runtime_response(
                            message["raw_output"],
                            request,
                            current_scope=request.model_input.scope,
                            completed_at_s=completed_at,
                            inference_latency_ms=elapsed * 1000,
                        )
                        self._result = OnboardVlmWorkerResult(
                            generation,
                            digest,
                            response,
                            message["raw_output"],
                            completed_at,
                            elapsed * 1000,
                            message["metrics"],
                        )
                        self._state = "ready"
        except Exception as exc:
            with self._lock:
                self._fail("failed", f"host_validation:{type(exc).__name__}")
        finally:
            # These are only owned children/queues. Never touch another model
            # process, Docker container or the user's desktop application.
            process = self._process
            if process is not None:
                if process.is_alive():
                    process.kill()
                process.join(timeout=1.0)
            for mailbox in (self._inbox, self._outbox):
                if mailbox is not None:
                    mailbox.cancel_join_thread()
                    mailbox.close()

    def close(self) -> None:
        """Bounded teardown, outside event-loop locks; not a flight command."""
        with self._lock:
            self._pending = self._result = None
            self._state = "closed"
            self._stop.set()
            self._retire.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        if thread is not None and thread.is_alive():
            raise RuntimeError("owned worker has not finished teardown")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.close()


__all__ = ["OnboardVlmProcessWorker", "OnboardVlmWorkerResult"]

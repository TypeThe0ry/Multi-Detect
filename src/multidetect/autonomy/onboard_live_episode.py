"""Create-only causal live observation episodes, without control authority.

The persistent episode is not a bounded model ledger: all observations can be
recorded in one session while each request selects its own real 1..8-frame
window. Optional per-request collectors supply the existing receive-only
execution semantics. A proposal, accepted revision, journal or ACK alone is
never an executed action or expert training label. Missing state stays unknown.

Caller-labelled source clocks are stored separately from the host clock. No
sensor-to-host clock conversion, source authentication, physical performance or
power-loss durability is inferred. Publication uses fsynced, same-directory
temporaries and atomic no-replace hard links; unsupported filesystems fail
closed. A failed publication faults the recorder rather than replaying facts.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .contracts import PromptEnvelope
from .onboard_continuation_binding import ContinuationReceivedFact
from .onboard_evidence_collector import (
    OnboardAcceptanceTrace,
    OnboardEvidenceCollector,
    OnboardMissionTelemetryBinding,
    OnboardTelemetryObservation,
)
from .onboard_multimodal import OnboardEvidenceReceipt, OnboardImageFrame, Scope
from .onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    OnboardVlmRuntimeResponse,
    parse_onboard_vlm_runtime_response,
)
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_real,
    require_text,
    strict_json_loads,
)

EPISODE_CONTRACT = "multidetect.onboard-live-causal-episode-v1"
_LIMIT_PROVENANCE = 256 * 1024
_LIMIT_BACKEND_METRICS = 256 * 1024
_FALSE_FLAGS = {
    "training_ready": False,
    "expert_action_labels_created": False,
    "execution_authority": False,
    "autonomous_flight_verified": False,
    "source_authentication_verified": False,
    "source_clock_mapping_proven": False,
}


class OnboardLiveEpisodeError(AutonomyContractError):
    """A causal, scope, clock or durable-byte boundary failed closed."""


def _json_bytes(document: dict) -> bytes:
    return (canonical_json(document) + "\n").encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _publish(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".episode-{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic create-only; never replace an existing file.
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(slots=True)
class _RequestState:
    request: OnboardVlmRuntimeRequest | None
    submitted_at_s: float
    collector: OnboardEvidenceCollector | None
    response: OnboardVlmRuntimeResponse | None = None
    decision_recorded: bool = False
    accepted: bool = False
    bound: bool = False
    message_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _FrameIdentity:
    metadata_sha256: str
    image_sha256: str
    captured_at_s: float
    provenance_sha256: str | None


class OnboardLiveEpisodeRecorder:
    """One session, one writer; no transport, actuation or policy callback.

    Use ``record_observation`` on each capture, then ``record_request`` at the
    actual worker submission and ``record_decision`` for its exact raw result.
    A fresh observation after that result is a measured successor, not proof
    that the desired movement occurred. To collect execution facts, associate
    an existing bounded collector with that request and route its actual typed
    acceptance/binding/receive inputs through this recorder, not model claims.
    The host owns receiver authentication and clock attribution.
    """

    @classmethod
    def create(
        cls,
        output_dir: Path,
        *,
        prompt: PromptEnvelope,
        prompt_text_original: str,
        scope: Scope,
        host_clock_id: str,
        created_at_s: float,
    ) -> OnboardLiveEpisodeRecorder:
        from .onboard_multimodal import MissionEvidenceLedger

        MissionEvidenceLedger(scope)
        if not isinstance(prompt, PromptEnvelope) or (
            prompt.canonical_hash != scope.prompt_sha256 or prompt.aircraft_id != scope.aircraft_id
        ):
            raise OnboardLiveEpisodeError("Prompt/aircraft does not match episode scope")
        if type(prompt_text_original) is not str or prompt_text_original.strip() != prompt.text:
            raise OnboardLiveEpisodeError("verbatim Prompt differs from PromptEnvelope")
        host_clock = require_text(host_clock_id, "host_clock_id")
        created = require_real(created_at_s, "created_at_s", minimum=0)
        if not isinstance(output_dir, Path) or not output_dir.name or ".." in output_dir.parts:
            raise OnboardLiveEpisodeError("output_dir must be a concrete new Path")
        absolute = output_dir.absolute()
        if any(path.is_symlink() for path in (absolute, *absolute.parents)):
            raise OnboardLiveEpisodeError("episode path must not traverse symlinks")
        parent = absolute.parent.resolve(strict=True)
        if not parent.is_dir():
            raise OnboardLiveEpisodeError("episode parent must already be a directory")
        root = parent / absolute.name
        root.mkdir()  # No exist_ok: an old episode is never reopened or overwritten.
        recorder = cls.__new__(cls)
        recorder.output_dir = root
        recorder.scope = scope
        recorder.prompt = prompt
        recorder.prompt_text_original = prompt_text_original
        recorder.host_clock_id = host_clock
        recorder._scope_hash = scope.canonical_hash
        recorder._prompt_hash = prompt.canonical_hash
        recorder._prompt_original_sha256 = _sha(prompt_text_original.encode("utf-8"))
        recorder._host_clock_id = host_clock
        recorder._lock = RLock()
        recorder._last_host_s = created
        # Persist full RGB, retain only small exact identities across a long episode.
        recorder._frames: dict[str, _FrameIdentity] = {}
        recorder._requests: dict[str, _RequestState] = {}
        recorder._receipts: dict[str, OnboardEvidenceReceipt] = {}
        recorder._source_times: dict[str, float] = {}
        recorder._receive_ids: set[str] = set()
        recorder._files: dict[str, dict] = {}
        recorder._event_hashes: list[str] = []
        recorder._last_decision: str | None = None
        recorder._last_evidence: str | None = None
        recorder._faulted = False
        recorder._closed = False
        (root / "events").mkdir()
        (root / "blobs").mkdir()
        header = {
            "schema_version": 1,
            "contract": EPISODE_CONTRACT,
            "scope": scope.to_mapping(),
            "scope_sha256": recorder._scope_hash,
            "prompt": prompt.to_mapping(),
            "prompt_sha256": prompt.canonical_hash,
            "prompt_text_original": prompt_text_original,
            "prompt_original_utf8_sha256": _sha(prompt_text_original.encode("utf-8")),
            "host_clock_id": host_clock,
            "clock_epoch": scope.clock_epoch,
            "created_at_s": created,
            "usage": "unreviewed_observation_and_receive_evidence_not_action_supervision",
            **_FALSE_FLAGS,
        }
        recorder._header = recorder._write("episode.json", _json_bytes(header))
        return recorder

    def _write(self, relative: str, payload: bytes) -> dict:
        record = {"path": relative, "bytes": len(payload), "sha256": _sha(payload)}
        prior = self._files.get(relative)
        if prior is not None:
            if prior != record:
                raise OnboardLiveEpisodeError("content-addressed file collision")
            self._verify(prior)
            return dict(prior)
        try:
            self._safe_root()
            path = self.output_dir / relative
            if path.parent.is_symlink():
                raise OnboardLiveEpisodeError("episode subdirectory changed to symlink")
            _publish(path, payload)
        except Exception:
            self._faulted = True
            raise
        self._files[relative] = record
        return dict(record)

    def _safe_root(self) -> None:
        if any(path.is_symlink() for path in (self.output_dir, *self.output_dir.parents)):
            raise OnboardLiveEpisodeError("episode path changed to symlink")

    def _verify(self, record: dict) -> None:
        self._safe_root()
        path = self.output_dir / record["path"]
        if path.is_symlink() or path.parent.is_symlink():
            raise OnboardLiveEpisodeError("recorded file changed to symlink")
        if path.stat().st_size != record["bytes"]:
            raise OnboardLiveEpisodeError("recorded original file bytes changed")
        with path.open("rb") as stream:
            payload = stream.read(record["bytes"] + 1)
        if len(payload) != record["bytes"] or _sha(payload) != record["sha256"]:
            raise OnboardLiveEpisodeError("recorded original file bytes changed")

    def validate_files(self) -> None:
        """Reopen every recorded original byte binding; also called on close."""
        with self._lock:
            for record in self._files.values():
                self._verify(record)

    def _open(self, at_s: float) -> float:
        if self._closed or self._faulted:
            raise OnboardLiveEpisodeError("episode is closed or publication-faulted")
        if (
            self.scope.canonical_hash != self._scope_hash
            or self.prompt.canonical_hash != (self._prompt_hash)
            or self._prompt_original_sha256 != _sha(self.prompt_text_original.encode("utf-8"))
            or (self.host_clock_id != self._host_clock_id)
        ):
            raise OnboardLiveEpisodeError("episode scope/Prompt changed")
        now = require_real(at_s, "host observed time", minimum=0)
        if now < self._last_host_s:
            raise OnboardLiveEpisodeError("host processing clock regressed")
        self._verify(self._header)
        if self._event_hashes:
            self._verify(self._files[f"events/{len(self._event_hashes) - 1:08d}.json"])
        return now

    def _blob(self, payload: bytes) -> dict:
        if type(payload) is not bytes or not payload:
            raise OnboardLiveEpisodeError("original source must be nonempty immutable bytes")
        return self._write(f"blobs/{_sha(payload)}.bin", payload)

    def _emit(self, kind: str, at_s: float, data: dict) -> dict:
        document = {
            "schema_version": 1,
            "contract": EPISODE_CONTRACT + "/event",
            "sequence": len(self._event_hashes),
            "kind": kind,
            "scope_sha256": self._scope_hash,
            "clock_epoch": self.scope.clock_epoch,
            "host_clock_id": self.host_clock_id,
            "host_observed_at_s": at_s,
            "previous_event_sha256": self._event_hashes[-1] if self._event_hashes else None,
            "preceding_decision_request_sha256": self._last_decision,
            "preceding_evidence_id": self._last_evidence,
            "data": data,
            **_FALSE_FLAGS,
        }
        record = self._write(f"events/{len(self._event_hashes):08d}.json", _json_bytes(document))
        self._event_hashes.append(record["sha256"])
        self._last_host_s = at_s
        return record

    def record_observation(
        self,
        frame: OnboardImageFrame,
        *,
        received_at_s: float,
        source_clock_id: str,
        source_timestamp_s: float | None = None,
        source_provenance: bytes | None = None,
        processed_at_s: float | None = None,
    ) -> dict:
        """Store exact RGB/state, not scene truth or an inferred movement label.

        ``captured_at_s`` keeps the input's explicit scope-clock semantics.
        Optional original sensor time can be in another named domain and is
        never subtracted from host time unless both domain IDs are identical.
        Provenance is original strict JSON bytes (e.g. raw depth associations),
        kept offline and never automatically supplied to a model or safety gate.
        """
        with self._lock:
            received = require_real(received_at_s, "received_at_s", minimum=0)
            now = self._open(received if processed_at_s is None else processed_at_s)
            if received > now:
                raise OnboardLiveEpisodeError("RGB receive time is future to host processing")
            if not isinstance(frame, OnboardImageFrame) or frame.scope != self.scope:
                raise OnboardLiveEpisodeError("observation crosses scope")
            frame.__post_init__()  # Recheck bytes/state even after illicit frozen mutation.
            if frame.frame_id in self._frames or frame.captured_at_s > received:
                raise OnboardLiveEpisodeError("duplicate frame or future scope-clock observation")
            if (
                self._frames
                and frame.captured_at_s <= next(reversed(self._frames.values())).captured_at_s
            ):
                raise OnboardLiveEpisodeError("RGB scope-clock order regressed or repeated")
            domain = require_text(source_clock_id, "source_clock_id")
            stamp = None
            if source_timestamp_s is not None:
                stamp = require_real(source_timestamp_s, "source_timestamp_s", minimum=0)
                if stamp <= self._source_times.get(domain, -1.0) or (
                    domain == self.host_clock_id and stamp > received
                ):
                    raise OnboardLiveEpisodeError("source clock regressed/repeated or is future")
            provenance = None
            provenance_sha256 = None
            if source_provenance is not None:
                if type(source_provenance) is not bytes or not (
                    0 < len(source_provenance) <= _LIMIT_PROVENANCE
                ):
                    raise OnboardLiveEpisodeError("provenance must be bounded immutable JSON bytes")
                parsed = strict_json_loads(source_provenance, name="source provenance")
                if not isinstance(parsed, dict):
                    raise OnboardLiveEpisodeError("source provenance must be an object")
                provenance_sha256 = canonical_sha256(parsed)
                provenance = self._blob(source_provenance)
            result = self._emit(
                "observation",
                now,
                {
                    "frame": frame.to_mapping(),
                    "rgb_original": self._blob(frame.image_bytes),
                    "host_received_at_s": received,
                    "source_clock_id": domain,
                    "source_timestamp_s": stamp,
                    "source_provenance_original": provenance,
                    "source_clock_mapping_proven": False,
                    "achieved_action": None,
                },
            )
            self._frames[frame.frame_id] = _FrameIdentity(
                canonical_sha256(frame.to_mapping()),
                frame.image_sha256,
                frame.captured_at_s,
                provenance_sha256,
            )
            if stamp is not None:
                self._source_times[domain] = stamp
            return result

    def record_request(
        self,
        request: OnboardVlmRuntimeRequest,
        *,
        submitted_at_s: float,
        collector: OnboardEvidenceCollector | None = None,
    ) -> dict:
        with self._lock:
            now = self._open(submitted_at_s)
            if not isinstance(request, OnboardVlmRuntimeRequest):
                raise TypeError("typed runtime request required")
            request.validate_binding()
            request.model_input.validate_freshness(now)
            model_input = request.model_input
            if model_input.scope != self.scope or (
                model_input.to_mapping()["prompt_original_text"] != self.prompt_text_original
            ):
                raise OnboardLiveEpisodeError("request scope/verbatim Prompt differs")
            for frame in model_input.ledger.frames:
                identity = self._frames.get(frame.frame_id)
                if identity is None or identity.metadata_sha256 != canonical_sha256(
                    frame.to_mapping()
                ):
                    raise OnboardLiveEpisodeError("request RGB/state not recorded exactly")
                self._verify(self._files[f"blobs/{frame.image_sha256}.bin"])
            for receipt in model_input.ledger.receipts:
                if self._receipts.get(receipt.evidence_id) != receipt:
                    raise OnboardLiveEpisodeError("request contains foreign/unrecorded receipt")
            request_mapping = request.to_mapping()
            if "factual_sensor_state" in request_mapping:
                facts = request_mapping["factual_sensor_state"]
                if not isinstance(facts, list) or len(facts) != len(model_input.frames):
                    raise OnboardLiveEpisodeError("live facts must bind each exact supplied frame")
                for frame, fact in zip(model_input.frames, facts, strict=True):
                    source = fact.get("source_provenance") if isinstance(fact, dict) else None
                    if (
                        not isinstance(fact, dict)
                        or fact.get("frame_id") != frame.frame_id
                        or not isinstance(source, dict)
                        or canonical_sha256(source)
                        != self._frames[frame.frame_id].provenance_sha256
                    ):
                        raise OnboardLiveEpisodeError(
                            "live facts differ from original frame provenance"
                        )
            identity = request.canonical_hash
            if identity in self._requests:
                raise OnboardLiveEpisodeError("duplicate request identity")
            if collector is not None:
                if (
                    not isinstance(collector, OnboardEvidenceCollector)
                    or collector.scope != self.scope
                ):
                    raise OnboardLiveEpisodeError("collector scope/type differs")
                snapshot = collector.snapshot()
                if snapshot != model_input.ledger:
                    raise OnboardLiveEpisodeError(
                        "request does not match its bounded collector ledger"
                    )
                if any(state.collector is collector for state in self._requests.values()):
                    raise OnboardLiveEpisodeError("each request requires its own bounded collector")
            result = self._emit(
                "request_submitted",
                now,
                {
                    "request_sha256": identity,
                    "input_sha256": request.input_sha256,
                    "request": request_mapping,
                    "model_input": model_input.to_mapping(),
                    "receive_collector_associated": collector is not None,
                },
            )
            self._requests[identity] = _RequestState(request, now, collector)
            return result

    def _state(self, request_sha256: str) -> _RequestState:
        state = self._requests.get(request_sha256)
        if state is None:
            raise OnboardLiveEpisodeError("unknown source request")
        if state.request is not None:
            state.request.validate_binding()
        return state

    def _receipt(
        self, receipt: OnboardEvidenceReceipt, collector: OnboardEvidenceCollector
    ) -> dict:
        receipt.__post_init__()
        if receipt.scope != self.scope or receipt not in collector.snapshot().receipts:
            raise OnboardLiveEpisodeError("receipt is not a collector-produced scoped fact")
        if receipt.evidence_id in self._receipts:
            raise OnboardLiveEpisodeError("duplicate evidence identity")
        if receipt.stage != "proposed":
            prior = self._receipts.get(receipt.prior_evidence_id)
            if prior is None or prior.proposal_id != receipt.proposal_id:
                raise OnboardLiveEpisodeError("receipt has no recorded causal predecessor")
        return {
            "receipt": receipt.to_mapping(),
            "receipt_original": self._blob(receipt.receipt_bytes),
            "source_receipt_original": self._blob(receipt.source_receipt_bytes),
        }

    def record_decision(
        self,
        request_sha256: str,
        raw_response: str | bytes | OnboardVlmRuntimeResponse,
        *,
        completed_at_s: float,
        inference_latency_ms: float,
        received_at_s: float | None = None,
        backend_metrics_json: str | None = None,
    ) -> OnboardVlmRuntimeResponse:
        with self._lock:
            completion = require_real(completed_at_s, "completed_at_s", minimum=0)
            now = self._open(completion if received_at_s is None else received_at_s)
            state = self._state(request_sha256)
            if state.decision_recorded or completion < state.submitted_at_s or completion > now:
                raise OnboardLiveEpisodeError("decision duplicated or predates submission")
            typed = raw_response if isinstance(raw_response, OnboardVlmRuntimeResponse) else None
            payload = typed.raw_response_bytes if typed is not None else raw_response
            if type(payload) not in {bytes, str}:
                raise TypeError("original response bytes/text required")
            original = payload.encode("utf-8") if isinstance(payload, str) else payload
            if not 0 < len(original) <= 1024 * 1024:
                raise OnboardLiveEpisodeError("raw decision exceeds response bound")
            metrics_original = None
            metrics_input_sha256 = None
            if backend_metrics_json is not None:
                if type(backend_metrics_json) is not str:
                    raise OnboardLiveEpisodeError("backend metrics must be immutable JSON text")
                source_bytes = backend_metrics_json.encode("utf-8")
                if not 0 < len(source_bytes) <= _LIMIT_BACKEND_METRICS:
                    raise OnboardLiveEpisodeError("backend metrics exceed byte bound")
                metrics = strict_json_loads(source_bytes, name="actual backend metrics")
                if not isinstance(metrics, dict):
                    raise OnboardLiveEpisodeError("backend metrics must be a JSON object")
                canonical_bytes = _json_bytes(metrics)  # Reject nonfinite; preserve every field.
                if len(canonical_bytes) > _LIMIT_BACKEND_METRICS:
                    raise OnboardLiveEpisodeError("canonical backend metrics exceed byte bound")
                metrics_original = self._blob(canonical_bytes)
                metrics_input_sha256 = _sha(source_bytes)
            try:
                response = parse_onboard_vlm_runtime_response(
                    original,
                    state.request,
                    current_scope=self.scope,
                    completed_at_s=completion,
                    inference_latency_ms=inference_latency_ms,
                )
                if typed is not None and typed != response:
                    raise OnboardLiveEpisodeError("typed decision differs from strict raw response")
            except (AutonomyContractError, ValueError, TypeError) as exc:
                self._emit(
                    "decision_rejected",
                    now,
                    {
                        "request_sha256": request_sha256,
                        "raw_response_original": self._blob(original),
                        "failure": str(exc),
                        "worker_completed_at_s": completion,
                        "backend_metrics_canonical": metrics_original,
                        "backend_metrics_input_utf8_sha256": metrics_input_sha256,
                        "typed_output": None,
                        "achieved_action": None,
                    },
                )
                state.decision_recorded = True
                state.request = None
                raise
            receipt = None
            facts = None
            if state.collector is not None:
                receipt = state.collector.record_proposal(
                    state.request,
                    original,
                    current_scope=self.scope,
                    completed_at_s=completion,
                    inference_latency_ms=inference_latency_ms,
                )
                facts = self._receipt(receipt, state.collector)
            self._emit(
                "decision_proposed",
                now,
                {
                    "request_sha256": request_sha256,
                    "response": response.to_mapping(),
                    "raw_response_original": self._blob(original),
                    "collector_evidence": facts,
                    "backend_metrics_canonical": metrics_original,
                    "backend_metrics_input_utf8_sha256": metrics_input_sha256,
                    "achieved_action": None,
                },
            )
            state.response, state.decision_recorded = response, True
            if state.collector is None:
                state.request = None  # Do not retain RGB for completed shadow windows.
            self._last_decision = request_sha256
            if receipt is not None:
                self._receipts[receipt.evidence_id] = receipt
                self._last_evidence = receipt.evidence_id
            return response

    def _receiving_state(self, request_sha256: str) -> _RequestState:
        state = self._state(request_sha256)
        if state.response is None or state.collector is None:
            raise OnboardLiveEpisodeError("execution evidence requires associated typed collector")
        return state

    def record_acceptance(self, trace: OnboardAcceptanceTrace) -> OnboardEvidenceReceipt:
        with self._lock:
            if not isinstance(trace, OnboardAcceptanceTrace):
                raise TypeError("actual typed acceptance trace required, not an ACK flag")
            now = self._open(trace.observed_at_s)
            state = self._receiving_state(trace.proposal_id)
            if state.accepted:
                raise OnboardLiveEpisodeError("acceptance already recorded")
            receipt = state.collector.record_acceptance(trace)
            self._emit(
                "revision_accepted",
                now,
                {
                    "request_sha256": trace.proposal_id,
                    **self._receipt(receipt, state.collector),
                    "achieved_action": None,
                },
            )
            state.accepted = True
            self._receipts[receipt.evidence_id] = receipt
            self._last_evidence = receipt.evidence_id
            return receipt

    def bind_vehicle_mission(
        self, proposal_id: str, binding: OnboardMissionTelemetryBinding, *, observed_at_s: float
    ) -> dict:
        with self._lock:
            now = self._open(observed_at_s)
            state = self._receiving_state(proposal_id)
            if not isinstance(binding, OnboardMissionTelemetryBinding) or state.bound:
                raise OnboardLiveEpisodeError("binding must be typed and recorded once")
            if binding.bound_at_s > now:
                raise OnboardLiveEpisodeError("binding time is future")
            original = binding.journal_path.read_bytes()
            if len(original) != binding.journal_bytes or _sha(original) != binding.journal_sha256:
                raise OnboardLiveEpisodeError("original Mission journal changed")
            state.collector.bind_vehicle_mission(proposal_id, binding)
            result = self._emit(
                "vehicle_mission_bound",
                now,
                {
                    "request_sha256": proposal_id,
                    "mission": binding.mission.to_mapping(),
                    "task": binding.task.to_mapping(),
                    "journal_original": self._blob(original),
                    "journal_source_path": str(binding.journal_path.absolute()),
                    "vehicle_identity_sha256": binding.vehicle_identity_sha256,
                    "bound_at_s": binding.bound_at_s,
                    "achieved_action": None,
                },
            )
            state.bound = True
            return result

    def record_vehicle_feedback(
        self, proposal_id: str, observation: OnboardTelemetryObservation, *, now_s: float
    ) -> OnboardEvidenceReceipt | None:
        with self._lock:
            now = self._open(now_s)
            state = self._receiving_state(proposal_id)
            if not isinstance(observation, OnboardTelemetryObservation):
                raise TypeError("actual original receive envelope required")
            raw = observation.to_mapping()
            if raw["message_id"] in state.message_ids:
                raise OnboardLiveEpisodeError("duplicate telemetry message identity")
            source = self._blob(observation.source_receipt_bytes)
            try:
                receipt = state.collector.ingest_vehicle_message(
                    proposal_id, observation, now_s=now
                )
            except (AutonomyContractError, ValueError, TypeError) as exc:
                self._emit(
                    "vehicle_feedback_rejected",
                    now,
                    {
                        "request_sha256": proposal_id,
                        "source_receipt_original": source,
                        "source_observed_at_s": raw["observed_at_s"],
                        "failure": str(exc),
                        "achieved_action": None,
                    },
                )
                state.message_ids.add(raw["message_id"])
                raise
            facts = None if receipt is None else self._receipt(receipt, state.collector)
            self._emit(
                "vehicle_feedback_received",
                now,
                {
                    "request_sha256": proposal_id,
                    "source_receipt_original": source,
                    "source_observed_at_s": raw["observed_at_s"],
                    "received_message": raw,
                    "collector_evidence": facts,
                    "execution_meaning": None if receipt is None else receipt.stage,
                    "expert_action_label": None,
                },
            )
            state.message_ids.add(raw["message_id"])
            if receipt is not None:
                self._receipts[receipt.evidence_id] = receipt
                self._last_evidence = receipt.evidence_id
            return receipt

    def record_navigation(self, fact: ContinuationReceivedFact, *, now_s: float) -> dict:
        """Preserve original typed HOME/GPI facts without granting execution."""
        with self._lock:
            now = self._open(now_s)
            if not isinstance(fact, ContinuationReceivedFact):
                raise TypeError("typed original navigation receive envelope required")
            raw = fact.to_mapping()
            if raw["message"]["message_type"] not in {"HOME_POSITION", "GLOBAL_POSITION_INT"}:
                raise OnboardLiveEpisodeError("navigation must be original HOME/GPI")
            if (
                raw["scope_sha256"] != self._scope_hash
                or raw["session_id"] != self.scope.session_id
                or raw["aircraft_id"] != self.scope.aircraft_id
                or raw["clock_epoch"] != self.scope.clock_epoch
                or raw["observed_at_s"] > now
                or raw["message_id"] in self._receive_ids
            ):
                raise OnboardLiveEpisodeError("navigation scope/time/identity mismatch")
            domain = "navigation-host-receive"
            if raw["observed_at_s"] < self._source_times.get(domain, -1.0):
                raise OnboardLiveEpisodeError("navigation receive clock regressed")
            result = self._emit(
                "navigation_received",
                now,
                {
                    "source_receipt_original": self._blob(fact.source_receipt_bytes),
                    "received_fact": raw,
                    "achieved_action": None,
                },
            )
            self._receive_ids.add(raw["message_id"])
            self._source_times[domain] = raw["observed_at_s"]
            return result

    def close(self, *, status: str, reason: str, closed_at_s: float) -> dict:
        """Seal exact existing bytes; failed/partial facts remain non-training-ready."""
        with self._lock:
            if self._closed:
                raise OnboardLiveEpisodeError("episode already sealed")
            if status not in {"closed", "failed_partial"}:
                raise OnboardLiveEpisodeError("unsupported episode termination status")
            reason = require_text(reason, "reason")
            now = require_real(closed_at_s, "closed_at_s", minimum=self._last_host_s)
            if (
                self.scope.canonical_hash != self._scope_hash
                or self.prompt.canonical_hash != self._prompt_hash
                or _sha(self.prompt_text_original.encode("utf-8")) != self._prompt_original_sha256
                or self.host_clock_id != self._host_clock_id
            ):
                raise OnboardLiveEpisodeError("episode identity changed before seal")
            if self._faulted and status != "failed_partial":
                raise OnboardLiveEpisodeError(
                    "publication-faulted episode cannot close successfully"
                )
            self.validate_files()
            stages = {name: 0 for name in ("proposed", "accepted", "executed", "completed")}
            stages["proposed"] = sum(
                state.response is not None for state in self._requests.values()
            )
            for receipt in self._receipts.values():
                if receipt.stage != "proposed":
                    stages[receipt.stage] += 1
            document = {
                "schema_version": 1,
                "contract": EPISODE_CONTRACT + "/seal",
                "scope_sha256": self._scope_hash,
                "clock_epoch": self.scope.clock_epoch,
                "status": status,
                "reason": reason,
                "closed_at_s": now,
                "host_clock_id": self.host_clock_id,
                "event_count": len(self._event_hashes),
                "event_chain_head_sha256": self._event_hashes[-1] if self._event_hashes else None,
                "observation_count": len(self._frames),
                "request_count": len(self._requests),
                "stage_counts": stages,
                "action_status": "unknown"
                if stages["executed"] == 0
                else "received_execution_evidence",
                "pending_request_sha256": [
                    key for key, value in self._requests.items() if not value.decision_recorded
                ],
                "files": list(self._files.values()),
                **_FALSE_FLAGS,
            }
            record = self._write("seal.json", _json_bytes(document))
            self._closed = True
            return record


__all__ = ["EPISODE_CONTRACT", "OnboardLiveEpisodeError", "OnboardLiveEpisodeRecorder"]

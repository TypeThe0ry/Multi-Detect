"""V8 finite normal controls with explicit NEW phase revalidation.

Frozen V7/V6 sources are unchanged. Original plan/context/permit identities
remain immutable; a separate current-source lease replaces only V7's original
snapshot TTL at mode/ARM boundaries. Missing real admission fails closed.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.mission_journal import MissionJournalIdentity
from scripts import onboard_teacher_mission_execution_v1 as frozen
from scripts import onboard_teacher_mission_live_revalidation_v1 as live_api

CONTRACT = "multidetect.teacher-owned-live-revalidated-Mission-execution-v2"


class TeacherMissionExecutionV2(frozen.TeacherMissionExecution):
    def __init__(self, *, original_inputs, live_input_provider, **inputs):
        if type(original_inputs) is not live_api.TeacherMissionOriginalInputs:
            raise TypeError("Typed approved original teacher inputs required")
        if not callable(live_input_provider):
            raise TypeError("Main-owned NEW actual phase input provider required")
        transport = inputs.get("transport")
        if type(transport) is not frozen.transfer.TeacherMissionTransport:
            raise TypeError("Exact original V6 transport required; no relaxed facade")
        identity = MissionJournalIdentity.from_transaction(transport._transaction.transaction)
        report = transport._transaction.transaction._validation_report
        live_api.validate_teacher_plan_source_admission(original_inputs, identity, report)
        if (
            inputs.get("context") != original_inputs.draft.context
            or inputs.get("admission") != original_inputs.draft.context_admission
        ):
            raise ValueError("V8 must keep the original approved context/admission")
        self._original, self._provider = original_inputs, live_input_provider
        self._live_inputs, self._live_receipt = None, None
        self._live_phase, self._phase_started_at_s = None, None
        self._live_evidence = []
        super().__init__(**inputs)
        live_api._historical_original_sources(
            original_inputs, artifact_root=self._root, clock_id=self.intent.clock_id
        )

    def _scope(self, *, starting=False):
        # Keep ALL old persistent/Grant/runtime/GCS/intent checks. A historical
        # plan context is not renamed current or exchanged for a new context.
        now = super()._scope(starting=False)
        if starting and self._active is not None:
            if self._active.kind not in {"mission-mode", "normal-arm"}:
                raise ValueError("Current-source lease is only for finite mode/ARM")
            return self._verify_live_receipt(self._active.kind, now)
        return now

    def _verify_live_receipt(self, kind, now=None):
        if self._live_receipt is None or self._live_inputs is None or self._live_phase != kind:
            raise ValueError("Missing exact NEW phase source revalidation lease")
        document = self._live_receipt.to_mapping()
        self._check_live_writer(self._live_inputs)
        initial = super()._scope(starting=False) if now is None else now
        if not document["qualified_at_s"] <= initial < document["expires_at_s"]:
            raise ValueError("Live phase receipt is future or stale")
        reproduced = live_api._reproduce_teacher_mission_live_receipt(
            self._original,
            self._live_inputs,
            receipt=self._live_receipt,
            identity=self._identity,
            validation_report=self._transport._transaction.transaction._validation_report,
            artifact_root=self._root,
            phase=kind,
            phase_started_at_s=self._phase_started_at_s,
            # The readonly reproduction API uses each receipt/checkpoint's
            # OWN original time. It never calls a main-owned writer API from
            # the sender or creates a refreshed lease. Expiry/energy below
            # use actual current time before AND after source reads.
            clock_id=self.intent.clock_id,
        )
        if reproduced != self._live_receipt:
            raise ValueError("Current phase source recomputation differs")
        self._check_live_writer(self._live_inputs)
        checked_now = super()._scope(starting=False)
        budget = document["current_budget"]
        if not checked_now < document["expires_at_s"] or budget[
            "full_route_estimated_duration_s"
        ] > budget["available_above_fixed_return_reserve_s"] - (
            checked_now - document["qualified_at_s"]
        ):
            raise ValueError("Live phase lease/energy exhausted while reading sources")
        return checked_now

    def _check_live_writer(self, inputs):
        checkpoint = inputs.navigation_checkpoint
        capture = self._transport._capture
        clock = checkpoint._live_clock
        if (
            checkpoint._live_capture is not capture
            or clock is None
            or clock.session_id != self.intent.clock_id
            or clock.monotonic != self._transport._clock
            or capture._owner != self._owner
            or capture._closed
            or capture._failure is not None
            or capture._busy
        ):
            raise ValueError("Current checkpoint must retain the exact open writer/live clock")
        # Append is valid for historical audits, not for an execution lease
        # ignoring already received and dual-committed newer observations.
        # This cannot detect updates not yet received/committed.
        prefixes = checkpoint.committed_prefixes
        for stage in ("raw", "projection"):
            expected = prefixes[stage]
            stat = Path(capture._streams[stage].name).stat()
            if (
                capture._sizes[stage] != expected["prefix_bytes"]
                or stat.st_size != expected["prefix_bytes"]
                or (stat.st_dev, stat.st_ino) != (expected["file_device"], expected["file_inode"])
                or capture._commits[stage] != expected["commits"]
                or capture._digests[stage].hexdigest() != expected["prefix_sha256"]
                or not expected["commits"]["attempted"]
                == expected["commits"]["fsync_returned"]
                == expected["commits"]["verified"]
            ):
                raise ValueError(
                    "Known dual-committed source generation changed; NEW phase required"
                )

    def _prepare_phase(self, kind):
        self._main()
        if self._active is not None or len(self._live_evidence) >= 2:
            raise RuntimeError("One bounded main-owned mode/ARM revalidation per phase")
        self.poll(0)
        self._runtime_now()
        started = super()._scope(starting=False)
        inputs = self._provider(phase=kind, phase_started_at_s=started, execution=self)
        if type(inputs) is not live_api.TeacherMissionLiveInputs:
            raise TypeError("Provider must return typed NEW actual phase sources")
        self._check_live_writer(inputs)
        receipt = live_api.revalidate_teacher_mission_for_control(
            self._original,
            inputs,
            identity=self._identity,
            validation_report=self._transport._transaction.transaction._validation_report,
            artifact_root=self._root,
            phase=kind,
            phase_started_at_s=started,
            now_s=super()._scope(starting=False),
            clock_id=self.intent.clock_id,
        )
        self._live_inputs, self._live_receipt = inputs, receipt
        self._live_phase, self._phase_started_at_s = kind, started
        binding = self._save(f"live-{kind}.json", receipt.to_mapping())
        self._verify_live_receipt(kind)
        self._live_evidence.append(
            {"phase": kind, "receipt": binding, "sha256": receipt.canonical_hash}
        )

    def _request(self, kind):
        """Prepare new sources, then calculate a fresh post-inspection API deadline."""
        self._main()
        try:
            if kind not in {"mission-mode", "normal-arm", "return-abort"}:
                raise ValueError("Only the three existing finite controls are allowed")
            if kind != "return-abort":
                self._prepare_phase(kind)
            self._transport._check_capture_binding()
            self._runtime_now()
            if len(self._attempts) >= 3:
                raise ValueError("Finite execution API budget exhausted")
            number = len(self._attempts)
            intent_at = super()._scope(starting=False)
            receipt = self._save(
                f"control-{number:02d}.intent.json",
                {
                    "kind": kind,
                    "requested_at_s": intent_at,
                    "clock_id": self.intent.clock_id,
                    "execution_intent_sha256": canonical_sha256(self.intent.to_mapping()),
                    "journal_identity_sha256": self._identity.canonical_hash,
                    "live_revalidation_sha256": None
                    if kind == "return-abort"
                    else self._live_receipt.canonical_hash,
                    "delivery_verified": False,
                },
            )
            # Source reads, runtime inspection AND intention fsync are complete.
            # Never spend the send timeout on those preparatory operations.
            now = super()._scope(starting=False)
            if kind != "return-abort":
                now = self._verify_live_receipt(kind, now)
            deadline = min(now + self._timeout, self.intent.expires_at_s)
            if kind != "return-abort":
                deadline = min(deadline, self._live_receipt.expires_at_s)
            if not now < deadline:
                raise ValueError("No current phase time remains before queueing API")
            command = frozen._ExecutionCommand(kind, now, deadline, threading.Event(), receipt)
            done, returned = threading.Event(), []
            with self._lock:
                if self._active is not None:
                    raise RuntimeError("Another execution API is already pending")
                self._active = command
            try:
                self._transport._receiver._commands.put_nowait(((command,), done, returned))
            except queue.Full as error:
                raise RuntimeError("Single execution command queue exhausted") from error
            wait_now = super()._scope(starting=False)
            if not done.wait(max(0, deadline - wait_now)):
                command.cancelled.set()
                raise RuntimeError("Execution API timeout; delivery unknown, no retry")
            valid, value = returned[0]
            if not valid:
                raise value
            super()._scope(starting=False)
            return value[0]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            with self._lock:
                self._active = None

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": CONTRACT,
            "original_plan_context_not_renewed": True,
            "live_revalidation_evidence": list(self._live_evidence),
            "live_provider_is_an_issuer": False,
        }


class OwnedTeacherGazeboSessionV8(frozen.OwnedTeacherGazeboSessionV7):
    """Exact existing receiver/transport, new subclass-bound execution only."""

    def bind_mission_execution(self, **inputs):
        if (
            threading.get_ident() != self._owner
            or self.mission_execution is not None
            or self.mission_transport is None
            or not self.execute
            or self._closed
        ):
            raise RuntimeError("One opened owned V8 session/transport execution binding required")
        if "transport" in inputs or "runtime_recheck" in inputs:
            raise ValueError("V8 retains the original exact transport and runtime reinspection")
        self.mission_execution = TeacherMissionExecutionV2(
            transport=self.mission_transport,
            runtime_recheck=lambda: self.attest_qualified(
                telemetry_qualified=inputs["runtime_attestation"].telemetry_qualified
            ),
            **inputs,
        )
        return self.mission_execution

    def to_mapping(self):
        return {**super().to_mapping(), "contract": "multidetect.onboard-teacher-gazebo-session-v8"}


__all__ = ["TeacherMissionExecutionV2", "OwnedTeacherGazeboSessionV8"]

"""V9 owned lifecycle bindings, sharing the ORIGINAL finite sortie deadline.

Docker creation/ownership/camera/RX cleanup reuse the frozen implementation;
only NEW budget, exact V4 transport and CP-v2 execution admission are added.
Importing or constructing this class does not start Docker or issue a Grant.
"""

from __future__ import annotations

import subprocess
import threading

from multidetect.px4_external_mission import ExternalRuntimeAttestation
from scripts import onboard_teacher_mission_execution_v2 as old
from scripts.onboard_teacher_mission_execution_v3 import TeacherMissionExecutionV3
from scripts.onboard_teacher_mission_transport_v2 import TeacherMissionTransportV2
from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget


class OwnedTeacherGazeboSessionV9(old.OwnedTeacherGazeboSessionV8):
    def __init__(self, plan, *, budget, **options):
        if type(budget) is not TeacherSortieLifecycleBudget:
            raise TypeError("Exact initial finite sortie budget required")
        clock = options.get("clock")
        if clock is None or clock.session_id != budget.clock_id:
            raise ValueError("Actual once-bound session clock must precede lifecycle budget")
        if plan.session_timeout_s != budget.duration_s:
            raise ValueError("Session plan and original complete lifecycle duration differ")
        budget.remaining_s(clock.monotonic(), clock_id=clock.session_id)
        self.budget = budget
        self._budget_sha256 = budget.canonical_hash
        super().__init__(plan, **options)

    def _remaining(self):
        if self.budget.canonical_hash != self._budget_sha256:
            raise ValueError("Original owned lifecycle budget changed")
        remaining = self.budget.remaining_s(self.clock.monotonic(), clock_id=self.clock.session_id)
        if remaining <= 0:
            raise RuntimeError("Original finite owned lifecycle deadline exhausted")
        return remaining

    def __enter__(self):
        # Frozen __enter__ writes a cosmetic relative deadline; every actual
        # wait/native call below already consults the original budget instead.
        result = super().__enter__()
        self.deadline_s = self.budget.deadline_s
        if self.camera is not None:
            self.camera.deadline_s = min(
                self.camera.deadline_s, self.budget.phase_deadline_s("rtl_land")
            )
        return result

    def _run(self, arguments, *, cleanup=False):
        if not cleanup:
            return super()._run(arguments, cleanup=False)
        # Cleanup is NOT a new standalone lease outside the experiment clock.
        timeout = min(15, self._remaining())
        argv = [self.plan.docker_plan.docker_executable, *arguments]
        result = (
            subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
            if self._runner is None
            else self._runner(argv, timeout_s=timeout)
        )
        if type(result.returncode) is not int:
            raise RuntimeError("Native cleanup exit missing")
        if len(result.stdout or "") + len(result.stderr or "") > 256 * 1024:
            raise RuntimeError("Native cleanup output cap exceeded")
        self._report.setdefault("native_commands", []).append(
            {"operation": arguments[:3], "native_exit_code": result.returncode}
        )
        self._remaining()
        return result

    def start_receiver(self, queue_capacity=None):
        capacity = self.budget.queue_capacity if queue_capacity is None else queue_capacity
        if type(capacity) is not int or capacity != self.budget.queue_capacity:
            raise ValueError("Original finite lifecycle queue capacity required")
        return super().start_receiver(queue_capacity=capacity)

    def bind_mission_transport(self, *, runtime_attestation, **inputs):
        if (
            threading.get_ident() != self._owner
            or self.mission_transport is not None
            or self._closed
            or not self.execute
        ):
            raise RuntimeError("One main-owned opened finite session transfer binding required")
        if type(
            runtime_attestation
        ) is not ExternalRuntimeAttestation or runtime_attestation != self.attest_qualified(
            telemetry_qualified=runtime_attestation.telemetry_qualified
        ):
            raise ValueError("Current exact owned runtime differs from qualified transfer input")
        if any(
            name in inputs
            for name in ("receiver", "connection", "clock", "clock_id", "budget", "remaining")
        ):
            raise ValueError("Session itself binds original RX/source/clock/budget")
        self.mission_transport = TeacherMissionTransportV2(
            receiver=self.receiver,
            connection=self.connection,
            clock=self.clock.monotonic,
            clock_id=self.clock.session_id,
            budget=self.budget,
            runtime_attestation=runtime_attestation,
            **inputs,
        )
        return self.mission_transport

    def bind_mission_execution(self, **inputs):
        if (
            threading.get_ident() != self._owner
            or self.mission_execution is not None
            or self.mission_transport is None
            or self._closed
            or not self.execute
        ):
            raise RuntimeError(
                "One opened finite session/exact transport execution binding required"
            )
        if "transport" in inputs or "runtime_recheck" in inputs:
            raise ValueError("Session binds actual transport/runtime reinspection itself")
        self.mission_execution = TeacherMissionExecutionV3(
            transport=self.mission_transport,
            runtime_recheck=lambda: self.attest_qualified(
                telemetry_qualified=inputs["runtime_attestation"].telemetry_qualified
            ),
            **inputs,
        )
        return self.mission_execution

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": "multidetect.onboard-teacher-gazebo-session-v9",
            "original_lifecycle_budget": self.budget.to_mapping(),
            "original_budget_sha256": self._budget_sha256,
            "cleanup_has_original_deadline": True,
        }


__all__ = ["OwnedTeacherGazeboSessionV9"]

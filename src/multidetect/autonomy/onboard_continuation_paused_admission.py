"""Host-owned, receive-only HOLD phase for an original continuation admission.

The ordinary parent verifier deliberately requires AUTO.MISSION.  It is used
once, before a pause, not replayed against an actual AUTO.LOITER heartbeat.
This module instead reopens the same journal and exact post-pause receive bytes.
It authors no flight permission, sends nothing, and has no watchdog. Tokens
are process-local and are not restored from mappings or across host restarts.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, replace
from threading import RLock

from ..px4_mission import MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
from . import onboard_continuation_binding as receive_contract
from .contracts import MissionCommand
from .onboard_continuation import ContinuationCompilation, ContinuationParentBinding
from .onboard_continuation_admission import (
    OnboardContinuationAdmission,
    _minimum_segment_path_length_m,
    prepare_anchored_continuation,
)
from .onboard_continuation_binding import ContinuationParentEvidence
from .onboard_vlm_admission import ExecutionSnapshot
from .serialization import (
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
)

PX4_AUTO_LOITER_CUSTOM_MODE = (4 << 16) | (3 << 24)
_TOKEN_KEY = object()


class PausedContinuationAdmissionError(ValueError):
    """A paused attempt is unavailable or its original/current facts differ."""


class ContinuationPauseAttempt:
    """One original pre-pause verification, one successful HOLD admission.

    Cancellation remains effective after consumption. The creating host owns
    this token; its mapping is provenance, not a serializable authorization.
    A dispatcher still needs its own source-consumption registry, send lease,
    cancellation linearization and durable restart recovery.
    """

    __slots__ = (
        "_original",
        "_identity",
        "_identity_sha256",
        "_original_sha256",
        "_generation",
        "_validated_at_s",
        "_consumed_sha256",
        "_cancel_reason",
        "_lock",
    )

    def __init__(self, original, attempt_id, generation, now_s, *, _key=None):
        if _key is not _TOKEN_KEY:
            raise TypeError("use begin_pause_attempt with the original admission")
        self._original = original
        self._original_sha256 = original.canonical_hash
        self._generation = require_int(generation, "generation", minimum=0)
        self._validated_at_s = require_real(now_s, "pre-pause verification time", minimum=0)
        self._identity = {
            "contract": "multidetect.continuation-pause-attempt-development-v1",
            "attempt_id": require_text(attempt_id, "attempt_id"),
            "generation": self._generation,
            "original_admission_sha256": self._original_sha256,
            "source_anchor_sha256": original.anchor.canonical_hash,
            "validated_at_s": self._validated_at_s,
            "execution_authority": False,
            "restart_restoration_supported": False,
        }
        self._identity_sha256 = canonical_sha256(self._identity)
        self._consumed_sha256 = None
        self._cancel_reason = None
        self._lock = RLock()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def original(self) -> OnboardContinuationAdmission:
        return self._original

    @property
    def validated_at_s(self) -> float:
        return self._validated_at_s

    @property
    def canonical_hash(self) -> str:
        return self._identity_sha256

    def to_mapping(self) -> dict:
        # The returned dictionary cannot mutate the token's identity.
        return dict(self._identity)

    def cancel(self, reason: str) -> None:
        reason = require_text(reason, "cancellation reason")
        with self._lock:
            if self._cancel_reason is None:
                self._cancel_reason = reason

    def validate_active(self, *, generation: int | None = None) -> None:
        """Host phase guard for either pending/consumed state, never a flight grant.

        This checks token/original identity and irreversible cancellation. The
        phase owner must still verify latest telemetry, safety and deadlines.
        """
        with self._lock:
            if (
                generation is not None
                and require_int(generation, "expected generation", minimum=0) != self._generation
            ):
                raise PausedContinuationAdmissionError("pause generation mismatch")
            self._assert(consumed_sha256=self._consumed_sha256)

    def validate_active_state(
        self, *, generation: int | None = None, consumed_sha256: str | None = None
    ) -> None:
        """Check only process-local token state, without reopening the original.

        None is the pending consumption marker. After admission the host must
        supply its independently cached exact admitted artifact SHA. This fast
        helper checks cancellation, generation and the token's small identity;
        it deliberately does NOT verify original/paused objects, journal bytes,
        received facts, freshness, or any flight permission. A phase owner must
        independently bind those unchanged bytes and enforce at-use deadlines.
        The existing validate_active/_assert retain their full original checks.
        """
        with self._lock:
            if self._cancel_reason is not None:
                raise PausedContinuationAdmissionError("pause attempt permanently cancelled")
            actual_generation = require_int(self._generation, "generation", minimum=0)
            if (
                generation is not None
                and require_int(generation, "expected generation", minimum=0) != actual_generation
            ):
                raise PausedContinuationAdmissionError("pause generation mismatch")
            if (
                actual_generation != self._identity["generation"]
                or require_real(self._validated_at_s, "pre-pause verification time", minimum=0)
                != self._identity["validated_at_s"]
                or self._original_sha256 != self._identity["original_admission_sha256"]
                or canonical_sha256(self._identity) != self._identity_sha256
            ):
                raise PausedContinuationAdmissionError("original/token identity changed")
            if consumed_sha256 is not None:
                if require_sha256(consumed_sha256, "expected consumed SHA") != consumed_sha256:
                    raise PausedContinuationAdmissionError("consumed SHA must be canonical")
            if self._consumed_sha256 != consumed_sha256:
                raise PausedContinuationAdmissionError("pause token already consumed or foreign")

    def _assert(self, *, consumed_sha256: str | None = None) -> None:
        with self._lock:
            if self._cancel_reason is not None:
                raise PausedContinuationAdmissionError("pause attempt permanently cancelled")
            if (
                require_int(self._generation, "generation", minimum=0)
                != self._identity["generation"]
                or self._validated_at_s != self._identity["validated_at_s"]
                or canonical_sha256(self._identity) != self._identity_sha256
                or self._original.canonical_hash != self._original_sha256
            ):
                raise PausedContinuationAdmissionError("original/token identity changed")
            if self._consumed_sha256 != consumed_sha256:
                raise PausedContinuationAdmissionError("pause token already consumed or foreign")

    def _consume(self, admission_sha256: str) -> None:
        with self._lock:
            self._assert()
            self._consumed_sha256 = admission_sha256


def begin_pause_attempt(
    original: OnboardContinuationAdmission, *, attempt_id: str, generation: int, now_s: float
) -> ContinuationPauseAttempt:
    """Verify the original AUTO.MISSION admission once, before any pause write."""
    if not isinstance(original, OnboardContinuationAdmission):
        raise TypeError("original must be OnboardContinuationAdmission")
    require_int(generation, "generation", minimum=0)
    require_real(now_s, "pre-pause verification time", minimum=0)
    require_text(attempt_id, "attempt_id")
    original.validate_binding(now_s=now_s)
    return ContinuationPauseAttempt(original, attempt_id, generation, now_s, _key=_TOKEN_KEY)


def _hold_parent(attempt, current, proof, now_s) -> ContinuationParentBinding:
    """Reuse strict receive parsers/journal identity, never AUTO.MISSION admission."""
    if not isinstance(proof, ContinuationParentEvidence):
        raise TypeError("HOLD parent evidence must be ContinuationParentEvidence")
    proof.__post_init__()
    original = attempt.original
    previous = original.parent_evidence
    scope = receive_contract._scope(current.scope)
    if (
        receive_contract._scope(proof.scope) != scope
        or proof.current_context != current.context
        or proof.mission_binding != previous.mission_binding
        or proof.journal_context != previous.journal_context
        or not set(proof.trusted_source_ids).issubset(previous.trusted_source_ids)
    ):
        raise PausedContinuationAdmissionError("HOLD source/current/original parent differs")
    _, identity = proof._journal()
    binding = proof.mission_binding
    raw = [item.to_mapping() for item in proof.telemetry]
    facts = [item.to_mapping() for item in proof.navigation_facts]
    if [x["message"]["message_type"] for x in raw] != [
        "HEARTBEAT",
        "EXTENDED_SYS_STATE",
        "MISSION_CURRENT",
    ] or [x["message"]["message_type"] for x in facts] != [
        "HOME_POSITION",
        "PARAM_VALUE",
        "GLOBAL_POSITION_INT",
    ]:
        raise PausedContinuationAdmissionError("ordered actual HOLD/navigation facts required")
    ids = set()
    stamp = attempt.validated_at_s
    for item in raw:
        receive_contract._fresh(item, now_s, stamp)
        stamp = item["observed_at_s"]
        if (
            item["clock_epoch"] != scope.clock_epoch
            or item["source_id"] not in proof.trusted_source_ids
            or (item["source_system_id"], item["source_component_id"]) != (1, 1)
            or item["mission_sha256"] != binding.mission.canonical_hash
            or item["transaction_id"] != identity["transaction_id"]
            or item["message_id"] in ids
            or item["observed_at_s"] > current.context.captured_at_s
        ):
            raise PausedContinuationAdmissionError("HOLD receiver/source/Mission/clock mismatch")
        ids.add(item["message_id"])
    heartbeat, lifecycle, cursor = (item["message"] for item in raw)
    sequence = cursor["seq"]
    if (
        not heartbeat["base_mode"] & 128
        or not heartbeat["base_mode"] & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        or heartbeat["custom_mode"] != PX4_AUTO_LOITER_CUSTOM_MODE
        or lifecycle["landed_state"] != 2
    ):
        raise PausedContinuationAdmissionError("actual armed AUTO.LOITER/IN_AIR required")
    if (
        sequence != original.compilation.parent.observed_current_sequence
        or not 1 <= sequence < len(binding.mission.items) - 1
        or binding.mission.items[sequence].command
        not in {MissionCommand.WAYPOINT, MissionCommand.LOITER_TIME}
    ):
        raise PausedContinuationAdmissionError("HOLD original parent cursor mismatch")
    stamp = attempt.validated_at_s
    for item in facts:
        receive_contract._receive_binding(
            item,
            scope,
            binding,
            identity["transaction_id"],
            proof.trusted_source_ids,
            now_s,
            stamp,
        )
        stamp = item["observed_at_s"]
        if (
            item["mission_sha256"] != binding.mission.canonical_hash
            or item["message_id"] in ids
            or item["observed_at_s"] > current.context.captured_at_s
        ):
            raise PausedContinuationAdmissionError("HOLD navigation Mission/time/ID mismatch")
        ids.add(item["message_id"])
    home, parameter, position = (item["message"] for item in facts)
    if parameter["param_id"] != "MIS_TKO_LAND_REQ" or parameter["param_type"] != 6:
        raise PausedContinuationAdmissionError("actual INT32 MIS_TKO_LAND_REQ required")
    value = struct.unpack(
        "<i" if parameter["parameter_encoding"] == "bytewise" else "<f",
        bytes.fromhex(parameter["raw_value_hex"]),
    )[0]
    if not math.isfinite(value) or value != 0:
        raise PausedContinuationAdmissionError("actual parameter does not allow continuation")
    context_home, aircraft = current.context.home_position, current.context.aircraft_position
    if (
        abs(context_home.latitude_deg - home["latitude"] / 1e7) > 0.5e-7 + 1e-12
        or abs(context_home.longitude_deg - home["longitude"] / 1e7) > 0.5e-7 + 1e-12
        or aircraft.altitude_reference != "relative_home"
        or aircraft.altitude_m is None
        or abs(aircraft.latitude_deg - position["lat"] / 1e7) > 0.5e-7 + 1e-12
        or abs(aircraft.longitude_deg - position["lon"] / 1e7) > 0.5e-7 + 1e-12
        or abs(aircraft.altitude_m - position["relative_alt"] / 1000) > 0.001
        or abs(position["alt"] - home["altitude"] - position["relative_alt"]) > 1
        or facts[-1]["observed_at_s"] != current.context.captured_at_s
        or (
            context_home.altitude_m is not None
            and (
                (
                    context_home.altitude_reference == "relative_home"
                    and context_home.altitude_m != 0
                )
                or (
                    context_home.altitude_reference == "amsl"
                    and abs(context_home.altitude_m - home["altitude"] / 1000) > 0.001
                )
                or context_home.altitude_reference not in {"relative_home", "amsl"}
            )
        )
    ):
        raise PausedContinuationAdmissionError("actual HOLD HOME/GPI/height differs from context")
    # Only after exact original journal and actual received facts have matched.
    return ContinuationParentBinding(
        binding.mission.mission_id,
        binding.mission.canonical_hash,
        identity["protocol_hash"],
        binding.journal_sha256,
        scope.airframe_profile_sha256,
        context_home,
        sequence,
        raw[-1]["observed_at_s"],
        1,
        1,
    )


def _compile_paused(attempt, current, proof, mission_id, now_s):
    require_real(now_s, "HOLD host time", minimum=attempt.validated_at_s)
    if not isinstance(current, ExecutionSnapshot):
        raise TypeError("current must be ExecutionSnapshot")
    if current.context.captured_at_s <= attempt.validated_at_s:
        raise PausedContinuationAdmissionError("HOLD context must be actually newer than pre-pause")
    parent = _hold_parent(attempt, current, proof, now_s)
    original = attempt.original
    original.authoring_evidence.validate_binding(original.anchor, current, proof, parent)
    return prepare_anchored_continuation(
        original.anchor,
        current,
        parent=parent,
        mission_id=mission_id,
        now_s=now_s,
        policy=original.policy,
        compile_policy=original.compile_policy,
    )


def prepare_paused_continuation(
    attempt: ContinuationPauseAttempt,
    current: ExecutionSnapshot,
    *,
    hold_parent_evidence: ContinuationParentEvidence,
    mission_id: str,
    now_s: float,
) -> ContinuationCompilation:
    """Geometry for independent current-wire clearance review; no consumption."""
    if not isinstance(attempt, ContinuationPauseAttempt):
        raise TypeError("attempt must be a host-owned pause token")
    attempt._assert()
    compilation = _compile_paused(attempt, current, hold_parent_evidence, mission_id, now_s)
    attempt._assert()
    return compilation


def _prepare(attempt, current, proof, mission_id, now_s):
    compilation = _compile_paused(attempt, current, proof, mission_id, now_s)
    original = attempt.original
    clearance = current.clearance
    policy = original.policy
    if clearance is None or clearance.source_id not in policy.trusted_clearance_source_ids:
        raise PausedContinuationAdmissionError("HOLD route clearance unknown/untrusted")
    replace(clearance)
    if (
        clearance.context_sha256 != current.context.canonical_hash
        or clearance.event_sha256 != current.event.canonical_hash
        or clearance.target != original.anchor.target
        or clearance.wire_hash != compilation.wire_hash
        or not attempt.validated_at_s <= clearance.observed_at_s <= current.context.captured_at_s
        or not 0 <= now_s - clearance.observed_at_s <= policy.max_current_age_s
        or clearance.minimum_clearance_m < policy.minimum_clearance_m
        or clearance.verified_path_length_m
        < (
            _minimum_segment_path_length_m(compilation)
            + 2 * current.context.gps_horizontal_accuracy_m
            + policy.minimum_clearance_m
        )
    ):
        raise PausedContinuationAdmissionError("HOLD clearance does not bind full current wire")
    return compilation


@dataclass(frozen=True, slots=True)
class PausedContinuationAdmission:
    attempt: ContinuationPauseAttempt
    current: ExecutionSnapshot
    hold_parent_evidence: ContinuationParentEvidence
    compilation: ContinuationCompilation
    admitted_at_s: float

    def __post_init__(self):
        if not isinstance(self.attempt, ContinuationPauseAttempt):
            raise TypeError("attempt must be a host-owned pause token")
        if (
            not isinstance(self.current, ExecutionSnapshot)
            or not isinstance(self.hold_parent_evidence, ContinuationParentEvidence)
            or not isinstance(self.compilation, ContinuationCompilation)
        ):
            raise TypeError("HOLD admission requires typed current/evidence/compilation")
        require_real(self.admitted_at_s, "HOLD admission time", minimum=0)

    def to_mapping(self) -> dict:
        return {
            "contract": "multidetect.onboard-continuation-paused-admission-development-v1",
            "attempt": self.attempt.to_mapping(),
            "original_admission_sha256": self.attempt.original.canonical_hash,
            "source_anchor": self.attempt.original.anchor.to_mapping(),
            "execution_snapshot": self.current.to_mapping(),
            "hold_parent_evidence": self.hold_parent_evidence.to_mapping(),
            "hold_parent_evidence_sha256": self.hold_parent_evidence.canonical_hash,
            "compilation": self.compilation.to_mapping(),
            "admitted_at_s": self.admitted_at_s,
            "source_timestamps_rewritten": False,
            "target_origin": "original_model_intent_at_source_position",
            "original_auto_mission_verifier_replayed_for_hold": False,
            "execution_authority": False,
            "pause_command_causal_proof_verified": False,
            "transport_created": False,
            "watchdog_implemented": False,
            "actual_flight_verified": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def validate_binding(self, *, now_s: float) -> None:
        self.attempt._assert(consumed_sha256=self.canonical_hash)
        require_real(now_s, "HOLD use time", minimum=self.admitted_at_s)
        ContinuationCompilation.from_mapping(self.compilation.to_mapping())
        rebuilt = _prepare(
            self.attempt,
            self.current,
            self.hold_parent_evidence,
            self.compilation.mission_id,
            now_s,
        ).to_mapping()
        rebuilt["compiled_at_s"] = self.compilation.compiled_at_s
        if canonical_sha256(rebuilt) != self.compilation.canonical_hash:
            raise PausedContinuationAdmissionError("HOLD compilation changed at use")
        self.attempt._assert(consumed_sha256=self.canonical_hash)


def admit_paused_continuation(
    attempt: ContinuationPauseAttempt,
    current: ExecutionSnapshot,
    *,
    hold_parent_evidence: ContinuationParentEvidence,
    mission_id: str,
    now_s: float,
) -> PausedContinuationAdmission:
    """Consume once after actual HOLD/current-wire checks; cancellation still wins."""
    if not isinstance(attempt, ContinuationPauseAttempt):
        raise TypeError("attempt must be a host-owned pause token")
    attempt._assert()
    compilation = _prepare(attempt, current, hold_parent_evidence, mission_id, now_s)
    result = PausedContinuationAdmission(attempt, current, hold_parent_evidence, compilation, now_s)
    attempt._consume(result.canonical_hash)
    return result


__all__ = [
    "PX4_AUTO_LOITER_CUSTOM_MODE",
    "PausedContinuationAdmissionError",
    "ContinuationPauseAttempt",
    "PausedContinuationAdmission",
    "begin_pause_attempt",
    "prepare_paused_continuation",
    "admit_paused_continuation",
]

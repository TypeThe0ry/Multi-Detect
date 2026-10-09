"""NEW V4 lifecycle source orchestration with explicit CP-v2 readonly API.

No frozen CP-v1 type gate is relaxed and no timestamp is renewed. Current
qualification stays main-owned; RX source reproduction returns the same lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from multidetect.autonomy.contracts import ValidationReport, ValidationVerdict
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.onboard_teacher_mission import TeacherMissionDraft
from multidetect.autonomy.serialization import (
    canonical_json,
    canonical_sha256,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from multidetect.autonomy.sim_navigation_reference import _native
from multidetect.autonomy.sim_planning_inputs import (
    SimPlanningQualification,
    _read_document,
    qualify_sim_planning_context,
    validate_sim_planning_qualification,
)
from multidetect.autonomy.sim_static_route_clearance import (
    StaticTeacherRouteClearance,
    evaluate_static_teacher_route,
    validate_static_teacher_route_clearance,
)
from multidetect.autonomy.sim_teacher_environment import StaticTeacherSimEnvironment
from multidetect.autonomy.teacher_live_navigation_checkpoint_v2 import (
    LiveTeacherNavigationCheckpointV2 as LiveTeacherNavigationCheckpoint,
)
from multidetect.mission_journal import MissionJournalIdentity
from scripts import onboard_teacher_mission_live_revalidation_v1 as frozen
from scripts.collect_onboard_teacher_gazebo_origin_window import WORLD_ARGS

CONTRACT = "multidetect.teacher-Mission-live-revalidation-v2"
PLAN_ADMISSION_RULE = frozen.PLAN_ADMISSION_RULE
PLAN_ADMISSION_CONTRACT = frozen.PLAN_ADMISSION_CONTRACT
MAXIMUM_AGE_S = frozen.MAXIMUM_AGE_S
_ADMISSION_FIELDS = frozen._ADMISSION_FIELDS
recompute_teacher_live_budget = frozen.recompute_teacher_live_budget


@dataclass(frozen=True, slots=True)
class TeacherMissionOriginalInputs:
    draft: TeacherMissionDraft
    navigation_checkpoint: LiveTeacherNavigationCheckpoint
    planning_qualification: SimPlanningQualification
    route_clearance: StaticTeacherRouteClearance
    environment: StaticTeacherSimEnvironment

    def admission_mapping(self):
        clearance = self.route_clearance.to_mapping()
        return {
            "contract": PLAN_ADMISSION_CONTRACT,
            "original_navigation_checkpoint_sha256": self.navigation_checkpoint.canonical_hash,
            "teacher_draft_authoring_sha256": self.draft.authoring_hash,
            "scene_sha256": clearance["scene_sha256"],
            "airframe_envelope_sha256": clearance["airframe_envelope_sha256"],
            "complete_route_audit_sha256": self.route_clearance.canonical_hash,
            "planning_model_source_sha256": self.planning_qualification.model_source.sha256,
            "environment_sha256": self.environment.canonical_hash,
        }


@dataclass(frozen=True, slots=True)
class TeacherMissionLiveInputs:
    navigation_checkpoint: LiveTeacherNavigationCheckpoint
    environment: StaticTeacherSimEnvironment
    loaded_world_native_source: EvidenceBinding


@dataclass(frozen=True, slots=True)
class TeacherMissionLiveRevalidation:
    """A recomputable phase lease, never authorization or execution evidence."""

    _document_json: str

    def to_mapping(self):
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def expires_at_s(self):
        return self.to_mapping()["expires_at_s"]


def _checkpoint_api():
    from multidetect.autonomy.teacher_live_navigation_checkpoint_v2 import (
        LiveTeacherNavigationCheckpointV2,
        reproduce_teacher_live_navigation_checkpoint_v2,
        validate_live_teacher_navigation_checkpoint_v2,
    )

    return (
        LiveTeacherNavigationCheckpointV2,
        validate_live_teacher_navigation_checkpoint_v2,
        reproduce_teacher_live_navigation_checkpoint_v2,
    )


def validate_teacher_plan_source_admission(original, identity, report):
    """Check exact source-bound finding already approved by the EXISTING Grant."""
    if type(original) is not TeacherMissionOriginalInputs:
        raise TypeError("Typed original teacher inputs required")
    if type(identity) is not MissionJournalIdentity or type(report) is not ValidationReport:
        raise TypeError("Existing journal identity and ValidationReport required")
    draft = original.draft
    if type(draft) is not TeacherMissionDraft or draft.schema_version != 2:
        raise ValueError("Original immutable v2 teacher draft required")
    if (
        report.verdict is not ValidationVerdict.PASS
        or report.canonical_hash != identity.validation_report_hash
        or report.executable_mission_hash != identity.executable_mission_hash
        or draft.context.canonical_hash != identity.bindings.context_snapshot_hash
        or draft.task.canonical_hash != identity.bindings.task_spec_hash
        or draft.compilation.plan.canonical_hash != identity.bindings.plan_hash
        or draft.airframe_profile_hash != identity.bindings.airframe_profile_hash
        or draft.compiled_mission.wire_hash != identity.protocol_hash
        or draft.compiled_mission.executable_mission.canonical_hash
        != identity.executable_mission_hash
    ):
        raise ValueError("Original plan/report/mission/journal binding differs")
    findings = [f for f in report.findings if f.rule_id == PLAN_ADMISSION_RULE]
    if len(findings) != 1 or findings[0].verdict is not ValidationVerdict.PASS:
        raise ValueError("One existing source-qualified teacher plan finding is required")
    finding = findings[0]
    value = strict_object(
        strict_json_loads(finding.reason),
        name="teacher plan source admission",
        required=frozenset({"contract", *_ADMISSION_FIELDS}),
    )
    for key in _ADMISSION_FIELDS:
        require_sha256(value[key], key)
    if canonical_json(value) != finding.reason or value != original.admission_mapping():
        raise ValueError("Approved source admission finding differs from original sources")


def _historical_original_sources(original, *, artifact_root, clock_id):
    kind, validate_checkpoint, reproduce_checkpoint = _checkpoint_api()
    checkpoint, draft, qualification = (
        original.navigation_checkpoint,
        original.draft,
        original.planning_qualification,
    )
    if type(checkpoint) is not kind or type(qualification) is not SimPlanningQualification:
        raise TypeError("Typed original navigation and budget qualifications required")
    # Original time is used solely to reproduce an OLD identity, not to grant
    # a current lease. All current-source checks use the actual current now.
    reproduce_checkpoint(
        checkpoint,
        artifact_root=artifact_root,
        clock_id=clock_id,
    )
    validate_sim_planning_qualification(
        qualification,
        observed_context=checkpoint.observed_context,
        qualified_context=draft.context,
        runtime_attestation=checkpoint.runtime_attestation,
        artifact_root=artifact_root,
        now_s=qualification.qualified_at_s,
        clock_id=clock_id,
    )
    if draft.context_admission.planning_qualification_sha256 != qualification.canonical_hash:
        raise ValueError("Original admission does not bind the actual original budget receipt")
    if type(original.environment) is not StaticTeacherSimEnvironment:
        raise TypeError("Original source-recomputed static environment required")
    original.environment.revalidate_sources(artifact_root)
    if type(original.route_clearance) is not StaticTeacherRouteClearance:
        raise TypeError("Original complete route source audit required")
    route = original.route_clearance.to_mapping()
    validate_static_teacher_route_clearance(
        original.route_clearance,
        observed_context=checkpoint.observed_context,
        geometry=draft.geometry,
        legs=draft.legs,
        runtime_attestation=checkpoint.runtime_attestation,
        artifact_root=artifact_root,
        now_s=route["evaluated_at_s"],
        clock_id=clock_id,
    )
    if (
        route["route_collision_clear"] is not True
        or route["scene_sha256"] != original.environment.scene.canonical_hash
        or original.environment.scene.world_source != checkpoint.world_source
    ):
        raise ValueError("Original source environment/complete route is not clear")
    identities = {}
    origin = _read_document(
        Path(artifact_root).resolve(),
        original.route_clearance.origin_source,
        128 * 1024,
        identities,
    )
    if origin != checkpoint.origin.to_mapping():
        raise ValueError("Original route origin is not the approved navigation checkpoint")


def _current_phase_sources(
    live,
    *,
    expected_runtime,
    expected_world,
    expected_scene,
    expected_origin_continuity_token,
    root,
    clock,
    start,
    now,
    _reproduce_checkpoint_identity_only=False,
):
    """Source-only subcheck; deliberately has no plan/Grant/dispatch admission."""
    if type(live) is not TeacherMissionLiveInputs:
        raise TypeError("Typed new phase sources required")
    kind, validate_checkpoint, reproduce_checkpoint = _checkpoint_api()
    checkpoint = live.navigation_checkpoint
    if type(checkpoint) is not kind:
        raise TypeError("Typed current live navigation checkpoint required")
    if not _reproduce_checkpoint_identity_only:
        if checkpoint._live_capture is None or checkpoint._live_clock is None:
            raise ValueError("Main current qualification requires the original live writer/clock")
        # This check is unconditional, including the case now equals the
        # checkpoint's own qualified time (which its historical API supports).
        checkpoint._live_capture._open()
        if not 0 <= checkpoint._live_clock.monotonic() - now <= 0.05:
            raise ValueError("Main phase qualification time is not the actual current clock")
    # Main qualification consumes an actual current clock and owner-checked
    # writer. Receipt reproduction instead reopens immutable source semantics
    # at the checkpoint's OWN original time. That is not a new live check or
    # lease; actual phase expiry remains independently checked below/by V8.
    if _reproduce_checkpoint_identity_only:
        reproduce_checkpoint(
            checkpoint,
            artifact_root=root,
            clock_id=clock,
            expected_origin_continuity_token=expected_origin_continuity_token,
        )
    else:
        validate_checkpoint(
            checkpoint,
            artifact_root=root,
            now_s=now,
            clock_id=clock,
            expected_origin_continuity_token=expected_origin_continuity_token,
        )
    if (
        checkpoint._live_capture is None
        or checkpoint._live_clock is None
        or checkpoint._live_capture._closed
        or checkpoint._live_capture._failure is not None
        or checkpoint._live_clock.session_id != clock
        or checkpoint._live_capture.clock_id != clock
        or checkpoint._live_capture._clock != checkpoint._live_clock.monotonic
        or not checkpoint.qualified_at_s <= now < checkpoint.expires_at_s
    ):
        raise ValueError("Historical/closed/different-writer checkpoint cannot be a live lease")
    if (
        checkpoint.runtime_attestation != expected_runtime
        or checkpoint.world_source != expected_world
        or checkpoint.observed_context.captured_at_s < start
        or checkpoint.inspected_at_s < start
    ):
        raise ValueError("Current phase/runtime/world observations are not NEW matching sources")
    if type(live.environment) is not StaticTeacherSimEnvironment:
        raise TypeError("Current loaded static environment required")
    live.environment.revalidate_sources(root)
    if live.environment.scene != expected_scene:
        raise ValueError("Current static scene differs from the approved route environment")
    native_identities = {}
    row = _read_document(root, live.loaded_world_native_source, 128 * 1024, native_identities)
    _, loaded_source, loaded_start, loaded_end = _native(
        root,
        row,
        WORLD_ARGS,
        {"session": {"container_id": checkpoint.runtime_attestation.container_id}},
        clock,
        native_identities,
    )
    if (
        loaded_source.path != live.environment.generated_sdf_source.path
        or loaded_source.bytes != live.environment.generated_sdf_source.bytes
        or loaded_source.sha256 != live.environment.generated_sdf_source.sha256
        or not start <= loaded_start <= loaded_end <= now
        or now - loaded_start >= MAXIMUM_AGE_S
    ):
        raise ValueError("Current loaded-world native source/window does not match")
    return checkpoint, loaded_start


def _revalidate_teacher_mission_for_control(
    original,
    live,
    *,
    identity,
    validation_report,
    artifact_root,
    phase,
    phase_started_at_s,
    now_s,
    clock_id,
    _reproduce_checkpoint_identity_only,
):
    """Reopen/recompute original authorization inputs and NEW phase sources."""
    if phase not in {"mission-mode", "normal-arm"}:
        raise ValueError("Only finite Mission-mode/normal-arm phase revalidation supported")
    now = require_real(now_s, "live control now", minimum=0)
    start = require_real(phase_started_at_s, "phase start", minimum=0, maximum=now)
    clock = require_text(clock_id, "live control clock")
    root = Path(artifact_root).resolve()
    validate_teacher_plan_source_admission(original, identity, validation_report)
    _historical_original_sources(original, artifact_root=root, clock_id=clock)
    checkpoint, loaded_start = _current_phase_sources(
        live,
        expected_runtime=original.navigation_checkpoint.runtime_attestation,
        expected_world=original.navigation_checkpoint.world_source,
        expected_scene=original.environment.scene,
        expected_origin_continuity_token=original.navigation_checkpoint.origin_continuity_token,
        root=root,
        clock=clock,
        start=start,
        now=now,
        _reproduce_checkpoint_identity_only=_reproduce_checkpoint_identity_only,
    )
    qualified, qualification = qualify_sim_planning_context(
        checkpoint.observed_context,
        runtime_attestation=checkpoint.runtime_attestation,
        observation_source=checkpoint.observation_source,
        model_source=original.planning_qualification.model_source,
        artifact_root=root,
        now_s=now,
        attested_at_s=checkpoint.inspected_at_s,
        clock_id=clock,
    )
    source = checkpoint.checkpoint_source
    origin_source = EvidenceBinding(
        source.path, source.bytes, source.sha256, "current-live-origin", "/origin"
    )
    clearance = evaluate_static_teacher_route(
        artifact_root=root,
        world_source=live.environment.scene.world_source,
        include_model_sources=dict(live.environment.scene.include_sources),
        origin_source=origin_source,
        airframe_envelope_source=original.route_clearance.airframe_envelope_source,
        observed_context=checkpoint.observed_context,
        geometry=original.draft.geometry,
        legs=original.draft.legs,
        runtime_attestation=checkpoint.runtime_attestation,
        now_s=now,
        clock_id=clock,
    )
    if clearance.to_mapping()["route_collision_clear"] is not True:
        raise ValueError("Current full swept teacher route is not clear")
    budget = recompute_teacher_live_budget(original.draft, qualified, qualification, now_s=now)
    expires = min(
        checkpoint.expires_at_s,
        loaded_start + MAXIMUM_AGE_S,
        checkpoint.observed_context.captured_at_s + MAXIMUM_AGE_S,
    )
    if not now < expires:
        raise ValueError("Current source lease is exhausted after revalidation")
    document = {
        "contract": CONTRACT,
        "phase": phase,
        "phase_started_at_s": start,
        "qualified_at_s": now,
        "expires_at_s": expires,
        "clock_id": clock,
        "journal_identity_sha256": identity.canonical_hash,
        "approved_original_sources": original.admission_mapping(),
        "original_context_sha256": original.draft.context.canonical_hash,
        "current_observed_context_sha256": checkpoint.observed_context.canonical_hash,
        "current_qualified_context_sha256": qualified.canonical_hash,
        "current_checkpoint_sha256": checkpoint.canonical_hash,
        "runtime_attestation_sha256": checkpoint.runtime_attestation.canonical_hash,
        "origin_continuity_token": checkpoint.origin_continuity_token,
        "current_environment_sha256": live.environment.canonical_hash,
        "current_loaded_world_native_source": live.loaded_world_native_source.to_mapping(),
        "current_route_clearance": clearance.to_mapping(),
        "current_planning_qualification": qualification.to_mapping(),
        "current_budget": budget,
        "claims": {
            name: False
            for name in (
                "Grant_created",
                "PASS_issued",
                "original_context_renewed",
                "resource_flags_modified",
                "controls_sent",
                "flight_executed",
                "source_authenticated",
                "hardware_control_enabled",
                "final_release_allowed",
            )
        },
    }
    return TeacherMissionLiveRevalidation(canonical_json(document))


def revalidate_teacher_mission_for_control(
    original,
    live,
    *,
    identity,
    validation_report,
    artifact_root,
    phase,
    phase_started_at_s,
    now_s,
    clock_id,
):
    """Main-owned complete CURRENT-source check; only this creates a phase lease."""
    return _revalidate_teacher_mission_for_control(
        original,
        live,
        identity=identity,
        validation_report=validation_report,
        artifact_root=artifact_root,
        phase=phase,
        phase_started_at_s=phase_started_at_s,
        now_s=now_s,
        clock_id=clock_id,
        _reproduce_checkpoint_identity_only=False,
    )


def _reproduce_teacher_mission_live_receipt(
    original,
    live,
    *,
    receipt,
    identity,
    validation_report,
    artifact_root,
    phase,
    phase_started_at_s,
    clock_id,
):
    """Readonly original-time source reproduction; NOT a current check/renewal.

    All source files, raw-prefix identity, navigation semantics, complete
    route and energy are reproduced. No writer owner API is called on RX.
    The V8 caller must independently enforce actual lease expiry, exact open
    transport writer/live clock and full reserve age before and after reads.
    """
    if type(receipt) is not TeacherMissionLiveRevalidation:
        raise TypeError("Existing immutable main-qualified phase receipt required")
    document = receipt.to_mapping()
    if (
        document["contract"] != CONTRACT
        or document["phase"] != phase
        or document["phase_started_at_s"] != phase_started_at_s
        or document["clock_id"] != clock_id
    ):
        raise ValueError("Original phase receipt identity differs")
    reproduced = _revalidate_teacher_mission_for_control(
        original,
        live,
        identity=identity,
        validation_report=validation_report,
        artifact_root=artifact_root,
        phase=phase,
        phase_started_at_s=phase_started_at_s,
        now_s=document["qualified_at_s"],
        clock_id=clock_id,
        _reproduce_checkpoint_identity_only=True,
    )
    if reproduced != receipt:
        raise ValueError("Readonly phase source reproduction differs; receipt is not renewed")
    return receipt  # Return the SAME lease; never a new qualification or expiry.


__all__ = [
    "TeacherMissionOriginalInputs",
    "TeacherMissionLiveInputs",
    "TeacherMissionLiveRevalidation",
    "validate_teacher_plan_source_admission",
    "recompute_teacher_live_budget",
    "revalidate_teacher_mission_for_control",
]

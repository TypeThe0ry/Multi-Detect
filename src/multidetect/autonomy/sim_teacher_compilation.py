"""Read/revalidate explicit SIM inputs before shared held-Mission authoring.

This source-bound compilation seam never arms, uploads, grants, starts or
labels a trajectory as executed. Container inspection and current camera,
origin/clearance checks belong to the caller's actual runtime admission; the
normal validator/operator grant/persistent Mission transaction still follow.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy
from .contracts import ContextSnapshot, ResolvedTaskSpec
from .mission_context_admission import ContextV2MissionAdmission
from .onboard_teacher_mission import (
    HeldTeacherLeg,
    TeacherMissionDraft,
    compile_teacher_held_mission,
)
from .planner import PlannerPolicy
from .serialization import canonical_sha256
from .sim_planning_inputs import (
    SimPlanningQualification,
    qualify_sim_planning_context,
    validate_sim_planning_qualification,
)

if TYPE_CHECKING:
    from ..px4_external_mission import ExternalRuntimeAttestation


def compile_qualified_sim_teacher_held_mission(
    *,
    task: ResolvedTaskSpec,
    observed_context: ContextSnapshot,
    qualified_context: ContextSnapshot,
    qualification: SimPlanningQualification,
    runtime_attestation: ExternalRuntimeAttestation,
    artifact_root: Path,
    now_s: float,
    clock_id: str,
    geometry: MultirotorMissionGeometry,
    safety_policy: Px4MultirotorSafetyPolicy,
    legs: tuple[HeldTeacherLeg, ...],
    planner_policy: PlannerPolicy | None = None,
    mission_id: str | None = None,
) -> tuple[TeacherMissionDraft, SimPlanningQualification]:
    """Return command artifact AND the current budget receipt it actually binds."""

    validate_sim_planning_qualification(
        qualification,
        observed_context=observed_context,
        qualified_context=qualified_context,
        runtime_attestation=runtime_attestation,
        artifact_root=artifact_root,
        now_s=now_s,
        clock_id=clock_id,
    )
    # A still-fresh old receipt proves identity, not unchanged remaining energy.
    # Age to the actual compilation time, without renewing any observation or
    # runtime inspection time. Return this receipt so its bound hash is auditable.
    qualified_context, qualification = qualify_sim_planning_context(
        observed_context,
        runtime_attestation=runtime_attestation,
        observation_source=qualification.observation_source,
        model_source=qualification.model_source,
        artifact_root=artifact_root,
        now_s=now_s,
        attested_at_s=qualification.attested_at_s,
        clock_id=clock_id,
        maximum_age_s=qualification.maximum_age_s,
    )
    admission = ContextV2MissionAdmission(
        context_snapshot_sha256=qualified_context.canonical_hash,
        task_spec_sha256=task.canonical_hash,
        geometry_sha256=canonical_sha256(geometry.to_mapping()),
        safety_policy_sha256=canonical_sha256(safety_policy.to_mapping()),
        airframe_profile_id=qualification.airframe_profile_id,
        airframe_profile_sha256=qualification.airframe_profile_hash,
        runtime_attestation_sha256=qualification.runtime_attestation_sha256,
        planning_qualification_sha256=qualification.canonical_hash,
        runtime_kind="SIM",
        vehicle_class="multirotor",
        clock_id=clock_id,
        observed_at_s=qualification.captured_at_s,
        qualified_at_s=qualification.qualified_at_s,
        expires_at_s=min(
            qualification.captured_at_s + qualification.maximum_age_s,
            qualification.attested_at_s + qualification.maximum_age_s,
        ),
    )
    draft = compile_teacher_held_mission(
        task=task,
        context=qualified_context,
        geometry=geometry,
        safety_policy=safety_policy,
        legs=legs,
        airframe_profile_hash=qualification.airframe_profile_hash,
        planner_policy=planner_policy,
        mission_id=mission_id,
        context_admission=admission,
        admission_now_s=now_s,
        admission_clock_id=clock_id,
    )
    return draft, qualification

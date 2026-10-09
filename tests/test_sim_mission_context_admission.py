"""CPU structural fixtures only; no actual Gazebo admission or flight claims."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from test_onboard_teacher_mission import inputs as teacher_inputs

from multidetect.autonomy.contracts import (
    ConservativeFlightCapability,
    ContextProvenance,
    ContextValueSource,
    MissionCommand,
    PlanAction,
    TaskType,
)
from multidetect.autonomy.mission_context_admission import ContextV2MissionAdmission
from multidetect.autonomy.onboard_teacher_mission import (
    TeacherMissionDraft,
    compile_teacher_held_mission,
)
from multidetect.autonomy.planner import (
    PlannerError,
    RouteAcquisitionRequirements,
    estimate_task,
    plan_task,
)
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.px4_mission import Px4MissionError, compile_multirotor_executable_mission


@pytest.fixture
def inputs_v2():
    inputs = teacher_inputs.__wrapped__()
    context = inputs["context"]
    context = replace(
        context,
        schema_version=2,
        airspeed_mps=None,
        perception_available=False,
        provenance=ContextProvenance(
            "SIM",
            ContextValueSource("unknown"),
            ContextValueSource("explicit_SIM_energy_model", "c" * 64),
            ContextValueSource("explicit_SIM_wind_model", "d" * 64),
        ),
        flight_capability=ConservativeFlightCapability(
            context.airframe_profile_id,
            context.airframe_profile_hash,
            "multirotor",
            "SIM",
            3.0,
            "c" * 64,
        ),
    )
    inputs["context"] = context
    inputs["context_admission"] = ContextV2MissionAdmission(
        context.canonical_hash,
        inputs["task"].canonical_hash,
        canonical_sha256(inputs["geometry"].to_mapping()),
        canonical_sha256(inputs["safety_policy"].to_mapping()),
        context.airframe_profile_id,
        context.airframe_profile_hash,
        "e" * 64,
        "f" * 64,
        "SIM",
        "multirotor",
        "fixture-monotonic",
        context.captured_at_s,
        10.5,
        12.0,
    )
    inputs["admission_now_s"] = 10.5
    inputs["admission_clock_id"] = "fixture-monotonic"
    return inputs


def _direct(inputs):
    context = inputs["context"]
    task = inputs["task"]
    plan = plan_task(
        task,
        context,
        "multirotor",
        resource_requirements=RouteAcquisitionRequirements(
            task.canonical_hash, context.canonical_hash
        ),
    )
    return compile_multirotor_executable_mission(
        platform_plan=plan,
        context_snapshot=context,
        geometry=inputs["geometry"],
        policy=inputs["safety_policy"],
        airframe_profile_hash=inputs["airframe_profile_hash"],
        context_admission=inputs["context_admission"],
        admission_now_s=inputs["admission_now_s"],
        admission_clock_id=inputs["admission_clock_id"],
    )


def test_scoped_v2_teacher_compiles_without_fabricating_perception(inputs_v2):
    draft = compile_teacher_held_mission(**inputs_v2)
    assert draft.schema_version == 2
    assert draft.contract.endswith("-v2")
    assert draft.context.airspeed_mps is None
    assert draft.context.perception_available is False
    assert draft.compilation.plan.context_snapshot_hash == draft.context.canonical_hash
    assert (
        draft.compiled_mission.executable_mission.airframe_profile_hash
        == inputs_v2["airframe_profile_hash"]
    )
    assert not any(draft.to_mapping()["claims"].values())
    assert TeacherMissionDraft.from_mapping(draft.to_mapping()).to_mapping() == draft.to_mapping()
    assert _direct(inputs_v2).executable_mission.items[0].command is MissionCommand.TAKEOFF


@pytest.mark.parametrize(
    "name",
    [
        "context_snapshot_sha256",
        "task_spec_sha256",
        "geometry_sha256",
        "safety_policy_sha256",
        "airframe_profile_sha256",
        "airframe_profile_id",
    ],
)
def test_binding_change_cannot_compile(inputs_v2, name):
    value = "0" * 64 if name.endswith("sha256") else "foreign-profile"
    inputs_v2["context_admission"] = replace(inputs_v2["context_admission"], **{name: value})
    with pytest.raises(Px4MissionError, match="binding mismatch|airframe profile mismatch"):
        _direct(inputs_v2)


@pytest.mark.parametrize("now", [10.49, 10.5001, 12.0, 12.01, float("nan"), True, None])
def test_original_observation_deadline_and_current_time_are_required(inputs_v2, now):
    inputs_v2["admission_now_s"] = now
    with pytest.raises(Px4MissionError):
        _direct(inputs_v2)


def test_clock_scope_and_missing_admission_are_rejected(inputs_v2):
    inputs_v2["admission_clock_id"] = "foreign-clock"
    with pytest.raises(Px4MissionError, match="clock domain"):
        _direct(inputs_v2)
    inputs_v2["context_admission"] = None
    with pytest.raises(Px4MissionError, match="actual SIM/HIL/REAL runtime scope"):
        _direct(inputs_v2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_kind", "REAL"),
        ("runtime_kind", "HIL"),
        ("vehicle_class", "vtol"),
        ("expires_at_s", 12.01),
        ("qualified_at_s", 9.0),
        ("observed_at_s", True),
        ("runtime_attestation_sha256", "invalid"),
        ("planning_qualification_sha256", "invalid"),
    ],
)
def test_admission_is_strict_and_not_a_generic_runtime_bypass(inputs_v2, field, value):
    with pytest.raises(ValueError):
        replace(inputs_v2["context_admission"], **{field: value})


def test_v2_artifact_tampering_is_not_recompiled_as_approved(inputs_v2):
    payload = copy.deepcopy(compile_teacher_held_mission(**inputs_v2).to_mapping())
    payload["compilation"]["compiled_mission"]["wire_items"][1]["param4"] = 180.0
    with pytest.raises(ValueError):
        TeacherMissionDraft.from_mapping(payload)


def test_route_requirement_exempts_only_detection_and_keeps_other_gates(inputs_v2):
    task, context = inputs_v2["task"], inputs_v2["context"]
    with pytest.raises(PlannerError, match="perception"):
        estimate_task(task, context, "multirotor")
    requirement = RouteAcquisitionRequirements(task.canonical_hash, context.canonical_hash)
    estimate_task(task, context, "multirotor", resource_requirements=requirement)
    for resource in ("camera_available", "geofence_valid", "map_cache_available", "dem_available"):
        unavailable = replace(context, **{resource: False})
        with pytest.raises(PlannerError):
            estimate_task(
                task,
                unavailable,
                "multirotor",
                resource_requirements=RouteAcquisitionRequirements(
                    task.canonical_hash, unavailable.canonical_hash
                ),
            )


@pytest.mark.parametrize(
    "change",
    [
        {"task_type": TaskType.AREA_SEARCH},
        {"on_detection": (PlanAction.CAPTURE_EVIDENCE,)},
        {"completion_action": PlanAction.LAND},
        {"task_id": "foreign-task"},
    ],
)
def test_route_requirement_cannot_be_reused_for_search_or_other_tasks(inputs_v2, change):
    task, context = inputs_v2["task"], inputs_v2["context"]
    requirement = RouteAcquisitionRequirements(task.canonical_hash, context.canonical_hash)
    with pytest.raises(PlannerError, match="route acquisition requirements"):
        estimate_task(
            replace(task, **change), context, "multirotor", resource_requirements=requirement
        )


def test_v1_teacher_mapping_and_identity_remain_unextended():
    draft = compile_teacher_held_mission(**teacher_inputs.__wrapped__())
    assert draft.schema_version == 1
    assert not set(draft.to_mapping()["inputs"]) & {
        "context_admission",
        "admission_now_s",
        "admission_clock_id",
    }
    assert (
        TeacherMissionDraft.from_mapping(draft.to_mapping()).canonical_hash == draft.canonical_hash
    )

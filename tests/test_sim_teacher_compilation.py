"""CPU/source fixtures only; no PX4/Gazebo movement or execution evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_sim_planning_inputs import _context, _model, _setup

from multidetect.autonomy.contracts import (
    GeoPoint,
    MissionCommand,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.onboard_teacher_mission import HeldTeacherLeg, TeacherMissionDraft
from multidetect.autonomy.planner import PlannerPolicy
from multidetect.autonomy.sim_planning_inputs import qualify_sim_planning_context
from multidetect.autonomy.sim_teacher_compilation import compile_qualified_sim_teacher_held_mission
from multidetect.px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy


def _inputs(root, **context_overrides):
    original, options = _setup(
        root,
        _context(
            geofence_valid=True,
            camera_available=True,
            perception_available=False,
            **context_overrides,
        ),
    )
    context, receipt = qualify_sim_planning_context(original, **options)
    home = context.home_position
    points = (
        GeoPoint(1.3, 103.80003, 12, "relative_home"),
        GeoPoint(1.3, 103.80006, 12, "relative_home"),
    )
    task = ResolvedTaskSpec(
        task_id="CPU-fixture-teacher-route",
        task_type=TaskType.SURVEY,
        geometry=TaskGeometry(
            kind="polyline",
            resolved_feature_id="CPU-fixture-route",
            direction="forward",
            length_m=6,
            centerline=points,
            resolution_confidence=1.0,
        ),
        target_classes=("declared-route",),
        on_detection=(),
        constraints=TaskConstraints(25, 900, True),
        completion_action=PlanAction.RETURN,
    )
    return dict(
        task=task,
        observed_context=original,
        qualified_context=context,
        qualification=receipt,
        runtime_attestation=options["runtime_attestation"],
        artifact_root=root,
        now_s=11.0,
        clock_id="fixture-clock",
        geometry=MultirotorMissionGeometry(
            home=home,
            waypoints=points,
            takeoff_altitude_m=12,
            loiter_time_s=2,
            loiter_radius_m=1,
            acceptance_radius_m=0.5,
        ),
        safety_policy=Px4MultirotorSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=100,
            allowed_airframe_profile_ids=(context.airframe_profile_id,),
            minimum_relative_altitude_m=5,
            maximum_relative_altitude_m=30,
            maximum_mission_radius_m=50,
            maximum_loiter_radius_m=2,
            maximum_loiter_time_s=100,
        ),
        legs=(HeldTeacherLeg(1, 2, 0), HeldTeacherLeg(2, 2, 90)),
    )


def test_source_qualified_teacher_compiles_shared_v2_mission_without_active_detector(tmp_path):
    inputs = _inputs(tmp_path)
    draft, current_receipt = compile_qualified_sim_teacher_held_mission(**inputs)
    assert current_receipt == inputs["qualification"]
    assert draft.schema_version == 2
    assert draft.context == inputs["qualified_context"]
    assert draft.context.airspeed_mps is None
    assert draft.context.perception_available is False
    assert draft.context_admission.context_snapshot_sha256 == draft.context.canonical_hash
    assert (
        draft.context_admission.planning_qualification_sha256
        == inputs["qualification"].canonical_hash
    )
    assert (
        draft.context_admission.runtime_attestation_sha256
        == inputs["runtime_attestation"].canonical_hash
    )
    assert draft.context_admission.observed_at_s == inputs["observed_context"].captured_at_s == 10.0
    assert draft.context_admission.expires_at_s == 12.0
    assert tuple(i.command for i in draft.compiled_mission.executable_mission.items) == (
        MissionCommand.TAKEOFF,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
        MissionCommand.LAND,
    )
    assert [i.param1 for i in draft.compiled_mission.wire_items[1:3]] == [2.0, 2.0]
    assert [i.param4 for i in draft.compiled_mission.wire_items[1:3]] == [0.0, 90.0]
    assert (
        draft.compilation.plan.context_snapshot_hash == inputs["qualified_context"].canonical_hash
    )
    assert (
        draft.budget_review["total_estimated_duration_s"]
        < draft.budget_review["available_duration_s"]
    )
    assert draft.budget_review["available_duration_s"] == 359.0
    assert (
        draft.context.battery_remaining_pct
        == inputs["observed_context"].battery_remaining_pct
        == 90.0
    )
    assert all(claim is False for claim in draft.to_mapping()["claims"].values())
    assert TeacherMissionDraft.from_mapping(draft.to_mapping()) == draft


def test_current_compilation_returns_the_aged_budget_receipt_bound_to_artifact(tmp_path):
    inputs = _inputs(tmp_path)
    previous_receipt = inputs["qualification"]
    previous_context = inputs["qualified_context"]
    inputs["now_s"] = 11.9
    draft, current_receipt = compile_qualified_sim_teacher_held_mission(**inputs)
    assert current_receipt != previous_receipt
    assert current_receipt.qualified_at_s == 11.9
    assert current_receipt.captured_at_s == previous_receipt.captured_at_s == 10.0
    assert current_receipt.attested_at_s == previous_receipt.attested_at_s == 10.75
    assert current_receipt.observed_context_sha256 == previous_receipt.observed_context_sha256
    assert current_receipt.runtime_attestation_sha256 == previous_receipt.runtime_attestation_sha256
    assert current_receipt.observation_source == previous_receipt.observation_source
    assert current_receipt.model_source == previous_receipt.model_source
    assert current_receipt.conservative_receive_age_deduction_s == pytest.approx(1.9)
    assert current_receipt.estimated_flight_time_s == pytest.approx(
        previous_receipt.estimated_flight_time_s - 0.9
    )
    assert draft.context.estimated_flight_time_s == current_receipt.estimated_flight_time_s
    assert draft.context.canonical_hash == current_receipt.qualified_context_sha256
    assert draft.context.canonical_hash != previous_context.canonical_hash
    assert draft.compilation.plan.context_snapshot_hash == current_receipt.qualified_context_sha256
    assert draft.context_admission.planning_qualification_sha256 == current_receipt.canonical_hash
    assert draft.context_admission.planning_qualification_sha256 != previous_receipt.canonical_hash
    assert draft.context_admission.qualified_at_s == 11.9
    assert draft.context_admission.expires_at_s == 12.0  # old observations are never renewed
    assert draft.budget_review["available_duration_s"] == pytest.approx(358.1, abs=1e-10)
    assert (
        draft.context.battery_remaining_pct
        == inputs["observed_context"].battery_remaining_pct
        == 90.0
    )
    assert all(claim is False for claim in current_receipt.to_mapping()["claims"].values())


def test_subsecond_age_can_flip_teacher_budget_from_pass_to_deny_near_reserve(tmp_path):
    inputs = _inputs(tmp_path)
    roomy_draft, _ = compile_qualified_sim_teacher_held_mission(**inputs)
    total_duration = roomy_draft.budget_review["total_estimated_duration_s"]
    policy = PlannerPolicy()
    battery = inputs["observed_context"].battery_remaining_pct
    reserve = inputs["task"].constraints.minimum_return_battery_pct
    usable_fraction = (battery - reserve - policy.minimum_battery_margin_pct) / battery
    # Declare a different explicit CPU-fixture model with just 0.4 s of
    # unreserved headroom at t=11. At t=11.9 the whole 0.9 s is spent, rather
    # than scaling elapsed consumption by the unreserved battery fraction.
    model = _model()
    remaining_endurance_at_ten = (total_duration + 1.0 + 0.4) / usable_fraction
    power = model["energy"]["avionics_power_w"] + model["energy"]["propulsion_max_power_w"]
    model["energy"]["capacity_wh"] = remaining_endurance_at_ten * power / 3600 / (battery / 100)
    original, options = _setup(tmp_path, inputs["observed_context"], model=model)
    context, receipt = qualify_sim_planning_context(original, **options)
    inputs.update(observed_context=original, qualified_context=context, qualification=receipt)
    permitted_draft, current_receipt = compile_qualified_sim_teacher_held_mission(**inputs)
    assert current_receipt.qualified_at_s == 11.0
    assert permitted_draft.budget_review["available_duration_s"] > total_duration
    assert permitted_draft.budget_review["available_duration_s"] - total_duration == pytest.approx(
        0.4
    )
    inputs["now_s"] = 11.9
    with pytest.raises(ValueError, match="supplemental duration exceeds energy budget"):
        compile_qualified_sim_teacher_held_mission(**inputs)


@pytest.mark.parametrize("path", ["raw.json", "observation.json", "model.json"])
def test_changed_source_cannot_enter_shared_compiler(tmp_path, path):
    inputs = _inputs(tmp_path)
    (tmp_path / path).write_bytes(b"{}")
    with pytest.raises(ValueError, match="byte size|SHA"):
        compile_qualified_sim_teacher_held_mission(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("now_s", 12.001),
        ("now_s", 10.99),
        ("clock_id", "other-clock"),
    ],
)
def test_old_future_and_wrong_clock_qualification_cannot_compile(tmp_path, field, value):
    inputs = _inputs(tmp_path)
    inputs[field] = value
    with pytest.raises(ValueError):
        compile_qualified_sim_teacher_held_mission(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("battery_remaining_pct", 91),
        ("captured_at_s", 10.001),
        ("home_position", GeoPoint(1.301, 103.8)),
        ("airspeed_mps", 1.0),
    ],
)
def test_changed_observed_or_derived_inputs_denied(tmp_path, field, value):
    inputs = _inputs(tmp_path)
    for name in ("observed_context", "qualified_context"):
        changed = inputs.copy()
        with pytest.raises(ValueError):
            changed[name] = replace(changed[name], **{field: value})
            compile_qualified_sim_teacher_held_mission(**changed)


@pytest.mark.parametrize(
    "field", ["camera_available", "geofence_valid", "map_cache_available", "dem_available"]
)
def test_no_resource_flag_is_fabricated_for_teacher_acquisition(tmp_path, field):
    inputs = _inputs(tmp_path)
    original = replace(inputs["observed_context"], **{field: False})
    # Save/re-qualify this distinct source context. It remains false thereafter.
    original, options = _setup(tmp_path, original)
    context, receipt = qualify_sim_planning_context(original, **options)
    inputs.update(observed_context=original, qualified_context=context, qualification=receipt)
    with pytest.raises(ValueError, match="unavailable|not valid"):
        compile_qualified_sim_teacher_held_mission(**inputs)


def test_detection_task_cannot_use_teacher_acquisition_exception(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["task"] = replace(inputs["task"], on_detection=(PlanAction.CAPTURE_EVIDENCE,))
    with pytest.raises(
        ValueError, match="without detection|no detection|non-reactive|empty|on_detection"
    ):
        compile_qualified_sim_teacher_held_mission(**inputs)


def test_different_owned_runtime_cannot_reuse_qualification(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["runtime_attestation"] = replace(inputs["runtime_attestation"], container_id="9" * 64)
    with pytest.raises(ValueError, match="recomputation differs"):
        compile_qualified_sim_teacher_held_mission(**inputs)


def test_forged_receipt_cannot_grant_source_qualified_compilation(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["qualification"] = replace(inputs["qualification"], modeled_endurance_before_age_s=9000)
    with pytest.raises(ValueError, match="recomputation differs"):
        compile_qualified_sim_teacher_held_mission(**inputs)

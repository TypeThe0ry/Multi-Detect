from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from test_onboard_continuation_binding import _change, _proof_for
from test_onboard_vlm_admission import _anchor, _clearance, _current

from multidetect.autonomy.contracts import GeoPoint
from multidetect.autonomy.onboard_agent import OnboardEventKind
from multidetect.autonomy.onboard_continuation import ContinuationParentBinding
from multidetect.autonomy.onboard_continuation_admission import (
    AUTHORING_CONTRACT,
    ContinuationAuthoringEvidence,
    OnboardContinuationAdmissionError,
    admit_anchored_continuation,
    prepare_anchored_continuation,
)
from multidetect.autonomy.onboard_vlm_admission import (
    ExecutionAdmissionPolicy,
)
from multidetect.autonomy.serialization import canonical_json, canonical_sha256
from multidetect.mission_planning import compile_planned_task_mission_from_runtime


def _parent(anchor, current):
    # Pure software geometry fixture, not a receiver or execution permit.
    return ContinuationParentBinding(
        parent_mission_id="fixture-parent",
        parent_mission_sha256=anchor.source.mission_sha256,
        parent_wire_sha256="b" * 64,
        parent_journal_sha256="c" * 64,
        airframe_profile_sha256=current.context.airframe_profile_hash,
        home_position=current.context.home_position,
        observed_current_sequence=1,
        observed_at_s=current.context.captured_at_s,
        source_system=1,
        source_component=1,
    )


def _prepare(anchor, current, **kwargs):
    return prepare_anchored_continuation(
        anchor,
        current,
        parent=kwargs.pop("parent", _parent(anchor, current)),
        mission_id="fixture-child",
        now_s=kwargs.pop("now_s", 2.2),
        **kwargs,
    )


def test_source_target_stays_fixed_while_current_position_changes_without_lifecycle():
    anchor = _anchor()
    original = anchor.to_mapping(), anchor.result.raw_output, anchor.request.canonical_hash
    current1 = _current(anchor, north=3, east=4)
    current2 = _current(anchor, north=-5, east=2)
    a, b = _prepare(anchor, current1), _prepare(anchor, current2)
    expected = round(anchor.target.latitude_deg * 1e7), round(anchor.target.longitude_deg * 1e7)
    for compilation, current in ((a, current1), (b, current2)):
        wire = compilation.compiled_mission.wire_items
        assert [item.command for item in wire] == [16, 16, 19]
        assert (wire[1].x, wire[1].y) == expected
        assert (wire[0].x, wire[0].y) == (
            round(current.context.aircraft_position.latitude_deg * 1e7),
            round(current.context.aircraft_position.longitude_deg * 1e7),
        )
        assert compilation.resume_sequence == 1
        assert compilation.compiled_mission.executable_mission.platform_plan_hash
        assert compilation.to_mapping()["authority"]["flight_authorized"] is False
    assert a.wire_hash != b.wire_hash
    assert original == (
        anchor.to_mapping(),
        anchor.result.raw_output,
        anchor.request.canonical_hash,
    )


@pytest.mark.parametrize("altitude", [0.0, 299.0])
def test_do_not_clamp_original_requested_altitude_into_another_goal(altitude):
    anchor = _anchor(altitude=altitude)
    with pytest.raises(OnboardContinuationAdmissionError, match="cannot be clamped"):
        _prepare(anchor, _current(anchor))


@pytest.mark.parametrize(
    "field,value",
    [
        ("mission_revision", 5),
        ("receipt_revision", 4),
        ("mission_sha256", "f" * 64),
        ("policy_artifact_sha256", "e" * 64),
    ],
)
def test_new_parent_or_execution_head_retires_old_source_intent(field, value):
    anchor = _anchor()
    current = replace(_current(anchor), **{field: value})
    with pytest.raises(OnboardContinuationAdmissionError, match="changed"):
        _prepare(anchor, current)


@pytest.mark.parametrize(
    "changes",
    [
        {"gps_fix_type": 2},
        {"gps_horizontal_accuracy_m": 99.0},
        {"geofence_valid": False},
        {"camera_available": False},
        {"perception_available": False},
        {"map_cache_available": False},
        {"dem_available": False},
    ],
)
def test_current_resource_failure_rejects_preparation(changes):
    anchor = _anchor()
    with pytest.raises(OnboardContinuationAdmissionError, match="navigation/resources"):
        _prepare(anchor, _current(anchor, context_changes=changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": OnboardEventKind.OBSTACLE_RISK},
        {"kind": OnboardEventKind.BATTERY_LOW},
        {"kind": OnboardEventKind.GPS_DEGRADED},
        {"kind": OnboardEventKind.INFERENCE_FAULT},
        {"kind": OnboardEventKind.MISSION_COMPLETE},
        {"obstacle_distance_m": None},
        {"obstacle_distance_m": 5000.0},
        {"obstacle_distance_m": 2.0},
    ],
)
def test_current_fault_does_not_become_a_valid_continuation(changes):
    anchor = _anchor()
    with pytest.raises(OnboardContinuationAdmissionError, match="latest event"):
        _prepare(anchor, _current(anchor, event_changes=changes))


def test_latest_unknown_altitude_is_not_inferred_from_source():
    anchor = _anchor()
    current = _current(anchor, context_changes={"aircraft_position": GeoPoint(1.3, 103.8)})
    with pytest.raises(OnboardContinuationAdmissionError, match="latest height"):
        _prepare(anchor, current)


def test_parent_identity_is_not_relabelled_from_model():
    anchor = _anchor()
    current = _current(anchor)
    parent = replace(_parent(anchor, current), parent_mission_sha256="f" * 64)
    with pytest.raises(OnboardContinuationAdmissionError, match="actual snapshot"):
        _prepare(anchor, current, parent=parent)


def test_current_expiry_and_future_processing_reject_without_time_rewrite():
    anchor = _anchor()
    current = _current(anchor)
    with pytest.raises(ValueError):
        _prepare(anchor, current, now_s=4.0)
    with pytest.raises(ValueError):
        _prepare(anchor, current, now_s=2.0)
    assert current.context.captured_at_s == 2.1


def test_stricter_source_age_is_enforced_even_if_model_input_age_permits_it():
    anchor = _anchor()
    current = _current(anchor)
    with pytest.raises(OnboardContinuationAdmissionError, match="stale"):
        _prepare(anchor, current, policy=ExecutionAdmissionPolicy(max_source_age_s=0.1))


def _admission_setup(tmp_path):
    anchor = _anchor()
    # This source was authored under a canonical full-sortie parent. Keep all
    # model bytes unchanged; set its independently constructed fixture Mission.
    parent_compilation = compile_planned_task_mission_from_runtime(
        anchor.task,
        anchor.source.context,
        "multirotor",
        runtime=anchor.runtime,
        airframe_profile_hash=anchor.source.context.airframe_profile_hash,
        mission_id="fixture-parent",
    )
    parent_mission = parent_compilation.compiled_mission.executable_mission
    anchor = replace(
        anchor, source=replace(anchor.source, mission_sha256=parent_mission.canonical_hash)
    )
    current = _current(anchor)
    proof = _proof_for(
        tmp_path,
        current.context,
        current.scope,
        parent_mission,
        journal_context=anchor.source.context,
        task=anchor.task,
        runtime=anchor.runtime,
    )
    parent = proof.validate_binding(
        current_scope=current.scope, current_context=current.context, now_s=2.2
    )
    compilation = _prepare(anchor, current, parent=parent)
    old_clearance = current.clearance
    current = replace(
        current,
        clearance=_clearance(
            current.context, current.event, anchor.target, wire_hash=compilation.wire_hash
        ),
    )
    policy = ExecutionAdmissionPolicy(trusted_clearance_source_ids=("fixture-segment-clearance",))
    return anchor, current, proof, policy, old_clearance


def _authoring(anchor, current, proof):
    # Original host-owner declaration is synthetic in these unit tests.
    parent = proof.validate_binding(
        current_scope=current.scope, current_context=current.context, now_s=2.2
    )
    payload = canonical_json(
        {
            "schema_version": 1,
            "contract": AUTHORING_CONTRACT,
            "source_id": "fixture-original-authoring",
            "observed_at_s": proof.journal_context.captured_at_s,
            "scope_sha256": parent_scope_hash(current),
            "parent_mission_sha256": parent.parent_mission_sha256,
            "parent_wire_sha256": parent.parent_wire_sha256,
            "journal_context_sha256": proof.journal_context.canonical_hash,
            "parent_journal_sha256": parent.parent_journal_sha256,
            "parent_transaction_id": proof.to_mapping()["journal_identity"]["transaction_id"],
            "task_sha256": anchor.task.canonical_hash,
            "runtime_sha256": canonical_sha256(anchor.runtime),
            "policy_artifact_sha256": current.policy_artifact_sha256,
        }
    ).encode()
    return ContinuationAuthoringEvidence(
        payload, hashlib.sha256(payload).hexdigest(), ("fixture-original-authoring",)
    )


def parent_scope_hash(current):
    from multidetect.autonomy.onboard_history import OnboardInferenceScope
    from multidetect.autonomy.onboard_multimodal import OnboardMultimodalScope

    scope = current.scope
    if isinstance(scope, OnboardInferenceScope):
        scope = OnboardMultimodalScope.from_inference_scope(scope)
    return scope.canonical_hash


def _admit(anchor, current, proof, policy, *, now_s=2.2, authoring=None):
    return admit_anchored_continuation(
        anchor,
        current,
        parent_evidence=proof,
        authoring_evidence=_authoring(anchor, current, proof) if authoring is None else authoring,
        mission_id="fixture-child",
        now_s=now_s,
        policy=policy,
    )


def test_original_parent_receive_proof_and_source_goal_bind_complete_segment(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    admission = _admit(anchor, current, proof, policy)
    admission.validate_binding(now_s=2.3)
    mapping = admission.to_mapping()
    assert mapping["parent_evidence_sha256"] == proof.canonical_hash
    assert mapping["source_anchor"]["source_timestamps_rewritten"] is False
    assert mapping["model_saw_execution_context"] is False
    assert mapping["execution_authority"] is False
    assert mapping["host_replacement_transport_implemented"] is False
    assert mapping["hold_watchdog_implemented"] is False
    assert mapping["actual_flight_verified"] is False
    assert mapping["full_runtime_body_identity_bound"] is True
    assert mapping["authenticated_original_authoring_source_verified"] is False
    assert mapping["live_original_authoring_producer_implemented"] is False
    assert [x.command for x in admission.compilation.compiled_mission.wire_items] == [16, 16, 19]
    assert admission.anchor.result.raw_output == anchor.result.raw_output


def test_default_clearance_trust_stays_empty_even_with_good_parent_facts(tmp_path):
    anchor, current, proof, _, _ = _admission_setup(tmp_path)
    with pytest.raises(OnboardContinuationAdmissionError, match="untrusted"):
        _admit(anchor, current, proof, ExecutionAdmissionPolicy())


def test_old_whole_sortie_clearance_is_not_relabelled_as_new_segment(tmp_path):
    anchor, current, proof, policy, old_clearance = _admission_setup(tmp_path)
    with pytest.raises(OnboardContinuationAdmissionError, match="bind this current segment"):
        _admit(anchor, replace(current, clearance=old_clearance), proof, policy)


def test_actual_position_mismatch_rejects_despite_fresh_hash_valid_envelope(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    facts = list(proof.navigation_facts)
    facts[-1] = _change(facts[-1], message={"relative_alt": 35002})
    with pytest.raises(ValueError, match="HOME/relative-altitude/position"):
        _admit(anchor, current, replace(proof, navigation_facts=tuple(facts)), policy)


def test_parent_receiver_trust_is_not_inherited_from_route_receiver_trust(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    with pytest.raises(ValueError):
        _admit(anchor, current, replace(proof, trusted_source_ids=()), policy)


def test_hashed_armed_false_fact_never_qualifies_continuation(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    telemetry = list(proof.telemetry)
    telemetry[0] = _change(telemetry[0], message={"base_mode": 1})
    with pytest.raises(ValueError, match="armed"):
        _admit(anchor, current, replace(proof, telemetry=tuple(telemetry)), policy)


def test_use_time_expiry_or_regression_revalidates_all_sources(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    admission = _admit(anchor, current, proof, policy)
    with pytest.raises(ValueError):
        admission.validate_binding(now_s=3.2)
    with pytest.raises(ValueError, match="precedes"):
        admission.validate_binding(now_s=2.15)
    assert admission.admitted_at_s == 2.2


def test_partial_horizontal_clearance_does_not_cover_climb_and_hold(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    short = _clearance(
        current.context,
        current.event,
        anchor.target,
        wire_hash=current.clearance.wire_hash,
        verified_path_length_m=1.0,
    )
    with pytest.raises(ValueError, match="full 3-D"):
        _admit(anchor, replace(current, clearance=short), proof, policy)


def test_same_task_id_cannot_substitute_different_original_task_requirements(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    foreign_task = replace(anchor.task, target_classes=("foreign-class",))
    foreign = _proof_for(
        tmp_path / "foreign",
        current.context,
        current.scope,
        proof.mission_binding.mission,
        journal_context=anchor.source.context,
        task=foreign_task,
        runtime=anchor.runtime,
    )
    assert foreign.mission_binding.task.task_id == anchor.task.task_id
    assert foreign.mission_binding.mission.canonical_hash == current.mission_sha256
    with pytest.raises(OnboardContinuationAdmissionError, match="original task"):
        _admit(anchor, current, foreign, policy)


def test_matching_parent_wire_cannot_hide_different_original_authoring_context(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    altered_context = replace(
        anchor.source.context, snapshot_id="different-authoring-context", battery_remaining_pct=79.0
    )
    foreign = _proof_for(
        tmp_path / "changed-context",
        current.context,
        current.scope,
        proof.mission_binding.mission,
        journal_context=altered_context,
        task=anchor.task,
        runtime=anchor.runtime,
    )
    assert foreign.mission_binding.mission.canonical_hash == current.mission_sha256
    with pytest.raises(OnboardContinuationAdmissionError, match="authoring lineage"):
        _admit(anchor, current, foreign, policy)


def test_full_runtime_policy_cannot_change_under_old_original_owner_declaration(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    authoring = _authoring(anchor, current, proof)
    changed_runtime = anchor.runtime
    changed_runtime["safety_policy"]["maximum_relative_altitude_m"] += 50.0
    changed_anchor = replace(anchor, runtime_json=canonical_json(changed_runtime))
    assert changed_anchor.target == anchor.target
    with pytest.raises(ValueError, match="authoring lineage/runtime"):
        _admit(changed_anchor, current, proof, policy, authoring=authoring)


def test_default_original_owner_trust_is_empty_not_inferred_from_vehicle_receiver(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    declaration = replace(_authoring(anchor, current, proof), trusted_source_ids=())
    with pytest.raises(ValueError, match="authoring source"):
        _admit(anchor, current, proof, policy, authoring=declaration)


def test_original_owner_bytes_and_sha_reopened_before_admission(tmp_path):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    declaration = _authoring(anchor, current, proof)
    with pytest.raises(ValueError, match="bytes/SHA"):
        replace(declaration, source_receipt_bytes=declaration.source_receipt_bytes + b" ")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope_sha256", "0" * 64),
        ("parent_mission_sha256", "0" * 64),
        ("parent_wire_sha256", "0" * 64),
        ("journal_context_sha256", "0" * 64),
        ("parent_journal_sha256", "0" * 64),
        ("parent_transaction_id", "foreign-parent-transaction"),
        ("task_sha256", "0" * 64),
        ("runtime_sha256", "0" * 64),
        ("policy_artifact_sha256", "0" * 64),
        ("observed_at_s", 1.99),
        ("observed_at_s", 2.01),
    ],
)
def test_rehashed_original_owner_declaration_cannot_cross_task_or_transaction(
    tmp_path, field, value
):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path)
    declaration = _authoring(anchor, current, proof)
    raw = declaration.to_mapping()
    raw[field] = value
    payload = canonical_json(raw).encode()
    changed = replace(
        declaration,
        source_receipt_bytes=payload,
        source_receipt_sha256=hashlib.sha256(payload).hexdigest(),
    )
    with pytest.raises(ValueError, match="authoring lineage/runtime"):
        _admit(anchor, current, proof, policy, authoring=changed)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"contract": "foreign-contract"},
        {"execution_authority": True},
        {"observed_at_s": float("nan")},
    ],
)
def test_original_owner_strict_schema_rejects_relabelled_or_nonfinite_facts(tmp_path, change):
    anchor, current, proof, _, _ = _admission_setup(tmp_path)
    declaration = _authoring(anchor, current, proof)
    raw = declaration.to_mapping()
    raw.update(change)
    payload = json.dumps(raw, allow_nan=True).encode()
    with pytest.raises(ValueError):
        replace(
            declaration,
            source_receipt_bytes=payload,
            source_receipt_sha256=hashlib.sha256(payload).hexdigest(),
        )

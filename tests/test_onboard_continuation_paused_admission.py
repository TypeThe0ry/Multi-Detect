"""Synthetic software receive fixtures only; no actual pause/flight permission."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_onboard_continuation_admission import _admission_setup, _admit
from test_onboard_continuation_binding import _change, _proof_for
from test_onboard_vlm_admission import _clearance, _current

from multidetect.autonomy.onboard_continuation_admission import OnboardContinuationAdmission
from multidetect.autonomy.onboard_continuation_paused_admission import (
    PX4_AUTO_LOITER_CUSTOM_MODE,
    ContinuationPauseAttempt,
    PausedContinuationAdmissionError,
    admit_paused_continuation,
    begin_pause_attempt,
    prepare_paused_continuation,
)


def _setup(tmp_path, *, policy_changes=None):
    anchor, source_current, original_proof, policy, _ = _admission_setup(tmp_path / "original")
    if policy_changes:
        policy = replace(policy, **policy_changes)
    original = _admit(anchor, source_current, original_proof, policy)
    attempt = begin_pause_attempt(original, attempt_id="fixture-pause-1", generation=1, now_s=2.2)
    current = _current(anchor, north=-6, east=2, at_s=2.4)
    measured = _proof_for(
        tmp_path / "hold-receiver",
        current.context,
        current.scope,
        original_proof.mission_binding.mission,
        journal_context=original_proof.journal_context,
        task=anchor.task,
        runtime=anchor.runtime,
    )
    # The actual original journal is retained, not re-labelled as a new parent.
    measured = replace(measured, mission_binding=original_proof.mission_binding)
    telemetry = list(measured.telemetry)
    telemetry[0] = _change(telemetry[0], message={"custom_mode": PX4_AUTO_LOITER_CUSTOM_MODE})
    measured = replace(measured, telemetry=tuple(telemetry))
    compilation = prepare_paused_continuation(
        attempt,
        current,
        hold_parent_evidence=measured,
        mission_id="fixture-paused-child",
        now_s=2.45,
    )
    current = replace(
        current,
        clearance=_clearance(
            current.context, current.event, anchor.target, wire_hash=compilation.wire_hash
        ),
    )
    return original, attempt, current, measured, compilation


def _accept(attempt, current, proof, *, now_s=2.45):
    return admit_paused_continuation(
        attempt,
        current,
        hold_parent_evidence=proof,
        mission_id="fixture-paused-child",
        now_s=now_s,
    )


def test_actual_post_pause_drift_recompiles_wp0_preserving_original_goal(tmp_path):
    original, attempt, current, proof, expected = _setup(tmp_path)
    source = original.anchor.to_mapping()
    result = _accept(attempt, current, proof)
    result.validate_binding(now_s=2.5)
    wire = result.compilation.compiled_mission.wire_items
    assert result.compilation.wire_hash == expected.wire_hash
    assert result.compilation.wire_hash != original.compilation.wire_hash
    assert [item.command for item in wire] == [16, 16, 19]
    assert (wire[0].x, wire[0].y) == (
        round(current.context.aircraft_position.latitude_deg * 1e7),
        round(current.context.aircraft_position.longitude_deg * 1e7),
    )
    assert (wire[1].x, wire[1].y) == (
        round(original.anchor.target.latitude_deg * 1e7),
        round(original.anchor.target.longitude_deg * 1e7),
    )
    assert result.compilation.target == original.anchor.target
    assert original.anchor.to_mapping() == source
    assert result.compilation.resume_sequence == 1
    mapping = result.to_mapping()
    for flag in (
        "execution_authority",
        "transport_created",
        "watchdog_implemented",
        "actual_flight_verified",
        "pause_command_causal_proof_verified",
        "source_timestamps_rewritten",
        "original_auto_mission_verifier_replayed_for_hold",
    ):
        assert mapping[flag] is False


def test_auto_mission_verifier_is_not_replayed_against_hold(tmp_path, monkeypatch):
    _, attempt, current, proof, _ = _setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary AUTO.MISSION admission replayed after pause")

    monkeypatch.setattr(OnboardContinuationAdmission, "validate_binding", forbidden)
    result = _accept(attempt, current, proof)
    result.validate_binding(now_s=2.5)


@pytest.mark.parametrize("generation", [True, False, -1, 1.0, "1"])
def test_generation_is_strict_nonnegative_non_bool_integer(tmp_path, generation):
    original, _, _, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        begin_pause_attempt(original, attempt_id="fixture", generation=generation, now_s=2.2)


def test_token_cannot_be_created_from_boolean_or_mutated_mapping(tmp_path):
    original, attempt, current, proof, _ = _setup(tmp_path)
    with pytest.raises(TypeError):
        ContinuationPauseAttempt(original, "fake", 1, 2.2)
    mapping = attempt.to_mapping()
    mapping["generation"] = 999
    assert attempt.generation == 1
    with pytest.raises(AttributeError):
        attempt.generation = 2
    _accept(attempt, current, proof).validate_binding(now_s=2.5)


def test_zero_generation_and_public_phase_guard_do_not_grant_auth(tmp_path):
    original, _, current, proof, _ = _setup(tmp_path)
    attempt = begin_pause_attempt(original, attempt_id="zero", generation=0, now_s=2.2)
    attempt.validate_active(generation=0)
    result = _accept(attempt, current, proof)
    attempt.validate_active(generation=0)
    with pytest.raises(ValueError, match="generation"):
        attempt.validate_active(generation=1)
    with pytest.raises(ValueError):
        attempt.validate_active(generation=False)
    attempt.cancel("fixture zero-generation cancel")
    with pytest.raises(ValueError, match="cancelled"):
        attempt.validate_active()
    with pytest.raises(ValueError, match="cancelled"):
        result.validate_binding(now_s=2.5)


def test_original_pre_pause_admission_must_really_validate_before_token(tmp_path):
    original, _, _, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        begin_pause_attempt(original, attempt_id="late", generation=1, now_s=4.0)
    foreign = replace(original, admitted_at_s=2.6)
    with pytest.raises(ValueError):
        begin_pause_attempt(foreign, attempt_id="future", generation=1, now_s=2.2)


def test_only_one_successful_consumer_but_preparation_does_not_consume(tmp_path):
    _, attempt, current, proof, expected = _setup(tmp_path)
    assert (
        prepare_paused_continuation(
            attempt,
            current,
            hold_parent_evidence=proof,
            mission_id="fixture-paused-child",
            now_s=2.45,
        ).canonical_hash
        == expected.canonical_hash
    )
    result = _accept(attempt, current, proof)
    with pytest.raises(PausedContinuationAdmissionError, match="consumed"):
        _accept(attempt, current, proof)
    result.validate_binding(now_s=2.5)


def test_cancel_before_admission_cannot_reactivate(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    attempt.cancel("fixture task superseded")
    attempt.cancel("different late reason")
    with pytest.raises(PausedContinuationAdmissionError, match="cancelled"):
        _accept(attempt, current, proof)
    with pytest.raises(PausedContinuationAdmissionError, match="cancelled"):
        prepare_paused_continuation(
            attempt,
            current,
            hold_parent_evidence=proof,
            mission_id="fixture-paused-child",
            now_s=2.5,
        )


def test_cancel_after_admission_retires_returned_artifact_at_use(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    sha = result.canonical_hash
    attempt.cancel("fixture cancellation after consume")
    assert result.canonical_hash == sha
    with pytest.raises(PausedContinuationAdmissionError, match="cancelled"):
        result.validate_binding(now_s=2.5)
    with pytest.raises(PausedContinuationAdmissionError, match="cancelled"):
        _accept(attempt, current, proof)


def test_cancel_during_validation_wins_before_first_consume(tmp_path, monkeypatch):
    _, attempt, current, proof, _ = _setup(tmp_path)
    original = type(proof)._journal

    def cancel_after_journal(self):
        result = original(self)
        attempt.cancel("fixture cancellation while validating raw evidence")
        return result

    monkeypatch.setattr(type(proof), "_journal", cancel_after_journal)
    with pytest.raises(PausedContinuationAdmissionError, match="cancelled"):
        _accept(attempt, current, proof)


@pytest.mark.parametrize(
    "index,message",
    [
        (0, {"base_mode": 1}),
        (0, {"base_mode": 128}),
        (0, {"custom_mode": (4 << 16) | (4 << 24)}),
        (0, {"custom_mode": 0}),
        (1, {"landed_state": 1}),
        (1, {"landed_state": 0}),
        (2, {"seq": 0}),
        (2, {"seq": 2}),
    ],
)
def test_hold_is_actual_armed_custom_mode_in_air_with_original_cursor(tmp_path, index, message):
    _, attempt, current, proof, _ = _setup(tmp_path)
    telemetry = list(proof.telemetry)
    telemetry[index] = _change(telemetry[index], message=message)
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, telemetry=tuple(telemetry)))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_system_id": 2},
        {"source_component_id": 2},
        {"source_id": "unregistered"},
        {"mission_sha256": "0" * 64},
        {"transaction_id": "foreign"},
        {"clock_epoch": "foreign-clock"},
        {"observed_at_s": 2.19},
        {"observed_at_s": 2.5},
    ],
)
def test_hold_receiver_source_mission_clock_and_post_pause_time_bound(tmp_path, changes):
    _, attempt, current, proof, _ = _setup(tmp_path)
    raw = list(proof.telemetry)
    raw[0] = _change(raw[0], **changes)
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, telemetry=tuple(raw)))


@pytest.mark.parametrize(
    "index,message",
    [
        (0, {"latitude": 130000001}),
        (0, {"altitude": 2002}),
        (1, {"param_id": "OTHER"}),
        (1, {"raw_value_hex": "01000000"}),
        (1, {"param_type": 9}),
        (2, {"relative_alt": 35002}),
        (2, {"lat": 130000000}),
        (2, {"alt": 37002}),
    ],
)
def test_actual_home_parameter_gpi_cannot_be_synthesized_from_context(tmp_path, index, message):
    _, attempt, current, proof, _ = _setup(tmp_path)
    raw = list(proof.navigation_facts)
    raw[index] = _change(raw[index], message=message)
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, navigation_facts=tuple(raw)))


@pytest.mark.parametrize(
    "changes",
    [
        {"scope_sha256": "0" * 64},
        {"session_id": "foreign-session"},
        {"aircraft_id": "foreign-aircraft"},
        {"vehicle_identity_sha256": "0" * 64},
        {"source_system_id": 2},
        {"source_component_id": 2},
        {"source_id": "foreign"},
        {"transaction_id": "foreign"},
        {"mission_sha256": "0" * 64},
        {"clock_epoch": "foreign"},
        {"observed_at_s": 2.399},
    ],
)
def test_actual_position_receipt_exact_scope_epoch_identity_and_context_time(tmp_path, changes):
    _, attempt, current, proof, _ = _setup(tmp_path)
    raw = list(proof.navigation_facts)
    raw[-1] = _change(raw[-1], **changes)
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, navigation_facts=tuple(raw)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("mission_revision", 100),
        ("receipt_revision", 100),
        ("mission_sha256", "0" * 64),
        ("policy_artifact_sha256", "0" * 64),
    ],
)
def test_hold_current_head_must_keep_source_mission_receipt_and_policy(tmp_path, field, value):
    _, attempt, current, proof, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        _accept(attempt, replace(current, **{field: value}), proof)


@pytest.mark.parametrize(
    "changes",
    [
        {"gps_fix_type": 2},
        {"gps_horizontal_accuracy_m": 99.0},
        {"geofence_valid": False},
        {"camera_available": False},
        {"perception_available": False},
        {"dem_available": False},
    ],
)
def test_actual_hold_mode_does_not_override_current_navigation_safety(tmp_path, changes):
    _, attempt, current, proof, _ = _setup(tmp_path)
    context = replace(current.context, **changes)
    with pytest.raises(ValueError):
        _accept(attempt, replace(current, context=context), replace(proof, current_context=context))


def test_old_current_geometry_cannot_hide_actual_post_pause_position(tmp_path):
    original, attempt, current, proof, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        _accept(attempt, original.current, replace(proof, current_context=original.current.context))


def test_journal_bytes_not_only_in_air_receipts_reopened_at_use(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    path = proof.mission_binding.journal_path
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="journal"):
        result.validate_binding(now_s=2.5)


def test_no_new_trusted_source_can_be_granted_by_hold_container(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        _accept(
            attempt, current, replace(proof, trusted_source_ids=(*proof.trusted_source_ids, "new"))
        )
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, trusted_source_ids=()))


@pytest.mark.parametrize("kind", ["missing", "old-wire", "short", "wrong-context", "old-time"])
def test_clearance_must_cover_exact_current_wire_not_original_plan(tmp_path, kind):
    original, attempt, current, proof, _ = _setup(tmp_path)
    if kind == "missing":
        clearance = None
    elif kind == "old-wire":
        clearance = _clearance(
            current.context,
            current.event,
            original.anchor.target,
            wire_hash=original.compilation.wire_hash,
        )
    else:
        changes = {
            "short": {"verified_path_length_m": 1.0},
            "wrong-context": {"context_sha256": "0" * 64},
            "old-time": {"observed_at_s": 2.19},
        }[kind]
        clearance = _clearance(
            current.context,
            current.event,
            original.anchor.target,
            wire_hash=current.clearance.wire_hash,
            **changes,
        )
    with pytest.raises(ValueError):
        _accept(attempt, replace(current, clearance=clearance), proof)


def test_failed_unknown_clearance_does_not_consume_valid_token(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        _accept(attempt, replace(current, clearance=None), proof)
    _accept(attempt, current, proof).validate_binding(now_s=2.5)


@pytest.mark.parametrize("now_s", [2.44, 4.0, float("nan"), float("inf")])
def test_use_time_regression_staleness_nonfinite_reject(tmp_path, now_s):
    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    with pytest.raises(ValueError):
        result.validate_binding(now_s=now_s)


def test_original_source_age_is_not_extended_by_new_hold_receipts(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path, policy_changes={"max_source_age_s": 0.8})
    result = _accept(attempt, current, proof)
    with pytest.raises(ValueError, match="stale"):
        result.validate_binding(now_s=2.9)


def test_forged_returned_admission_is_not_the_single_consumed_artifact(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    foreign = replace(result, admitted_at_s=2.46)
    with pytest.raises(ValueError, match="foreign"):
        foreign.validate_binding(now_s=2.5)


def test_token_private_identity_tamper_is_rejected_not_relabelled(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    attempt._generation = 2
    with pytest.raises(ValueError, match="identity"):
        _accept(attempt, current, proof)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bound_at_s", 2.05),
        ("clock_epoch", "new-epoch"),
        ("journal_sha256", "0" * 64),
        ("vehicle_identity_sha256", "0" * 64),
    ],
)
def test_original_parent_binding_not_silently_replaced_by_pause_receipt(tmp_path, field, value):
    _, attempt, current, proof, _ = _setup(tmp_path)
    foreign = replace(proof, mission_binding=replace(proof.mission_binding, **{field: value}))
    with pytest.raises(ValueError, match="original parent"):
        _accept(attempt, current, foreign)


def test_changed_epoch_cannot_use_original_pause_token(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    scope = replace(current.scope, clock_epoch="new-clock-epoch")
    with pytest.raises(ValueError):
        _accept(attempt, replace(current, scope=scope), replace(proof, scope=scope))


def test_duplicate_or_out_of_order_hold_receive_facts_reject(tmp_path):
    _, attempt, current, proof, _ = _setup(tmp_path)
    telemetry = list(proof.telemetry)
    telemetry[-1] = _change(telemetry[-1], message_id=telemetry[0].to_mapping()["message_id"])
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, telemetry=tuple(telemetry)))
    telemetry = list(proof.telemetry)
    telemetry[1] = _change(telemetry[1], observed_at_s=2.369)
    with pytest.raises(ValueError):
        _accept(attempt, current, replace(proof, telemetry=tuple(telemetry)))


def test_use_time_clearance_expiry_does_not_hide_behind_fresh_context(tmp_path):
    original, attempt, current, proof, _ = _setup(
        tmp_path, policy_changes={"max_current_age_s": 0.3}
    )
    clearance = _clearance(
        current.context,
        current.event,
        original.anchor.target,
        wire_hash=current.clearance.wire_hash,
        observed_at_s=2.24,
    )
    result = _accept(attempt, replace(current, clearance=clearance), proof)
    with pytest.raises(ValueError, match="clearance"):
        result.validate_binding(now_s=2.55)


def test_cancel_during_use_time_recompute_is_checked_again_before_return(tmp_path, monkeypatch):
    import multidetect.autonomy.onboard_continuation_paused_admission as module

    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    original = module._prepare

    def cancellation_cliff(*args, **kwargs):
        compilation = original(*args, **kwargs)
        attempt.cancel("fixture use-time final cancellation cliff")
        return compilation

    monkeypatch.setattr(module, "_prepare", cancellation_cliff)
    with pytest.raises(ValueError, match="cancelled"):
        result.validate_binding(now_s=2.5)


@pytest.mark.parametrize("field", ["current", "hold_parent_evidence", "compilation"])
def test_paused_artifact_rejects_untyped_aliases(tmp_path, field):
    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    with pytest.raises(TypeError):
        replace(result, **{field: None})


def test_state_only_helper_does_not_reopen_original_but_full_active_still_does(
    tmp_path, monkeypatch
):
    _, attempt, _, _, _ = _setup(tmp_path)

    def forbidden(self):
        raise AssertionError("original canonical property reopened")

    monkeypatch.setattr(OnboardContinuationAdmission, "canonical_hash", property(forbidden))
    attempt.validate_active_state(generation=1)
    with pytest.raises(AssertionError, match="original canonical"):
        attempt.validate_active(generation=1)


def test_state_only_consumed_helper_requires_exact_independent_artifact_marker(
    tmp_path, monkeypatch
):
    _, attempt, current, proof, _ = _setup(tmp_path)
    result = _accept(attempt, current, proof)
    admitted_sha = result.canonical_hash

    def forbidden(self):
        raise AssertionError("original canonical property reopened")

    monkeypatch.setattr(OnboardContinuationAdmission, "canonical_hash", property(forbidden))
    attempt.validate_active_state(generation=1, consumed_sha256=admitted_sha)
    with pytest.raises(ValueError, match="consumed"):
        attempt.validate_active_state(generation=1)
    with pytest.raises(ValueError, match="foreign"):
        attempt.validate_active_state(generation=1, consumed_sha256="0" * 64)
    attempt._consumed_sha256 = "1" * 64
    with pytest.raises(ValueError, match="foreign"):
        attempt.validate_active_state(generation=1, consumed_sha256=admitted_sha)


def test_state_only_helper_is_not_original_journal_validation(tmp_path):
    _, attempt, _, proof, _ = _setup(tmp_path)
    path = proof.mission_binding.journal_path
    path.write_bytes(path.read_bytes() + b"\n")
    # This helper intentionally has no journal/flight-authority semantics.
    attempt.validate_active_state(generation=1)
    with pytest.raises(ValueError, match="journal"):
        attempt.validate_active(generation=1)


@pytest.mark.parametrize("generation", [True, False, -1, 1.0, "1", 2])
def test_state_only_helper_rejects_noninteger_or_different_generation(tmp_path, generation):
    _, attempt, _, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        attempt.validate_active_state(generation=generation)


@pytest.mark.parametrize(
    "field,value",
    [
        ("_generation", True),
        ("_generation", 2),
        ("_validated_at_s", 2.21),
        ("_validated_at_s", float("nan")),
        ("_original_sha256", "0" * 64),
        ("_identity_sha256", "0" * 64),
    ],
)
def test_state_only_helper_rejects_token_identity_drift(tmp_path, field, value):
    _, attempt, _, _, _ = _setup(tmp_path)
    setattr(attempt, field, value)
    with pytest.raises(ValueError):
        attempt.validate_active_state()


@pytest.mark.parametrize("consumed_sha256", [True, 1, "invalid", " " + "0" * 64, "A" * 64])
def test_state_only_helper_rejects_noncanonical_consumption_marker(tmp_path, consumed_sha256):
    _, attempt, _, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        attempt.validate_active_state(consumed_sha256=consumed_sha256)


@pytest.mark.parametrize("consumed", [False, True])
def test_state_only_cancelled_token_cannot_reactivate_even_with_correct_marker(tmp_path, consumed):
    _, attempt, current, proof, _ = _setup(tmp_path)
    marker = _accept(attempt, current, proof).canonical_hash if consumed else None
    attempt.validate_active_state(generation=1, consumed_sha256=marker)
    attempt.cancel("fixture token-state cancellation")
    with pytest.raises(ValueError, match="cancelled"):
        attempt.validate_active_state(generation=1, consumed_sha256=marker)

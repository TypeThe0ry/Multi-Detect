"""Typed software fixtures for at-use host guards, not aircraft acceptance.

The explicit attachment below represents the host's already-admitted phase;
no wire, operator grant, model inference, or vehicle proof is manufactured.
All fixture receipt times are chosen before admission, never refreshed at use.
Ordered facts have overlapping expiry gates: old CURSOR also expires the
compile parent, and old GPI also expires the current ContextSnapshot.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_onboard_continuation_admission import _admission_setup, _authoring
from test_onboard_continuation_binding import _change, _proof_for
from test_onboard_vlm_admission import _clearance, _current

from multidetect.autonomy.onboard_continuation import ContinuationCompilePolicy
from multidetect.autonomy.onboard_continuation_admission import admit_anchored_continuation
from multidetect.autonomy.onboard_continuation_host import (
    ContinuationHostError,
    OriginalParentContinuationHost,
)
from multidetect.autonomy.onboard_continuation_paused_admission import (
    PX4_AUTO_LOITER_CUSTOM_MODE,
    admit_paused_continuation,
    prepare_paused_continuation,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import parse_onboard_vlm_runtime_response

FACT_NAMES = (
    "HEARTBEAT",
    "EXTENDED_SYS_STATE",
    "MISSION_CURRENT",
    "HOME_POSITION",
    "PARAM_VALUE",
    "GLOBAL_POSITION_INT",
)


class _Clock:
    """Controllable monotonic fixture clock; never a sensor measurement."""

    def __init__(self, at_s):
        self.at_s = at_s
        self.jump_after_next_read = None

    def __call__(self):
        now = self.at_s
        if self.jump_after_next_read is not None:
            self.at_s, self.jump_after_next_read = self.jump_after_next_read, None
        return now


def _guard_fixture(
    tmp_path,
    *,
    current_at_s=3.1,
    admitted_at_s=3.2,
    telemetry_times=(2.9, 3.0, 3.08),
    navigation_times=None,
    compile_policy=None,
):
    anchor, source_current, original_proof, policy, _ = _admission_setup(tmp_path / "root")
    # A separate legitimate fixture request has a longer deadline. Original
    # source capture/completion times and the raw model output remain unchanged.
    request = replace(
        anchor.request,
        model_input=replace(anchor.request.model_input, deadline_at_s=6.0),
    )
    result = replace(
        anchor.result,
        request_sha256=request.canonical_hash,
        response=parse_onboard_vlm_runtime_response(
            anchor.result.raw_output,
            request,
            current_scope=request.model_input.scope,
            completed_at_s=anchor.result.completed_at_s,
            inference_latency_ms=anchor.result.inference_latency_ms,
        ),
    )
    anchor = replace(anchor, request=request, result=result)
    original = admit_anchored_continuation(
        anchor,
        source_current,
        parent_evidence=original_proof,
        authoring_evidence=_authoring(anchor, source_current, original_proof),
        mission_id="fixture-child",
        now_s=2.2,
        policy=policy,
        compile_policy=compile_policy,
    )
    clock = _Clock(2.2)
    gate_calls = []

    def current_gate(phase, generation):
        gate_calls.append((phase, generation))
        return True  # Explicit software-only current gate, not physical proof.

    host = OriginalParentContinuationHost(
        original,
        attempt_id="fixture-cached-host-phase",
        generation=0,
        host_clock=clock,
        current_gate=current_gate,
    )
    current = _current(anchor, north=-6, east=2, at_s=current_at_s)
    proof = _proof_for(
        tmp_path / "hold",
        current.context,
        current.scope,
        original_proof.mission_binding.mission,
        journal_context=original_proof.journal_context,
        task=anchor.task,
        runtime=anchor.runtime,
    )
    telemetry = tuple(
        _change(
            fact,
            observed_at_s=stamp,
            **({"message": {"custom_mode": PX4_AUTO_LOITER_CUSTOM_MODE}} if index == 0 else {}),
        )
        for index, (fact, stamp) in enumerate(zip(proof.telemetry, telemetry_times, strict=True))
    )
    navigation_times = navigation_times or (2.95, 3.0, current_at_s)
    navigation = tuple(
        _change(fact, observed_at_s=stamp)
        for fact, stamp in zip(proof.navigation_facts, navigation_times, strict=True)
    )
    proof = replace(
        proof,
        mission_binding=original_proof.mission_binding,
        telemetry=telemetry,
        navigation_facts=navigation,
    )
    compilation = prepare_paused_continuation(
        host.attempt,
        current,
        hold_parent_evidence=proof,
        mission_id="fixture-postpause-child",
        now_s=admitted_at_s,
    )
    current = replace(
        current,
        clearance=_clearance(
            current.context, current.event, anchor.target, wire_hash=compilation.wire_hash
        ),
    )
    paused = admit_paused_continuation(
        host.attempt,
        current,
        hold_parent_evidence=proof,
        mission_id="fixture-postpause-child",
        now_s=admitted_at_s,
    )
    # Exactly the typed state installed by execute after admit_after_pause.
    # Avoid a network emulator when isolating this pure at-use guard behavior.
    host._started = True
    clock.at_s = admitted_at_s
    with host._lock:
        host._bind_paused(paused)
    assert host.phase_guard("install", 0) is True
    assert host._full_validation_phase == "install"
    return host, paused, clock, gate_calls


def _prepause_fixture(tmp_path, *, compile_policy=None, max_source_age_s=None):
    anchor, current, proof, policy, _ = _admission_setup(tmp_path / "prepause")
    if max_source_age_s is not None:
        policy = replace(policy, max_source_age_s=max_source_age_s)
    original = admit_anchored_continuation(
        anchor,
        current,
        parent_evidence=proof,
        authoring_evidence=_authoring(anchor, current, proof),
        mission_id="fixture-prepause-child",
        now_s=2.2,
        policy=policy,
        compile_policy=compile_policy,
    )
    clock = _Clock(2.2)
    host = OriginalParentContinuationHost(
        original,
        attempt_id="fixture-prepause-attached-owner",
        generation=0,
        host_clock=clock,
        current_gate=lambda phase, generation: True,
    )
    host._started = True  # Attached owner, without inventing a wire result.
    assert host.phase_guard("pause", 0) is True
    return host, clock


def test_same_phase_baseline_stays_admitted_without_recompilation_or_retiming(
    tmp_path, monkeypatch
):
    host, paused, clock, gate_calls = _guard_fixture(tmp_path)
    identity = paused.to_mapping()

    def forbidden(*args, **kwargs):
        raise AssertionError("same-phase fast guard recompiles the full immutable admission")

    monkeypatch.setattr(type(paused), "validate_binding", forbidden)
    clock.at_s = 3.25
    assert host.phase_guard("install", 0) is True
    assert gate_calls == [("install", 0), ("install", 0)]
    assert paused.to_mapping() == identity


@pytest.mark.parametrize("kind", FACT_NAMES)
def test_cached_phase_rejects_expired_original_hold_fact(tmp_path, kind):
    telemetry = [2.9, 3.0, 3.08]
    navigation = [2.95, 3.0, 3.1]
    kwargs = {}
    if kind in FACT_NAMES[:3]:
        index = FACT_NAMES.index(kind)
        telemetry[: index + 1] = [2.2] * (index + 1)
    elif kind != "GLOBAL_POSITION_INT":
        index = FACT_NAMES.index(kind) - 3
        navigation[: index + 1] = [2.2] * (index + 1)
    else:
        # GPI is the actual current timestamp, not an independent old sample.
        telemetry = [2.2, 2.205, 2.208]
        navigation = [2.2, 2.205, 2.21]
        kwargs = {"current_at_s": 2.21, "admitted_at_s": 2.3}
    host, paused, clock, gate_calls = _guard_fixture(
        tmp_path,
        telemetry_times=tuple(telemetry),
        navigation_times=tuple(navigation),
        **kwargs,
    )
    before = paused.to_mapping()
    target_fact = next(
        fact
        for fact in (
            *paused.hold_parent_evidence.telemetry,
            *paused.hold_parent_evidence.navigation_facts,
        )
        if fact.to_mapping()["message"]["message_type"] == kind
    )
    clock.at_s = target_fact.to_mapping()["observed_at_s"] + 1.51
    # The frozen full validator agrees with the at-use guard. For CURSOR/GPI,
    # canonical parent/context expiry can legitimately reject even earlier.
    with pytest.raises(ValueError):
        paused.validate_binding(now_s=clock.at_s)
    with pytest.raises((ContinuationHostError, ValueError)):
        host.phase_guard("install", 0)
    assert all(call == ("install", 0) for call in gate_calls)
    assert paused.to_mapping() == before


@pytest.mark.parametrize("kind", FACT_NAMES)
def test_cached_phase_cannot_rewind_clock_below_any_original_hold_fact(tmp_path, kind):
    host, paused, clock, gate_calls = _guard_fixture(tmp_path)
    stamp = next(
        fact.to_mapping()["observed_at_s"]
        for fact in (
            *paused.hold_parent_evidence.telemetry,
            *paused.hold_parent_evidence.navigation_facts,
        )
        if fact.to_mapping()["message"]["message_type"] == kind
    )
    before = paused.to_mapping()
    # Immutable admitted facts cannot become future on a monotonic clock. A
    # real host clock rewind must reject, rather than re-label their times.
    clock.at_s = stamp - 0.01
    with pytest.raises(ValueError):
        host.phase_guard("install", 0)
    assert gate_calls == [("install", 0)]
    assert paused.to_mapping() == before


@pytest.mark.parametrize("kind", FACT_NAMES)
def test_future_original_hold_fact_cannot_establish_a_cached_admission(tmp_path, kind):
    telemetry = [2.9, 3.0, 3.08]
    navigation = [2.95, 3.0, 3.1]
    index = FACT_NAMES.index(kind)
    if index < 3:
        telemetry[index] = 3.21
    else:
        navigation[index - 3] = 3.21
    with pytest.raises(ValueError, match="future"):
        _guard_fixture(
            tmp_path,
            telemetry_times=tuple(telemetry),
            navigation_times=tuple(navigation),
        )


@pytest.mark.parametrize(
    "field,limit,expired_at_s",
    [
        ("maximum_context_age_s", 0.25, 3.36),
        ("maximum_parent_age_s", 0.2, 3.29),
    ],
)
def test_cached_phase_preserves_stricter_original_compile_age_policy(
    tmp_path, field, limit, expired_at_s
):
    policy = replace(ContinuationCompilePolicy(), **{field: limit})
    host, paused, clock, gate_calls = _guard_fixture(tmp_path, compile_policy=policy)
    identity = paused.to_mapping()
    clock.at_s = expired_at_s
    assert clock.at_s - paused.current.context.captured_at_s < 1.0
    assert clock.at_s - paused.current.clearance.observed_at_s < 1.0
    assert all(
        clock.at_s - fact.to_mapping()["observed_at_s"] < 1.5
        for fact in (
            *paused.hold_parent_evidence.telemetry,
            *paused.hold_parent_evidence.navigation_facts,
        )
    )
    with pytest.raises(ValueError):
        paused.validate_binding(now_s=clock.at_s)
    with pytest.raises(ContinuationHostError):
        host.phase_guard("install", 0)
    assert all(call == ("install", 0) for call in gate_calls)
    assert paused.to_mapping() == identity


def test_cached_phase_rejects_default_parent_age_while_all_raw_facts_are_fresh(tmp_path):
    host, paused, clock, gate_calls = _guard_fixture(tmp_path, telemetry_times=(2.55, 2.58, 2.6))
    identity = paused.to_mapping()
    clock.at_s = 3.61
    assert clock.at_s - paused.current.context.captured_at_s < 1.0
    assert clock.at_s - paused.current.clearance.observed_at_s < 1.0
    assert clock.at_s - paused.compilation.parent.observed_at_s > 1.0
    assert all(
        clock.at_s - fact.to_mapping()["observed_at_s"] < 1.5
        for fact in (
            *paused.hold_parent_evidence.telemetry,
            *paused.hold_parent_evidence.navigation_facts,
        )
    )
    with pytest.raises(ValueError):
        paused.validate_binding(now_s=clock.at_s)
    with pytest.raises(ContinuationHostError):
        host.phase_guard("install", 0)
    assert all(call == ("install", 0) for call in gate_calls)
    assert paused.to_mapping() == identity


def test_guard_resamples_clock_after_hashing_and_full_validation_work(tmp_path):
    host, paused, clock, gate_calls = _guard_fixture(tmp_path, telemetry_times=(2.2, 3.0, 3.08))
    identity = paused.to_mapping()
    clock.jump_after_next_read = 3.71
    with pytest.raises(ContinuationHostError, match="HOLD facts"):
        host.phase_guard("install", 0)
    assert all(call == ("install", 0) for call in gate_calls)
    assert paused.to_mapping() == identity


def test_wrong_generation_does_not_consume_or_retire_the_valid_generation(tmp_path):
    host, paused, clock, gate_calls = _guard_fixture(tmp_path)
    identity = paused.to_mapping()
    clock.at_s = 3.25
    assert host.phase_guard("install", 1) is False
    assert gate_calls == [("install", 0)]
    assert host.phase_guard("install", 0) is True
    host.attempt.validate_active(generation=0)
    assert paused.to_mapping() == identity


def test_cancelled_cached_phase_cannot_reactivate_with_fresh_facts(tmp_path):
    host, paused, clock, gate_calls = _guard_fixture(tmp_path)
    identity = paused.to_mapping()
    host.cancel("fixture cancellation after legitimate HOLD admission")
    clock.at_s = 3.25
    assert host.phase_guard("install", 0) is False
    assert host.phase_guard("resume", 0) is False
    with pytest.raises(ValueError, match="cancelled"):
        paused.validate_binding(now_s=clock.at_s)
    assert gate_calls == [("install", 0)]
    assert paused.to_mapping() == identity


@pytest.mark.parametrize(
    "kind", ["original-runtime", "current-context", "raw-receipt", "admission"]
)
def test_cached_memory_identity_rejects_frozen_field_drift_before_reuse(tmp_path, kind):
    host, paused, clock, _ = _guard_fixture(tmp_path)
    if kind == "original-runtime":
        anchor = host.attempt.original.anchor
        object.__setattr__(anchor, "runtime_json", anchor.runtime_json + "\n")
    elif kind == "current-context":
        context = paused.current.context
        object.__setattr__(context, "captured_at_s", context.captured_at_s + 0.01)
    elif kind == "raw-receipt":
        proof = paused.hold_parent_evidence
        telemetry = (
            _change(proof.telemetry[0], message_id="fixture-rehashed-receipt-drift"),
            *proof.telemetry[1:],
        )
        object.__setattr__(proof, "telemetry", telemetry)
    else:
        object.__setattr__(paused, "admitted_at_s", paused.admitted_at_s + 0.01)
    clock.at_s = 3.25
    with pytest.raises(ContinuationHostError, match="changed"):
        host.phase_guard("install", 0)


def test_cached_phase_reopens_exact_parent_journal_bytes_without_recompiling(tmp_path, monkeypatch):
    host, paused, clock, _ = _guard_fixture(tmp_path)
    original_journal = host.attempt.original.parent_evidence.mission_binding.journal_path
    original_journal.write_bytes(original_journal.read_bytes() + b"\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("same-phase guard tried deep recompile rather than exact journal hash")

    monkeypatch.setattr(type(paused), "validate_binding", forbidden)
    clock.at_s = 3.25
    with pytest.raises(ContinuationHostError, match="journal bytes"):
        host.phase_guard("install", 0)


def test_prepaused_baseline_is_still_available_on_its_original_clock(tmp_path):
    host, clock = _prepause_fixture(tmp_path)
    source_identity = host.attempt.original.to_mapping()
    clock.at_s = 2.21
    assert host.phase_guard("ready", 0) is True
    assert host.phase_guard("pause", 0) is True
    assert host.attempt.original.to_mapping() == source_identity


@pytest.mark.parametrize("kind", ["source-age", "deadline", "compile-context", "compile-parent"])
def test_prepaused_guard_rejects_original_expiry_without_postpause_refresh(tmp_path, kind):
    changes = {}
    if kind == "source-age":
        changes = {"max_source_age_s": 0.25}
        expired_at_s = 2.26
    elif kind == "deadline":
        expired_at_s = 3.01
    else:
        field = "maximum_context_age_s" if kind == "compile-context" else "maximum_parent_age_s"
        changes = {"compile_policy": replace(ContinuationCompilePolicy(), **{field: 0.2})}
        expired_at_s = 2.31 if kind == "compile-context" else 2.3
    host, clock = _prepause_fixture(tmp_path, **changes)
    identity = host.attempt.original.to_mapping()
    clock.at_s = expired_at_s
    with pytest.raises((ContinuationHostError, ValueError)):
        host.phase_guard("pause", 0)
    assert host.attempt.original.to_mapping() == identity


@pytest.mark.parametrize("postpaused", [False, True])
def test_current_gate_callback_cancellation_wins_even_if_callback_returns_true(
    tmp_path, postpaused
):
    if postpaused:
        host, _, clock, _ = _guard_fixture(tmp_path)
        phase = "install"
        clock.at_s = 3.25
    else:
        host, clock = _prepause_fixture(tmp_path)
        phase = "pause"
        clock.at_s = 2.21

    def cancel_in_callback(actual_phase, generation):
        assert actual_phase == phase and generation == 0
        host.cancel("fixture cancellation in current gate callback")
        return True

    host.current_gate = cancel_in_callback
    with pytest.raises(ValueError, match="cancelled"):
        host.phase_guard(phase, 0)
    assert host.phase_guard(phase, 0) is False
    with pytest.raises(ValueError, match="cancelled"):
        host.attempt.validate_active_state(generation=0, consumed_sha256=host._paused_sha256)


def test_prepaused_guard_resamples_after_current_gate_work_crosses_source_budget(tmp_path):
    host, clock = _prepause_fixture(tmp_path, max_source_age_s=0.25)

    def slow_current_gate(phase, generation):
        clock.at_s = 2.26
        return True

    host.current_gate = slow_current_gate
    with pytest.raises(ContinuationHostError, match="pre-pause"):
        host.phase_guard("pause", 0)

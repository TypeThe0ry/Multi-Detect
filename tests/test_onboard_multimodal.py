from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from io import BytesIO

import pytest
from test_onboard_agent import _context

from multidetect.autonomy.contracts import PromptEnvelope
from multidetect.autonomy.onboard_agent import OnboardResponse
from multidetect.autonomy.onboard_history import OnboardInferenceScope
from multidetect.autonomy.onboard_multimodal import (
    INTENT_CONTRACT,
    RECEIPT_CONTRACT,
    MissionEvidenceLedger,
    OnboardEvidenceReceipt,
    OnboardImageFrame,
    OnboardMultimodalInput,
    OnboardMultimodalScope,
    parse_onboard_multimodal_intent,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prompt() -> PromptEnvelope:
    return PromptEnvelope(
        "prompt-1",
        "观察目标；没有足够证据时不要声称完成。",
        "zh",
        "2026-10-07T00:00:00Z",
        "fixture-operator",
        "aircraft-1",
    )


def _scope() -> OnboardMultimodalScope:
    return OnboardMultimodalScope(
        "11111111-1111-4111-8111-111111111111",
        _prompt().canonical_hash,
        "aircraft-1",
        "task-1",
        "declared-fixture-clock-not-gazebo",
    )


def _frame(at: float = 10.0, *, scope=None, frame_id=None) -> OnboardImageFrame:
    pixels = b"\x01\x02\x03\x04\x05\x06"
    return OnboardImageFrame(
        scope or _scope(),
        frame_id or f"frame-{at}",
        at,
        pixels,
        _sha(pixels),
        2,
        1,
    )


def _input(*, frames=None, ledger=None, **kwargs) -> OnboardMultimodalInput:
    frames = (_frame(9.0), _frame()) if frames is None else frames
    ledger = MissionEvidenceLedger(_scope(), frames=frames) if ledger is None else ledger
    return OnboardMultimodalInput(
        _scope(),
        _prompt(),
        frames,
        ledger,
        10.0,
        10.5,
        **kwargs,
    )


def _raw(model_input=None) -> dict:
    model_input = _input() if model_input is None else model_input
    return {
        "schema_version": 1,
        "contract": INTENT_CONTRACT,
        "input_sha256": model_input.canonical_hash,
        "scope_sha256": model_input.scope.canonical_hash,
        "frame_ids": [model_input.frames[-1].frame_id],
        "evidence_ids": [],
        "output": {"response": "hold", "reason": "fixture proposal", "confidence": 0.5},
    }


def _parse(raw=None, model_input=None, **kwargs):
    model_input = _input() if model_input is None else model_input
    raw = _raw(model_input) if raw is None else raw
    return parse_onboard_multimodal_intent(
        json.dumps(raw),
        model_input,
        completed_at_s=kwargs.pop("completed_at_s", 10.05),
        inference_latency_ms=kwargs.pop("inference_latency_ms", 40.0),
        expected_scope=kwargs.pop("expected_scope", model_input.scope),
        **kwargs,
    )


def _receipt(stage="proposed", *, prior=None, at=10.0, source=None, proposal="proposal-1"):
    scope = _scope()
    source = source or ("model-fixture" if stage == "proposed" else "verified-executor-fixture")
    evidence_id = f"evidence-{stage}"
    source_bytes = json.dumps({"fixture_only": True, "native_stage": stage}).encode()
    raw = {
        "schema_version": 1,
        "contract": RECEIPT_CONTRACT,
        "evidence_id": evidence_id,
        "scope_sha256": scope.canonical_hash,
        "stage": stage,
        "observed_at_s": at,
        "source_id": source,
        "proposal_id": proposal,
        "prior_evidence_id": prior,
        "frame_ids": ["frame-10.0"],
        "authority": "model_proposal" if stage == "proposed" else "executor_receipt",
        "source_receipt_sha256": _sha(source_bytes),
        "success": True,
    }
    payload = json.dumps(raw).encode()
    return OnboardEvidenceReceipt(
        evidence_id,
        scope,
        stage,
        at,
        source,
        payload,
        _sha(payload),
        proposal,
        source_bytes,
        prior,
        ("frame-10.0",),
    )


def _change_receipt(receipt, **updates):
    raw = json.loads(receipt.receipt_bytes)
    raw.update(updates)
    payload = json.dumps(raw).encode()
    return replace(receipt, receipt_bytes=payload, receipt_sha256=_sha(payload))


def test_actual_bytes_prompt_and_unknown_context_are_bound_without_defaults():
    model_input = _input(prompt_text_original="  " + _prompt().text + "\n")
    value = model_input.to_mapping()
    assert value["prompt_original_text"] == "  " + _prompt().text + "\n"
    assert value["prompt_envelope_sha256"] == _prompt().canonical_hash
    assert value["prompt_text_sha256"] == _sha(value["prompt_original_text"].encode())
    assert value["frames"][-1]["context"] is None
    assert value["frames"][-1]["context_status"] == "unknown"
    assert "features" not in value and "aircraft_position" not in value
    assert value["training_ready"] is False and value["execution_authority"] is False
    changed = replace(model_input.frames[-1], image_bytes=b"abcdef", image_sha256=_sha(b"abcdef"))
    changed_frames = (*model_input.frames[:-1], changed)
    assert _input(frames=changed_frames).canonical_hash != _input().canonical_hash
    with pytest.raises(FrozenInstanceError):
        model_input.frames[-1].frame_id = "mutated"


@pytest.mark.parametrize(
    "changes",
    [
        {"image_sha256": "a" * 64},
        {"image_bytes": bytearray(b"123456")},
        {"image_bytes": b""},
        {"width": 1},
        {"height": True},
        {"encoding": "bgr8"},
        {"captured_at_s": float("nan")},
        {"frame_id": " frame"},
    ],
)
def test_frame_invalid_payload_or_metadata_fails_closed(changes):
    with pytest.raises((ValueError, TypeError)):
        replace(_frame(), **changes)


@pytest.mark.parametrize("encoding", ["png", "jpeg"])
def test_encoded_rgb_image_is_really_decoded_and_hash_bound(encoding):
    image = pytest.importorskip("PIL.Image")
    target = BytesIO()
    image.new("RGB", (2, 1), (10, 20, 30)).save(target, format=encoding.upper())
    payload = target.getvalue()
    frame = replace(_frame(), image_bytes=payload, image_sha256=_sha(payload), encoding=encoding)
    assert frame.image_bytes == payload
    for changes in ({"width": 1}, {"encoding": "jpeg" if encoding == "png" else "png"}):
        with pytest.raises(ValueError):
            replace(frame, **changes)
    corrupt = payload[:12]
    with pytest.raises(ValueError):
        replace(frame, image_bytes=corrupt, image_sha256=_sha(corrupt))


def test_grayscale_or_animated_image_does_not_pass_as_rgb():
    image = pytest.importorskip("PIL.Image")
    target = BytesIO()
    image.new("L", (2, 1)).save(target, format="PNG")
    payload = target.getvalue()
    with pytest.raises(ValueError):
        replace(_frame(), image_bytes=payload, image_sha256=_sha(payload), encoding="png")


def test_existing_inference_scope_and_exact_context_can_be_reused():
    context = _context()
    scope = OnboardInferenceScope(
        _scope().session_id,
        _prompt().canonical_hash,
        "aircraft-1",
        "task-1",
        context.airframe_profile_id,
        context.airframe_profile_hash,
        "multirotor",
        context.home_position.canonical_hash,
        _scope().clock_epoch,
    )
    frame = replace(_frame(), scope=scope, context=context)
    model_input = OnboardMultimodalInput(
        scope,
        _prompt(),
        (frame,),
        MissionEvidenceLedger(scope, (frame,)),
        10.0,
        10.5,
    )
    assert _parse(model_input=model_input).output.response is OnboardResponse.HOLD
    equivalent = OnboardMultimodalScope.from_inference_scope(scope)
    replace(frame, scope=equivalent)  # Matching known home/profile; no fabricated values.
    with pytest.raises(ValueError):
        replace(frame, context=_context(10.01))
    with pytest.raises(ValueError):
        replace(frame, scope=_scope())  # Known context cannot bind to unknown home/profile.
    with pytest.raises(ValueError):
        replace(frame, context=replace(context, airframe_profile_hash="a" * 64))


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "22222222-2222-4222-8222-222222222222"),
        ("prompt_sha256", "a" * 64),
        ("aircraft_id", "aircraft-2"),
        ("selected_task_id", "task-2"),
        ("clock_epoch", "different-clock"),
        ("home_sha256", "b" * 64),
        ("vehicle_class", "vtol"),
    ],
)
def test_history_and_output_cannot_cross_any_scope_boundary(field, value):
    changed = replace(_scope(), **{field: value})
    with pytest.raises(ValueError):
        MissionEvidenceLedger(_scope(), (_frame(scope=changed),))
    with pytest.raises(ValueError):
        _parse(expected_scope=changed)


@pytest.mark.parametrize(
    "frames",
    [
        (),
        [_frame()],
        tuple(_frame(2.0 + index) for index in range(9)),
        (_frame(), _frame(9.0)),
        (_frame(), _frame(frame_id="other-id")),
        (_frame(9.0, frame_id="same-id"), _frame(frame_id="same-id")),
        (_frame(10.1),),
        (_frame(4.0),),
        (_frame(4.9), _frame()),
    ],
)
def test_history_rejects_missing_mutable_future_expired_duplicate_or_gap(frames):
    with pytest.raises((ValueError, TypeError)):
        _input(frames=frames)


def test_input_rejects_unprovided_observation_future_receipts_and_prompt_mismatch():
    with pytest.raises(ValueError):
        _input(ledger=MissionEvidenceLedger(_scope()))
    with pytest.raises(ValueError):
        _input(ledger=MissionEvidenceLedger(_scope(), (_frame(),), (_receipt(at=10.1),)))
    with pytest.raises(ValueError):
        replace(_input(), prompt=replace(_prompt(), text="another mission"))
    with pytest.raises(ValueError):
        replace(_input(), prompt_text_original="not the original request")
    with pytest.raises(ValueError):
        replace(_input(), deadline_at_s=10.0)


def test_parser_keeps_intent_and_completion_facts_separate():
    model_input = _input()
    raw = _raw(model_input)
    raw["output"]["reason"] = "model claims it has completed the task"
    raw["output"]["task_action"] = "report"
    before = model_input.ledger.to_mapping()
    parsed = _parse(raw, model_input)
    assert parsed.to_mapping()["completion_evidence_created"] is False
    assert parsed.to_mapping()["execution_authority"] is False
    assert model_input.ledger.to_mapping() == before
    assert model_input.ledger.receipts == ()
    assert not hasattr(parsed, "execute") and not hasattr(parsed, "callback")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("contract", "unversioned"),
        ("input_sha256", "a" * 64),
        ("scope_sha256", "b" * 64),
        ("frame_ids", ["unknown"]),
        ("frame_ids", []),
        ("frame_ids", ["frame-10.0", "frame-10.0"]),
        ("frame_ids", "frame-10.0"),
        ("evidence_ids", ["invented-completion"]),
        ("evidence_ids", None),
    ],
)
def test_wrapper_identity_references_and_version_are_strict(field, value):
    raw = _raw()
    raw[field] = value
    with pytest.raises((ValueError, TypeError)):
        _parse(raw)


@pytest.mark.parametrize("extra", ["mavlink", "arming", "actuator", "completed", "latency_ms"])
def test_model_cannot_add_transport_completion_or_its_own_latency(extra):
    raw = _raw()
    raw[extra] = True
    with pytest.raises(ValueError):
        _parse(raw)
    del raw[extra]
    raw["output"][extra] = True
    with pytest.raises(ValueError):
        _parse(raw)


@pytest.mark.parametrize(
    "fragment",
    [
        '"confidence": NaN',
        '"confidence": Infinity',
        '"confidence": 1e999',
        '"confidence": 0.5, "confidence": 0.9',
    ],
)
def test_duplicate_and_nonfinite_json_rejected_without_repair(fragment):
    payload = json.dumps(_raw()).replace('"confidence": 0.5', fragment)
    with pytest.raises(ValueError):
        parse_onboard_multimodal_intent(
            payload,
            _input(),
            completed_at_s=10.05,
            inference_latency_ms=40,
            expected_scope=_scope(),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"north_offset_m": 501},
        {"altitude_m": -1},
        {"speed_mps": 51},
        {"duration_s": 901},
        {"speed_mps": True},
        {"speed_kmh": 10},
        {"mavlink": {}},
        {"arming": True},
        {"actuator": []},
        {"action": "arm"},
    ],
)
def test_nested_flight_plan_extra_fields_and_physical_units_are_strict(changes):
    raw = _raw()
    raw["output"]["flight_plan"] = {
        "action": "move_relative",
        "north_offset_m": 1.0,
        "east_offset_m": 0.0,
        **changes,
    }
    with pytest.raises((ValueError, TypeError)):
        _parse(raw)


def test_bounded_existing_flight_plan_is_preserved_not_executed():
    raw = _raw()
    raw["output"] = {
        "response": "replan",
        "reason": "fixture suggestion",
        "confidence": 0.8,
        "task_action": "search",
        "flight_plan": {
            "action": "search",
            "north_offset_m": 5,
            "east_offset_m": -3,
            "altitude_m": 20,
            "speed_mps": 4,
            "duration_s": 15,
        },
    }
    parsed = _parse(raw)
    assert parsed.output.flight_plan.north_offset_m == 5
    assert parsed.output.flight_plan.speed_mps == 4
    assert parsed.to_mapping()["execution_authority"] is False


def test_frame_or_evidence_ids_do_not_authorize_a_target_reference():
    raw = _raw()
    raw["output"]["flight_plan"] = {
        "action": "move_relative",
        "north_offset_m": 1.0,
        "east_offset_m": 0.0,
        "reference_id": "imagined-target",
    }
    with pytest.raises(ValueError):
        _parse(raw)
    raw["output"]["flight_plan"]["reference_id"] = "frame-10.0"
    with pytest.raises(ValueError):
        _parse(raw)  # An image ID is evidence, not a typed target registry.
    raw["output"]["flight_plan"]["reference_id"] = None
    assert _parse(raw).output.flight_plan.reference_id is None


def test_existing_task_flight_compatibility_reused_without_new_task_answers():
    raw = _raw()
    raw["output"]["task_action"] = "search"
    raw["output"]["flight_plan"] = {
        "action": "return_home",
        "north_offset_m": 0,
        "east_offset_m": 0,
    }
    with pytest.raises(ValueError):
        _parse(raw)


@pytest.mark.parametrize(
    "payload",
    ["", bytearray(b"{}"), " " * (1024 * 1024 + 1), b" " * (1024 * 1024 + 1)],
    ids=["empty-string", "mutable-buffer", "oversized-string", "oversized-bytes"],
)
def test_intent_payload_bounded_and_not_mutably_aliased(payload):
    with pytest.raises(ValueError):
        parse_onboard_multimodal_intent(
            payload,
            _input(),
            completed_at_s=10.05,
            inference_latency_ms=40,
            expected_scope=_scope(),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"completed_at_s": 10.6},
        {"completed_at_s": 9.9},
        {"inference_latency_ms": 51},
        {"inference_latency_ms": float("nan")},
        {"inference_latency_ms": -1},
    ],
)
def test_host_measured_latency_and_deadline_not_model_claims(kwargs):
    with pytest.raises(ValueError):
        _parse(**kwargs)


def test_old_history_expiring_during_inference_is_rejected():
    model_input = _input(frames=(_frame(5.01), _frame()))
    with pytest.raises(ValueError):
        _parse(model_input=model_input)


def test_real_fixture_receipt_chain_is_causal_and_can_be_cited():
    receipts = []
    prior = None
    for index, stage in enumerate(("proposed", "accepted", "executed", "completed")):
        receipt = _receipt(stage, prior=prior, at=10 + index * 0.01)
        receipts.append(receipt)
        prior = receipt.evidence_id
    ledger = MissionEvidenceLedger(
        _scope(),
        (_frame(),),
        tuple(receipts),
        ("verified-executor-fixture",),
    )
    model_input = OnboardMultimodalInput(_scope(), _prompt(), (_frame(),), ledger, 10.1, 10.6)
    raw = _raw(model_input)
    raw["evidence_ids"] = ["evidence-completed"]
    result = _parse(raw, model_input, completed_at_s=10.15)
    assert result.evidence_ids == ("evidence-completed",)
    assert len(ledger.receipts) == 4  # The parser did not add a fifth completion.


@pytest.mark.parametrize(
    "receipts,trusted",
    [
        ((_receipt("completed"),), ("verified-executor-fixture",)),
        ((_receipt(), _receipt("accepted", prior="evidence-proposed")), ()),
        (
            (_receipt(), _receipt("executed", prior="evidence-proposed")),
            ("verified-executor-fixture",),
        ),
        ((_receipt(), _receipt("accepted", prior="wrong")), ("verified-executor-fixture",)),
        ((_receipt(), _receipt()), ("verified-executor-fixture",)),
    ],
)
def test_ledger_rejects_missing_untrusted_skipped_or_duplicate_receipts(receipts, trusted):
    with pytest.raises(ValueError):
        MissionEvidenceLedger(_scope(), (_frame(),), receipts, trusted)


def test_receipt_wrapper_and_original_receipt_bytes_must_both_match():
    receipt = _receipt()
    for change in (
        {"receipt_sha256": "a" * 64},
        {"source_receipt_bytes": b"changed original"},
        {"receipt_bytes": bytearray(receipt.receipt_bytes)},
        {"frame_ids": ["frame-10.0"]},
    ):
        with pytest.raises((ValueError, TypeError)):
            replace(receipt, **change)
    for change in (
        {"authority": "executor_receipt"},
        {"success": False},
        {"scope_sha256": "a" * 64},
        {"stage": "completed"},
        {"extra": "claimed fact"},
    ):
        with pytest.raises(ValueError):
            _change_receipt(receipt, **change)
    accepted = _receipt("accepted", prior="evidence-proposed")
    with pytest.raises(ValueError):
        _change_receipt(accepted, authority="model_proposal")


def test_ledger_rejects_mutable_aliases_receipt_order_and_unknown_frames():
    with pytest.raises(ValueError):
        MissionEvidenceLedger(_scope(), [_frame()])
    with pytest.raises(ValueError):
        MissionEvidenceLedger(_scope(), (_frame(),), [_receipt()])
    with pytest.raises(ValueError):
        MissionEvidenceLedger(_scope(), (_frame(),), (), ["trusted"])
    with pytest.raises(ValueError):
        MissionEvidenceLedger(_scope(), (_frame(9.0),), (_receipt(),))
    first = _receipt(at=10.1)
    second = _receipt("accepted", prior=first.evidence_id, at=10.0)
    with pytest.raises(ValueError):
        MissionEvidenceLedger(
            _scope(),
            (_frame(),),
            (first, second),
            ("verified-executor-fixture",),
        )

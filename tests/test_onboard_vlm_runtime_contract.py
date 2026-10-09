"""CPU fixtures for a non-executing, host-bound VLM development contract."""

from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import FrozenInstanceError, replace
from io import BytesIO

import pytest
from PIL import Image

from multidetect.autonomy.contracts import ContextSnapshot, GeoPoint, PromptEnvelope
from multidetect.autonomy.onboard_agent import OnboardEventKind, OnboardRuntimeEvent
from multidetect.autonomy.onboard_multimodal import (
    RECEIPT_CONTRACT,
    MissionEvidenceLedger,
    OnboardEvidenceReceipt,
    OnboardImageFrame,
    OnboardMultimodalInput,
    OnboardMultimodalScope,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import (
    RESPONSE_CONTRACT,
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
    serialize_onboard_vlm_messages,
)
from multidetect.autonomy.serialization import canonical_json


def _context(at_s: float) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id=f"fixture-{at_s}",
        captured_at_s=at_s,
        captured_at_utc="2026-10-07T00:00:00Z",
        aircraft_position=GeoPoint(1.3, 103.8, 20.0, "relative_home"),
        home_position=GeoPoint(1.3, 103.8),
        airframe_profile_id="fixture-profile",
        airframe_profile_hash="b" * 64,
        battery_remaining_pct=81.0,
        estimated_flight_time_s=1000.0,
        ground_speed_mps=2.1,
        airspeed_mps=1.2,
        wind_north_mps=0.4,
        wind_east_mps=-0.2,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.1,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=False,
        dem_available=False,
    )


def _input(*, known=False, encoding="rgb8", receipt_source=None) -> OnboardMultimodalInput:
    original = "  查看当前图像，按已有证据判断下一步；未知事实别编造。\n"
    prompt = PromptEnvelope(
        "prompt-fixture",
        original,
        "zh",
        "2026-10-07T00:00:00Z",
        "fixture-operator",
        "fixture-aircraft",
    )
    context = _context(2.0)
    scope = OnboardMultimodalScope(
        session_id="00000000-0000-0000-0000-000000000001",
        prompt_sha256=prompt.canonical_hash,
        aircraft_id=prompt.aircraft_id,
        selected_task_id="fixture-task",
        clock_epoch="fixture-host-clock",
        airframe_profile_id=context.airframe_profile_id if known else None,
        airframe_profile_sha256=context.airframe_profile_hash if known else None,
        vehicle_class="multirotor" if known else None,
        home_sha256=context.home_position.canonical_hash if known else None,
    )
    frames = []
    for index in (1, 2):
        image = Image.new("RGB", (8, 6), (index * 40, 90, 120))
        if encoding == "rgb8":
            payload = image.tobytes()
        else:
            stream = BytesIO()
            image.save(stream, format={"png": "PNG", "jpeg": "JPEG"}[encoding])
            payload = stream.getvalue()
        frames.append(
            OnboardImageFrame(
                scope,
                f"frame-{index}",
                float(index),
                payload,
                hashlib.sha256(payload).hexdigest(),
                8,
                6,
                encoding,
                _context(float(index)) if known else None,
            )
        )
    receipts = []
    if receipt_source is not None:
        prior = None
        for stage in ("proposed", "accepted", "executed", "completed"):
            source_id = "proposal-host" if stage == "proposed" else "executor-host"
            original_receipt = canonical_json(
                {
                    "operation": "fixture-search",
                    "stage": stage,
                    "truth_free": True,
                    **receipt_source,
                }
            ).encode("utf-8")
            identifier = f"receipt-{stage}"
            raw = canonical_json(
                {
                    "schema_version": 1,
                    "contract": RECEIPT_CONTRACT,
                    "evidence_id": identifier,
                    "scope_sha256": scope.canonical_hash,
                    "stage": stage,
                    "observed_at_s": 1.5,
                    "source_id": source_id,
                    "proposal_id": "proposal-1",
                    "prior_evidence_id": prior,
                    "frame_ids": ["frame-1"],
                    "authority": "model_proposal" if stage == "proposed" else "executor_receipt",
                    "source_receipt_sha256": hashlib.sha256(original_receipt).hexdigest(),
                    "success": True,
                }
            ).encode("utf-8")
            receipts.append(
                OnboardEvidenceReceipt(
                    identifier,
                    scope,
                    stage,
                    1.5,
                    source_id,
                    raw,
                    hashlib.sha256(raw).hexdigest(),
                    "proposal-1",
                    original_receipt,
                    prior,
                    ("frame-1",),
                )
            )
            prior = identifier
    ledger = MissionEvidenceLedger(scope, tuple(frames), tuple(receipts), ("executor-host",))
    return OnboardMultimodalInput(
        scope,
        prompt,
        tuple(frames),
        ledger,
        requested_at_s=2.0,
        deadline_at_s=4.0,
        max_frame_age_s=5.0,
        max_gap_s=1.0,
        prompt_text_original=original,
    )


def _reply(**changes) -> dict:
    return {
        "schema_version": 2,
        "contract": RESPONSE_CONTRACT,
        "frame_ids": ["frame-2"],
        "evidence_ids": [],
        "output": {
            "response": "hold",
            "reason": "facts insufficient",
            "confidence": 0.3,
            "task_action": "hold",
            "flight_plan": None,
        },
        **changes,
    }


def _parse(reply=None, request=None, **changes):
    request = request or OnboardVlmRuntimeRequest(_input())
    return parse_onboard_vlm_runtime_response(
        json.dumps(reply or _reply()),
        request,
        **{
            "current_scope": request.model_input.scope,
            "completed_at_s": 2.5,
            "inference_latency_ms": 500.0,
            **changes,
        },
    )


def _metadata(messages) -> dict:
    return json.loads(messages[1]["content"][-1]["text"])


@pytest.mark.parametrize("encoding", ["rgb8", "png", "jpeg"])
def test_serializer_uses_actual_rgb_original_prompt_unknowns_and_no_hash_echo(encoding):
    model_input = _input(encoding=encoding)
    request = OnboardVlmRuntimeRequest(model_input)
    messages = serialize_onboard_vlm_messages(request)
    assert [item["role"] for item in messages] == ["system", "user"]
    images = [item["image"] for item in messages[1]["content"] if item["type"] == "image"]
    assert len(images) == 2 and all(image.mode == "RGB" for image in images)
    assert images[0].getpixel((0, 0)) != images[1].getpixel((0, 0))
    assert messages[1]["content"][-2]["text"] == (
        "Original mission prompt (verbatim):\n" + model_input.prompt_text_original
    )
    metadata = _metadata(messages)
    assert metadata["host_reviewed_receipts"] == []
    assert metadata["missing_execution_evidence"] is True
    assert metadata["source_event_status"] == metadata["obstacle_measurement_status"] == "unknown"
    assert all(
        row["context"] is None and row["context_status"] == "unknown"
        for row in metadata["observations"]
    )
    assert request.canonical_hash not in json.dumps(metadata)
    assert request.input_sha256 not in json.dumps(metadata)
    for image, row, source in zip(
        images, metadata["observations"], model_input.frames, strict=True
    ):
        assert row["preprocessing"]["source_sha256"] == source.image_sha256
        assert (
            row["preprocessing"]["processed_rgb_pixels_sha256"]
            == hashlib.sha256(image.tobytes()).hexdigest()
        )


def test_context_is_per_actual_frame_not_hardcoded_empty_or_latest_for_every_frame():
    model_input = _input(known=True)
    metadata = _metadata(serialize_onboard_vlm_messages(OnboardVlmRuntimeRequest(model_input)))
    for source, row in zip(model_input.frames, metadata["observations"], strict=True):
        assert row["context_status"] == "provided"
        assert row["context"] == source.context.to_mapping()
    assert metadata["observations"][0]["context"] != metadata["observations"][1]["context"]
    assert metadata["execution_authority"] is False


def test_total_pixel_resize_is_auditable_and_does_not_relax_source_byte_admission():
    model_input = _input()
    source_bytes = sum(len(item.image_bytes) for item in model_input.frames)
    with pytest.raises(ValueError, match="source image bytes"):
        OnboardVlmRuntimeRequest(
            model_input, max_total_image_bytes=source_bytes - 1, max_total_pixels=24
        )
    request = OnboardVlmRuntimeRequest(
        model_input, max_total_image_bytes=source_bytes, max_total_pixels=24
    )
    metadata = _metadata(serialize_onboard_vlm_messages(request))
    assert (
        sum(
            row["preprocessing"]["processed_width"] * row["preprocessing"]["processed_height"]
            for row in metadata["observations"]
        )
        <= 24
    )
    assert all(
        row["preprocessing"]["resize_method"] == "LANCZOS" for row in metadata["observations"]
    )
    assert (
        sum(row["preprocessing"]["source_bytes"] for row in metadata["observations"])
        == source_bytes
    )


def test_parent_request_and_typed_response_are_pickleable_immutable_and_host_bound():
    request = OnboardVlmRuntimeRequest(_input(known=True))
    restored = pickle.loads(pickle.dumps(request))
    assert restored == request and restored.canonical_hash == request.canonical_hash
    restored.validate_binding()
    response = _parse(request=restored)
    assert pickle.loads(pickle.dumps(response)) == response
    assert response.request_sha256 == request.canonical_hash
    assert response.input_sha256 == request.model_input.canonical_hash
    assert response.scope_sha256 == request.model_input.scope.canonical_hash
    assert response.raw_response_sha256 == hashlib.sha256(response.raw_response_bytes).hexdigest()
    assert response.to_mapping()["execution_authority"] is False
    assert response.to_mapping()["completion_evidence_created"] is False
    with pytest.raises(FrozenInstanceError):
        request.max_total_pixels = 10
    with pytest.raises(FrozenInstanceError):
        response.output = None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_total_image_bytes", 0),
        ("max_total_image_bytes", True),
        ("max_total_pixels", 1),
        ("max_total_pixels", float("inf")),
        ("max_total_receipt_bytes", 0),
        ("host_reviewed_receipt_sources", []),
        ("host_reviewed_receipt_sources", ("x", "x")),
        ("host_reviewed_receipt_sources", (" x",)),
    ],
)
def test_request_rejects_invalid_budgets_and_mutable_review_grants(field, value):
    with pytest.raises((ValueError, TypeError)):
        OnboardVlmRuntimeRequest(_input(), **{field: value})


def test_reviewed_ledger_facts_are_real_causal_sources_and_model_citations_are_separate():
    model_input = _input(receipt_source={"native_receipt_id": "actual-fixture-receipt"})
    unreviewed = OnboardVlmRuntimeRequest(model_input)
    assert _metadata(serialize_onboard_vlm_messages(unreviewed))["host_reviewed_receipts"] == []
    with pytest.raises(ValueError, match="not supplied"):
        _parse(_reply(evidence_ids=["receipt-completed"]), unreviewed)
    with pytest.raises(ValueError, match="lineage"):
        OnboardVlmRuntimeRequest(model_input, ("executor-host",))
    reviewed = OnboardVlmRuntimeRequest(model_input, ("proposal-host", "executor-host"))
    records = _metadata(serialize_onboard_vlm_messages(reviewed))["host_reviewed_receipts"]
    assert [record["stage"] for record in records] == [
        "proposed",
        "accepted",
        "executed",
        "completed",
    ]
    assert records[0]["authority"] == "model_proposal"
    assert records[-1]["source_receipt"]["native_receipt_id"] == "actual-fixture-receipt"
    assert _parse(_reply(evidence_ids=["receipt-completed"]), reviewed).evidence_ids == (
        "receipt-completed",
    )
    with pytest.raises(ValueError, match="receipt bytes"):
        replace(reviewed, max_total_receipt_bytes=1)


@pytest.mark.parametrize(
    "truth",
    [
        {"scenario_id": "hidden-world"},
        {"ground_truth": {"action": "return"}},
        {"nested": [{"amodal_mask_path": "truth.png"}]},
        {"truth_free": False},
        {"evaluation_metadata": {"expected": "visible"}},
    ],
)
def test_reviewed_original_receipt_cannot_smuggle_scenario_or_label_truth(truth):
    with pytest.raises(ValueError):
        OnboardVlmRuntimeRequest(_input(receipt_source=truth), ("proposal-host", "executor-host"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1),
        ("contract", "old"),
        ("frame_ids", []),
        ("frame_ids", ["frame-1", "frame-1"]),
        ("frame_ids", ["unknown"]),
        ("frame_ids", [" frame-2"]),
        ("evidence_ids", ["unknown"]),
        ("evidence_ids", [None]),
    ],
)
def test_v2_version_and_citations_are_strict(field, value):
    with pytest.raises((ValueError, TypeError)):
        _parse(_reply(**{field: value}))


@pytest.mark.parametrize(
    "extra",
    [
        {"input_sha256": "a" * 64},
        {"scope_sha256": "a" * 64},
        {"completed_at_s": 2.4},
        {"execution_authority": True},
        {"completed": True},
        {"mavlink": "ARM"},
    ],
)
def test_model_cannot_select_host_request_or_report_timing_execution_and_transport(extra):
    with pytest.raises(ValueError):
        _parse(_reply(**extra))


@pytest.mark.parametrize(
    "fragment",
    [
        '"confidence":NaN',
        '"confidence":1e999',
        '"confidence":0.2,"confidence":0.3',
        '"confidence":Infinity',
        '"confidence":true',
    ],
)
def test_raw_json_is_not_repaired_and_nonfinite_duplicate_fields_are_rejected(fragment):
    request = OnboardVlmRuntimeRequest(_input())
    raw = json.dumps(_reply()).replace('"confidence": 0.3', fragment)
    with pytest.raises(ValueError):
        parse_onboard_vlm_runtime_response(
            raw,
            request,
            current_scope=request.model_input.scope,
            completed_at_s=2.5,
            inference_latency_ms=500,
        )


def _movement(**changes) -> dict:
    return {
        "response": "replan",
        "reason": "observe more",
        "confidence": 0.8,
        "task_action": "search",
        "flight_plan": {
            "action": "search",
            "north_offset_m": 4.0,
            "east_offset_m": -3.0,
            "altitude_m": 20.0,
            "speed_mps": None,
            "duration_s": None,
            "reference_id": None,
            **changes,
        },
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"speed_mps": 0},
        {"speed_mps": 4},
        {"duration_s": 0},
        {"duration_s": 5},
        {"reference_id": "frame-2"},
        {"reference_id": "receipt-completed"},
        {"north_offset_m": 501},
        {"altitude_m": -1},
        {"actuator": 1},
        {"action": "arm"},
    ],
)
def test_nested_plan_is_physically_bounded_and_unsupported_fields_do_not_become_execution(changes):
    with pytest.raises(ValueError):
        _parse(_reply(output=_movement(**changes)))


def test_supported_typed_intent_survives_without_execution_or_fixed_correct_action():
    for output in (_movement(), {**_movement(), "task_action": "report"}):
        if output["task_action"] == "report":
            with pytest.raises(ValueError, match="compatibility"):
                _parse(_reply(output=output))
        else:
            result = _parse(_reply(output=output))
            assert result.output.flight_plan.north_offset_m == 4.0
            assert result.output.flight_plan.east_offset_m == -3.0
            assert result.to_mapping()["execution_authority"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"completed_at_s": 4.001},
        {"completed_at_s": 1.9},
        {"completed_at_s": 2.5, "inference_latency_ms": 501},
        {"inference_latency_ms": -1},
        {"inference_latency_ms": float("nan")},
    ],
)
def test_host_time_latency_deadline_and_expiration_are_rechecked(changes):
    with pytest.raises(ValueError):
        _parse(**changes)
    request = OnboardVlmRuntimeRequest(replace(_input(), max_frame_age_s=1.1))
    with pytest.raises(ValueError, match="expired"):
        _parse(request=request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "00000000-0000-0000-0000-000000000002"),
        ("prompt_sha256", "a" * 64),
        ("aircraft_id", "other"),
        ("selected_task_id", "other"),
        ("clock_epoch", "other"),
        ("home_sha256", "a" * 64),
        ("vehicle_class", "vtol"),
    ],
)
def test_parent_current_scope_must_still_match_every_mission_boundary(field, value):
    request = OnboardVlmRuntimeRequest(_input())
    with pytest.raises(ValueError, match="scope changed"):
        _parse(request=request, current_scope=replace(request.model_input.scope, **{field: value}))


def test_input_or_source_bytes_tampering_cannot_rebind_saved_parent_request():
    request = OnboardVlmRuntimeRequest(_input())
    object.__setattr__(request.model_input.frames[0], "image_bytes", b"x" * 144)
    with pytest.raises(ValueError, match="RGB source bytes changed"):
        serialize_onboard_vlm_messages(request)
    with pytest.raises(ValueError, match="RGB source bytes changed"):
        _parse(request=request)
    request = OnboardVlmRuntimeRequest(_input())
    object.__setattr__(request, "max_total_pixels", 100)
    with pytest.raises(ValueError, match="identity changed"):
        _parse(request=request)


def test_actual_optional_event_is_bound_and_legacy_clearance_cap_is_not_measured():
    model_input = _input(known=True)
    event = OnboardRuntimeEvent(
        "real-source-event", OnboardEventKind.TARGET_LOST, 2.0, obstacle_distance_m=5000.0
    )
    request = OnboardVlmRuntimeRequest(model_input, source_event=event)
    assert request.canonical_hash != OnboardVlmRuntimeRequest(model_input).canonical_hash
    metadata = _metadata(serialize_onboard_vlm_messages(request))
    assert metadata["source_event_status"] == "provided"
    assert metadata["source_event"]["event_id"] == event.event_id
    assert metadata["source_event"]["obstacle_distance_m"] is None
    assert metadata["obstacle_measurement_status"] == "unknown"
    assert request.source_event.obstacle_distance_m == 5000.0
    measured = replace(request, source_event=replace(event, obstacle_distance_m=12.0))
    assert (
        _metadata(serialize_onboard_vlm_messages(measured))["source_event"]["obstacle_distance_m"]
        == 12.0
    )


@pytest.mark.parametrize("at_s", [0.5, 2.001])
def test_event_without_contemporaneous_actual_context_is_rejected(at_s):
    event = OnboardRuntimeEvent("fixture", OnboardEventKind.TARGET_LOST, at_s)
    with pytest.raises(ValueError, match="contemporaneous"):
        OnboardVlmRuntimeRequest(_input(known=True), source_event=event)
    with pytest.raises(ValueError, match="contemporaneous"):
        OnboardVlmRuntimeRequest(_input(), source_event=replace(event, observed_at_s=2.0))


@pytest.mark.parametrize("response", ["execute", "hold", "return", "land"])
def test_response_must_agree_with_actual_flight_action_not_only_task_action(response):
    with pytest.raises(ValueError, match="response/flight"):
        _parse(_reply(output={**_movement(), "response": response}))


@pytest.mark.parametrize(
    "task,action,response",
    [
        ("hold", "hold_position", "hold"),
        ("return_home", "return_home", "return"),
        ("land", "land", "land"),
    ],
)
def test_safety_intents_remain_proposals_and_require_matching_response(task, action, response):
    output = {
        **_movement(north_offset_m=0.0, east_offset_m=0.0, action=action),
        "task_action": task,
        "response": response,
    }
    assert _parse(_reply(output=output)).output.response.value == response
    with pytest.raises(ValueError, match="response/flight"):
        _parse(_reply(output={**output, "response": "replan"}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_total_image_bytes", 16 * 1024 * 1024 + 1),
        ("max_total_receipt_bytes", 256 * 1024 + 1),
    ],
)
def test_runtime_ipc_budget_caps_cannot_be_relaxed_by_request(field, value):
    with pytest.raises(ValueError):
        OnboardVlmRuntimeRequest(_input(), **{field: value})


def test_parent_ledger_metadata_bound_is_enforced_without_truncating_evidence():
    model_input = _input()
    first = model_input.frames[0]
    frames = (
        tuple(
            replace(first, frame_id=f"old-{index}", captured_at_s=index / 100.0)
            for index in range(63)
        )
        + model_input.frames
    )
    ledger = replace(model_input.ledger, frames=frames)
    with pytest.raises(ValueError, match="64 entries"):
        OnboardVlmRuntimeRequest(replace(model_input, ledger=ledger))
    receipts = []
    source = _input(receipt_source={}).ledger.receipts[0]
    for index in range(65):
        identifier = f"proposal-{index}"
        payload = json.loads(source.receipt_bytes)
        payload.update(evidence_id=identifier, proposal_id=identifier)
        raw = canonical_json(payload).encode("utf-8")
        receipts.append(
            replace(
                source,
                evidence_id=identifier,
                proposal_id=identifier,
                receipt_bytes=raw,
                receipt_sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    with pytest.raises(ValueError, match="64 entries"):
        OnboardVlmRuntimeRequest(
            replace(model_input, ledger=replace(model_input.ledger, receipts=tuple(receipts)))
        )


def test_ipc_byte_caps_include_retained_but_unsupplied_frames_and_unreviewed_receipts():
    model_input = _input()
    first = model_input.frames[0]
    older = replace(first, frame_id="not-supplied", captured_at_s=0.5)
    enlarged = replace(
        model_input, ledger=replace(model_input.ledger, frames=(older, *model_input.frames))
    )
    with pytest.raises(ValueError, match="source image bytes"):
        OnboardVlmRuntimeRequest(enlarged, max_total_image_bytes=288)
    with pytest.raises(ValueError, match="ledger receipt bytes"):
        OnboardVlmRuntimeRequest(_input(receipt_source={}), max_total_receipt_bytes=1)

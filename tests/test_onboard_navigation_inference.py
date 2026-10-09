"""CPU contract tests; no processor/model/tokenizer or flight evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from math import pi
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_multimodal import OnboardImageFrame, OnboardMultimodalScope
from multidetect.autonomy.onboard_navigation_actor import NavigationActorFrame, NavigationActorInput
from multidetect.autonomy.onboard_navigation_inference import (
    MAX_NEW_TOKENS,
    MAX_PREFIX_TOKENS,
    MAX_RESPONSE_BYTES,
    NavigationInferenceRequest,
    NavigationInferenceResult,
    PlannedNavigationTarget,
    parse_navigation_inference_response,
    parse_navigation_target,
    serialize_navigation_inference_messages,
)
from multidetect.autonomy.onboard_navigation_supervision import TARGET_CONTRACT
from multidetect.autonomy.serialization import AutonomyContractError, canonical_json

SHA = "a" * 64


def _target(**changes):
    return {
        "schema_version": 1,
        "contract": TARGET_CONTRACT,
        "kind": "translate",
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": [3, 0, 0],
        "yaw_delta_rad": None,
    } | changes


def _scope(prompt="  Follow the building.\n", **changes):
    return OnboardMultimodalScope(
        **(
            {
                "session_id": "12345678-1234-4234-8234-123456789abc",
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "aircraft_id": "fixture-aircraft",
                "selected_task_id": "fixture-task",
                "clock_epoch": "fixture-host-epoch",
            }
            | changes
        )
    )


def _request(two_frames=False, **changes):
    scope = _scope()
    observations, actor_frames = [], []
    for index in range(2 if two_frames else 1):
        payload = bytes((20 + index, 40, 60)) * 12
        digest = hashlib.sha256(payload).hexdigest()
        observations.append(
            OnboardImageFrame(
                scope,
                "source-" + str(index),
                9.0 + index,
                payload,
                digest,
                4,
                3,
            )
        )
        actor_frames.append(
            NavigationActorFrame.from_observation(
                sequence=index,
                image_bytes=payload,
                byte_count=len(payload),
                sha256=digest,
                width=4,
                height=3,
            )
        )
    actor = NavigationActorInput.from_observation(
        prompt_original="  Follow the building.\n",
        current_sequence=len(actor_frames) - 1,
        frames=tuple(actor_frames),
    )
    return NavigationInferenceRequest(
        **(
            {
                "actor_input": actor,
                "scope": scope,
                "source_frames": tuple(observations),
                "source_binding_sha256": SHA,
                "model_sha256": "b" * 64,
                "adapter_sha256": "c" * 64,
                "adapter_config_sha256": "d" * 64,
                "generation": 7,
                "submitted_at_s": 10.0,
                "deadline_s": 14.0,
            }
            | changes
        )
    )


def _result(request=None, **changes):
    request = request or _request()
    return NavigationInferenceResult(
        **(
            {
                "request": request,
                "generation": request.generation,
                "request_sha256": request.canonical_hash,
                "raw_output": canonical_json(_target()).encode("utf-8"),
                "generated_token_ids": (10, 11, 12),
                "generated_tokens": 3,
                "prefix_tokens": 400,
                "completed_at_s": 11.0,
                "inference_latency_ms": 900.0,
                "model_sha256": request.model_sha256,
                "adapter_sha256": request.adapter_sha256,
                "adapter_config_sha256": request.adapter_config_sha256,
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"translation_m": [-500, 500, 1.5]},
        {"translation_m": [0, 0, -3]},
        {"translation_m": None},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": -pi},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": pi},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": None},
        {"kind": "stop", "translation_m": None},
    ],
)
def test_strict_targets_preserve_primitives_and_unknown_quantities(changes):
    raw = _target(**changes)
    target = parse_navigation_target(json.dumps(raw))
    assert target.to_mapping() == raw
    assert target.metric_known == (
        raw["translation_m"] is not None or raw["yaw_delta_rad"] is not None
    )
    assert not hasattr(target, "confidence")
    assert not hasattr(target, "reason")
    assert not hasattr(target, "flight_plan")


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"contract": "wrong"},
        {"translation_frame": "ned"},
        {"yaw_convention": "counterclockwise"},
        {"kind": "land"},
        {"kind": True},
        {"translation_m": [0, 0, 0]},
        {"translation_m": [1, 0]},
        {"translation_m": [1, 0, 0, 0]},
        {"translation_m": [True, 0, 0]},
        {"translation_m": [None, 0, 0]},
        {"translation_m": ["3", 0, 0]},
        {"translation_m": [500.01, 0, 0]},
        {"translation_m": [float("inf"), 0, 0]},
        {"translation_m": {"forward": 3}},
        {"yaw_delta_rad": 0},
        {"yaw_delta_rad": 1},
        {"kind": "turn_yaw", "yaw_delta_rad": 1},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": False},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": 0},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": pi + 0.01},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": float("nan")},
        {"kind": "stop"},
        {"kind": "stop", "translation_m": None, "yaw_delta_rad": 1},
        {"confidence": 1.0},
        {"reason": "invented"},
        {"task_complete": True},
    ],
)
def test_reject_unsupported_ambiguous_or_nonfinite_target(changes):
    with pytest.raises(AutonomyContractError):
        parse_navigation_target(json.dumps(_target(**changes)))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xff",
        b"[]",
        b"null",
        b"true",
        b'{"kind":"stop","kind":"translate"}',
        b'{"schema_version":NaN}',
        b'{"schema_version":1e999}',
        b"{} trailing",
        b" " * (MAX_RESPONSE_BYTES + 1),
    ],
    ids=[
        "empty",
        "bad-utf8",
        "array",
        "null",
        "bool",
        "duplicate",
        "nan",
        "overflow",
        "trailing",
        "oversized",
    ],
)
def test_reject_malformed_raw_json(payload):
    with pytest.raises(AutonomyContractError):
        parse_navigation_target(payload)


@pytest.mark.parametrize("key", list(_target()))
def test_target_requires_all_seven_fields(key):
    raw = _target()
    del raw[key]
    with pytest.raises(AutonomyContractError):
        parse_navigation_target(json.dumps(raw))


def test_target_owns_no_mutable_response_array():
    raw = _target()
    target = PlannedNavigationTarget.from_mapping(raw)
    raw["translation_m"][0] = 99
    assert target.translation_m == (3.0, 0.0, 0.0)
    mapping = target.to_mapping()
    mapping["translation_m"][0] = 88
    assert target.translation_m == (3.0, 0.0, 0.0)
    with pytest.raises(FrozenInstanceError):
        target.kind = "stop"


@pytest.mark.parametrize("two_frames", [False, True])
def test_shared_actor_is_exactly_bound_to_source_scope_rgb_and_times(two_frames):
    request = _request(two_frames)
    request.validate_binding()
    request.validate_freshness(11.0)
    assert request.actor_identity_sha256 == request.actor_input.identity_sha256
    mapping = request.to_mapping()
    assert mapping["source_binding_sha256"] == SHA
    assert mapping["max_prefix_tokens"] == MAX_PREFIX_TOKENS
    assert mapping["max_new_tokens"] == MAX_NEW_TOKENS
    assert mapping["transport_authority"] is False
    assert mapping["execution_authority"] is False
    assert mapping["completion_evidence_created"] is False
    assert "confidence" not in mapping


def _message_identity(request):
    messages = serialize_navigation_inference_messages(request)
    try:
        return [
            (
                message["role"],
                [
                    dict(item)
                    if item["type"] == "text"
                    else {
                        "type": "image",
                        "mode": item["image"].mode,
                        "size": item["image"].size,
                        "pixels": item["image"].tobytes(),
                    }
                    for item in message["content"]
                ],
            )
            for message in messages
        ]
    finally:
        for message in messages:
            for item in message["content"]:
                if item["type"] == "image":
                    item["image"].close()


def test_outer_bindings_change_request_identity_but_never_actor_messages():
    original = _request(two_frames=True)
    scope = replace(original.scope, aircraft_id="secret-other-aircraft")
    sources = tuple(replace(frame, scope=scope) for frame in original.source_frames)
    changed = replace(
        original,
        scope=scope,
        source_frames=sources,
        generation=8,
        model_sha256="e" * 64,
        adapter_sha256="f" * 64,
        adapter_config_sha256="0" * 64,
        source_binding_sha256="1" * 64,
        submitted_at_s=10.1,
        deadline_s=14.1,
    )
    assert changed.canonical_hash != original.canonical_hash
    assert changed.actor_identity_sha256 == original.actor_identity_sha256
    assert _message_identity(changed) == _message_identity(original)
    assert "secret-other-aircraft" not in repr(_message_identity(changed))


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": True},
        {"generation": 0},
        {"generation": 1.0},
        {"max_new_tokens": True},
        {"max_new_tokens": 0},
        {"max_new_tokens": 193},
        {"submitted_at_s": True},
        {"submitted_at_s": float("nan")},
        {"deadline_s": False},
        {"deadline_s": float("inf")},
        {"deadline_s": 10},
        {"deadline_s": 41},
        {"max_frame_age_s": True},
        {"max_frame_age_s": 0},
        {"max_frame_age_s": 31},
        {"source_binding_sha256": "bad"},
        {"model_sha256": "B" * 64},
        {"adapter_sha256": " " + "c" * 64},
        {"adapter_config_sha256": None},
        {"scope": object()},
        {"actor_input": object()},
        {"source_frames": []},
        {"source_frames": ()},
    ],
)
def test_request_rejects_invalid_outer_bindings(changes):
    with pytest.raises((AutonomyContractError, TypeError)):
        _request(**changes)


@pytest.mark.parametrize(
    "case",
    [
        "future",
        "stale",
        "time_reversed",
        "time_equal",
        "duplicate_id",
        "rgb",
        "shape",
        "frame_scope",
        "prompt_scope",
        "missing",
        "mutable",
        "wrong_type",
    ],
)
def test_request_rejects_bad_source_to_actor_alignment(case):
    request = _request(two_frames=True)
    frames = list(request.source_frames)
    changes = {}
    if case in {"future", "stale", "time_reversed", "time_equal"}:
        index = 1 if case == "future" else 0
        at = {"future": 10.1, "stale": 4, "time_reversed": 10, "time_equal": 10}[case]
        frames[index] = replace(frames[index], captured_at_s=at)
        if case == "time_reversed":
            frames[1] = replace(frames[1], captured_at_s=9)
    elif case == "duplicate_id":
        frames[1] = replace(frames[1], frame_id=frames[0].frame_id)
    elif case == "rgb":
        rgb = bytes((99, 99, 99)) * 12
        frames[0] = replace(
            frames[0], image_bytes=rgb, image_sha256=hashlib.sha256(rgb).hexdigest()
        )
    elif case == "frame_scope":
        frames[0] = replace(frames[0], scope=replace(request.scope, aircraft_id="other"))
    elif case == "shape":
        frames[0] = replace(frames[0], width=6, height=2)
    elif case == "prompt_scope":
        changes["scope"] = replace(request.scope, prompt_sha256=SHA)
    elif case == "missing":
        frames.pop()
    elif case == "wrong_type":
        frames[0] = request.actor_input.frames[0]
    changes["source_frames"] = frames if case == "mutable" else tuple(frames)
    with pytest.raises((AutonomyContractError, TypeError)):
        replace(request, **changes)


@pytest.mark.parametrize("now", [True, float("inf"), 9, 14, 15])
def test_request_rechecks_absolute_deadline_and_frame_age(now):
    with pytest.raises(AutonomyContractError):
        _request().validate_freshness(now)
    with pytest.raises(AutonomyContractError):
        _request(max_frame_age_s=1.5).validate_freshness(11)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("generation", 8),
        ("adapter_sha256", "e" * 64),
        ("submitted_at_s", 10.1),
        ("source_binding_sha256", "f" * 64),
    ],
)
def test_request_detects_postconstruction_identity_mutation(field_name, value):
    request = _request()
    object.__setattr__(request, field_name, value)
    with pytest.raises(AutonomyContractError):
        request.validate_binding()


def test_request_detects_actor_and_source_bytes_mutation():
    request = _request()
    object.__setattr__(request.actor_input.frames[0], "image_bytes", b"x" * 36)
    with pytest.raises(AutonomyContractError):
        request.validate_binding()
    request = _request()
    object.__setattr__(request.source_frames[0], "image_bytes", b"x" * 36)
    with pytest.raises(AutonomyContractError):
        request.validate_binding()


def test_result_binds_raw_token_observations_without_claiming_tokenizer_verification():
    result = _result()
    result.validate_binding(current_scope=result.request.scope, now_s=11.5)
    mapping = result.to_mapping()
    assert result.target.translation_m == (3.0, 0.0, 0.0)
    assert result.raw_output_sha256 == hashlib.sha256(result.raw_output).hexdigest()
    assert mapping["generated_token_ids"] == [10, 11, 12]
    assert mapping["generated_tokens"] == 3
    assert mapping["max_new_tokens_reached"] is False
    for name in (
        "tokenizer_independently_verified",
        "transport_authority",
        "execution_authority",
        "completion_evidence_created",
    ):
        assert mapping[name] is False
    for name in ("confidence", "reason", "output", "flight_plan"):
        assert name not in mapping
    changed = replace(result, generated_token_ids=(11, 12, 13))
    assert changed.canonical_hash != result.canonical_hash


def test_prefix_and_generated_suffix_have_separate_caps_not_training_full_cap():
    result = _result(
        prefix_tokens=4096, generated_token_ids=tuple(range(191)), generated_tokens=191
    )
    assert result.prefix_tokens + result.generated_tokens > 4096
    result.validate_binding(current_scope=result.request.scope, now_s=11)


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": True},
        {"generation": 8},
        {"request_sha256": SHA},
        {"request_sha256": True},
        {"model_sha256": "e" * 64},
        {"adapter_sha256": "e" * 64},
        {"adapter_config_sha256": "e" * 64},
        {"raw_output": "{}"},
        {"raw_output": bytearray(b"{}")},
        {"raw_output": b"{}"},
        {"generated_token_ids": [10, 11, 12]},
        {"generated_token_ids": ()},
        {"generated_token_ids": (True, 11, 12)},
        {"generated_token_ids": (-1, 11, 12)},
        {"generated_token_ids": (1.0, 11, 12)},
        {"generated_token_ids": (10, 11)},
        {"generated_tokens": True},
        {"generated_tokens": 0},
        {"generated_tokens": -1},
        {"generated_tokens": 3.0},
        {"generated_tokens": 4},
        {"generated_token_ids": tuple(range(192)), "generated_tokens": 192},
        {"generated_token_ids": tuple(range(193)), "generated_tokens": 193},
        {"prefix_tokens": True},
        {"prefix_tokens": 0},
        {"prefix_tokens": 4097},
        {"completed_at_s": True},
        {"completed_at_s": float("nan")},
        {"completed_at_s": 9},
        {"completed_at_s": 14},
        {"inference_latency_ms": False},
        {"inference_latency_ms": float("inf")},
        {"inference_latency_ms": -1},
        {"inference_latency_ms": 1000.1},
    ],
)
def test_result_rejects_invalid_counts_ids_cap_time_and_identities(changes):
    with pytest.raises((AutonomyContractError, TypeError)):
        _result(**changes)


def test_result_rejects_wrong_request_type_before_accessing_its_fields():
    with pytest.raises(TypeError):
        replace(_result(), request=object())


def test_result_requires_nonexhausted_request_specific_cap():
    request = _request(max_new_tokens=4)
    _result(request)
    with pytest.raises(AutonomyContractError):
        _result(request, generated_token_ids=(1, 2, 3, 4), generated_tokens=4)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "translate", "translation_m": None},
        {"kind": "turn_yaw", "translation_m": None, "yaw_delta_rad": None},
        {"kind": "stop", "translation_m": None},
    ],
)
def test_unknown_and_stop_are_parsed_not_fabricated_into_execution(changes):
    result = _result(raw_output=canonical_json(_target(**changes)).encode("utf-8"))
    assert result.target.metric_known is False
    assert result.to_mapping()["execution_authority"] is False
    assert result.to_mapping()["completion_evidence_created"] is False


@pytest.mark.parametrize("case", ["scope", "expired", "future_completion", "stale_frame"])
def test_result_current_admission_rechecks_scope_and_actual_host_time(case):
    result = _result()
    scope, now = result.request.scope, 11.0
    if case == "scope":
        scope = replace(scope, clock_epoch="another-host-epoch")
    elif case == "expired":
        now = 14
    elif case == "future_completion":
        now = 10.5
    else:
        result = _result(_request(max_frame_age_s=2.5))
        now = 12
    with pytest.raises(AutonomyContractError):
        result.validate_binding(current_scope=scope, now_s=now)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("generated_token_ids", (11, 12, 13)),
        ("prefix_tokens", 401),
        ("completed_at_s", 11.1),
        ("inference_latency_ms", 800),
        ("raw_output", canonical_json(_target(translation_m=[4, 0, 0])).encode("utf-8")),
        ("target", PlannedNavigationTarget("stop")),
    ],
)
def test_result_detects_postconstruction_mutation(field_name, value):
    result = _result()
    object.__setattr__(result, field_name, value)
    with pytest.raises(AutonomyContractError):
        result.validate_binding(current_scope=result.request.scope, now_s=11.5)


def test_response_factory_binds_original_text_bytes_without_canonicalizing_them():
    request = _request()
    values = _result(request)
    payload = json.dumps(_target(), indent=2) + "\n"
    result = parse_navigation_inference_response(
        payload,
        request,
        current_scope=request.scope,
        generation=request.generation,
        request_sha256=request.canonical_hash,
        generated_token_ids=values.generated_token_ids,
        generated_tokens=values.generated_tokens,
        prefix_tokens=values.prefix_tokens,
        completed_at_s=values.completed_at_s,
        inference_latency_ms=values.inference_latency_ms,
        model_sha256=request.model_sha256,
        adapter_sha256=request.adapter_sha256,
        adapter_config_sha256=request.adapter_config_sha256,
    )
    assert result.raw_output == payload.encode("utf-8")
    assert result.raw_output_sha256 != values.raw_output_sha256
    assert result.target == values.target


def test_result_and_request_mappings_are_defensive_and_dataclasses_frozen():
    result = _result()
    mapping = result.to_mapping()
    mapping["target"]["translation_m"][0] = 99
    mapping["generated_token_ids"][0] = 99
    outer = result.request.to_mapping()
    outer["source_frames"][0]["frame_id"] = "changed"
    result.validate_binding(current_scope=result.request.scope, now_s=11)
    assert result.target.translation_m[0] == 3
    assert result.generated_token_ids[0] == 10
    with pytest.raises(FrozenInstanceError):
        result.generated_tokens = 0
    with pytest.raises(FrozenInstanceError):
        result.request.generation = 9


def test_raw_surrogate_text_is_rejected_as_a_contract_error():
    with pytest.raises(AutonomyContractError):
        parse_navigation_target("\ud800")


def test_module_import_does_not_load_optional_model_or_gpu_libraries():
    """Fresh guarded import, not a mocked processor or model acceptance test."""
    src = Path(__file__).resolve().parents[1] / "src"
    probe = """
import builtins
import sys
original = builtins.__import__
denied = {'torch', 'transformers', 'peft', 'safetensors', 'datasets', 'cuda'}
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in denied:
        raise AssertionError('unexpected heavy import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.path.insert(0, sys.argv[1])
import multidetect.autonomy.onboard_navigation_inference
assert not denied.intersection(sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(src)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

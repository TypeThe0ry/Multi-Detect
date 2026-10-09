"""CPU wire fixtures are not live simulator, semantic or flight acceptance."""

from __future__ import annotations

import hashlib
import io
import json
import pickle
from dataclasses import FrozenInstanceError, replace

import pytest
from test_onboard_vlm_runtime_contract import _input, _reply

from multidetect.autonomy.onboard_live_vlm_contract import (
    LIVE_INPUT_CONTRACT,
    LIVE_REQUEST_CONTRACT,
    OnboardLiveMultimodalInput,
    OnboardLiveVlmRequest,
    gazebo_sensor_fact_json,
    serialize_onboard_live_vlm_messages,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
    serialize_onboard_vlm_messages,
)
from multidetect.autonomy.serialization import canonical_json
from multidetect.backends.gazebo_sim import (
    GazeboSimFrameProvenance,
    GazeboSimFrameTelemetryProvenance,
    GazeboSimTelemetryProvider,
    GazeboSimWireTelemetryProvenance,
    read_gazebo_sim_wire_frame,
)
from scripts.gazebo_sensor_stream import build_wire_frame


def _live_input(*, latitude=47.3, derived=True):
    original = _input()
    facts = []
    for sequence, frame in enumerate(original.frames):
        bgr = bytearray(frame.image_bytes)
        bgr[0::3], bgr[2::3] = frame.image_bytes[2::3], frame.image_bytes[0::3]
        stamp = (sequence + 1) * 1_000_000_000
        record = build_wire_frame(
            sequence=sequence,
            sensor_stamp_ns=stamp,
            width=frame.width,
            height=frame.height,
            camera_info_stamp_ns=stamp - 3,
            navsat_stamp_ns=stamp - 2,
            odometry_stamp_ns=stamp - 1,
            latitude_deg=latitude,
            longitude_deg=8.5,
            altitude_m=488.2,
            position_east_m=1.0,
            position_north_m=2.0,
            position_up_m=3.0,
            orientation_x=0.0,
            orientation_y=0.0,
            orientation_z=0.0,
            orientation_w=1.0,
            velocity_forward_mps=4.0,
            velocity_left_mps=5.0,
            velocity_up_mps=6.0,
            attitude_covariance_enu_rad2=(0.0001, 0.0, 0.0, 0.0, 0.0004, 0.0, 0.0, 0.0, 0.0009),
            bgr_payload=bytes(bgr),
        )
        wire = read_gazebo_sim_wire_frame(
            io.BytesIO(record),
            expected_sequence=sequence,
            expected_width=frame.width,
            expected_height=frame.height,
            previous_sensor_stamp_ns=None,
        )
        provider = GazeboSimTelemetryProvider()
        provider.update(wire, observed_at_s=frame.captured_at_s)
        provenance = GazeboSimFrameProvenance(
            frame_id=frame.frame_id,
            sequence=sequence,
            captured_at_s=frame.captured_at_s,
            captured_at_clock="host_monotonic",
            sensor_stamp_ns=stamp,
            camera_info_stamp_ns=stamp - 3,
            navsat_stamp_ns=stamp - 2,
            odometry_stamp_ns=stamp - 1,
            camera_info_source_skew_ns=3,
            navsat_source_skew_ns=2,
            odometry_source_skew_ns=1,
            record_sha256=wire.record_sha256,
            payload_sha256=hashlib.sha256(bgr).hexdigest(),
            width=frame.width,
            height=frame.height,
            stride=frame.width * 3,
            payload_bytes=len(bgr),
            flags=wire.flags,
            pixel_format="BGR24",
            wire_unsigned_header_hex=wire.unsigned_header.hex(),
            wire_telemetry=GazeboSimWireTelemetryProvenance.from_wire(wire),
            frame_telemetry=GazeboSimFrameTelemetryProvenance.from_telemetry(
                provider.snapshot(now_s=frame.captured_at_s)
            )
            if derived
            else None,
        )
        facts.append(gazebo_sensor_fact_json(frame, provenance))
    return OnboardLiveMultimodalInput(
        original.scope,
        original.prompt,
        original.frames,
        original.ledger,
        original.requested_at_s,
        original.deadline_at_s,
        original.max_frame_age_s,
        original.max_gap_s,
        original.prompt_text_original,
        factual_sensor_state_json=tuple(facts),
    )


def _changed(value, mutate):
    facts = [json.loads(item) for item in value.factual_sensor_state_json]
    mutate(facts)
    return replace(value, factual_sensor_state_json=tuple(canonical_json(item) for item in facts))


def test_wire_bound_live_input_and_request_have_distinct_hash_contracts():
    value = _live_input()
    other = _live_input(latitude=48.0)
    assert value.frames == other.frames
    assert value.canonical_hash != other.canonical_hash
    request = OnboardLiveVlmRequest(value)
    assert request.input_sha256 == value.canonical_hash
    assert request.canonical_hash != OnboardLiveVlmRequest(other).canonical_hash
    assert value.to_mapping()["contract"] == LIVE_INPUT_CONTRACT
    assert request.to_mapping()["contract"] == LIVE_REQUEST_CONTRACT
    assert (
        request.to_mapping()["factual_sensor_state"] == value.to_mapping()["factual_sensor_state"]
    )
    assert request.to_mapping()["execution_authority"] is False
    request.validate_binding()
    pickle.loads(pickle.dumps(request)).validate_binding()


def test_actual_rgb_channel_swap_is_not_equal_to_bgr_hash():
    value = _live_input()
    provenance = json.loads(value.factual_sensor_state_json[0])["source_provenance"]
    assert value.frames[0].image_sha256 != provenance["payload_sha256"]
    OnboardLiveVlmRequest(value).validate_binding()


def test_partial_messages_use_real_gps_odom_verbatim_prompt_but_keep_safety_unknown():
    request = OnboardLiveVlmRequest(_live_input())
    messages = serialize_onboard_live_vlm_messages(request)
    partial = json.loads(messages[-1]["content"][-1]["text"])
    obs = partial["partial_factual_sensor_observations"][0]
    assert obs["measured_wire_gps_and_odometry"]["latitude_deg"] == 47.3
    assert obs["measured_wire_gps_and_odometry"]["position_up_m"] == 3.0
    assert obs["source_timestamps_ns"]["navsat_stamp_ns"] == 999999998
    assert obs["host_received_at_s"] == 1.0
    assert all(value is None for value in partial["navigation_safety_context"].values())
    assert "gps_fix_type" not in canonical_json(partial)
    assert "frame_telemetry" not in canonical_json(partial)
    assert "wire_unsigned_header_hex" not in canonical_json(partial)
    assert "world_truth" not in canonical_json(partial)
    assert partial["execution_authority"] is False
    texts = [item["text"] for item in messages[-1]["content"] if item["type"] == "text"]
    assert (
        "Original mission prompt (verbatim):\n" + request.model_input.prompt_text_original in texts
    )
    # Frozen v2 still has no live partial observation block.
    legacy = serialize_onboard_vlm_messages(OnboardVlmRuntimeRequest(_input()))
    assert not any(
        "partial_factual_sensor_observations" in item.get("text", "")
        for item in legacy[-1]["content"]
    )


@pytest.mark.parametrize("where", ["envelope", "provenance", "preprocessing", "wire", "derived"])
@pytest.mark.parametrize(
    "key", ["world_truth", "labels", "wind_north_mps", "home_position", "position_healthy"]
)
def test_unobserved_fields_or_truth_cannot_enter_any_fact_layer(where, key):
    def mutate(facts):
        fact = facts[0]
        destination = {
            "envelope": fact,
            "provenance": fact["source_provenance"],
            "preprocessing": fact["preprocessing"],
            "wire": fact["source_provenance"]["wire_telemetry"],
            "derived": fact["source_provenance"]["frame_telemetry"],
        }[where]
        destination[key] = True

    with pytest.raises(ValueError):
        _changed(_live_input(), mutate)


@pytest.mark.parametrize(
    "key,value", [("frame_id", "other"), ("rgb_sha256", "a" * 64), ("clock_epoch", "other-clock")]
)
def test_frame_rgb_and_clock_are_exact(key, value):
    with pytest.raises(ValueError):
        _changed(_live_input(), lambda facts: facts[0].__setitem__(key, value))


@pytest.mark.parametrize(
    "key,value",
    [
        ("frame_id", "other"),
        ("captured_at_s", 1.01),
        ("width", 9),
        ("stride", 25),
        ("payload_bytes", 1),
        ("pixel_format", "RGB24"),
        ("payload_sha256", "a" * 64),
        ("record_sha256", "a" * 64),
        ("navsat_stamp_ns", 1),
        ("navsat_source_skew_ns", 1),
        ("wire_unsigned_header_hex", "00"),
        ("flags", True),
        ("sequence", True),
        ("captured_at_clock", "utc"),
    ],
)
def test_source_metadata_cannot_disagree_with_wire(key, value):
    with pytest.raises((ValueError, RuntimeError)):
        _changed(_live_input(), lambda facts: facts[0]["source_provenance"].__setitem__(key, value))


def test_rehashed_json_gps_does_not_replace_original_header():
    with pytest.raises(ValueError, match="byte-bound"):
        _changed(
            _live_input(),
            lambda f: f[0]["source_provenance"]["wire_telemetry"].__setitem__("latitude_deg", 48.0),
        )


def test_derived_telemetry_time_cannot_be_freshened():
    with pytest.raises(ValueError, match="derived telemetry"):
        _changed(
            _live_input(),
            lambda f: f[0]["source_provenance"]["frame_telemetry"].__setitem__(
                "gps_observed_at_s", 1.0
            ),
        )


def test_original_rgb_bytes_must_reconstruct_wire_payload():
    value = _live_input()
    frame = replace(
        value.frames[0], image_bytes=b"z" * 144, image_sha256=hashlib.sha256(b"z" * 144).hexdigest()
    )
    frames = (frame, value.frames[1])
    ledger = replace(value.ledger, frames=frames)
    facts = [json.loads(x) for x in value.factual_sensor_state_json]
    facts[0]["rgb_sha256"] = frame.image_sha256
    facts[0]["preprocessing"]["rgb_sha256"] = frame.image_sha256
    with pytest.raises(ValueError, match="actual RGB"):
        replace(
            value,
            frames=frames,
            ledger=ledger,
            factual_sensor_state_json=tuple(canonical_json(x) for x in facts),
        )


def test_exact_canonical_json_no_duplicates_no_overflow_and_tuple_required():
    value = _live_input()
    text = value.factual_sensor_state_json[0]
    for bad in (
        " " + text,
        text.replace('"frame_id":', '"frame_id":"duplicate","frame_id":', 1),
        text.replace("47.3", "1e999"),
        text.replace("47.3", "NaN"),
    ):
        with pytest.raises(ValueError):
            replace(value, factual_sensor_state_json=(bad, value.factual_sensor_state_json[1]))
    for bad in ([], list(value.factual_sensor_state_json), (), (text,)):
        with pytest.raises(ValueError):
            replace(value, factual_sensor_state_json=bad)


def test_history_cannot_mix_clock_identity_or_reverse_wire_sequence():
    value = _live_input()
    identity = {
        "schema_version": 1,
        "source": "python.perf_counter",
        "session_id": "fixture-only",
        "implementation": "fixture",
        "resolution_ns": 1,
        "accuracy_verified": False,
        "legacy_monotonic_equivalent": False,
    }
    with pytest.raises(ValueError, match="clock identity"):
        _changed(
            value,
            lambda f: f[1]["source_provenance"].update(
                captured_at_clock="host_perf_counter", host_clock_identity=identity
            ),
        )
    with pytest.raises((ValueError, RuntimeError)):
        _changed(value, lambda f: f[1]["source_provenance"].__setitem__("sequence", 0))


def test_full_context_and_non_rgb8_are_not_promoted_from_partial_state():
    with pytest.raises(TypeError):
        OnboardLiveVlmRequest(_input())
    known = _input(known=True)
    with pytest.raises(ValueError, match="full context unknown"):
        replace(_live_input(), scope=known.scope, frames=known.frames, ledger=known.ledger)


def test_immutable_request_detects_malicious_facts_replacement_and_mapping_has_no_alias():
    request = OnboardLiveVlmRequest(_live_input())
    export = request.to_mapping()
    export["factual_sensor_state"][0]["source_provenance"]["wire_telemetry"]["latitude_deg"] = 9
    request.validate_binding()
    with pytest.raises(FrozenInstanceError):
        request.model_input.factual_sensor_state_json = ()
    object.__setattr__(
        request.model_input,
        "factual_sensor_state_json",
        _live_input(latitude=48.0).factual_sensor_state_json,
    )
    with pytest.raises(ValueError, match="identity changed"):
        request.validate_binding()


def test_v2_response_parser_binds_live_request_without_model_hash_copy():
    request = OnboardLiveVlmRequest(_live_input())
    response = parse_onboard_vlm_runtime_response(
        json.dumps(_reply()),
        request,
        current_scope=request.model_input.scope,
        completed_at_s=2.1,
        inference_latency_ms=50.0,
    )
    assert response.input_sha256 == request.input_sha256
    assert response.request_sha256 == request.canonical_hash
    assert response.to_mapping()["execution_authority"] is False


@pytest.mark.parametrize(
    "where,key",
    [
        ("wire_telemetry", "position_east_m"),
        ("wire_telemetry", "orientation_w"),
        ("frame_telemetry", "local_east_m"),
        ("frame_telemetry", "gps_fix_type"),
    ],
)
def test_bool_is_not_a_source_number_even_if_python_equality_would_pass(where, key):
    with pytest.raises(ValueError):
        _changed(_live_input(), lambda f: f[0]["source_provenance"][where].__setitem__(key, True))


@pytest.mark.parametrize(
    "key", ["roll_deg", "heading_deg", "velocity_north_mps", "velocity_east_mps"]
)
def test_derived_orientation_and_velocity_cannot_replace_wire_measurements(key):
    with pytest.raises(ValueError, match="derived telemetry"):
        _changed(
            _live_input(),
            lambda f: f[0]["source_provenance"]["frame_telemetry"].__setitem__(key, 123.0),
        )


def test_positive_host_perf_counter_identity_and_optional_derived_absence():
    value = _live_input(derived=False)
    identity = {
        "schema_version": 1,
        "source": "python.perf_counter",
        "session_id": "fixture-only",
        "implementation": "fixture",
        "resolution_ns": 1,
        "accuracy_verified": False,
        "legacy_monotonic_equivalent": False,
    }

    def update(facts):
        for item in facts:
            item["source_provenance"].update(
                captured_at_clock="host_perf_counter", host_clock_identity=identity
            )

    request = OnboardLiveVlmRequest(_changed(value, update))
    request.validate_binding()
    metadata = json.loads(serialize_onboard_live_vlm_messages(request)[-1]["content"][-1]["text"])
    assert (
        metadata["partial_factual_sensor_observations"][0]["host_clock_domain"]
        == "host_perf_counter"
    )


@pytest.mark.parametrize("key", ["wire_telemetry", "wire_unsigned_header_hex"])
def test_source_header_and_telemetry_are_mandatory(key):
    with pytest.raises(ValueError):
        _changed(_live_input(), lambda f: f[0]["source_provenance"].pop(key))


def test_encoded_images_cannot_assert_lossless_original_rgb8_provenance():
    from PIL import Image

    value = _live_input()
    frame = value.frames[0]
    stream = io.BytesIO()
    Image.frombytes("RGB", (frame.width, frame.height), frame.image_bytes).save(
        stream, format="PNG"
    )
    payload = stream.getvalue()
    encoded = replace(
        frame, image_bytes=payload, image_sha256=hashlib.sha256(payload).hexdigest(), encoding="png"
    )
    frames = (encoded, value.frames[1])
    facts = [json.loads(x) for x in value.factual_sensor_state_json]
    facts[0]["rgb_sha256"] = encoded.image_sha256
    facts[0]["preprocessing"]["rgb_sha256"] = encoded.image_sha256
    with pytest.raises(ValueError, match="original RGB8"):
        replace(
            value,
            frames=frames,
            ledger=replace(value.ledger, frames=frames),
            factual_sensor_state_json=tuple(canonical_json(x) for x in facts),
        )

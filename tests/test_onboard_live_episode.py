"""Receive/record fixtures, never a physical flight or expert action dataset."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

import pytest
from test_onboard_evidence_collector import VEHICLE_HASH, _journal, _observation, _setup
from test_onboard_vlm_provider import _raw, _source

from multidetect.autonomy.onboard_continuation_binding import (
    RECEIVE_CONTRACT,
    ContinuationReceivedFact,
)
from multidetect.autonomy.onboard_evidence_collector import OnboardEvidenceCollector
from multidetect.autonomy.onboard_live_episode import (
    OnboardLiveEpisodeError,
    OnboardLiveEpisodeRecorder,
)
from multidetect.autonomy.onboard_multimodal import (
    MissionEvidenceLedger,
    OnboardImageFrame,
    OnboardMultimodalInput,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
)
from multidetect.autonomy.serialization import AutonomyContractError, canonical_json


def _bytes(document):
    return canonical_json(document).encode()


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _create(tmp_path, request=None, *, original=None):
    request = request or _source()[0]
    return OnboardLiveEpisodeRecorder.create(
        tmp_path / "episode",
        prompt=request.model_input.prompt,
        prompt_text_original=(
            request.model_input.to_mapping()["prompt_original_text"]
            if original is None
            else original
        ),
        scope=request.model_input.scope,
        host_clock_id="host-monotonic-fixture",
        created_at_s=min(9.0, request.model_input.frames[0].captured_at_s),
    )


def _record_frame(recorder, frame, received=None, **kwargs):
    return recorder.record_observation(
        frame,
        received_at_s=frame.captured_at_s if received is None else received,
        source_clock_id=kwargs.pop("source_clock_id", "sensor-fixture-boot"),
        **kwargs,
    )


def _events(recorder):
    return [
        json.loads(path.read_bytes())
        for path in sorted((recorder.output_dir / "events").glob("*.json"))
    ]


def _read(recorder, record):
    payload = (recorder.output_dir / record["path"]).read_bytes()
    assert len(payload) == record["bytes"]
    assert _sha(payload) == record["sha256"]
    return payload


def _shadow(tmp_path):
    request = _source()[0]
    recorder = _create(tmp_path, request)
    _record_frame(recorder, request.model_input.frames[0])
    recorder.record_request(request, submitted_at_s=10.0)
    return recorder, request


def _receiving(tmp_path):
    _, request, raw, trace = _setup()
    collector = OnboardEvidenceCollector(
        request.model_input.scope,
        expected_system_id=1,
        expected_component_id=1,
        vehicle_identity_sha256=VEHICLE_HASH,
    )
    collector.add_observation(request.model_input.frames[0])
    recorder = _create(tmp_path, request)
    _record_frame(recorder, request.model_input.frames[0])
    recorder.record_request(request, submitted_at_s=10.0, collector=collector)
    recorder.record_decision(
        request.canonical_hash, raw, completed_at_s=10.05, inference_latency_ms=50
    )
    return recorder, request, trace, collector


def _bound(tmp_path):
    recorder, request, trace, collector = _receiving(tmp_path)
    recorder.record_acceptance(trace)
    binding = _journal(tmp_path, trace)
    recorder.bind_vehicle_mission(trace.proposal_id, binding, observed_at_s=10.07)
    return recorder, request, trace, binding, collector


def test_actual_source_raw_typed_bytes_chain_successor_unknown_and_create_only(tmp_path):
    request = _source()[0]
    original = "\n  " + request.model_input.prompt.text + " \n"
    request = OnboardVlmRuntimeRequest(replace(request.model_input, prompt_text_original=original))
    recorder = _create(tmp_path, request, original=original)
    frame = request.model_input.frames[0]
    provenance = b'{"original_sim_time_ns":1234567890123456789,"depth":null}'
    _record_frame(recorder, frame, source_provenance=provenance)
    recorder.record_request(request, submitted_at_s=10)
    raw = _raw(request)
    response = recorder.record_decision(
        request.canonical_hash, raw, completed_at_s=10.05, inference_latency_ms=50
    )
    successor = replace(
        frame,
        frame_id="next",
        captured_at_s=10.1,
        context=replace(frame.context, captured_at_s=10.1, snapshot_id="next"),
    )
    _record_frame(recorder, successor)
    seal = recorder.close(status="closed", reason="real input recording fixture", closed_at_s=10.2)
    header = json.loads((recorder.output_dir / "episode.json").read_bytes())
    assert header["prompt_text_original"] == original
    assert header["prompt_original_utf8_sha256"] == _sha(original.encode())
    events = _events(recorder)
    assert [event["sequence"] for event in events] == list(range(4))
    for previous, current in zip(events, events[1:], strict=False):
        assert current["previous_event_sha256"] == _sha(_bytes(previous) + b"\n")
    assert _read(recorder, events[0]["data"]["rgb_original"]) == frame.image_bytes
    assert _read(recorder, events[0]["data"]["source_provenance_original"]) == provenance
    assert _read(recorder, events[2]["data"]["raw_response_original"]) == raw.encode()
    assert events[2]["data"]["response"] == response.to_mapping()
    assert events[3]["preceding_decision_request_sha256"] == request.canonical_hash
    assert events[3]["data"]["achieved_action"] is None
    sealed = json.loads(_read(recorder, seal))
    assert sealed["stage_counts"] == {"proposed": 1, "accepted": 0, "executed": 0, "completed": 0}
    assert sealed["action_status"] == "unknown"
    assert sealed["training_ready"] is sealed["expert_action_labels_created"] is False
    before = (recorder.output_dir / "episode.json").read_bytes()
    with pytest.raises(FileExistsError):
        _create(tmp_path, request, original=original)
    assert (recorder.output_dir / "episode.json").read_bytes() == before
    with pytest.raises(OnboardLiveEpisodeError, match="closed"):
        _record_frame(
            recorder, replace(successor, frame_id="after-close", captured_at_s=11, context=None)
        )
    with pytest.raises(OnboardLiveEpisodeError, match="sealed"):
        recorder.close(status="closed", reason="duplicate", closed_at_s=11)


def test_eight_large_actual_rgb_windows_remain_one_episode_over_collector_byte_cap(tmp_path):
    first = _source()[0]
    recorder = _create(tmp_path, first)
    for index in range(8):
        stamp = 10.0 + index
        pixels = bytes([index, index + 1, index + 2]) * (1280 * 960)
        frame = OnboardImageFrame(
            first.model_input.scope,
            f"large-{index}",
            stamp,
            pixels,
            _sha(pixels),
            1280,
            960,
            context=None,
        )
        _record_frame(recorder, frame)
        model_input = OnboardMultimodalInput(
            frame.scope,
            first.model_input.prompt,
            (frame,),
            MissionEvidenceLedger(frame.scope, (frame,)),
            stamp,
            stamp + 1,
        )
        request = OnboardVlmRuntimeRequest(model_input)
        recorder.record_request(request, submitted_at_s=stamp)
        recorder.record_decision(
            request.canonical_hash,
            _raw(request),
            completed_at_s=stamp + 0.05,
            inference_latency_ms=50,
        )
    seal = json.loads(
        _read(
            recorder, recorder.close(status="closed", reason="eight real windows", closed_at_s=18)
        )
    )
    assert seal["observation_count"] == seal["request_count"] == 8
    assert (
        sum(record["bytes"] for record in seal["files"] if record["path"].endswith(".bin"))
        > 16 * 1024 * 1024
    )
    assert len(_events(recorder)) == 24
    assert not any(hasattr(identity, "image_bytes") for identity in recorder._frames.values())
    assert all(
        event["scope_sha256"] == first.model_input.scope.canonical_hash
        for event in _events(recorder)
    )
    assert all(
        event["data"]["frame"]["context"] is None
        for event in _events(recorder)
        if event["kind"] == "observation"
    )


@pytest.mark.parametrize(
    "part", ["frame-id", "pixels", "state", "scope", "time", "prompt", "unrecorded"]
)
def test_foreign_relabelled_or_unrecorded_request_rejected(tmp_path, part):
    request = _source()[0]
    recorder = _create(tmp_path, request)
    frame = request.model_input.frames[0]
    _record_frame(recorder, frame)
    if part == "frame-id":
        frame = replace(frame, frame_id="foreign")
    elif part == "pixels":
        frame = replace(frame, image_bytes=b"abc", image_sha256=_sha(b"abc"))
    elif part == "state":
        frame = replace(frame, context=replace(frame.context, battery_remaining_pct=79))
    elif part == "scope":
        frame = replace(frame, scope=replace(frame.scope, clock_epoch="different"))
    elif part == "time":
        frame = replace(
            frame, captured_at_s=10.01, context=replace(frame.context, captured_at_s=10.01)
        )
    elif part == "prompt":
        request = OnboardVlmRuntimeRequest(
            replace(request.model_input, prompt_text_original=" " + request.model_input.prompt.text)
        )
    else:
        frame = replace(frame, frame_id="not-stored", context=None)
    if part != "prompt":
        request = OnboardVlmRuntimeRequest(
            replace(
                request.model_input,
                scope=frame.scope,
                frames=(frame,),
                ledger=MissionEvidenceLedger(frame.scope, (frame,)),
                requested_at_s=frame.captured_at_s,
            )
        )
    with pytest.raises(AutonomyContractError):
        recorder.record_request(request, submitted_at_s=10.02)
    assert len(_events(recorder)) == 1


@pytest.mark.parametrize(
    "kind", ["duplicate", "conflict", "scope", "host-back", "source-back", "future", "capture-back"]
)
def test_observation_order_clock_duplicate_conflict_and_scope_are_fail_closed(tmp_path, kind):
    request = _source()[0]
    recorder = _create(tmp_path, request)
    frame = request.model_input.frames[0]
    _record_frame(recorder, frame, received=10.02, source_timestamp_s=1)
    later = replace(
        frame,
        frame_id="later",
        captured_at_s=10.01,
        context=replace(frame.context, captured_at_s=10.01),
    )
    host, source = 10.03, 1.01
    if kind == "duplicate":
        later = frame
    elif kind == "conflict":
        later = replace(later, frame_id=frame.frame_id)
    elif kind == "scope":
        later = replace(later, scope=replace(frame.scope, clock_epoch="foreign"))
    elif kind == "host-back":
        host = 10.01
    elif kind == "source-back":
        source = 0.99
    elif kind == "future":
        host = 10.025
        later = replace(
            later, captured_at_s=10.04, context=replace(later.context, captured_at_s=10.04)
        )
    else:
        later = replace(
            later, captured_at_s=9.99, context=replace(later.context, captured_at_s=9.99)
        )
    with pytest.raises(AutonomyContractError):
        _record_frame(recorder, later, received=host, source_timestamp_s=source)
    assert len(_events(recorder)) == 1


def test_sensor_and_processing_clock_domains_not_mixed_and_queue_delay_not_backdated(tmp_path):
    request = _source()[0]
    recorder = _create(tmp_path, request)
    frame = request.model_input.frames[0]
    _record_frame(recorder, frame, received=10.02, source_timestamp_s=500000)
    later = replace(
        frame,
        frame_id="later",
        captured_at_s=10.01,
        context=replace(frame.context, captured_at_s=10.01),
    )
    _record_frame(recorder, later, received=10.03, source_timestamp_s=500000.01)
    event = _events(recorder)[1]
    assert event["host_observed_at_s"] == 10.03
    assert event["data"]["frame"]["captured_at_s"] == 10.01
    assert event["data"]["source_timestamp_s"] == 500000.01
    assert event["source_clock_mapping_proven"] is False


@pytest.mark.parametrize("raw", [b"{}", b'{"x":1,"x":2}', b'{"x":NaN}', b"not-json"])
def test_failed_raw_decisions_remain_original_failed_partial_not_executed(tmp_path, raw):
    recorder, request = _shadow(tmp_path)
    with pytest.raises(AutonomyContractError):
        recorder.record_decision(
            request.canonical_hash, raw, completed_at_s=10.05, inference_latency_ms=50
        )
    event = _events(recorder)[-1]
    assert event["kind"] == "decision_rejected"
    assert _read(recorder, event["data"]["raw_response_original"]) == raw
    assert event["data"]["typed_output"] is None
    seal = json.loads(
        _read(
            recorder,
            recorder.close(
                status="failed_partial", reason="strict parser rejected raw", closed_at_s=10.1
            ),
        )
    )
    assert seal["stage_counts"]["executed"] == seal["stage_counts"]["proposed"] == 0
    assert seal["action_status"] == "unknown"


def test_missing_result_is_pending_not_fake_hold_or_action(tmp_path):
    recorder, request = _shadow(tmp_path)
    seal = json.loads(
        _read(
            recorder,
            recorder.close(status="failed_partial", reason="worker unavailable", closed_at_s=10.1),
        )
    )
    assert seal["pending_request_sha256"] == [request.canonical_hash]
    assert seal["stage_counts"]["proposed"] == 0
    assert not any(event["kind"].startswith("decision") for event in _events(recorder))


def test_duplicate_request_and_decision_rejected_once_only(tmp_path):
    recorder, request = _shadow(tmp_path)
    with pytest.raises(OnboardLiveEpisodeError, match="duplicate request"):
        recorder.record_request(request, submitted_at_s=10)
    raw = _raw(request)
    recorder.record_decision(
        request.canonical_hash, raw, completed_at_s=10.05, inference_latency_ms=50
    )
    with pytest.raises(OnboardLiveEpisodeError, match="duplicated"):
        recorder.record_decision(
            request.canonical_hash, raw, completed_at_s=10.05, inference_latency_ms=50
        )
    assert len(_events(recorder)) == 3


def test_no_collector_ack_boolean_or_claimed_completed_cannot_upgrade_shadow(tmp_path):
    recorder, request = _shadow(tmp_path)
    recorder.record_decision(
        request.canonical_hash, _raw(request), completed_at_s=10.05, inference_latency_ms=50
    )
    _, _, _, trace = _setup()
    with pytest.raises(OnboardLiveEpisodeError, match="collector"):
        recorder.record_acceptance(replace(trace, proposal_id=request.canonical_hash))
    with pytest.raises(TypeError):
        recorder.record_acceptance({"ack": True, "completed": True})
    assert not hasattr(recorder, "record_execution")
    assert not hasattr(recorder, "record_completion")


def test_typed_response_reparsed_original_drift_rejected_and_stored(tmp_path):
    recorder, request = _shadow(tmp_path)
    typed = parse_onboard_vlm_runtime_response(
        _raw(request),
        request,
        current_scope=recorder.scope,
        completed_at_s=10.05,
        inference_latency_ms=50,
    )
    typed = replace(typed, input_sha256="b" * 64)
    with pytest.raises(OnboardLiveEpisodeError, match="typed decision differs"):
        recorder.record_decision(
            request.canonical_hash, typed, completed_at_s=10.05, inference_latency_ms=50
        )
    assert _events(recorder)[-1]["kind"] == "decision_rejected"


def test_actual_typed_journal_ack_and_acceptance_are_not_execution(tmp_path):
    recorder, _, _, binding, _ = _bound(tmp_path)
    journal_original = binding.journal_path.read_bytes()
    events = _events(recorder)
    assert events[-1]["kind"] == "vehicle_mission_bound"
    assert _read(recorder, events[-1]["data"]["journal_original"]) == journal_original
    seal = json.loads(
        _read(
            recorder,
            recorder.close(status="closed", reason="no vehicle feedback", closed_at_s=10.08),
        )
    )
    assert seal["stage_counts"] == {"proposed": 1, "accepted": 1, "executed": 0, "completed": 0}
    assert seal["action_status"] == "unknown"


def test_real_typed_receive_fixture_lifecycle_only_existing_monitor_can_upgrade(tmp_path):
    recorder, _, trace, binding, _ = _bound(tmp_path)
    expected = [
        (10.10, "HEARTBEAT", {"base_mode": 129, "custom_mode": 67371008}, None),
        (10.11, "EXTENDED_SYS_STATE", {"landed_state": 2}, None),
        (10.12, "MISSION_CURRENT", {"seq": 0}, "executed"),
        (10.20, "MISSION_CURRENT", {"seq": 2}, None),
        (10.21, "MISSION_ITEM_REACHED", {"seq": 2}, None),
        (10.22, "HEARTBEAT", {"base_mode": 1, "custom_mode": 67371008}, None),
        (10.23, "EXTENDED_SYS_STATE", {"landed_state": 1}, "completed"),
    ]
    for stamp, kind, fields, stage in expected:
        observation = _observation(binding, stamp, kind, **fields)
        receipt = recorder.record_vehicle_feedback(
            trace.proposal_id, observation, now_s=stamp + 0.02
        )
        assert (None if receipt is None else receipt.stage) == stage
        event = _events(recorder)[-1]
        assert (
            _read(recorder, event["data"]["source_receipt_original"])
            == observation.source_receipt_bytes
        )
        assert event["data"]["source_observed_at_s"] == stamp
        assert event["host_observed_at_s"] == stamp + 0.02
    seal = json.loads(
        _read(
            recorder,
            recorder.close(
                status="closed", reason="receive-only fixture finished", closed_at_s=10.3
            ),
        )
    )
    assert seal["stage_counts"] == {"proposed": 1, "accepted": 1, "executed": 1, "completed": 1}
    assert seal["autonomous_flight_verified"] is seal["training_ready"] is False
    assert all(event["expert_action_labels_created"] is False for event in _events(recorder))


@pytest.mark.parametrize("failure", ["foreign", "stale", "regression", "duplicate"])
def test_receive_failed_partial_does_not_generate_action(tmp_path, failure):
    recorder, _, trace, binding, _ = _bound(tmp_path)
    first = _observation(binding, 10.10, "HEARTBEAT", base_mode=129, custom_mode=67371008)
    recorder.record_vehicle_feedback(trace.proposal_id, first, now_s=10.12)
    observation = _observation(binding, 10.11, "EXTENDED_SYS_STATE", landed_state=2)
    now = 10.13
    if failure == "foreign":
        raw = observation.to_mapping()
        raw["source_system_id"] = 2
        observation = type(observation)(_bytes(raw), _sha(_bytes(raw)))
    elif failure == "stale":
        now = 12
    elif failure == "regression":
        observation = _observation(binding, 10.09, "EXTENDED_SYS_STATE", landed_state=2)
    else:
        observation = first
    with pytest.raises(ValueError):
        recorder.record_vehicle_feedback(trace.proposal_id, observation, now_s=now)
    seal = json.loads(
        _read(
            recorder,
            recorder.close(
                status="failed_partial", reason="actual receive gate rejected", closed_at_s=12.1
            ),
        )
    )
    assert seal["stage_counts"]["executed"] == seal["stage_counts"]["completed"] == 0


@pytest.mark.parametrize("file", ["episode.json", "events/00000000.json", "rgb"])
def test_original_file_tampering_never_seals_green(tmp_path, file):
    recorder, request = _shadow(tmp_path)
    relative = file if file != "rgb" else f"blobs/{request.model_input.frames[0].image_sha256}.bin"
    path = recorder.output_dir / relative
    path.write_bytes(
        b"tampered-original"
    )  # Deliberate adversarial fixture, not production editing.
    with pytest.raises(OnboardLiveEpisodeError, match="bytes changed"):
        recorder.close(status="closed", reason="must reject", closed_at_s=10.1)
    assert not (recorder.output_dir / "seal.json").exists()


def test_existing_next_event_never_overwritten_and_faulted_partial_can_seal(tmp_path):
    recorder, request = _shadow(tmp_path)
    path = recorder.output_dir / "events/00000002.json"
    path.write_bytes(b"foreign-event")
    with pytest.raises(FileExistsError):
        recorder.record_decision(
            request.canonical_hash, _raw(request), completed_at_s=10.05, inference_latency_ms=50
        )
    assert path.read_bytes() == b"foreign-event"
    with pytest.raises(OnboardLiveEpisodeError, match="faulted"):
        recorder.close(status="closed", reason="cannot succeed", closed_at_s=10.1)
    # Unregistered/foreign file is not silently included as a recorded fact.
    seal = json.loads(
        _read(
            recorder,
            recorder.close(
                status="failed_partial", reason="create-only event conflict", closed_at_s=10.1
            ),
        )
    )
    assert seal["event_count"] == 2
    assert seal["stage_counts"]["proposed"] == 0


@pytest.mark.parametrize(
    "provenance",
    [b'{"x":NaN}', b'{"x":1,"x":2}', b"[]", b"{}" * 200000, bytearray(b"{}")],
    ids=["nan", "duplicate-key", "array", "oversized", "mutable"],
)
def test_provenance_strict_bounded_immutable_and_not_model_truth(tmp_path, provenance):
    request = _source()[0]
    recorder = _create(tmp_path, request)
    with pytest.raises(AutonomyContractError):
        _record_frame(recorder, request.model_input.frames[0], source_provenance=provenance)
    assert not _events(recorder)


@dataclass(frozen=True, slots=True)
class _LiveRequest(OnboardVlmRuntimeRequest):
    def to_mapping(self):
        return {
            **super(_LiveRequest, self).to_mapping(),
            "factual_sensor_state": [
                {
                    "frame_id": self.model_input.frames[0].frame_id,
                    "source_provenance": {"gps": None},
                }
            ],
        }


def test_subclass_live_request_full_mapping_retained_not_dropped(tmp_path):
    original = _source()[0]
    request = _LiveRequest(original.model_input)
    recorder = _create(tmp_path, request)
    _record_frame(recorder, request.model_input.frames[0], source_provenance=b'{"gps":null}')
    recorder.record_request(request, submitted_at_s=10)
    data = _events(recorder)[1]["data"]
    assert data["request"] == request.to_mapping()
    assert data["request"]["factual_sensor_state"] == [
        {"frame_id": request.model_input.frames[0].frame_id, "source_provenance": {"gps": None}}
    ]
    assert data["request_sha256"] == request.canonical_hash


def _navigation(recorder, stamp=10.01, **changes):
    raw = {
        "schema_version": 1,
        "contract": RECEIVE_CONTRACT,
        "message_id": "actual-nav-fixture",
        "scope_sha256": recorder.scope.canonical_hash,
        "session_id": recorder.scope.session_id,
        "aircraft_id": recorder.scope.aircraft_id,
        "observed_at_s": stamp,
        "clock_epoch": recorder.scope.clock_epoch,
        "source_id": "fixture-receiver",
        "source_system_id": 1,
        "source_component_id": 1,
        "vehicle_identity_sha256": VEHICLE_HASH,
        "mission_sha256": "f" * 64,
        "transaction_id": "fixture-tx",
        "message": {
            "message_type": "GLOBAL_POSITION_INT",
            "lat": 377000000,
            "lon": -1220000000,
            "alt": 50000,
            "relative_alt": 10000,
            "time_boot_ms": 123,
        },
        **changes,
    }
    return ContinuationReceivedFact(_bytes(raw), _sha(_bytes(raw)))


def test_original_navigation_boot_receive_processing_clocks_stay_separate(tmp_path):
    recorder, _ = _shadow(tmp_path)
    navigation = _navigation(recorder)
    recorder.record_navigation(navigation, now_s=10.03)
    event = _events(recorder)[-1]
    assert (
        _read(recorder, event["data"]["source_receipt_original"]) == navigation.source_receipt_bytes
    )
    assert event["host_observed_at_s"] == 10.03
    assert event["data"]["received_fact"]["observed_at_s"] == 10.01
    assert event["data"]["received_fact"]["message"]["time_boot_ms"] == 123
    assert event["data"]["achieved_action"] is None
    with pytest.raises(OnboardLiveEpisodeError, match="identity"):
        recorder.record_navigation(navigation, now_s=10.04)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "foreign-session"),
        ("scope_sha256", "b" * 64),
        ("aircraft_id", "foreign-aircraft"),
        ("clock_epoch", "foreign-clock"),
        ("observed_at_s", 11),
    ],
)
def test_navigation_scope_and_future_rejected(tmp_path, field, value):
    recorder, _ = _shadow(tmp_path)
    with pytest.raises(OnboardLiveEpisodeError):
        recorder.record_navigation(_navigation(recorder, **{field: value}), now_s=10.03)


def test_request_and_original_image_object_mutation_rechecked(tmp_path):
    recorder, request = _shadow(tmp_path)
    object.__setattr__(request.model_input.frames[0], "image_bytes", b"xyz")
    with pytest.raises(AutonomyContractError):
        recorder.record_decision(
            request.canonical_hash, _raw(request), completed_at_s=10.05, inference_latency_ms=50
        )


def test_unknown_request_never_creates_proposal(tmp_path):
    recorder = _create(tmp_path)
    with pytest.raises(OnboardLiveEpisodeError, match="unknown"):
        recorder.record_decision("b" * 64, b"{}", completed_at_s=10, inference_latency_ms=0)
    assert not _events(recorder)


def test_close_time_regression_rejected(tmp_path):
    recorder, _ = _shadow(tmp_path)
    with pytest.raises(AutonomyContractError):
        recorder.close(status="closed", reason="clock regression", closed_at_s=9.99)
    assert not (recorder.output_dir / "seal.json").exists()


def test_first_received_frame_precedes_recorder_creation_without_faking_receive_time(tmp_path):
    request = _source()[0]
    recorder = OnboardLiveEpisodeRecorder.create(
        tmp_path / "episode",
        prompt=request.model_input.prompt,
        prompt_text_original=request.model_input.prompt.text,
        scope=request.model_input.scope,
        host_clock_id="host-monotonic-fixture",
        created_at_s=10.01,
    )
    recorder.record_observation(
        request.model_input.frames[0],
        received_at_s=10,
        processed_at_s=10.02,
        source_clock_id="sensor-sim-ns",
        source_timestamp_s=None,
        source_provenance=b'{"source_stamp_ns":9007199254740993}',
    )
    event = _events(recorder)[0]
    assert event["host_observed_at_s"] == 10.02
    assert event["data"]["host_received_at_s"] == 10
    assert event["data"]["frame"]["captured_at_s"] == 10
    assert event["data"]["source_timestamp_s"] is None
    assert (
        json.loads(_read(recorder, event["data"]["source_provenance_original"]))["source_stamp_ns"]
        == 9007199254740993
    )


def test_async_result_finished_before_new_rgb_is_recorded_at_real_poll_time(tmp_path):
    recorder, request = _shadow(tmp_path)
    frame = request.model_input.frames[0]
    later = replace(
        frame,
        frame_id="newer-than-result",
        captured_at_s=10.2,
        context=replace(frame.context, captured_at_s=10.2),
    )
    _record_frame(recorder, later, received=10.21)
    response = recorder.record_decision(
        request.canonical_hash,
        _raw(request),
        completed_at_s=10.15,
        received_at_s=10.3,
        inference_latency_ms=150,
    )
    event = _events(recorder)[-1]
    assert event["host_observed_at_s"] == 10.3
    assert event["data"]["response"]["completed_at_s"] == response.completed_at_s == 10.15
    assert recorder._requests[request.canonical_hash].request is None
    successor = replace(
        later,
        frame_id="after-result-received",
        captured_at_s=10.4,
        context=replace(later.context, captured_at_s=10.4),
    )
    _record_frame(recorder, successor)
    assert _events(recorder)[-1]["preceding_decision_request_sha256"] == request.canonical_hash


def test_late_receive_does_not_repair_expired_original_worker_completion(tmp_path):
    recorder, request = _shadow(tmp_path)
    with pytest.raises(AutonomyContractError, match="deadline"):
        recorder.record_decision(
            request.canonical_hash,
            _raw(request),
            completed_at_s=11.1,
            received_at_s=11.2,
            inference_latency_ms=1100,
        )
    event = _events(recorder)[-1]
    assert event["kind"] == "decision_rejected"
    assert event["data"]["worker_completed_at_s"] == 11.1
    assert event["host_observed_at_s"] == 11.2


@pytest.mark.parametrize("part", ["rgb", "completion"])
def test_receive_time_cannot_be_future_to_actual_processing(tmp_path, part):
    recorder, request = _shadow(tmp_path)
    with pytest.raises(OnboardLiveEpisodeError):
        if part == "rgb":
            recorder.record_observation(
                request.model_input.frames[0],
                received_at_s=10.1,
                processed_at_s=10.05,
                source_clock_id="sensor",
            )
        else:
            recorder.record_decision(
                request.canonical_hash,
                _raw(request),
                completed_at_s=10.1,
                received_at_s=10.05,
                inference_latency_ms=100,
            )


@pytest.mark.parametrize("part", ["scope", "prompt-original", "host-clock"])
def test_episode_identity_cannot_mutate_before_seal(tmp_path, part):
    recorder, _ = _shadow(tmp_path)
    if part == "scope":
        recorder.scope = replace(recorder.scope, clock_epoch="foreign-epoch")
    elif part == "prompt-original":
        recorder.prompt_text_original = " " + recorder.prompt_text_original
    else:
        recorder.host_clock_id = "different-host-clock"
    with pytest.raises(OnboardLiveEpisodeError, match="identity"):
        recorder.close(status="closed", reason="must reject changed identity", closed_at_s=10.1)
    assert not (recorder.output_dir / "seal.json").exists()


def test_navigation_source_receive_regression_and_non_navigation_rejected(tmp_path):
    recorder, _ = _shadow(tmp_path)
    recorder.record_navigation(_navigation(recorder), now_s=10.02)
    with pytest.raises(OnboardLiveEpisodeError, match="regressed"):
        recorder.record_navigation(
            _navigation(recorder, stamp=10, message_id="second"), now_s=10.03
        )
    raw = _navigation(recorder).to_mapping()
    raw["message"] = {"message_type": "MISSION_CURRENT", "seq": 1}
    fact = ContinuationReceivedFact(_bytes(raw), _sha(_bytes(raw)))
    with pytest.raises(OnboardLiveEpisodeError, match="HOME/GPI"):
        recorder.record_navigation(fact, now_s=10.03)


def test_host_domain_source_stamp_cannot_be_future(tmp_path):
    request = _source()[0]
    recorder = _create(tmp_path, request)
    with pytest.raises(OnboardLiveEpisodeError, match="future"):
        _record_frame(
            recorder,
            request.model_input.frames[0],
            source_timestamp_s=11,
            source_clock_id=recorder.host_clock_id,
        )


def test_external_collector_extra_or_foreign_frames_not_exact_request(tmp_path):
    _, request, _, _ = _setup()
    recorder = _create(tmp_path, request)
    frame = request.model_input.frames[0]
    _record_frame(recorder, frame)
    collector = OnboardEvidenceCollector(frame.scope, expected_system_id=1, expected_component_id=1)
    collector.add_observation(frame)
    collector.add_observation(replace(frame, frame_id="foreign", captured_at_s=10.01, context=None))
    with pytest.raises(OnboardLiveEpisodeError, match="bounded collector ledger"):
        recorder.record_request(request, submitted_at_s=10.02, collector=collector)


def test_mutated_original_journal_is_rejected_before_bind(tmp_path):
    recorder, _, trace, _ = _receiving(tmp_path)
    recorder.record_acceptance(trace)
    binding = _journal(tmp_path, trace)
    binding.journal_path.write_bytes(b"journal tampered")
    with pytest.raises(OnboardLiveEpisodeError, match="journal changed"):
        recorder.bind_vehicle_mission(trace.proposal_id, binding, observed_at_s=10.07)
    assert _events(recorder)[-1]["kind"] == "revision_accepted"


def test_symlink_output_directory_rejected_without_following(tmp_path):
    actual = tmp_path / "real"
    actual.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host does not permit symlink fixture: {exc}")
    request = _source()[0]
    with pytest.raises(OnboardLiveEpisodeError, match="symlinks"):
        OnboardLiveEpisodeRecorder.create(
            link / "episode",
            prompt=request.model_input.prompt,
            prompt_text_original=request.model_input.prompt.text,
            scope=request.model_input.scope,
            host_clock_id="host-fixture",
            created_at_s=9,
        )
    assert not (actual / "episode").exists()


def test_actual_live_request_sensor_facts_match_recorded_original_provenance(tmp_path):
    from test_onboard_live_vlm_contract import _live_input

    from multidetect.autonomy.onboard_live_vlm_contract import OnboardLiveVlmRequest

    model_input = _live_input()
    request = OnboardLiveVlmRequest(model_input)
    recorder = _create(tmp_path, request)
    for frame, text in zip(model_input.frames, model_input.factual_sensor_state_json, strict=True):
        _record_frame(
            recorder, frame, source_provenance=_bytes(json.loads(text)["source_provenance"])
        )
    recorder.record_request(request, submitted_at_s=model_input.requested_at_s)
    assert _events(recorder)[-1]["data"]["request"] == request.to_mapping()


def test_internally_valid_other_gps_wire_cannot_replace_recorded_live_source_state(tmp_path):
    from test_onboard_live_vlm_contract import _live_input

    from multidetect.autonomy.onboard_live_vlm_contract import OnboardLiveVlmRequest

    model_input = _live_input()
    recorder = _create(tmp_path, OnboardLiveVlmRequest(model_input))
    for frame, text in zip(model_input.frames, model_input.factual_sensor_state_json, strict=True):
        _record_frame(
            recorder, frame, source_provenance=_bytes(json.loads(text)["source_provenance"])
        )
    other = OnboardLiveVlmRequest(_live_input(latitude=48.0))
    other.validate_binding()  # Valid different wire, but not this episode's original source.
    assert other.model_input.frames == model_input.frames
    with pytest.raises(OnboardLiveEpisodeError, match="original frame provenance"):
        recorder.record_request(other, submitted_at_s=model_input.requested_at_s)


def test_live_facts_require_prior_exact_provenance_not_only_rgb(tmp_path):
    original = _source()[0]
    request = _LiveRequest(original.model_input)
    recorder = _create(tmp_path, request)
    _record_frame(recorder, request.model_input.frames[0])
    with pytest.raises(OnboardLiveEpisodeError, match="original frame provenance"):
        recorder.record_request(request, submitted_at_s=10)


def test_each_decision_retains_its_own_actual_backend_fields_without_dropping(tmp_path):
    recorder, request = _shadow(tmp_path)
    first_metrics = {
        "output_token_ids": [3, 7, 11],
        "actual_image_grid_thw": [[1, 14, 14]],
        "input_tensors": {"pixel_values": {"sha256": "a" * 64, "dtype": "torch.float32"}},
        "peak_allocated_bytes": 123456789,
        "new_unknown_field": {"retained": None},
    }
    first_text = json.dumps(first_metrics, indent=2)
    recorder.record_decision(
        request.canonical_hash,
        _raw(request),
        completed_at_s=10.05,
        inference_latency_ms=50,
        backend_metrics_json=first_text,
    )
    frame = request.model_input.frames[0]
    later = replace(
        frame,
        frame_id="next-window",
        captured_at_s=10.1,
        context=replace(frame.context, captured_at_s=10.1),
    )
    _record_frame(recorder, later)
    second_request = OnboardVlmRuntimeRequest(
        replace(
            request.model_input,
            frames=(later,),
            ledger=MissionEvidenceLedger(later.scope, (later,)),
            requested_at_s=10.1,
            deadline_at_s=11.1,
        )
    )
    recorder.record_request(second_request, submitted_at_s=10.1)
    second_metrics = {
        **first_metrics,
        "output_token_ids": [13, 17],
        "peak_allocated_bytes": 987654321,
    }
    recorder.record_decision(
        second_request.canonical_hash,
        _raw(second_request),
        completed_at_s=10.15,
        inference_latency_ms=50,
        backend_metrics_json=canonical_json(second_metrics),
    )
    decisions = [event for event in _events(recorder) if event["kind"] == "decision_proposed"]
    assert len(decisions) == 2
    for event, expected in zip(decisions, (first_metrics, second_metrics), strict=True):
        payload = _read(recorder, event["data"]["backend_metrics_canonical"])
        assert payload == _bytes(expected) + b"\n"
        assert json.loads(payload) == expected
    assert decisions[0]["data"]["backend_metrics_input_utf8_sha256"] == _sha(first_text.encode())
    assert (
        decisions[0]["data"]["backend_metrics_canonical"]
        != decisions[1]["data"]["backend_metrics_canonical"]
    )


def test_strict_rejected_raw_keeps_same_worker_backend_metrics(tmp_path):
    recorder, request = _shadow(tmp_path)
    metrics = {
        "output_token_ids": [1, 2, 3],
        "generated_tokens": 3,
        "max_new_tokens_reached": False,
        "model_sha256": "f" * 64,
    }
    with pytest.raises(AutonomyContractError):
        recorder.record_decision(
            request.canonical_hash,
            "{invalid model JSON}",
            completed_at_s=10.05,
            inference_latency_ms=50,
            backend_metrics_json=canonical_json(metrics),
        )
    event = _events(recorder)[-1]
    assert event["kind"] == "decision_rejected"
    assert json.loads(_read(recorder, event["data"]["backend_metrics_canonical"])) == metrics
    assert _read(recorder, event["data"]["raw_response_original"]) == b"{invalid model JSON}"
    assert event["data"]["typed_output"] is None
    assert event["training_ready"] is False


@pytest.mark.parametrize(
    "metrics",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        "[]",
        "{} trailing",
        "x" * (256 * 1024 + 1),
        b"{}",
    ],
    ids=["duplicate-key", "nan", "overflow", "array", "trailing", "oversized", "not-text"],
)
def test_backend_metrics_fail_closed_before_proposal_on_bad_json_or_budget(tmp_path, metrics):
    recorder, request = _shadow(tmp_path)
    with pytest.raises(AutonomyContractError):
        recorder.record_decision(
            request.canonical_hash,
            _raw(request),
            completed_at_s=10.05,
            inference_latency_ms=50,
            backend_metrics_json=metrics,
        )
    assert len(_events(recorder)) == 2
    assert recorder._requests[request.canonical_hash].decision_recorded is False

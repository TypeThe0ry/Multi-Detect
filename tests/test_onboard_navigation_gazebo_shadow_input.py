"""Wire/clock fixtures test input closure, not an external Gazebo or AI run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from test_onboard_live_shadow import source

from multidetect.autonomy.contracts import PromptEnvelope
from multidetect.autonomy.onboard_navigation_actor import serialize_navigation_actor_messages
from multidetect.autonomy.onboard_navigation_supervision import NavigationSupervisionBudget
from scripts.onboard_navigation_gazebo_shadow_input import (
    GazeboNavigationInputBuffer,
    PreparedGazeboNavigationRequest,
)
from scripts.onboard_navigation_inference_backend import NavigationEngineIdentity

IDENTITY = NavigationEngineIdentity("1" * 64, "2" * 64, "3" * 64)
RAW = "  Find the nearest matching object\n"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def setup(*, age=5.0, deadline=5.0, budget=None):
    frame, p = source(0)
    clock = Clock(p.captured_at_s + 0.01)
    prompt = PromptEnvelope("prompt", RAW, "en", "2026-10-08T00:00:00Z", "operator", "sim")
    buffer = GazeboNavigationInputBuffer(
        prompt,
        prompt_text_original=RAW,
        host_clock_identity=dict(p.host_clock_identity),
        clock=clock,
        max_frame_age_s=age,
        deadline_s=deadline,
        budget=budget,
        session_id="d6b00b1d-47c7-4d35-965f-ea0621eff8b7",
    )
    return buffer, frame, p, clock


def next_frame(p, sequence, captured):
    frame, new = source(sequence, identity=dict(p.host_clock_identity))
    telemetry = replace(
        new.frame_telemetry,
        gps_observed_at_s=captured - new.navsat_source_skew_ns / 1e9,
        attitude_observed_at_s=captured - new.odometry_source_skew_ns / 1e9,
        velocity_observed_at_s=captured - new.odometry_source_skew_ns / 1e9,
    )
    return frame, replace(new, captured_at_s=captured, frame_telemetry=telemetry)


def test_real_wire_reconstruction_and_original_prompt_boundary():
    buffer, frame, p, _ = setup()
    buffer.offer(frame, p)
    prepared = buffer.build_request(IDENTITY, generation=1)
    request = prepared.request
    assert request.actor_input.prompt_original == RAW
    assert request.scope.prompt_sha256 == hashlib.sha256(RAW.encode()).hexdigest()
    assert (
        request.scope.prompt_sha256
        != json.loads(prepared.source_receipt_json)["prompt_envelope_sha256"]
    )
    original_bgr = frame.image_bgr.tobytes()
    assert request.source_frames[0].image_bytes == bytes(
        [original_bgr[2], original_bgr[1], original_bgr[0]]
        + [original_bgr[5], original_bgr[4], original_bgr[3]]
    )
    assert request.source_frames[0].context is None
    assert (
        request.source_binding_sha256
        == hashlib.sha256(prepared.source_receipt_json.encode()).hexdigest()
    )
    messages = serialize_navigation_actor_messages(request.actor_input)
    try:
        text = " ".join(str(part.get("text", "")) for row in messages for part in row["content"])
        assert RAW in text
        assert p.record_sha256 not in text
        assert request.scope.clock_epoch not in text
        assert "wire_telemetry" not in text
        assert "confidence" not in messages[-1]["content"][-1].get("text", "")
    finally:
        for row in messages:
            for part in row["content"]:
                if part.get("type") == "image":
                    part["image"].close()


def test_source_pixels_are_immutable_copy_after_callback():
    buffer, frame, p, _ = setup()
    buffer.offer(frame, p)
    expected = buffer._history[0].actor.image_bytes
    # Replace the mutable caller-owned array, not the retained wire's readonly buffer.
    frame.image_bgr = frame.image_bgr.copy()
    frame.image_bgr[:] = 0
    assert (
        buffer.build_request(IDENTITY, generation=1).request.actor_input.frames[0].image_bytes
        == expected
    )


def test_recent_history_uses_two_real_frames_in_original_order():
    buffer, frame, p, clock = setup()
    buffer.offer(frame, p)
    second, q = next_frame(p, 1, p.captured_at_s + 0.1)
    clock.now = q.captured_at_s + 0.01
    buffer.offer(second, q)
    request = buffer.build_request(IDENTITY, generation=1).request
    assert [row.sequence for row in request.actor_input.frames] == [0, 1]
    assert [row.frame_id for row in request.source_frames] == [p.frame_id, q.frame_id]


def test_stale_history_is_dropped_without_relaxing_current_frame_age():
    buffer, frame, p, clock = setup(age=0.1)
    buffer.offer(frame, p)
    second, q = next_frame(p, 1, p.captured_at_s + 0.2)
    clock.now = q.captured_at_s + 0.01
    buffer.offer(second, q)
    request = buffer.build_request(IDENTITY, generation=1).request
    assert len(request.actor_input.frames) == 1
    assert request.max_frame_age_s == 0.1
    clock.now += 0.11
    with pytest.raises(ValueError, match="stale"):
        buffer.build_request(IDENTITY, generation=2)


def test_live_history_does_not_enlarge_training_actor_pixel_budget():
    budget = replace(NavigationSupervisionBudget(), max_total_pixels=2)
    buffer, frame, p, clock = setup(budget=budget)
    buffer.offer(frame, p)
    second, q = next_frame(p, 1, p.captured_at_s + 0.1)
    clock.now = q.captured_at_s + 0.01
    buffer.offer(second, q)
    request = buffer.build_request(IDENTITY, generation=1).request
    assert len(request.actor_input.frames) == 1
    assert request.actor_input.budget == budget


@pytest.mark.parametrize("field", ["record_sha256", "payload_sha256", "wire_unsigned_header_hex"])
def test_tampered_wire_refused(field):
    buffer, frame, p, _ = setup()
    value = "0" * (len(getattr(p, field)))
    with pytest.raises((ValueError, RuntimeError)):
        buffer.offer(frame, replace(p, **{field: value}))
    assert buffer._history == []


@pytest.mark.parametrize("field", ["sequence", "sensor_stamp_ns", "captured_at_s"])
def test_frame_chronology_refused(field):
    buffer, frame, p, clock = setup()
    buffer.offer(frame, p)
    second, q = next_frame(p, 1, p.captured_at_s + 0.1)
    clock.now = q.captured_at_s + 0.01
    with pytest.raises(ValueError, match="regressed"):
        buffer.offer(second, replace(q, **{field: getattr(p, field)}))


@pytest.mark.parametrize("clock_kind", ["other_identity", "host_monotonic", "missing"])
def test_clock_domains_not_interchangeable(clock_kind):
    buffer, frame, p, _ = setup()
    if clock_kind == "host_monotonic":
        p = replace(p, captured_at_clock="host_monotonic")
    elif clock_kind == "missing":
        p = replace(p, host_clock_identity=None)
    else:
        d = dict(p.host_clock_identity)
        d["session_id"] = "other"
        p = replace(p, host_clock_identity=tuple(d.items()))
    with pytest.raises(ValueError):
        buffer.offer(frame, p)


def test_future_stale_or_regressing_host_clock_refused():
    buffer, frame, p, clock = setup()
    with pytest.raises(ValueError, match="future or stale"):
        buffer.offer(frame, replace(p, captured_at_s=clock.now + 1))
    clock.now -= 1
    with pytest.raises(ValueError, match="clock regressed"):
        buffer.offer(frame, p)


def test_duplicate_request_and_generation_refused():
    buffer, frame, p, clock = setup()
    buffer.offer(frame, p)
    buffer.build_request(IDENTITY, generation=1)
    with pytest.raises(ValueError, match="advance"):
        buffer.build_request(IDENTITY, generation=1)
    with pytest.raises(ValueError, match="not previously"):
        buffer.build_request(IDENTITY, generation=2)
    second, q = next_frame(p, 1, p.captured_at_s + 0.1)
    clock.now = q.captured_at_s + 0.01
    buffer.offer(second, q)
    assert buffer.build_request(IDENTITY, generation=2).request.generation == 2


def test_receipt_replacement_rejected():
    buffer, frame, p, _ = setup()
    buffer.offer(frame, p)
    value = buffer.build_request(IDENTITY, generation=1)
    with pytest.raises(ValueError, match="receipt changed"):
        PreparedGazeboNavigationRequest(value.request, "{}")


def test_decoder_time_counted_as_freshness(monkeypatch):
    buffer, frame, p, clock = setup(age=0.1)
    from scripts import onboard_navigation_gazebo_shadow_input as api

    original = api._frame_facts

    def delayed(*args):
        result = original(*args)
        clock.now += 0.2
        return result

    monkeypatch.setattr(api, "_frame_facts", delayed)
    with pytest.raises(ValueError, match="during source"):
        buffer.offer(frame, p)


@pytest.mark.parametrize("age,deadline", [(0.1, 5.0), (5.0, 0.1)])
def test_prepared_binding_time_counted_without_consuming_request(monkeypatch, age, deadline):
    buffer, frame, p, clock = setup(age=age, deadline=deadline)
    buffer.offer(frame, p)
    from scripts import onboard_navigation_gazebo_shadow_input as api

    original = api.PreparedGazeboNavigationRequest

    def delayed(*args):
        result = original(*args)
        clock.now += 0.2
        return result

    monkeypatch.setattr(api, "PreparedGazeboNavigationRequest", delayed)
    with pytest.raises(ValueError, match="prepared binding"):
        buffer.build_request(IDENTITY, generation=1)
    assert buffer._last_generation == 0
    assert buffer._last_requested_sequence == -1


def test_clock_regression_during_prepared_binding_refused(monkeypatch):
    buffer, frame, p, clock = setup()
    buffer.offer(frame, p)
    from scripts import onboard_navigation_gazebo_shadow_input as api

    original = api.PreparedGazeboNavigationRequest

    def delayed(*args):
        result = original(*args)
        clock.now -= 0.005
        return result

    monkeypatch.setattr(api, "PreparedGazeboNavigationRequest", delayed)
    with pytest.raises(ValueError, match="clock regressed"):
        buffer.build_request(IDENTITY, generation=1)
    assert buffer._last_generation == 0
    assert buffer._last_requested_sequence == -1


@pytest.mark.parametrize("value", [0, -1, float("nan"), 31, True])
def test_invalid_freshness_bounds_rejected(value):
    with pytest.raises(ValueError):
        setup(age=value)

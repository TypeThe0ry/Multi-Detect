from __future__ import annotations

import json
from dataclasses import replace

import pytest

from multidetect.payload_hil_protocol import (
    PayloadHilCodec,
    PayloadHilProtocolError,
    PayloadHilReleaseRequest,
    PayloadHilRequestGuard,
    PayloadHilResult,
    PayloadHilResultGuard,
    PayloadHilResultStatus,
)

KEY = b"payload-hil-test-key-with-32-byte-minimum"
KEY_ID = "payload-hil-key-v1"


def _request(**changes: object) -> PayloadHilReleaseRequest:
    values: dict[str, object] = {
        "mission_id": "fire-fixed-wing-hil-001",
        "module_id": "inert-controller-1",
        "release_id": "release-001",
        "payload_slot_id": "payload-1",
        "payload_type": "fire_suppression_agent",
        "authorization_challenge_id": "challenge-001",
        "operator_id": "operator-1",
        "target_id": "track-42",
        "target_revision": 8,
        "scene_digest": "scene-digest-001",
        "ruleset_version": "rules-v1",
        "requested_at_s": 100.0,
        "expires_at_s": 105.0,
        "sequence": 1,
        "key_id": KEY_ID,
    }
    values.update(changes)
    return PayloadHilReleaseRequest(**values)


def _result(**changes: object) -> PayloadHilResult:
    values: dict[str, object] = {
        "mission_id": "fire-fixed-wing-hil-001",
        "module_id": "inert-controller-1",
        "release_id": "release-001",
        "payload_slot_id": "payload-1",
        "status": PayloadHilResultStatus.ACCEPTED,
        "observed_at_s": 100.2,
        "sequence": 1,
        "key_id": KEY_ID,
        "controller_healthy": True,
        "interlock_healthy": True,
    }
    values.update(changes)
    return PayloadHilResult(**values)


def test_codec_round_trip_binds_every_request_and_result_field() -> None:
    codec = PayloadHilCodec(hmac_key=KEY, expected_key_id=KEY_ID)
    request = _request()
    result = _result()

    assert codec.decode_request(codec.encode_request(request)) == request
    assert codec.decode_result(codec.encode_result(result)) == result


def test_codec_rejects_tamper_wrong_key_and_unsafe_flags() -> None:
    codec = PayloadHilCodec(hmac_key=KEY, expected_key_id=KEY_ID)
    document = json.loads(codec.encode_request(_request()))
    document["payload_slot_id"] = "payload-2"
    with pytest.raises(PayloadHilProtocolError, match="HMAC"):
        codec.decode_request(json.dumps(document).encode())
    with pytest.raises(PayloadHilProtocolError, match="physical release"):
        _request(physical_release_enabled=True)
    with pytest.raises(ValueError, match="32 bytes"):
        PayloadHilCodec(hmac_key=b"short", expected_key_id=KEY_ID)
    with pytest.raises(PayloadHilProtocolError, match="size limit"):
        codec.encode_request(_request(target_id="x" * 5000))


def test_request_guard_enforces_idempotency_sequence_and_single_slot_interlock() -> None:
    guard = PayloadHilRequestGuard(
        mission_id="fire-fixed-wing-hil-001",
        module_id="inert-controller-1",
        installed_slots={"payload-1": "fire_suppression_agent", "payload-2": "sensor_node"},
        maximum_age_s=1.0,
    )
    request = _request()

    assert guard.verify(request, now_s=100.2).valid is True
    replay = guard.verify(request, now_s=100.3)
    assert replay.valid is True
    assert replay.idempotent_replay is True
    changed = guard.verify(replace(request, target_revision=9), now_s=100.3)
    assert changed.valid is False
    assert any("reused" in reason for reason in changed.reasons)

    blocked = guard.verify(
        _request(
            release_id="release-002",
            payload_slot_id="payload-2",
            payload_type="sensor_node",
            sequence=2,
        ),
        now_s=100.3,
    )
    assert blocked.valid is False
    assert any("active release" in reason for reason in blocked.reasons)

    guard.finish(release_id=request.release_id)
    next_request = _request(
        release_id="release-002",
        payload_slot_id="payload-2",
        payload_type="sensor_node",
        sequence=2,
    )
    assert guard.verify(next_request, now_s=100.4).valid is True
    guard.finish(release_id=next_request.release_id)
    rollback = guard.verify(_request(release_id="release-003", sequence=1), now_s=100.5)
    assert rollback.valid is False
    assert any("sequence" in reason for reason in rollback.reasons)


@pytest.mark.parametrize(
    ("release_request", "reason"),
    [
        (_request(mission_id="wrong-mission"), "mission ID"),
        (_request(module_id="wrong-module"), "module ID"),
        (_request(payload_slot_id="missing-slot"), "not installed"),
        (_request(requested_at_s=90.0), "stale"),
    ],
)
def test_request_guard_rejects_wrong_bindings_or_stale_request(
    release_request: PayloadHilReleaseRequest, reason: str
) -> None:
    guard = PayloadHilRequestGuard(
        mission_id="fire-fixed-wing-hil-001",
        module_id="inert-controller-1",
        installed_slots={"payload-1": "fire_suppression_agent"},
        maximum_age_s=1.0,
    )

    verification = guard.verify(release_request, now_s=100.2)

    assert verification.valid is False
    assert any(reason in item for item in verification.reasons)


def test_result_guard_requires_correlation_health_and_monotonic_content() -> None:
    guard = PayloadHilResultGuard(request=_request(), maximum_age_s=1.0)
    accepted = _result()
    assert guard.verify(accepted, now_s=100.3).valid is True
    replay = guard.verify(accepted, now_s=100.4)
    assert replay.valid is True
    assert replay.idempotent_replay is True

    changed_same_sequence = guard.verify(
        replace(accepted, status=PayloadHilResultStatus.EXECUTED),
        now_s=100.4,
    )
    assert changed_same_sequence.valid is False
    assert any("changed" in reason for reason in changed_same_sequence.reasons)

    unhealthy = guard.verify(
        _result(
            status=PayloadHilResultStatus.EXECUTED,
            sequence=2,
            observed_at_s=100.4,
            controller_healthy=False,
        ),
        now_s=100.5,
    )
    assert unhealthy.valid is False
    assert any("healthy interlocks" in reason for reason in unhealthy.reasons)

    executed = guard.verify(
        _result(
            status=PayloadHilResultStatus.EXECUTED,
            sequence=2,
            observed_at_s=100.4,
        ),
        now_s=100.5,
    )
    assert executed.valid is True
    regressed = guard.verify(
        _result(
            status=PayloadHilResultStatus.ACCEPTED,
            sequence=3,
            observed_at_s=100.6,
        ),
        now_s=100.7,
    )
    assert regressed.valid is False
    assert any("status transition" in reason for reason in regressed.reasons)


def test_result_guard_rejects_result_that_predates_request() -> None:
    guard = PayloadHilResultGuard(request=_request(), maximum_age_s=20.0)

    verification = guard.verify(_result(observed_at_s=99.0), now_s=100.1)

    assert verification.valid is False
    assert any("predates" in reason for reason in verification.reasons)


def test_rejected_result_may_report_unhealthy_controller_but_requires_reason() -> None:
    rejected = _result(
        status=PayloadHilResultStatus.REJECTED,
        controller_healthy=False,
        interlock_healthy=False,
        reason="mechanical interlock open",
    )
    guard = PayloadHilResultGuard(request=_request(), maximum_age_s=1.0)

    assert guard.verify(rejected, now_s=100.3).valid is True
    with pytest.raises(PayloadHilProtocolError, match="require a reason"):
        _result(status=PayloadHilResultStatus.FAILED)

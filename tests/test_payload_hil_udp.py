from __future__ import annotations

import threading
import time

import pytest

from multidetect.payload_hil_protocol import (
    PayloadHilCodec,
    PayloadHilReleaseRequest,
    PayloadHilRequestGuard,
    PayloadHilResultStatus,
)
from multidetect.payload_hil_udp import UdpInertPayloadHilController, UdpPayloadHilClient

REQUEST_KEY = b"payload-hil-udp-request-key-32-byte-minimum"
RESULT_KEY = b"payload-hil-udp-result-key-32-bytes-minimum"
REQUEST_KEY_ID = "request-key-v1"
RESULT_KEY_ID = "result-key-v1"


def _request(*, slot_id: str = "payload-1", payload_type: str = "fire_suppression_agent"):
    now_s = time.monotonic()
    return PayloadHilReleaseRequest(
        mission_id="fire-fixed-wing-hil-001",
        module_id="inert-controller-1",
        release_id=f"release-{slot_id}",
        payload_slot_id=slot_id,
        payload_type=payload_type,
        authorization_challenge_id="challenge-1",
        operator_id="operator-1",
        target_id="track-1",
        target_revision=5,
        scene_digest="scene-digest",
        ruleset_version="rules-v1",
        requested_at_s=now_s,
        expires_at_s=now_s + 2.0,
        sequence=1,
        key_id=REQUEST_KEY_ID,
    )


def _controller() -> UdpInertPayloadHilController:
    return UdpInertPayloadHilController(
        bind_host="127.0.0.1",
        port=0,
        request_codec=PayloadHilCodec(
            hmac_key=REQUEST_KEY,
            expected_key_id=REQUEST_KEY_ID,
        ),
        result_codec=PayloadHilCodec(
            hmac_key=RESULT_KEY,
            expected_key_id=RESULT_KEY_ID,
        ),
        request_guard=PayloadHilRequestGuard(
            mission_id="fire-fixed-wing-hil-001",
            module_id="inert-controller-1",
            installed_slots={"payload-1": "fire_suppression_agent"},
            maximum_age_s=1.0,
        ),
    )


def _client(controller: UdpInertPayloadHilController, *, attempts: int = 3):
    return UdpPayloadHilClient(
        host=controller.local_address[0],
        port=controller.local_address[1],
        request_codec=PayloadHilCodec(
            hmac_key=REQUEST_KEY,
            expected_key_id=REQUEST_KEY_ID,
        ),
        result_codec=PayloadHilCodec(
            hmac_key=RESULT_KEY,
            expected_key_id=RESULT_KEY_ID,
        ),
        response_timeout_s=0.08,
        maximum_attempts=attempts,
    )


def test_udp_hil_retries_identical_request_and_receives_cached_terminal_results() -> None:
    with _controller() as controller:

        def serve() -> None:
            controller.serve_once(
                simulate_inert_execution=True,
                drop_first_response=True,
            )
            controller.serve_once(simulate_inert_execution=True)

        thread = threading.Thread(target=serve)
        thread.start()
        exchange = _client(controller).exchange(_request(), maximum_result_age_s=1.0)
        thread.join(timeout=2.0)

        assert not thread.is_alive()
        assert exchange.attempts == 2
        assert [result.status for result in exchange.results] == [
            PayloadHilResultStatus.ACCEPTED,
            PayloadHilResultStatus.EXECUTED,
        ]
        assert exchange.terminal_result is not None
        assert exchange.terminal_result.status is PayloadHilResultStatus.EXECUTED
        assert controller.received_datagrams == 2
        assert controller.command_messages_sent == 0
        assert controller.physical_release_enabled is False


def test_udp_hil_returns_authenticated_rejection_for_wrong_slot() -> None:
    with _controller() as controller:
        thread = threading.Thread(target=controller.serve_once)
        thread.start()
        exchange = _client(controller).exchange(
            _request(slot_id="payload-9"),
            maximum_result_age_s=1.0,
        )
        thread.join(timeout=2.0)

        assert exchange.attempts == 1
        assert exchange.terminal_result is not None
        assert exchange.terminal_result.status is PayloadHilResultStatus.REJECTED
        assert exchange.terminal_result.reason is not None
        assert "not installed" in exchange.terminal_result.reason
        assert controller.rejected_datagrams == 1


def test_udp_hil_never_reports_execution_without_explicit_inert_simulation() -> None:
    with _controller() as controller:

        def serve() -> None:
            controller.serve_once()
            controller.serve_once()

        thread = threading.Thread(target=serve)
        thread.start()
        with pytest.raises(TimeoutError, match="no terminal result"):
            _client(controller, attempts=2).exchange(
                _request(),
                maximum_result_age_s=1.0,
            )
        thread.join(timeout=2.0)

        assert not thread.is_alive()
        assert controller.received_datagrams == 2
        assert controller.command_messages_sent == 0

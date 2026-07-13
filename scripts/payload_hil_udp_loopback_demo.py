from __future__ import annotations

import json
import threading
import time

from multidetect.payload_hil_protocol import (
    PayloadHilCodec,
    PayloadHilReleaseRequest,
    PayloadHilRequestGuard,
)
from multidetect.payload_hil_udp import UdpInertPayloadHilController, UdpPayloadHilClient


def main() -> int:
    request_key = b"payload-hil-loopback-request-key-32-byte-minimum"
    result_key = b"payload-hil-loopback-result-key-32-bytes-minimum"
    request_key_id = "loopback-request-key-v1"
    result_key_id = "loopback-result-key-v1"
    request_codec = PayloadHilCodec(
        hmac_key=request_key,
        expected_key_id=request_key_id,
    )
    result_codec = PayloadHilCodec(
        hmac_key=result_key,
        expected_key_id=result_key_id,
    )
    now_s = time.monotonic()
    request = PayloadHilReleaseRequest(
        mission_id="fire-fixed-wing-hil-001",
        module_id="inert-controller-loopback",
        release_id="loopback-release-001",
        payload_slot_id="payload-1",
        payload_type="fire_suppression_agent",
        authorization_challenge_id="loopback-challenge-001",
        operator_id="loopback-operator",
        target_id="loopback-track-001",
        target_revision=2,
        scene_digest="loopback-scene-digest",
        ruleset_version="safety-rules-fixed-wing-hil-v1",
        requested_at_s=now_s,
        expires_at_s=now_s + 2.0,
        sequence=1,
        key_id=request_key_id,
    )
    with UdpInertPayloadHilController(
        bind_host="127.0.0.1",
        port=0,
        request_codec=request_codec,
        result_codec=result_codec,
        request_guard=PayloadHilRequestGuard(
            mission_id=request.mission_id,
            module_id=request.module_id,
            installed_slots={request.payload_slot_id: request.payload_type},
            maximum_age_s=1.0,
        ),
    ) as controller:

        def serve() -> None:
            controller.serve_once(
                simulate_inert_execution=True,
                drop_first_response=True,
            )
            controller.serve_once(simulate_inert_execution=True)

        thread = threading.Thread(target=serve)
        thread.start()
        exchange = UdpPayloadHilClient(
            host=controller.local_address[0],
            port=controller.local_address[1],
            request_codec=request_codec,
            result_codec=result_codec,
            response_timeout_s=0.1,
            maximum_attempts=3,
        ).exchange(request, maximum_result_age_s=1.0)
        thread.join(timeout=2.0)
        if thread.is_alive():
            raise RuntimeError("payload HIL loopback controller did not stop")
        print(
            json.dumps(
                {
                    "event": "payload_hil_udp_loopback_finished",
                    "bind_host": controller.local_address[0],
                    "attempts": exchange.attempts,
                    "statuses": [result.status.value for result in exchange.results],
                    "terminal_status": (
                        exchange.terminal_result.status.value
                        if exchange.terminal_result is not None
                        else None
                    ),
                    "first_response_dropped": True,
                    "idempotent_retry_verified": exchange.attempts == 2,
                    "received_datagrams": controller.received_datagrams,
                    "command_messages_sent": controller.command_messages_sent,
                    "independent_confirmation_still_required": True,
                    "mission_port_connected": False,
                    "simulation_only": True,
                    "inert_load_required": True,
                    "flight_control_enabled": False,
                    "physical_release_enabled": controller.physical_release_enabled,
                },
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

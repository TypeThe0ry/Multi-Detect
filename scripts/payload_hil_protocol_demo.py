from __future__ import annotations

import json

from multidetect.payload_hil_protocol import (
    PayloadHilCodec,
    PayloadHilReleaseRequest,
    PayloadHilRequestGuard,
    PayloadHilResult,
    PayloadHilResultGuard,
    PayloadHilResultStatus,
)


def main() -> int:
    request_key = b"local-payload-hil-request-key-32-byte-minimum"
    result_key = b"local-payload-hil-result-key-32-bytes-minimum"
    request_key_id = "demo-payload-request-key-v1"
    result_key_id = "demo-payload-result-key-v1"
    request_codec = PayloadHilCodec(hmac_key=request_key, expected_key_id=request_key_id)
    result_codec = PayloadHilCodec(hmac_key=result_key, expected_key_id=result_key_id)
    request = PayloadHilReleaseRequest(
        mission_id="fire-fixed-wing-hil-001",
        module_id="inert-controller-demo",
        release_id="demo-release-001",
        payload_slot_id="payload-1",
        payload_type="fire_suppression_agent",
        authorization_challenge_id="demo-challenge-001",
        operator_id="demo-operator",
        target_id="demo-track-001",
        target_revision=4,
        scene_digest="demo-scene-digest",
        ruleset_version="safety-rules-fixed-wing-hil-v1",
        requested_at_s=100.0,
        expires_at_s=105.0,
        sequence=1,
        key_id=request_key_id,
    )
    encoded_request = request_codec.encode_request(request)
    decoded_request = request_codec.decode_request(encoded_request)
    request_guard = PayloadHilRequestGuard(
        mission_id=request.mission_id,
        module_id=request.module_id,
        installed_slots={request.payload_slot_id: request.payload_type},
        maximum_age_s=1.0,
    )
    request_verification = request_guard.verify(decoded_request, now_s=100.2)
    accepted = PayloadHilResult(
        mission_id=request.mission_id,
        module_id=request.module_id,
        release_id=request.release_id,
        payload_slot_id=request.payload_slot_id,
        status=PayloadHilResultStatus.ACCEPTED,
        observed_at_s=100.3,
        sequence=1,
        key_id=result_key_id,
        controller_healthy=True,
        interlock_healthy=True,
    )
    executed = PayloadHilResult(
        mission_id=request.mission_id,
        module_id=request.module_id,
        release_id=request.release_id,
        payload_slot_id=request.payload_slot_id,
        status=PayloadHilResultStatus.EXECUTED,
        observed_at_s=100.5,
        sequence=2,
        key_id=result_key_id,
        controller_healthy=True,
        interlock_healthy=True,
    )
    result_guard = PayloadHilResultGuard(request=request, maximum_age_s=1.0)
    accepted_verification = result_guard.verify(
        result_codec.decode_result(result_codec.encode_result(accepted)),
        now_s=100.4,
    )
    executed_verification = result_guard.verify(
        result_codec.decode_result(result_codec.encode_result(executed)),
        now_s=100.6,
    )
    request_guard.finish(release_id=request.release_id)
    print(
        json.dumps(
            {
                "event": "payload_hil_protocol_demo_finished",
                "request_bytes": len(encoded_request),
                "request_valid": request_verification.valid,
                "accepted_result_valid": accepted_verification.valid,
                "executed_result_valid": executed_verification.valid,
                "idempotency_verified": request_guard.verify(
                    request, now_s=100.7
                ).idempotent_replay,
                "directional_keys_separated": True,
                "independent_confirmation_still_required": True,
                "port_connected_to_mission": False,
                "simulation_only": True,
                "inert_load_required": True,
                "flight_control_enabled": False,
                "physical_release_enabled": False,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

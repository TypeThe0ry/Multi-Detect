from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from multidetect.config import MissionConfig
from multidetect.domain import MissionPhase, PayloadState
from multidetect.mission import MissionController
from multidetect.payload_confirmation_hil import (
    MissionPayloadConfirmationHilAdapter,
    PayloadConfirmationHilCodec,
    PayloadConfirmationHilMessage,
)
from multidetect.payload_confirmation_udp import (
    UdpPayloadConfirmationHilReceiver,
    UdpPayloadConfirmationHilSender,
)
from multidetect.payload_hil_mission import MissionPayloadHilAdapter
from multidetect.payload_hil_protocol import (
    PayloadHilCodec,
    PayloadHilRequestGuard,
)
from multidetect.payload_hil_udp import UdpInertPayloadHilController, UdpPayloadHilClient
from multidetect.replay import load_jsonl_replay


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = MissionConfig.from_json(
        root / "configs/missions/fire_suppression_fixed_wing.demo.json"
    )
    source_frames = load_jsonl_replay(root / "examples/fire_fixed_wing_hil_replay.jsonl")
    base_s = time.monotonic() - 3.2
    frames = tuple(
        replace(frame, captured_at_s=base_s + index) for index, frame in enumerate(source_frames)
    )
    mission = MissionController(config)
    mission.launch(now_s=base_s - 2.0)
    mission.arrive_task_area(now_s=base_s - 1.0)
    challenge = None
    for frame in frames:
        outcome = mission.process_observation(frame, now_s=frame.captured_at_s)
        challenge = outcome.challenge or challenge
    if challenge is None:
        raise RuntimeError("fixed-wing replay produced no authorization challenge")
    mission.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="mission-loopback-operator",
        now_s=max(frames[-1].captured_at_s + 0.01, time.monotonic()),
    )

    request_key = b"mission-loopback-request-key-32-byte-minimum"
    result_key = b"mission-loopback-result-key-32-bytes-minimum"
    request_key_id = "mission-loopback-request-v1"
    result_key_id = "mission-loopback-result-v1"
    request_codec = PayloadHilCodec(hmac_key=request_key, expected_key_id=request_key_id)
    result_codec = PayloadHilCodec(hmac_key=result_key, expected_key_id=result_key_id)
    with UdpInertPayloadHilController(
        bind_host="127.0.0.1",
        port=0,
        request_codec=request_codec,
        result_codec=result_codec,
        request_guard=PayloadHilRequestGuard(
            mission_id=config.mission_id,
            module_id="inert-controller-loopback",
            installed_slots={"payload-1": "fire_suppression_agent"},
            maximum_age_s=1.0,
        ),
    ) as controller:
        server = threading.Thread(
            target=controller.serve_once,
            kwargs={"simulate_inert_execution": True},
        )
        server.start()
        adapter = MissionPayloadHilAdapter(
            mission=mission,
            client=UdpPayloadHilClient(
                host=controller.local_address[0],
                port=controller.local_address[1],
                request_codec=request_codec,
                result_codec=result_codec,
                response_timeout_s=0.25,
                maximum_attempts=2,
            ),
            module_id="inert-controller-loopback",
            request_key_id=request_key_id,
        )
        hil_outcome = adapter.request_and_exchange(now_s=time.monotonic())
        server.join(timeout=2.0)
        if server.is_alive():
            raise RuntimeError("mission HIL loopback controller did not stop")
        if mission.state.phase is not MissionPhase.VERIFYING_RELEASE:
            raise RuntimeError("controller execution bypassed the independent confirmation leg")
        confirmation_key = b"mission-loopback-independent-sensor-key-32-byte-minimum"
        confirmation_key_id = "mission-loopback-independent-sensor-v1"
        confirmation_codec = PayloadConfirmationHilCodec(
            hmac_key=confirmation_key,
            expected_key_id=confirmation_key_id,
        )
        confirmation_adapter = MissionPayloadConfirmationHilAdapter(
            mission=mission,
            release_id=hil_outcome.request.release_id,
            controller_module_id=hil_outcome.request.module_id,
            allowed_sensor_ids=frozenset({"bay-departure-sensor-loopback"}),
            codec=confirmation_codec,
        )
        confirmation_now_s = time.monotonic()
        with UdpPayloadConfirmationHilReceiver(
            bind_host="127.0.0.1",
            port=0,
        ) as confirmation_receiver:
            confirmation_sender = UdpPayloadConfirmationHilSender(
                host=confirmation_receiver.local_address[0],
                port=confirmation_receiver.local_address[1],
                codec=confirmation_codec,
            )
            confirmation_sender.send(
                PayloadConfirmationHilMessage(
                    mission_id=config.mission_id,
                    sensor_id="bay-departure-sensor-loopback",
                    release_id=hil_outcome.request.release_id,
                    payload_slot_id=hil_outcome.request.payload_slot_id,
                    payload_absent=True,
                    sensor_healthy=True,
                    observed_at_s=confirmation_now_s,
                    sequence=1,
                    key_id=confirmation_key_id,
                )
            )
            confirmation = confirmation_receiver.receive_until_confirmed(
                confirmation_adapter,
                timeout_s=0.5,
                clock=lambda: confirmation_now_s,
            )
            confirmation_datagrams = confirmation_receiver.received_datagrams
            confirmation_rejected_datagrams = confirmation_receiver.rejected_datagrams
        slot = mission.payload.get_slot("payload-1")
        if slot.state is not PayloadState.RELEASE_CONFIRMED:
            raise RuntimeError("independent confirmation did not complete the HIL transaction")
        print(
            json.dumps(
                {
                    "event": "payload_mission_hil_loopback_finished",
                    "mission_id": config.mission_id,
                    "module_id": hil_outcome.request.module_id,
                    "release_id": hil_outcome.request.release_id,
                    "payload_slot_id": hil_outcome.request.payload_slot_id,
                    "authorization_bound": (
                        hil_outcome.request.authorization_challenge_id == challenge.challenge_id
                    ),
                    "target_bound": hil_outcome.request.target_id == challenge.target_id,
                    "statuses": [result.status.value for result in hil_outcome.exchange.results],
                    "attempts": hil_outcome.exchange.attempts,
                    "independent_confirmation_required": True,
                    "independent_confirmation_received": True,
                    "independent_confirmation_authenticated": (confirmation.verification.valid),
                    "independent_sensor_id": confirmation.message.sensor_id,
                    "controller_and_sensor_id_separated": (
                        confirmation.message.sensor_id != hil_outcome.request.module_id
                    ),
                    "independent_confirmation_udp_datagrams": confirmation_datagrams,
                    "independent_confirmation_rejected_datagrams": (
                        confirmation_rejected_datagrams
                    ),
                    "final_payload_state": slot.state.value,
                    "final_mission_phase": mission.state.phase.value,
                    "command_messages_sent": controller.command_messages_sent,
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

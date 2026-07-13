from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

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
from multidetect.replay import load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]
HMAC_KEY = b"independent-confirmation-udp-key-32-byte-minimum"
KEY_ID = "independent-confirmation-udp-v1"
SENSOR_ID = "udp-bay-sensor-1"


def _verifying_mission() -> tuple[MissionController, str]:
    config = MissionConfig.from_json(
        ROOT / "configs/missions/fire_suppression_fixed_wing.demo.json"
    )
    frames = load_jsonl_replay(ROOT / "examples/fire_fixed_wing_hil_replay.jsonl")
    mission = MissionController(config)
    mission.launch(now_s=998.0)
    mission.arrive_task_area(now_s=999.0)
    challenge = None
    for frame in frames:
        outcome = mission.process_observation(frame, now_s=frame.captured_at_s)
        challenge = outcome.challenge or challenge
    assert challenge is not None
    mission.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="confirmation-udp-operator",
        now_s=1003.1,
    )
    release_id = mission.request_simulated_deployment(now_s=1003.2)
    mission.report_simulated_execution(release_id=release_id, now_s=1003.3)
    return mission, release_id


def _codec() -> PayloadConfirmationHilCodec:
    return PayloadConfirmationHilCodec(hmac_key=HMAC_KEY, expected_key_id=KEY_ID)


def _adapter(mission: MissionController, release_id: str) -> MissionPayloadConfirmationHilAdapter:
    return MissionPayloadConfirmationHilAdapter(
        mission=mission,
        release_id=release_id,
        controller_module_id="controller-module-1",
        allowed_sensor_ids=frozenset({SENSOR_ID}),
        codec=_codec(),
    )


def _message(release_id: str) -> PayloadConfirmationHilMessage:
    return PayloadConfirmationHilMessage(
        mission_id="fire-fixed-wing-hil-001",
        sensor_id=SENSOR_ID,
        release_id=release_id,
        payload_slot_id="payload-1",
        payload_absent=True,
        sensor_healthy=True,
        observed_at_s=1003.4,
        sequence=1,
        key_id=KEY_ID,
    )


def test_udp_independent_confirmation_advances_mission_without_commands() -> None:
    mission, release_id = _verifying_mission()
    with UdpPayloadConfirmationHilReceiver(bind_host="127.0.0.1", port=0) as receiver:
        sender = UdpPayloadConfirmationHilSender(
            host=receiver.local_address[0],
            port=receiver.local_address[1],
            codec=_codec(),
        )
        sender.send(_message(release_id))

        receipt = receiver.receive_until_confirmed(
            _adapter(mission, release_id),
            timeout_s=0.5,
            clock=lambda: 1003.5,
        )

        assert receipt.mission_advanced is True
        assert receiver.received_datagrams == 1
        assert receiver.rejected_datagrams == 0
        assert receiver.command_messages_sent == 0
        assert receiver.physical_release_enabled is False
        assert sender.command_messages_sent == 0
        assert sender.physical_release_enabled is False
    assert mission.state.phase is MissionPhase.RETURN_REQUESTED
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASE_CONFIRMED


def test_udp_receiver_ignores_tampered_packet_then_accepts_valid_evidence() -> None:
    mission, release_id = _verifying_mission()
    with UdpPayloadConfirmationHilReceiver(bind_host="127.0.0.1", port=0) as receiver:
        encoded = _codec().encode(_message(release_id))
        document = json.loads(encoded)
        document["sensor_healthy"] = False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as raw_sender:
            raw_sender.sendto(json.dumps(document).encode(), receiver.local_address)
        UdpPayloadConfirmationHilSender(
            host=receiver.local_address[0],
            port=receiver.local_address[1],
            codec=_codec(),
        ).send(_message(release_id))

        receipt = receiver.receive_until_confirmed(
            _adapter(mission, release_id),
            timeout_s=0.5,
            clock=lambda: 1003.5,
        )

        assert receipt.verification.valid is True
        assert receiver.received_datagrams == 2
        assert receiver.rejected_datagrams == 1


def test_udp_confirmation_timeout_does_not_fabricate_release_success() -> None:
    mission, release_id = _verifying_mission()
    with UdpPayloadConfirmationHilReceiver(bind_host="127.0.0.1", port=0) as receiver:
        with pytest.raises(TimeoutError, match="no valid"):
            receiver.receive_until_confirmed(
                _adapter(mission, release_id),
                timeout_s=0.02,
                clock=lambda: 1003.5,
            )

    assert mission.state.phase is MissionPhase.VERIFYING_RELEASE
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASED

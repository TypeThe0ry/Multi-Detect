from __future__ import annotations

from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.domain import MissionPhase, PayloadState
from multidetect.mission import MissionController
from multidetect.payload_confirmation_hil import (
    PayloadConfirmationHilCodec,
    PayloadConfirmationHilMessage,
)
from multidetect.payload_hil_cycle import InertPayloadHilCycleCoordinator
from multidetect.payload_hil_mission import MissionPayloadHilAdapter, MissionPayloadHilError
from multidetect.payload_hil_protocol import (
    PayloadHilReleaseRequest,
    PayloadHilResult,
    PayloadHilResultStatus,
)
from multidetect.payload_hil_udp import PayloadHilExchange
from multidetect.replay import load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]
CONFIRMATION_KEY = b"cycle-confirmation-hil-key-32-byte-minimum"
CONFIRMATION_KEY_ID = "cycle-confirmation-key-v1"
SENSOR_ID = "cycle-bay-sensor-1"
CONTROLLER_ID = "cycle-controller-1"


class _ExecutedClient:
    def exchange(
        self,
        request: PayloadHilReleaseRequest,
        *,
        maximum_result_age_s: float,
    ) -> PayloadHilExchange:
        result = PayloadHilResult(
            mission_id=request.mission_id,
            module_id=request.module_id,
            release_id=request.release_id,
            payload_slot_id=request.payload_slot_id,
            status=PayloadHilResultStatus.EXECUTED,
            observed_at_s=request.requested_at_s + 0.05,
            sequence=1,
            key_id="cycle-result-key-v1",
            controller_healthy=True,
            interlock_healthy=True,
        )
        return PayloadHilExchange(request.release_id, 1, (result,))


class _ConfirmationReceiver:
    def __init__(self, codec: PayloadConfirmationHilCodec, *, timeout: bool = False) -> None:
        self.codec = codec
        self.timeout = timeout
        self.closed = False

    def receive_until_confirmed(self, adapter, *, timeout_s: float, clock):
        if self.timeout:
            raise TimeoutError("simulated independent sensor timeout")
        message = PayloadConfirmationHilMessage(
            mission_id="fire-fixed-wing-hil-001",
            sensor_id=SENSOR_ID,
            release_id=adapter.release_id,
            payload_slot_id="payload-1",
            payload_absent=True,
            sensor_healthy=True,
            observed_at_s=1003.4,
            sequence=1,
            key_id=CONFIRMATION_KEY_ID,
        )
        return adapter.accept(self.codec.encode(message), now_s=float(clock()))

    def close(self) -> None:
        self.closed = True


def _ready_mission() -> MissionController:
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
        operator_id="cycle-operator",
        now_s=1003.1,
    )
    return mission


def _coordinator(
    mission: MissionController,
    receiver: _ConfirmationReceiver,
) -> InertPayloadHilCycleCoordinator:
    codec = receiver.codec
    return InertPayloadHilCycleCoordinator(
        mission=mission,
        controller_adapter=MissionPayloadHilAdapter(
            mission=mission,
            client=_ExecutedClient(),
            module_id=CONTROLLER_ID,
            request_key_id="cycle-request-key-v1",
            clock=lambda: 1003.3,
        ),
        confirmation_receiver=receiver,
        confirmation_codec=codec,
        controller_module_id=CONTROLLER_ID,
        allowed_confirmation_sensor_ids=frozenset({SENSOR_ID}),
        confirmation_timeout_s=0.5,
        clock=lambda: 1003.5,
    )


def _codec() -> PayloadConfirmationHilCodec:
    return PayloadConfirmationHilCodec(
        hmac_key=CONFIRMATION_KEY,
        expected_key_id=CONFIRMATION_KEY_ID,
    )


def test_two_channel_cycle_completes_only_after_independent_confirmation() -> None:
    mission = _ready_mission()
    receiver = _ConfirmationReceiver(_codec())
    coordinator = _coordinator(mission, receiver)

    outcome = coordinator.execute(now_s=1003.2)
    coordinator.close()

    assert outcome.controller.execution_result.status is PayloadHilResultStatus.EXECUTED
    assert outcome.confirmation.verification.valid is True
    assert outcome.physical_release_enabled is False
    assert mission.state.phase is MissionPhase.RETURN_REQUESTED
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASE_CONFIRMED
    assert receiver.closed is True


def test_independent_confirmation_timeout_faults_as_uncertain() -> None:
    mission = _ready_mission()
    receiver = _ConfirmationReceiver(_codec(), timeout=True)
    coordinator = _coordinator(mission, receiver)

    with pytest.raises(MissionPayloadHilError, match="no safe result"):
        coordinator.execute(now_s=1003.2)

    slot = mission.payload.get_slot("payload-1")
    assert mission.state.phase is MissionPhase.FAULT
    assert slot.state is PayloadState.FAILED
    assert slot.uncertain_release is True
    assert "independent payload confirmation failed" in (slot.failure_reason or "")

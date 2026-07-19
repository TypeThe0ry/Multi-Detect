from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.domain import MissionPhase, PayloadState
from multidetect.mission import MissionController
from multidetect.payload_hil_mission import MissionPayloadHilAdapter, MissionPayloadHilError
from multidetect.payload_hil_protocol import (
    PayloadHilCodec,
    PayloadHilReleaseRequest,
    PayloadHilRequestGuard,
    PayloadHilResult,
    PayloadHilResultStatus,
)
from multidetect.payload_hil_udp import (
    PayloadHilExchange,
    UdpInertPayloadHilController,
    UdpPayloadHilClient,
)
from multidetect.replay import load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]
REQUEST_KEY = b"mission-payload-hil-request-key-32-byte-minimum"
RESULT_KEY = b"mission-payload-hil-result-key-32-byte-minimum"
REQUEST_KEY_ID = "mission-request-key-v1"
RESULT_KEY_ID = "mission-result-key-v1"


class _ResultClient:
    def __init__(self, status: PayloadHilResultStatus) -> None:
        self.status = status
        self.requests: list[PayloadHilReleaseRequest] = []

    def exchange(
        self,
        request: PayloadHilReleaseRequest,
        *,
        maximum_result_age_s: float,
    ) -> PayloadHilExchange:
        assert maximum_result_age_s == pytest.approx(1.0)
        self.requests.append(request)
        reason = None
        if self.status in {PayloadHilResultStatus.REJECTED, PayloadHilResultStatus.FAILED}:
            reason = f"simulated {self.status.value}"
        result = PayloadHilResult(
            mission_id=request.mission_id,
            module_id=request.module_id,
            release_id=request.release_id,
            payload_slot_id=request.payload_slot_id,
            status=self.status,
            observed_at_s=request.requested_at_s + 0.05,
            sequence=1,
            key_id=RESULT_KEY_ID,
            controller_healthy=self.status is not PayloadHilResultStatus.FAILED,
            interlock_healthy=self.status is PayloadHilResultStatus.EXECUTED,
            reason=reason,
        )
        return PayloadHilExchange(request.release_id, 1, (result,))


class _TimeoutClient:
    def exchange(
        self,
        request: PayloadHilReleaseRequest,
        *,
        maximum_result_age_s: float,
    ) -> PayloadHilExchange:
        raise TimeoutError("simulated controller timeout")


def _ready_fixed_wing_mission() -> tuple[MissionController, object]:
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
        operator_id="mission-hil-operator",
        now_s=1003.1,
    )
    return mission, challenge


def test_executed_hil_result_advances_only_first_confirmation_leg() -> None:
    mission, challenge = _ready_fixed_wing_mission()
    client = _ResultClient(PayloadHilResultStatus.EXECUTED)
    adapter = MissionPayloadHilAdapter(
        mission=mission,
        client=client,
        module_id="inert-controller-1",
        request_key_id=REQUEST_KEY_ID,
        clock=lambda: 1003.3,
    )

    outcome = adapter.request_and_exchange(now_s=1003.2)

    request = outcome.request
    assert request.authorization_challenge_id == challenge.challenge_id
    assert request.operator_id == "mission-hil-operator"
    assert request.target_id == challenge.target_id
    assert request.target_revision == challenge.target_revision
    assert request.scene_digest == challenge.scene_digest
    assert request.ruleset_version == challenge.ruleset_version
    assert request.payload_slot_id == "payload-1"
    assert request.payload_type == "fire_suppression_agent"
    assert request.simulation_only is True
    assert request.physical_release_enabled is False
    assert mission.state.phase is MissionPhase.VERIFYING_RELEASE
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASED
    assert mission.fake_payload_port.request_count == 1

    mission.report_independent_confirmation(
        release_id=request.release_id,
        source_id="independent-bay-sensor",
        now_s=1003.4,
    )

    assert mission.state.phase is MissionPhase.RETURN_REQUESTED
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASE_CONFIRMED
    event_types = [event.event_type for event in mission.audit.events()]
    assert "payload.hil_exchange_started" in event_types
    assert "payload.hil_terminal_result" in event_types


def test_hil_rejection_fails_closed_without_automatic_retry() -> None:
    mission, _challenge = _ready_fixed_wing_mission()
    client = _ResultClient(PayloadHilResultStatus.REJECTED)
    adapter = MissionPayloadHilAdapter(
        mission=mission,
        client=client,
        module_id="inert-controller-1",
        request_key_id=REQUEST_KEY_ID,
        clock=lambda: 1003.3,
    )

    with pytest.raises(MissionPayloadHilError, match="reported rejected"):
        adapter.request_and_exchange(now_s=1003.2)

    slot = mission.payload.get_slot("payload-1")
    assert len(client.requests) == 1
    assert mission.state.phase is MissionPhase.FAULT
    assert slot.state is PayloadState.FAILED
    assert slot.uncertain_release is False
    assert mission.fake_payload_port.request_count == 1


def test_hil_timeout_fails_closed_as_uncertain() -> None:
    mission, _challenge = _ready_fixed_wing_mission()
    adapter = MissionPayloadHilAdapter(
        mission=mission,
        client=_TimeoutClient(),
        module_id="inert-controller-1",
        request_key_id=REQUEST_KEY_ID,
        clock=lambda: float("nan"),
    )

    with pytest.raises(MissionPayloadHilError, match="no safe result"):
        adapter.request_and_exchange(now_s=1003.2)

    slot = mission.payload.get_slot("payload-1")
    assert mission.state.phase is MissionPhase.FAULT
    assert slot.state is PayloadState.FAILED
    assert slot.uncertain_release is True


def test_real_udp_loopback_closes_authorized_fixed_wing_hil_path() -> None:
    config = MissionConfig.from_json(
        ROOT / "configs/missions/fire_suppression_fixed_wing.demo.json"
    )
    source_frames = load_jsonl_replay(ROOT / "examples/fire_fixed_wing_hil_replay.jsonl")
    base_s = time.monotonic() - 3.2
    frames = []
    for index, frame in enumerate(source_frames):
        captured_at_s = base_s + index
        frames.append(
            replace(
                frame,
                captured_at_s=captured_at_s,
                telemetry=replace(
                    frame.telemetry,
                    velocity_observed_at_s=captured_at_s,
                    airspeed_observed_at_s=captured_at_s,
                    wind_observed_at_s=captured_at_s,
                ),
            )
        )
    frames = tuple(frames)
    mission = MissionController(config)
    mission.launch(now_s=base_s - 2.0)
    mission.arrive_task_area(now_s=base_s - 1.0)
    challenge = None
    for frame in frames:
        outcome = mission.process_observation(frame, now_s=frame.captured_at_s)
        challenge = outcome.challenge or challenge
    assert challenge is not None
    approved_at_s = max(frames[-1].captured_at_s + 0.01, time.monotonic())
    mission.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="udp-loopback-operator",
        now_s=approved_at_s,
    )
    request_codec = PayloadHilCodec(hmac_key=REQUEST_KEY, expected_key_id=REQUEST_KEY_ID)
    result_codec = PayloadHilCodec(hmac_key=RESULT_KEY, expected_key_id=RESULT_KEY_ID)
    with UdpInertPayloadHilController(
        bind_host="127.0.0.1",
        port=0,
        request_codec=request_codec,
        result_codec=result_codec,
        request_guard=PayloadHilRequestGuard(
            mission_id=config.mission_id,
            module_id="inert-controller-1",
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
            module_id="inert-controller-1",
            request_key_id=REQUEST_KEY_ID,
        )
        outcome = adapter.request_and_exchange(now_s=time.monotonic())
        server.join(timeout=2.0)

        assert not server.is_alive()
        assert outcome.execution_result.status is PayloadHilResultStatus.EXECUTED
        assert controller.received_datagrams == 1
        assert controller.command_messages_sent == 0
        assert controller.physical_release_enabled is False

    mission.report_independent_confirmation(
        release_id=outcome.request.release_id,
        source_id="udp-loopback-independent-sensor",
        now_s=time.monotonic(),
    )
    assert mission.state.phase is MissionPhase.RETURN_REQUESTED
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASE_CONFIRMED

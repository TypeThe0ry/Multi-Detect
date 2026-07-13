from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.domain import MissionPhase, PayloadState
from multidetect.mission import MissionController
from multidetect.payload_confirmation_hil import (
    MissionPayloadConfirmationHilAdapter,
    PayloadConfirmationHilCodec,
    PayloadConfirmationHilError,
    PayloadConfirmationHilMessage,
)
from multidetect.replay import load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]
HMAC_KEY = b"independent-confirmation-test-key-32-byte-minimum"
KEY_ID = "independent-sensor-key-v1"
SENSOR_ID = "bay-departure-sensor-1"
CONTROLLER_ID = "inert-controller-1"


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
        operator_id="confirmation-test-operator",
        now_s=1003.1,
    )
    release_id = mission.request_simulated_deployment(now_s=1003.2)
    mission.report_simulated_execution(release_id=release_id, now_s=1003.3)
    assert mission.state.phase is MissionPhase.VERIFYING_RELEASE
    return mission, release_id


def _codec() -> PayloadConfirmationHilCodec:
    return PayloadConfirmationHilCodec(hmac_key=HMAC_KEY, expected_key_id=KEY_ID)


def _adapter(
    mission: MissionController,
    release_id: str,
) -> MissionPayloadConfirmationHilAdapter:
    return MissionPayloadConfirmationHilAdapter(
        mission=mission,
        release_id=release_id,
        controller_module_id=CONTROLLER_ID,
        allowed_sensor_ids=frozenset({SENSOR_ID}),
        codec=_codec(),
        maximum_age_s=1.0,
    )


def _message(active_release_id: str, **changes: object) -> PayloadConfirmationHilMessage:
    values: dict[str, object] = {
        "mission_id": "fire-fixed-wing-hil-001",
        "sensor_id": SENSOR_ID,
        "release_id": active_release_id,
        "payload_slot_id": "payload-1",
        "payload_absent": True,
        "sensor_healthy": True,
        "observed_at_s": 1003.4,
        "sequence": 7,
        "key_id": KEY_ID,
    }
    values.update(changes)
    return PayloadConfirmationHilMessage(**values)


def test_authenticated_independent_sensor_completes_release() -> None:
    mission, release_id = _verifying_mission()
    adapter = _adapter(mission, release_id)
    encoded = _codec().encode(_message(release_id))

    receipt = adapter.accept(encoded, now_s=1003.5)

    assert receipt.verification.valid is True
    assert receipt.mission_advanced is True
    assert receipt.simulation_only is True
    assert receipt.physical_release_enabled is False
    assert mission.state.phase is MissionPhase.RETURN_REQUESTED
    slot = mission.payload.get_slot("payload-1")
    assert slot.state is PayloadState.RELEASE_CONFIRMED
    assert slot.independent_confirmation_sources == (f"independent-hil:{SENSOR_ID}",)

    replay = adapter.accept(encoded, now_s=1003.6)
    assert replay.verification.idempotent_replay is True
    assert replay.mission_advanced is False


def test_tampered_confirmation_is_rejected_without_advancing_mission() -> None:
    mission, release_id = _verifying_mission()
    adapter = _adapter(mission, release_id)
    document = json.loads(_codec().encode(_message(release_id)))
    document["payload_absent"] = False

    with pytest.raises(PayloadConfirmationHilError, match="HMAC"):
        adapter.accept(json.dumps(document).encode(), now_s=1003.5)

    assert mission.state.phase is MissionPhase.VERIFYING_RELEASE
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASED


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"mission_id": "another-mission"}, "mission ID"),
        ({"release_id": "another-release"}, "release ID"),
        ({"payload_slot_id": "payload-9"}, "payload slot"),
        ({"sensor_id": CONTROLLER_ID}, "not independent"),
        ({"sensor_healthy": False}, "health"),
        ({"payload_absent": False}, "payload departure"),
        ({"observed_at_s": 1001.0}, "stale"),
    ],
)
def test_invalid_authenticated_evidence_stays_fail_closed(
    changes: dict[str, object], reason: str
) -> None:
    mission, release_id = _verifying_mission()
    adapter = _adapter(mission, release_id)
    encoded = _codec().encode(_message(release_id, **changes))

    with pytest.raises(PayloadConfirmationHilError, match=reason):
        adapter.accept(encoded, now_s=1003.5)

    assert mission.state.phase is MissionPhase.VERIFYING_RELEASE
    assert mission.payload.get_slot("payload-1").state is PayloadState.RELEASED


def test_same_sequence_with_changed_content_is_rejected() -> None:
    mission, release_id = _verifying_mission()
    adapter = _adapter(mission, release_id)
    original = _codec().encode(_message(release_id))
    receipt = adapter.accept(original, now_s=1003.5)
    assert receipt.mission_advanced is True

    changed = _codec().encode(_message(release_id, observed_at_s=1003.45))
    with pytest.raises(PayloadConfirmationHilError, match="without a new sequence"):
        adapter.accept(changed, now_s=1003.6)


def test_controller_identity_cannot_be_registered_as_independent_sensor() -> None:
    mission, release_id = _verifying_mission()

    with pytest.raises(ValueError, match="must differ"):
        MissionPayloadConfirmationHilAdapter(
            mission=mission,
            release_id=release_id,
            controller_module_id=CONTROLLER_ID,
            allowed_sensor_ids=frozenset({CONTROLLER_ID}),
            codec=_codec(),
        )


def test_confirmation_message_cannot_enable_physical_release() -> None:
    with pytest.raises(PayloadConfirmationHilError, match="cannot enable"):
        _message("release-1", physical_release_enabled=True)

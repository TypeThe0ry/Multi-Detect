from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import MissionConfig
from .domain import BoundingBox, DeploymentWindowStatus, Detection, MissionPhase, SensorKind
from .mission import MissionController
from .operator_link import (
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    operator_identifier_token,
)
from .operator_status import build_authorization_challenge_status_message
from .replay import load_jsonl_replay


def run_software_acceptance(project_root: Path) -> dict[str, Any]:
    """Exercise patrol, authorized fixed-wing HIL and person-veto paths without hardware."""

    root = project_root.resolve()
    patrol_config = MissionConfig.from_json(root / "configs/missions/fire_patrol.demo.json")
    patrol_frames = load_jsonl_replay(root / "examples/fire_mission_replay.jsonl")
    patrol = MissionController(patrol_config)
    _start(patrol, patrol_frames[0].captured_at_s)
    patrol_alerts = []
    patrol_challenges = 0
    for frame in patrol_frames:
        outcome = patrol.process_observation(frame, now_s=frame.captured_at_s)
        patrol_alerts.extend(outcome.alerts)
        patrol_challenges += outcome.challenge is not None
    patrol_status = patrol.status()
    if not (
        len(patrol_alerts) == 1
        and patrol_challenges == 0
        and patrol_status.phase is MissionPhase.SEARCHING
        and patrol.fake_payload_port.request_count == 0
    ):
        raise RuntimeError("patrol-only acceptance path failed")

    fixed_config = MissionConfig.from_json(
        root / "configs/missions/fire_suppression_fixed_wing.demo.json"
    )
    fixed_frames = load_jsonl_replay(root / "examples/fire_fixed_wing_hil_replay.jsonl")
    payload = MissionController(fixed_config)
    _start(payload, fixed_frames[0].captured_at_s)
    challenge = None
    ready_window = None
    for frame in fixed_frames:
        outcome = payload.process_observation(frame, now_s=frame.captured_at_s)
        if outcome.decisions and outcome.decisions[0].deployment_window is not None:
            ready_window = outcome.decisions[0].deployment_window
        challenge = outcome.challenge or challenge
    if (
        challenge is None
        or ready_window is None
        or ready_window.status is not DeploymentWindowStatus.READY
        or payload.fake_payload_port.request_count != 0
    ):
        raise RuntimeError("fixed-wing readiness acceptance path failed")
    challenge_status = build_authorization_challenge_status_message(
        challenge=challenge,
        sequence=1,
        produced_at_s=fixed_frames[-1].captured_at_s + 0.05,
    )
    remote_guard = AuthorizationDecisionCommandGuard(clock_tolerance_s=0.0)
    remote_guard.set_active_challenge(challenge_status)
    remote_command = AuthorizationDecisionCommand(
        command_token=operator_identifier_token("software-acceptance-command"),
        session_token=operator_identifier_token("software-acceptance-session"),
        challenge_token=challenge_status.challenge_token,
        mission_token=challenge_status.mission_token,
        target_token=challenge_status.target_token,
        scene_token=challenge_status.scene_token,
        ruleset_token=challenge_status.ruleset_token,
        payload_slot_token=challenge_status.payload_slot_token,
        target_revision=challenge_status.target_revision,
        decision=AuthorizationDecision.APPROVE,
        operator_token=operator_identifier_token("software-acceptance-operator"),
        sequence=2,
        issued_at_s=fixed_frames[-1].captured_at_s + 0.05,
        expires_at_s=fixed_frames[-1].captured_at_s + 2.05,
    )
    remote_acceptance = remote_guard.evaluate(
        remote_command,
        received_at_s=fixed_frames[-1].captured_at_s + 0.06,
    )
    if not remote_acceptance.allowed:
        raise RuntimeError("G20 authorization command binding acceptance failed")
    payload.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id=f"g20:{remote_command.operator_token:016x}",
        now_s=fixed_frames[-1].captured_at_s + 0.1,
    )
    remote_ready_phase = payload.status().phase
    if (
        remote_ready_phase is not MissionPhase.DEPLOYMENT_READY
        or payload.fake_payload_port.request_count != 0
    ):
        raise RuntimeError("authorization directly triggered a fake payload request")
    release_id = payload.request_simulated_deployment(now_s=fixed_frames[-1].captured_at_s + 0.2)
    payload.report_simulated_execution(
        release_id=release_id,
        now_s=fixed_frames[-1].captured_at_s + 0.3,
    )
    payload.report_independent_confirmation(
        release_id=release_id,
        source_id="software-acceptance-independent-sensor",
        now_s=fixed_frames[-1].captured_at_s + 0.4,
    )
    payload_status = payload.status()
    if not (
        payload.fake_payload_port.request_count == 1
        and payload.payload.remaining_payload_count == 0
        and payload_status.phase is MissionPhase.RETURN_REQUESTED
    ):
        raise RuntimeError("fixed-wing fake payload transaction failed")

    veto = MissionController(fixed_config)
    _start(veto, fixed_frames[0].captured_at_s)
    veto_decisions = ()
    veto_challenges = 0
    for frame in fixed_frames:
        person = Detection(
            "person",
            0.95,
            BoundingBox(0.46, 0.41, 0.54, 0.49),
            SensorKind.RGB,
            "qualified-person-hil",
        )
        unsafe_frame = replace(frame, detections=(*frame.detections, person))
        outcome = veto.process_observation(unsafe_frame, now_s=unsafe_frame.captured_at_s)
        veto_decisions = outcome.decisions or veto_decisions
        veto_challenges += outcome.challenge is not None
    if not (
        veto_decisions
        and all(not decision.allowed for decision in veto_decisions)
        and veto_challenges == 0
        and veto.fake_payload_port.request_count == 0
        and veto.status().phase is MissionPhase.SEARCHING
    ):
        raise RuntimeError("person-veto acceptance path failed")

    return {
        "event": "software_acceptance_passed",
        "patrol_only": {
            "alerts": len(patrol_alerts),
            "authorization_challenges": patrol_challenges,
            "fake_release_requests": patrol.fake_payload_port.request_count,
            "final_phase": patrol_status.phase.value,
        },
        "fixed_wing_payload_hil": {
            "release_window": ready_window.status.value,
            "advisory_only": ready_window.advisory_only,
            "authorization_challenges": 1,
            "fake_release_requests": payload.fake_payload_port.request_count,
            "remaining_payloads": payload.payload.remaining_payload_count,
            "final_phase": payload_status.phase.value,
        },
        "g20_authorization": {
            "decision": remote_command.decision.value,
            "binding_accepted": remote_acceptance.allowed,
            "nonce_transmitted": False,
            "phase_after_approval": remote_ready_phase.value,
            "fake_release_requests_after_approval": 0,
        },
        "person_veto": {
            "authorization_challenges": veto_challenges,
            "fake_release_requests": veto.fake_payload_port.request_count,
            "final_phase": veto.status().phase.value,
        },
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "model_training_executed": False,
        "model_inference_executed": False,
    }


def _start(controller: MissionController, first_timestamp: float) -> None:
    controller.launch(now_s=max(0.0, first_timestamp - 2.0))
    controller.arrive_task_area(now_s=max(0.0, first_timestamp - 1.0))


__all__ = ["run_software_acceptance"]

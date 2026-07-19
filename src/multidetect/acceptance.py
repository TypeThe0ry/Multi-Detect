from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import MissionConfig
from .domain import (
    BoundingBox,
    DeploymentWindowStatus,
    Detection,
    MissionPhase,
    SensorKind,
    TrackSnapshot,
)
from .manual_tracking import OpenCVManualTargetTracker
from .mission import MissionController
from .operator_link import (
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    VideoGeometry,
    operator_identifier_token,
)
from .operator_status import build_authorization_challenge_status_message
from .operator_tracking import OperatorTargetLock, TargetLockConfig
from .replay import load_jsonl_replay
from .unified_acceptance import run_unified_tracking_acceptance


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

    geometry = VideoGeometry("camera-main", 1280, 720)
    target_lock = OperatorTargetLock(
        geometry,
        TargetLockConfig(frozenset({"flame", "smoke"})),
    )
    selection = TargetSelectionCommand(
        command_id="55555555-5555-4555-8555-555555555555",
        session_id="66666666-6666-4666-8666-666666666666",
        sequence=1,
        action=SelectionAction.SELECT,
        geometry=geometry,
        issued_at_s=100.0,
        expires_at_s=103.0,
        bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
        displayed_frame_id="frame-100",
    )
    selection_guard = SelectionCommandGuard(geometry, clock_tolerance_s=0.0)
    selection_acceptance = selection_guard.evaluate(selection, received_at_s=100.1)
    selection_replay = selection_guard.evaluate(selection, received_at_s=100.2)
    if not (
        selection_acceptance.allowed
        and not selection_replay.allowed
        and "selection command ID has already been processed" in selection_replay.reasons
    ):
        raise RuntimeError("remote selection guard acceptance path failed")
    manual_backend = _AcceptanceManualTrackerBackend(updates=[(True, (448.0, 180.0, 384.0, 360.0))])
    manual_tracker = OpenCVManualTargetTracker(
        geometry,
        tracker_factory=lambda: manual_backend,
    )
    detector_fallback_lock = OperatorTargetLock(
        geometry,
        TargetLockConfig(frozenset({"flame", "smoke"})),
    )
    manual_selected = manual_tracker.apply_command(
        selection,
        image_bgr=object(),
        frame_id="frame-100",
        now_s=100.0,
    )
    detector_initializing = detector_fallback_lock.apply_command(
        selection,
        tracks=(),
        frame_id="frame-100",
        now_s=100.0,
    )
    manual_moved = manual_tracker.update(
        image_bgr=object(),
        frame_id="frame-101",
        captured_at_s=100.2,
        produced_at_s=100.2,
    )
    if manual_moved is None or manual_moved.bbox is None:
        raise RuntimeError("manual tracker produced no moving-box status")
    detector_fallback_lock.hint_bbox(manual_moved.bbox, now_s=100.2)
    detector_reacquired = detector_fallback_lock.update(
        tracks=(
            _acceptance_track(
                "track-fire-from-manual-hint",
                "flame",
                BoundingBox(0.40, 0.30, 0.60, 0.70),
                last_seen_at_s=100.3,
            ),
        ),
        frame_id="frame-102",
        captured_at_s=100.3,
        produced_at_s=100.31,
    )
    if not (
        manual_selected.state is TrackingState.TRACKING
        and detector_initializing.state is TrackingState.INITIALIZING
        and manual_moved.state is TrackingState.TRACKING
        and manual_moved.bbox != selection.bbox
        and detector_reacquired is not None
        and detector_reacquired.state is TrackingState.TRACKING
        and detector_reacquired.target_id == "track-fire-from-manual-hint"
    ):
        raise RuntimeError("detector-independent manual tracking bridge failed")

    person_track = _acceptance_track(
        "track-person",
        "person",
        BoundingBox(0.35, 0.25, 0.55, 0.65),
        last_seen_at_s=100.0,
    )
    fire_track = _acceptance_track(
        "track-fire-1",
        "flame",
        BoundingBox(0.40, 0.30, 0.58, 0.68),
        last_seen_at_s=100.0,
    )
    selected = target_lock.apply_command(
        selection,
        tracks=(person_track, fire_track),
        frame_id="frame-100",
        now_s=100.0,
    )
    moved_track = _acceptance_track(
        "track-fire-1",
        "flame",
        BoundingBox(0.42, 0.31, 0.59, 0.69),
        last_seen_at_s=100.2,
    )
    continued = target_lock.update(
        tracks=(moved_track,),
        frame_id="frame-101",
        captured_at_s=100.2,
        produced_at_s=100.21,
    )
    lost = target_lock.update(
        tracks=(moved_track,),
        frame_id="frame-102",
        captured_at_s=101.0,
        produced_at_s=101.0,
    )
    reacquired_track = _acceptance_track(
        "track-fire-2",
        "flame",
        BoundingBox(0.43, 0.32, 0.60, 0.70),
        last_seen_at_s=101.2,
    )
    reacquired = target_lock.update(
        tracks=(reacquired_track,),
        frame_id="frame-103",
        captured_at_s=101.2,
        produced_at_s=101.21,
    )
    lost_again = target_lock.update(
        tracks=(reacquired_track,),
        frame_id="frame-104",
        captured_at_s=102.0,
        produced_at_s=102.0,
    )
    rejected = target_lock.update(
        tracks=(),
        frame_id="frame-105",
        captured_at_s=104.1,
        produced_at_s=104.1,
    )
    if not (
        selected.state is TrackingState.TRACKING
        and selected.target_id == "track-fire-1"
        and selected.label == "flame"
        and continued is not None
        and continued.state is TrackingState.TRACKING
        and continued.target_id == "track-fire-1"
        and continued.bbox == moved_track.bbox
        and lost is not None
        and lost.state is TrackingState.LOST
        and reacquired is not None
        and reacquired.state is TrackingState.TRACKING
        and reacquired.target_id == "track-fire-2"
        and lost_again is not None
        and lost_again.state is TrackingState.LOST
        and rejected is not None
        and rejected.state is TrackingState.REJECTED
        and target_lock.active_track_id is None
    ):
        raise RuntimeError("manual target tracking acceptance path failed")

    unified_tracking = run_unified_tracking_acceptance()

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
        "manual_target_tracking": {
            "selected_state": selected.state.value,
            "selected_label": selected.label,
            "person_track_ignored": selected.target_id != person_track.track_id,
            "continuous_update_state": continued.state.value,
            "lost_state": lost.state.value,
            "reacquired_state": reacquired.state.value,
            "reacquired_with_new_track_id": reacquired.target_id != selected.target_id,
            "reacquisition_timeout_state": rejected.state.value,
            "manual_tracker_state_without_detection": manual_moved.state.value,
            "manual_tracker_bbox_changed": manual_moved.bbox != selection.bbox,
            "detector_initial_state": detector_initializing.state.value,
            "detector_reacquired_after_manual_hint": detector_reacquired.state.value,
            "selection_guard_accepted": selection_acceptance.allowed,
            "selection_replay_rejected": not selection_replay.allowed,
            "selection_is_payload_authorization": False,
        },
        "unified_tracking": unified_tracking,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "model_training_executed": False,
        "model_inference_executed": False,
    }


def _start(controller: MissionController, first_timestamp: float) -> None:
    controller.launch(now_s=max(0.0, first_timestamp - 2.0))
    controller.arrive_task_area(now_s=max(0.0, first_timestamp - 1.0))


def _acceptance_track(
    track_id: str,
    label: str,
    bbox: BoundingBox,
    *,
    last_seen_at_s: float,
) -> TrackSnapshot:
    return TrackSnapshot(
        track_id=track_id,
        revision=4,
        label=label,
        bbox=bbox,
        first_seen_at_s=max(0.0, last_seen_at_s - 1.0),
        last_seen_at_s=last_seen_at_s,
        observation_count=5,
        consecutive_observations=5,
        confidence_floor=0.85,
        confidence_mean=0.90,
        maximum_gap_s=0.1,
        area_growth_rate=0.0,
        thermal_corroborated=False,
        confirmed=True,
    )


class _AcceptanceManualTrackerBackend:
    def __init__(self, *, updates: list[tuple[bool, tuple[float, float, float, float]]]) -> None:
        self._updates = list(updates)

    def init(self, _image: Any, _bbox: tuple[int, int, int, int]) -> bool:
        return True

    def update(self, _image: Any) -> tuple[bool, tuple[float, float, float, float]]:
        if not self._updates:
            return False, (0.0, 0.0, 0.0, 0.0)
        return self._updates.pop(0)


__all__ = ["run_software_acceptance"]

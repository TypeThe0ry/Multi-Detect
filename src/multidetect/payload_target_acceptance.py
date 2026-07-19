from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import MissionConfig
from .domain import BoundingBox, Detection, MissionPhase, SensorKind, TrackSnapshot
from .mission import MissionController
from .operator_link import (
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    PayloadTargetConfirmationCommand,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    VideoGeometry,
    operator_identifier_token,
)
from .operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
from .operator_protocol import OperatorTunnelCodec
from .operator_status import build_authorization_challenge_status_message
from .operator_udp import UdpOperatorSelectionServer, UdpOperatorSessionClient
from .payload_target_gate import PayloadTargetEligibility
from .payload_target_live import LivePayloadTargetCoordinator
from .replay import load_jsonl_replay
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState

_APPLICATION_KEY = b"mode2-payload-hil-application-key-32-bytes"
_MAVLINK_KEY = b"M" * 32
_GEOMETRY = VideoGeometry("camera-main", 1280, 720)
_SELECTION_ID = "77777777-7777-4777-8777-777777777777"
_SESSION_ID = "88888888-8888-4888-8888-888888888888"


def run_mode2_payload_hil_acceptance(project_root: Path) -> dict[str, Any]:
    """Close the Mode-2 selection-to-simulated-release loop without hardware control."""

    root = project_root.resolve()
    config = MissionConfig.from_json(root / "configs/missions/fire_suppression.demo.json")
    frames = load_jsonl_replay(root / "examples/fire_mission_replay.jsonl")
    mission, fire_track = _prepared_mission(config, frames)
    coordinator = LivePayloadTargetCoordinator()
    selected = _selected_track(fire_track, now_s=1003.1)
    wire_now_s = time.time()
    prepared = coordinator.prepare_frame(
        selection_command_id=_SELECTION_ID,
        selected=selected,
        fire_tracks=(fire_track,),
        now_s=1003.1,
        wire_now_s=wire_now_s,
    )
    if (
        prepared.resolution is None
        or prepared.resolution.eligibility is not PayloadTargetEligibility.ELIGIBLE_FIRE
        or prepared.challenge is None
        or prepared.status is None
        or prepared.intent is not None
    ):
        raise RuntimeError("qualified fire did not produce a pending Mode-2 slide challenge")

    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    operator = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))
    selection_acknowledged = False
    payload_target_acknowledged = False
    authorization_acknowledged = False
    payload_status_received = False
    payload_delivery_attempts = 0
    authorization_delivery_attempts = 0
    transport_started_s = time.perf_counter()
    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(_GEOMETRY),
        receive_timeout_s=0.05,
    ) as server:
        server.start_background()
        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=operator,
            retry_interval_s=0.25,
            maximum_attempts=3,
        ) as client:
            selection_now_s = time.time()
            selection = TargetSelectionCommand(
                command_id=_SELECTION_ID,
                session_id=_SESSION_ID,
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=_GEOMETRY,
                issued_at_s=selection_now_s,
                expires_at_s=selection_now_s + 3.0,
                bbox=fire_track.bbox,
                displayed_frame_id=frames[-1].frame_id,
            )
            selection_receipt = client.deliver(selection)
            selection_acknowledged = selection_receipt.acknowledgement.accepted
            queued_selection = _poll(server.poll_selection)
            if queued_selection is None or not selection_acknowledged:
                raise RuntimeError("signed Mode-2 target selection was not acknowledged")
            peer = queued_selection[1]

            server.publish_payload_target_challenge(prepared.challenge, peer=peer)
            received_challenge = client.receive_payload_target_challenge(timeout_s=1.0)
            confirmation_now_s = time.time()
            confirmation = PayloadTargetConfirmationCommand(
                command_token=operator_identifier_token("mode2-hil-slide-command"),
                session_token=operator_identifier_token(_SESSION_ID),
                challenge_token=received_challenge.challenge_token,
                selected_target_token=received_challenge.selected_target_token,
                selected_target_revision=received_challenge.selected_target_revision,
                aimpoint_target_token=received_challenge.aimpoint_target_token,
                aimpoint_target_revision=received_challenge.aimpoint_target_revision,
                selection_command_id=received_challenge.selection_command_id,
                sequence=2,
                issued_at_s=confirmation_now_s,
                expires_at_s=min(confirmation_now_s + 2.0, received_challenge.expires_at_s),
                slide_duration_s=0.8,
                completion_fraction=1.0,
                continuous=True,
            )
            payload_receipt = client.deliver_payload_target_confirmation(confirmation)
            payload_target_acknowledged = payload_receipt.acknowledgement.accepted
            payload_delivery_attempts = payload_receipt.attempts
            queued_confirmation = _poll(server.poll_payload_target_confirmation)
            if queued_confirmation is None or not payload_target_acknowledged:
                raise RuntimeError("Mode-2 continuous-slide confirmation was not acknowledged")

            refresh_frame = replace(
                frames[-1],
                frame_id="mode2-fire-refresh",
                captured_at_s=1003.8,
            )
            refreshed = mission.process_observation(
                refresh_frame,
                now_s=refresh_frame.captured_at_s,
                require_payload_target_intent=True,
            )
            refreshed_fire = _confirmed_fire(refreshed.tracks)
            refreshed_selected = _selected_track(refreshed_fire, now_s=1003.8)
            refreshed_gate = coordinator.prepare_frame(
                selection_command_id=_SELECTION_ID,
                selected=refreshed_selected,
                fire_tracks=(refreshed_fire,),
                now_s=1003.8,
                wire_now_s=time.time(),
            )
            if refreshed_gate.challenge is None:
                raise RuntimeError("stable fire identity unexpectedly revoked the slide challenge")
            if not coordinator.consume_confirmation(queued_confirmation[0], now_s=1003.9):
                raise RuntimeError("Jetson rejected an authenticated continuous-slide confirmation")
            accepted_gate = coordinator.prepare_frame(
                selection_command_id=_SELECTION_ID,
                selected=_selected_track(refreshed_fire, now_s=1003.91),
                fire_tracks=(refreshed_fire,),
                now_s=1003.91,
                wire_now_s=time.time(),
            )
            intent = accepted_gate.intent
            if intent is None or accepted_gate.status is None:
                raise RuntimeError(
                    "accepted slide did not create a short-lived payload target intent"
                )
            server.publish_payload_target_status(accepted_gate.status, peer=peer)
            received_status = client.receive_payload_target_status(timeout_s=1.0)
            payload_status_received = bool(
                received_status.confirmation_accepted
                and received_status.eligibility is PayloadTargetEligibility.ELIGIBLE_FIRE
            )
            if not payload_status_received:
                raise RuntimeError("operator did not receive the accepted Mode-2 target status")

            authorization_frame = replace(
                frames[-1],
                frame_id="mode2-authorization-frame",
                captured_at_s=1003.95,
            )
            outcome = mission.process_observation(
                authorization_frame,
                now_s=authorization_frame.captured_at_s,
                payload_target_intent=intent,
                require_payload_target_intent=True,
            )
            if outcome.challenge is None or mission.fake_payload_port.request_count != 0:
                raise RuntimeError("valid Mode-2 intent did not stop at independent authorization")
            authorization_status = build_authorization_challenge_status_message(
                challenge=outcome.challenge,
                sequence=3,
                produced_at_s=time.time(),
                challenge_clock_now_s=authorization_frame.captured_at_s,
            )
            server.publish_authorization_challenge(authorization_status, peer=peer)
            received_authorization = client.receive_authorization_challenge(timeout_s=1.0)
            decision_now_s = time.time()
            decision = AuthorizationDecisionCommand(
                command_token=operator_identifier_token("mode2-hil-authorization-command"),
                session_token=operator_identifier_token(_SESSION_ID),
                challenge_token=received_authorization.challenge_token,
                mission_token=received_authorization.mission_token,
                target_token=received_authorization.target_token,
                scene_token=received_authorization.scene_token,
                ruleset_token=received_authorization.ruleset_token,
                payload_slot_token=received_authorization.payload_slot_token,
                target_revision=received_authorization.target_revision,
                decision=AuthorizationDecision.APPROVE,
                operator_token=operator_identifier_token("mode2-hil-operator"),
                sequence=4,
                issued_at_s=decision_now_s,
                expires_at_s=min(decision_now_s + 2.0, received_authorization.expires_at_s),
            )
            authorization_receipt = client.deliver_authorization_decision(decision)
            authorization_acknowledged = authorization_receipt.acknowledgement.accepted
            authorization_delivery_attempts = authorization_receipt.attempts
            queued_decision = _poll(server.poll_authorization_decision)
            if queued_decision is None or not authorization_acknowledged:
                raise RuntimeError("independent Mode-2 authorization was not acknowledged")
            mission.approve_authorization(
                challenge_id=outcome.challenge.challenge_id,
                nonce=outcome.challenge.nonce,
                operator_id=f"g20:{queued_decision[0].operator_token:016x}",
                now_s=1004.05,
            )
            if (
                mission.status().phase is not MissionPhase.DEPLOYMENT_READY
                or mission.fake_payload_port.request_count != 0
            ):
                raise RuntimeError("authorization did not stop at deployment-ready")
            release_id = mission.request_simulated_deployment(now_s=1004.10)
            mission.report_simulated_execution(release_id=release_id, now_s=1004.20)
            mission.report_independent_confirmation(
                release_id=release_id,
                source_id="mode2-hil-independent-release-sensor",
                now_s=1004.30,
            )
    transport_elapsed_ms = (time.perf_counter() - transport_started_s) * 1000.0

    mission_status = mission.status()
    if mission.fake_payload_port.request_count != 1 or mission.payload.remaining_payload_count != 2:
        raise RuntimeError("Mode-2 simulated release transaction did not complete exactly once")

    person_result = _ineligible_selection(
        coordinator=LivePayloadTargetCoordinator(),
        selection_id="11111111-1111-4111-8111-111111111111",
        selected=_selected_track(fire_track, now_s=1003.1, label="person", track_id="person-1"),
        fire_tracks=(fire_track,),
        now_s=1003.1,
    )
    vehicle_result = _ineligible_selection(
        coordinator=LivePayloadTargetCoordinator(),
        selection_id="22222222-2222-4222-8222-222222222222",
        selected=_selected_track(
            fire_track,
            now_s=1003.1,
            label="vehicle",
            track_id="ordinary-vehicle-1",
        ),
        fire_tracks=(),
        now_s=1003.1,
    )

    switched = coordinator.prepare_frame(
        selection_command_id="33333333-3333-4333-8333-333333333333",
        selected=_selected_track(refreshed_fire, now_s=1004.31),
        fire_tracks=(replace(refreshed_fire, last_seen_at_s=1004.31),),
        now_s=1004.31,
        wire_now_s=time.time(),
    )
    stale_intent = coordinator.active_intent(
        selection_command_id=_SELECTION_ID,
        track=_selected_track(refreshed_fire, now_s=1004.32),
        now_s=1004.32,
    )
    target_switch_revoked = switched.intent is None and stale_intent is None
    if not target_switch_revoked:
        raise RuntimeError("target switch did not revoke the previous Mode-2 slide grant")

    expired_coordinator = LivePayloadTargetCoordinator()
    expiry_frame = expired_coordinator.prepare_frame(
        selection_command_id="44444444-4444-4444-8444-444444444444",
        selected=_selected_track(fire_track, now_s=2000.0),
        fire_tracks=(replace(fire_track, last_seen_at_s=2000.0),),
        now_s=2000.0,
        wire_now_s=time.time(),
    )
    if expiry_frame.challenge is None:
        raise RuntimeError("expiry scenario did not create a slide challenge")
    expiry_command = PayloadTargetConfirmationCommand(
        command_token=operator_identifier_token("mode2-hil-expiry-command"),
        session_token=operator_identifier_token("mode2-hil-expiry-session"),
        challenge_token=expiry_frame.challenge.challenge_token,
        selected_target_token=expiry_frame.challenge.selected_target_token,
        selected_target_revision=expiry_frame.challenge.selected_target_revision,
        aimpoint_target_token=expiry_frame.challenge.aimpoint_target_token,
        aimpoint_target_revision=expiry_frame.challenge.aimpoint_target_revision,
        selection_command_id=expiry_frame.challenge.selection_command_id,
        sequence=1,
        issued_at_s=time.time(),
        expires_at_s=time.time() + 2.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    if not expired_coordinator.consume_confirmation(expiry_command, now_s=2000.8):
        raise RuntimeError("expiry scenario could not establish a valid initial grant")
    expired_intent = expired_coordinator.active_intent(
        selection_command_id=expiry_frame.challenge.selection_command_id,
        track=_selected_track(fire_track, now_s=2005.1),
        now_s=2005.1,
    )
    if expired_intent is not None:
        raise RuntimeError("expired Mode-2 slide grant remained active")

    veto_mission, veto_fire = _prepared_mission(config, frames)
    unsafe_frame = replace(
        frames[-1],
        frame_id="mode2-person-entry-veto",
        captured_at_s=1003.95,
        detections=(
            *frames[-1].detections,
            Detection(
                "person",
                0.98,
                BoundingBox(0.43, 0.38, 0.57, 0.60),
                SensorKind.RGB,
                "qualified-person-hil",
            ),
        ),
    )
    unsafe_intent = replace(intent, aimpoint_target_id=veto_fire.track_id)
    unsafe_outcome = veto_mission.process_observation(
        unsafe_frame,
        now_s=unsafe_frame.captured_at_s,
        payload_target_intent=unsafe_intent,
        require_payload_target_intent=True,
    )
    person_entry_vetoed = bool(
        unsafe_outcome.decisions
        and all(not decision.allowed for decision in unsafe_outcome.decisions)
        and unsafe_outcome.challenge is None
        and veto_mission.fake_payload_port.request_count == 0
    )
    if not person_entry_vetoed:
        raise RuntimeError("person entry was not fail-closed after a valid Mode-2 slide intent")

    return {
        "event": "mode2_payload_hil_acceptance_passed",
        "positive_fire": {
            "eligibility": prepared.resolution.eligibility.value,
            "aimpoint_is_confirmed_fire": intent.aimpoint_target_id == refreshed_fire.track_id,
            "selection_acknowledged": selection_acknowledged,
            "continuous_slide_acknowledged": payload_target_acknowledged,
            "payload_status_received": payload_status_received,
            "authorization_acknowledged": authorization_acknowledged,
            "authorization_was_separate": True,
            "fake_release_requests": mission.fake_payload_port.request_count,
            "remaining_payloads": mission.payload.remaining_payload_count,
            "final_phase": mission_status.phase.value,
        },
        "transport": {
            "loopback_udp": True,
            "hmac_authenticated": True,
            "mavlink2_signed": True,
            "payload_delivery_attempts": payload_delivery_attempts,
            "authorization_delivery_attempts": authorization_delivery_attempts,
            "round_trip_session_elapsed_ms": transport_elapsed_ms,
        },
        "negative_cases": {
            "person_eligibility": person_result.value,
            "ordinary_vehicle_eligibility": vehicle_result.value,
            "target_switch_revoked": target_switch_revoked,
            "expired_slide_revoked": expired_intent is None,
            "person_entry_after_slide_vetoed": person_entry_vetoed,
        },
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "real_payload_interface_present": False,
        "model_training_executed": False,
        "model_inference_executed": False,
    }


def _prepared_mission(
    config: MissionConfig,
    frames: tuple[Any, ...],
) -> tuple[MissionController, TrackSnapshot]:
    mission = MissionController(config)
    mission.launch(now_s=max(0.0, frames[0].captured_at_s - 2.0))
    mission.arrive_task_area(now_s=max(0.0, frames[0].captured_at_s - 1.0))
    outcome = None
    for frame in frames:
        outcome = mission.process_observation(
            frame,
            now_s=frame.captured_at_s,
            require_payload_target_intent=True,
        )
        if outcome.challenge is not None:
            raise RuntimeError("authorization existed before Mode-2 target intent")
    if outcome is None:
        raise RuntimeError("Mode-2 replay contained no frames")
    return mission, _confirmed_fire(outcome.tracks)


def _confirmed_fire(tracks: tuple[TrackSnapshot, ...]) -> TrackSnapshot:
    qualified = tuple(
        track
        for track in tracks
        if track.confirmed and track.independent_rgb_corroborated and track.label == "flame"
    )
    if len(qualified) != 1:
        raise RuntimeError("Mode-2 acceptance requires exactly one independently confirmed fire")
    return qualified[0]


def _selected_track(
    source: TrackSnapshot,
    *,
    now_s: float,
    label: str | None = None,
    track_id: str | None = None,
) -> UnifiedTrackSnapshot:
    return UnifiedTrackSnapshot(
        track_id=track_id or source.track_id,
        state=UnifiedTrackState.TRACKING,
        label=label or source.label,
        bbox=source.bbox,
        predicted_bbox=source.bbox,
        first_seen_at_s=max(0.0, now_s - 3.0),
        last_seen_at_s=now_s,
        state_changed_at_s=max(0.0, now_s - 1.0),
        observation_count=8,
        missed_frame_count=0,
        confidence=0.94,
        tracking_quality=0.93,
        velocity_x_s=0.0,
        velocity_y_s=0.0,
        appearance_sample_count=3,
        last_appearance_distance=0.05,
        reid_confirmed=True,
        locked=True,
        primary=True,
        actionable=True,
    )


def _ineligible_selection(
    *,
    coordinator: LivePayloadTargetCoordinator,
    selection_id: str,
    selected: UnifiedTrackSnapshot,
    fire_tracks: tuple[TrackSnapshot, ...],
    now_s: float,
) -> PayloadTargetEligibility:
    frame = coordinator.prepare_frame(
        selection_command_id=selection_id,
        selected=selected,
        fire_tracks=fire_tracks,
        now_s=now_s,
        wire_now_s=time.time(),
    )
    if (
        frame.resolution is None
        or frame.resolution.eligible
        or frame.challenge is not None
        or frame.intent is not None
    ):
        raise RuntimeError("ineligible Mode-2 selection created a payload path")
    return frame.resolution.eligibility


def _adapter(endpoint: OperatorMavlinkEndpoint) -> OperatorMavlinkTunnelAdapter:
    return OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=_APPLICATION_KEY, geometries=(_GEOMETRY,)),
        endpoint,
        signing_key=_MAVLINK_KEY,
        signing_link_id=endpoint.local_component_id,
        initial_signing_timestamp=4_000_000 + endpoint.local_system_id,
    )


def _poll(callback: Any, *, timeout_s: float = 1.0) -> Any | None:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        value = callback()
        if value is not None:
            return value
        time.sleep(0.005)
    return None


__all__ = ["run_mode2_payload_hil_acceptance"]

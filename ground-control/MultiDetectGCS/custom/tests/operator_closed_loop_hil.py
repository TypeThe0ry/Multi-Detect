from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import replace
from typing import cast

from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    RuleCheck,
    Verdict,
)
from multidetect.operator_link import (
    OPERATOR_SAFETY_RULE_IDS,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    SafetyStatusMessage,
    SelectionAction,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_protocol import (
    OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
    AuthorizationDecisionAck,
    AuthorizationDecisionAckReason,
    OperatorTunnelCodec,
    SelectionAck,
    SelectionAckReason,
    WireMessageType,
)
from multidetect.patrol_advisory import (
    AdvisoryValidity,
    PatrolPhase,
    ReturnObserveDirection,
)
from multidetect.unified_tracking import UnifiedTrackState
from pymavlink.dialects.v20 import common as mavlink


class HilFailure(RuntimeError):
    """Raised when the QGC/Jetson metadata HIL contract is not completed."""


TRACK_STATUS_RATE_HZ = 20.0
TRACK_STATUS_SAMPLE_COUNT = 30


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exercise the QGC operator-metadata loop over localhost UDP only."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--key-env", default="MULTIDETECT_OPERATOR_KEY")
    parser.add_argument("--stream-id", default="camera-main")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--rotation", type=int, default=0)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help=(
            "announce only Jetson component 191 and require an external PX4 autopilot heartbeat "
            "to create the QGC vehicle"
        ),
    )
    return parser


def _send_tunnel(
    channel: socket.socket,
    serializer: mavlink.MAVLink,
    destination: tuple[str, int],
    payload: bytes,
) -> None:
    padded = payload.ljust(128, b"\0")
    message = mavlink.MAVLink_tunnel_message(
        255,
        190,
        OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
        len(payload),
        list(padded),
    )
    channel.sendto(message.pack(serializer), destination)


def _status_payloads(
    codec: OperatorTunnelCodec,
    geometry: VideoGeometry,
    command: TargetSelectionCommand,
) -> tuple[list[bytes], AuthorizationChallengeStatusMessage]:
    now = time.time()
    target_id = "hil-fire-target"
    mission_id = "hil-fire-mission"
    challenge = AuthorizationChallengeStatusMessage(
        challenge_token=11,
        mission_token=12,
        target_token=13,
        scene_token=14,
        ruleset_token=15,
        payload_slot_token=16,
        target_revision=7,
        created_at_s=now,
        expires_at_s=now + 8.0,
        sequence=205,
        produced_at_s=now,
    )
    track = TrackStatusMessage(
        status_id="33333333-3333-4333-8333-333333333333",
        selection_command_id=command.command_id,
        sequence=202,
        geometry=geometry,
        state=TrackingState.TRACKING,
        target_id=target_id,
        bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
        label="flame",
        confidence=0.93,
        tracking_quality=0.89,
        source_frame_id="hil-frame-1",
        source_captured_at_s=now - 0.02,
        produced_at_s=now,
        relative_bearing_deg=-3.2,
        estimated_range_m=62.8,
    )
    track_payloads = [
        codec.encode_track_status(
            replace(
                track,
                sequence=track.sequence + index,
                source_frame_id=f"hil-frame-{index + 1}",
                source_captured_at_s=now - 0.02 + index / TRACK_STATUS_RATE_HZ,
                produced_at_s=now + index / TRACK_STATUS_RATE_HZ,
            )
        )
        for index in range(TRACK_STATUS_SAMPLE_COUNT)
    ]
    mission = MissionStatusMessage(
        status_id="44444444-4444-4444-8444-444444444444",
        sequence=203,
        mission_id=mission_id,
        phase=MissionPhase.AWAITING_AUTHORIZATION,
        authorization_state=AuthorizationDisplayState.PENDING,
        release_window=cast("DeploymentWindowStatus", DeploymentWindowStatus.READY),
        safety_allowed=True,
        remaining_payload_count=1,
        total_payload_count=1,
        target_id=target_id,
        active_payload_slot_id="payload-slot-1",
        target_confidence=0.93,
        relative_bearing_deg=-3.2,
        estimated_range_m=62.8,
        cross_track_error_m=-0.4,
        along_track_error_m=0.2,
        release_lead_distance_m=61.5,
        produced_at_s=now,
    )
    safety = SafetyStatusMessage(
        status_id="55555555-5555-4555-8555-555555555555",
        sequence=204,
        mission_id=mission_id,
        target_id=target_id,
        ruleset_version="rules-v1",
        checks=tuple(
            RuleCheck(rule_id, Verdict.PASS, "software HIL pass")
            for rule_id in OPERATOR_SAFETY_RULE_IDS
        ),
        produced_at_s=now,
    )
    patrol = PatrolStatusMessage(
        status_id="33333333-3333-4333-8333-333333333333",
        sequence=207,
        mission_id=mission_id,
        phase=PatrolPhase.LOST,
        primary_target_id=target_id,
        target_state=UnifiedTrackState.LOST,
        bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
        label="flame",
        confidence=0.93,
        tracking_quality=0.89,
        total_track_count=10,
        locked_track_count=3,
        source_frame_id="hil-frame-1",
        source_captured_at_s=now - 0.05,
        produced_at_s=now,
        return_direction=ReturnObserveDirection.LEFT,
        return_validity=AdvisoryValidity.DEGRADED,
        return_evidence_age_s=0.4,
        estimated_minimum_turn_radius_m=87.3,
    )
    pool_entries = (
        TargetPoolEntry(
            target_id=target_id,
            state=UnifiedTrackState.TRACKING,
            label="flame",
            confidence=0.93,
            tracking_quality=0.89,
            locked=True,
            primary=True,
            actionable=True,
            reid_confirmed=True,
            bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
        ),
        TargetPoolEntry(
            target_id="hil-vehicle-background",
            state=UnifiedTrackState.OCCLUDED,
            label="vehicle",
            confidence=0.84,
            tracking_quality=0.68,
            locked=True,
            primary=False,
            actionable=False,
            reid_confirmed=True,
            bbox=BoundingBox(0.05, 0.35, 0.22, 0.62),
        ),
        TargetPoolEntry(
            target_id="hil-person-background",
            state=UnifiedTrackState.REACQUIRING,
            label="person",
            confidence=0.79,
            tracking_quality=0.51,
            locked=True,
            primary=False,
            actionable=False,
            reid_confirmed=False,
            bbox=BoundingBox(0.68, 0.18, 0.82, 0.66),
        ),
    )
    pool_pages = (
        TargetPoolStatusMessage(
            sequence=208,
            pool_revision=3,
            page_index=0,
            page_count=2,
            total_track_count=3,
            entries=pool_entries[:2],
            produced_at_s=now,
        ),
        TargetPoolStatusMessage(
            sequence=209,
            pool_revision=3,
            page_index=1,
            page_count=2,
            total_track_count=3,
            entries=pool_entries[2:],
            produced_at_s=now,
        ),
    )
    return (
        [
            *track_payloads,
            codec.encode_mission_status(mission),
            codec.encode_safety_status(safety),
            codec.encode_patrol_status(patrol),
            *(codec.encode_target_pool_status(page) for page in pool_pages),
            codec.encode_authorization_challenge(challenge),
        ],
        challenge,
    )


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.host != "127.0.0.1":
        raise HilFailure("the unsigned outer-MAVLink HIL driver is restricted to 127.0.0.1")
    if not 1024 <= args.port <= 65535:
        raise HilFailure("HIL UDP port must be in 1024..65535")
    key_text = os.environ.get(args.key_env, "")
    key = key_text.encode("utf-8")
    if len(key) < 32:
        raise HilFailure(f"{args.key_env} must contain at least 32 UTF-8 bytes")

    geometry = VideoGeometry(args.stream_id, args.width, args.height, args.rotation)
    codec = OperatorTunnelCodec(hmac_key=key, geometries=(geometry,))
    destination = (args.host, args.port)
    receiver = mavlink.MAVLink(None)
    heartbeat_serializer = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    jetson_serializer = mavlink.MAVLink(None, srcSystem=1, srcComponent=191)
    autopilot_heartbeat = mavlink.MAVLink_heartbeat_message(
        mavlink.MAV_TYPE_FIXED_WING,
        mavlink.MAV_AUTOPILOT_PX4,
        0,
        0,
        mavlink.MAV_STATE_STANDBY,
        3,
    )
    jetson_heartbeat = mavlink.MAVLink_heartbeat_message(
        mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
        mavlink.MAV_AUTOPILOT_INVALID,
        0,
        0,
        mavlink.MAV_STATE_ACTIVE,
        3,
    )

    channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    channel.bind(("127.0.0.1", 0))
    channel.settimeout(0.1)
    deadline = time.monotonic() + args.timeout
    next_heartbeat = 0.0
    selection: TargetSelectionCommand | None = None
    challenge: AuthorizationChallengeStatusMessage | None = None
    inbound_tunnel_count = 0
    autopilot_heartbeats_sent = 0
    jetson_component_heartbeats_sent = 0
    udp_connection_resets = 0

    try:
        while time.monotonic() < deadline:
            now_monotonic = time.monotonic()
            if selection is None and now_monotonic >= next_heartbeat:
                if args.metadata_only:
                    channel.sendto(jetson_heartbeat.pack(jetson_serializer), destination)
                    jetson_component_heartbeats_sent += 1
                else:
                    channel.sendto(autopilot_heartbeat.pack(heartbeat_serializer), destination)
                    autopilot_heartbeats_sent += 1
                next_heartbeat = now_monotonic + 0.25
            try:
                datagram, _source = channel.recvfrom(4096)
            except TimeoutError:
                continue
            except ConnectionResetError:
                # Windows reports an ICMP port-unreachable response as
                # WSAECONNRESET while QGC is still starting. Keep the bounded
                # localhost HIL alive until the listener appears.
                udp_connection_resets += 1
                continue
            for outer in receiver.parse_buffer(datagram) or ():
                if outer.get_type() != "TUNNEL":
                    continue
                tunnel = cast("mavlink.MAVLink_tunnel_message", outer)
                inbound_tunnel_count += 1
                if tunnel.get_srcSystem() != 255 or tunnel.get_srcComponent() != 190:
                    raise HilFailure("QGC TUNNEL source endpoint is not 255/190")
                if tunnel.target_system != 1 or tunnel.target_component != 191:
                    raise HilFailure("QGC TUNNEL target endpoint is not 1/191")
                if tunnel.payload_type != OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL:
                    continue
                payload = bytes(tunnel.payload[: tunnel.payload_length])
                packet = codec.decode(payload)

                if packet.message_type is WireMessageType.TARGET_SELECTION:
                    if not isinstance(packet.message, TargetSelectionCommand):
                        raise HilFailure("decoded target selection has the wrong type")
                    decoded_selection = packet.message
                    if (
                        decoded_selection.action is not SelectionAction.SELECT
                        or decoded_selection.bbox is None
                    ):
                        raise HilFailure("QGC did not send the expected SELECT rectangle")
                    selection = decoded_selection
                    acknowledgement = SelectionAck(
                        decoded_selection.command_id,
                        True,
                        SelectionAckReason.ACCEPTED,
                        decoded_selection.sequence,
                    )
                    _send_tunnel(
                        channel,
                        jetson_serializer,
                        destination,
                        codec.encode_ack(
                            acknowledgement,
                            sequence=201,
                            sent_at_s=time.time(),
                        ),
                    )
                    status_payloads, challenge = _status_payloads(
                        codec, geometry, decoded_selection
                    )
                    for status_payload in status_payloads:
                        time.sleep(0.05)
                        _send_tunnel(channel, jetson_serializer, destination, status_payload)
                    continue

                if packet.message_type is WireMessageType.AUTHORIZATION_DECISION:
                    if not isinstance(packet.message, AuthorizationDecisionCommand):
                        raise HilFailure("decoded authorization decision has the wrong type")
                    decision = packet.message
                    if selection is None or selection.bbox is None or challenge is None:
                        raise HilFailure("authorization arrived before selection/status setup")
                    selection_bbox = selection.bbox
                    if decision.decision is not AuthorizationDecision.APPROVE:
                        raise HilFailure("QGC HIL did not send the expected APPROVE decision")
                    binding = (
                        decision.challenge_token,
                        decision.mission_token,
                        decision.target_token,
                        decision.scene_token,
                        decision.ruleset_token,
                        decision.payload_slot_token,
                        decision.target_revision,
                    )
                    expected_binding = (
                        challenge.challenge_token,
                        challenge.mission_token,
                        challenge.target_token,
                        challenge.scene_token,
                        challenge.ruleset_token,
                        challenge.payload_slot_token,
                        challenge.target_revision,
                    )
                    if binding != expected_binding:
                        raise HilFailure("QGC authorization decision is not challenge-bound")
                    acknowledgement = AuthorizationDecisionAck(
                        command_token=decision.command_token,
                        accepted=True,
                        reason=AuthorizationDecisionAckReason.ACCEPTED,
                        acknowledged_sequence=decision.sequence,
                    )
                    _send_tunnel(
                        channel,
                        jetson_serializer,
                        destination,
                        codec.encode_authorization_ack(
                            acknowledgement,
                            sequence=206,
                            sent_at_s=time.time(),
                        ),
                    )
                    time.sleep(0.5)
                    return {
                        "authorization": "APPROVE",
                        "authorization_bound": True,
                        "bbox": [
                            selection_bbox.x1,
                            selection_bbox.y1,
                            selection_bbox.x2,
                            selection_bbox.y2,
                        ],
                        "inbound_tunnel_count": inbound_tunnel_count,
                        "metadata_only": args.metadata_only,
                        "autopilot_heartbeats_sent": autopilot_heartbeats_sent,
                        "jetson_component_heartbeats_sent": jetson_component_heartbeats_sent,
                        "patrol_status_sent": True,
                        "target_pool_page_count": 2,
                        "target_pool_track_count": 3,
                        "tracking_metadata_packets_sent": TRACK_STATUS_SAMPLE_COUNT,
                        "tracking_metadata_rate_hz": TRACK_STATUS_RATE_HZ,
                        "udp_connection_resets": udp_connection_resets,
                        "external_autopilot_required": args.metadata_only,
                        "real_v6x_contacted": False,
                        "selection": selection.action.value,
                        "selection_sequence": selection.sequence,
                    }
    finally:
        channel.close()
    raise HilFailure("timed out before the selection/authorization metadata loop completed")


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run(args)
    except (HilFailure, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    RuleCheck,
    Verdict,
)
from multidetect.multimodal_ranging import RangeValidity
from multidetect.operator_link import (
    OPERATOR_SAFETY_RULE_IDS,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    RangeStatusMessage,
    SafetyStatusMessage,
    SelectionAction,
    TargetGeolocationStatusMessage,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
from multidetect.operator_protocol import (
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
    parser.add_argument(
        "--local-port",
        type=int,
        default=0,
        help="fixed localhost source port for the signed operator endpoint (0 selects an ephemeral port)",
    )
    parser.add_argument("--key-env", default="MULTIDETECT_OPERATOR_KEY")
    parser.add_argument("--signing-key-hex-env", default="MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX")
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
    parser.add_argument(
        "--target-geolocation-evidence",
        default="",
        help=(
            "optional passed Gazebo target-geolocation evidence whose final prediction is "
            "rebased onto the live HIL target/frame and sent to native QGC"
        ),
    )
    parser.add_argument(
        "--expected-release-label",
        default="",
        help="required release label when target-geolocation evidence is supplied",
    )
    return parser


def _load_target_geolocation_evidence(
    path_text: str,
    expected_release_label: str = "",
) -> dict[str, object] | None:
    if not path_text:
        return None
    path = Path(path_text).resolve()
    raw = path.read_bytes()
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HilFailure(f"target geolocation evidence is not valid JSON: {error}") from error
    if evidence.get("schema") != "multidetect.gazebo-target-geolocation-acceptance":
        raise HilFailure("target geolocation evidence schema is invalid")
    if evidence.get("schema_version") != 2 or evidence.get("gate_passed") is not True:
        raise HilFailure("target geolocation evidence has not passed its schema-v2 gate")
    if (
        evidence.get("flight_control_enabled") is not False
        or evidence.get("physical_release_enabled") is not False
        or evidence.get("production_approved") is not False
    ):
        raise HilFailure("target geolocation evidence violates the display-only safety boundary")
    frames = evidence.get("frames")
    if not isinstance(frames, list) or not frames:
        raise HilFailure("target geolocation evidence contains no evaluated frames")
    frame = frames[-1]
    if not isinstance(frame, dict):
        raise HilFailure("target geolocation evidence final frame is invalid")
    predicted = frame.get("predicted_target")
    ranging = frame.get("range_solution")
    detections = frame.get("detections")
    if not isinstance(predicted, dict) or not isinstance(ranging, dict):
        raise HilFailure("target geolocation evidence lacks prediction/range data")
    if not isinstance(detections, list) or len(detections) != 1 or not isinstance(detections[0], dict):
        raise HilFailure("target geolocation evidence must contain one final-frame detection")
    required_range_fields = (
        "slant_range_m",
        "ground_range_m",
        "slant_range_ci95_m",
        "ground_range_ci95_m",
        "relative_bearing_deg",
        "absolute_bearing_deg",
        "bearing_sigma_deg",
        "north_offset_m",
        "east_offset_m",
        "data_freshness_s",
        "sensor_consistency",
        "sources",
        "rejected_sources",
        "reasons",
        "fusion_profile",
        "vehicle_profile",
        "navigation_state",
        "motion_regime",
        "validity",
    )
    if any(field not in ranging for field in required_range_fields):
        raise HilFailure("target geolocation evidence range solution is incomplete")
    for field in ("latitude_deg", "longitude_deg", "horizontal_sigma_m"):
        if field not in predicted:
            raise HilFailure(f"target geolocation evidence prediction lacks {field}")
    scenario = evidence.get("scenario")
    release = evidence.get("release_identity")
    if not isinstance(scenario, dict) or not isinstance(release, dict):
        raise HilFailure("target geolocation evidence lacks scenario/release identity")
    scenario_id = scenario.get("scenario_id")
    release_label = release.get("release_label")
    source_frame_id = frame.get("frame_id")
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (scenario_id, release_label, source_frame_id)
    ):
        raise HilFailure("target geolocation evidence identity is incomplete")
    if expected_release_label and release_label != expected_release_label:
        raise HilFailure("target geolocation evidence release label does not match the QGC build")
    for field in ("evidence_sha256", "deterministic_result_sha256"):
        digest = evidence.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest.lower())
        ):
            raise HilFailure(f"target geolocation evidence {field} is invalid")
    if (
        ranging.get("advisory_only") is not True
        or ranging.get("flight_control_enabled") is not False
        or ranging.get("physical_release_enabled") is not False
    ):
        raise HilFailure("target geolocation range solution violates the display-only safety boundary")
    return {
        "file_path": str(path),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "evidence_sha256": str(evidence.get("evidence_sha256", "")),
        "deterministic_result_sha256": str(evidence.get("deterministic_result_sha256", "")),
        "scenario_id": str(scenario_id),
        "release_label": str(release_label),
        "source_frame_id": str(source_frame_id),
        "source_captured_at_s": float(frame.get("captured_at_s")),
        "label": str(detections[0].get("label", "target")),
        "predicted": predicted,
        "range_solution": ranging,
    }


def _signing_timestamp() -> int:
    return max(1, int((time.time() - 1_420_070_400) * 100_000))


def _signed_serializer(key: bytes, *, system_id: int, component_id: int) -> mavlink.MAVLink:
    serializer = mavlink.MAVLink(None, srcSystem=system_id, srcComponent=component_id)
    serializer.signing.secret_key = bytes(key)
    serializer.signing.link_id = 1
    serializer.signing.timestamp = _signing_timestamp()
    serializer.signing.sign_outgoing = True
    return serializer


def _status_payloads(
    codec: OperatorTunnelCodec,
    geometry: VideoGeometry,
    command: TargetSelectionCommand,
    target_geolocation_evidence: dict[str, object] | None = None,
) -> tuple[list[bytes], AuthorizationChallengeStatusMessage]:
    now = time.time()
    target_id = "hil-geolocated-target" if target_geolocation_evidence else "hil-fire-target"
    target_label = (
        str(target_geolocation_evidence["label"])
        if target_geolocation_evidence
        else "flame"
    )
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
        label=target_label,
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
    geolocation_payloads: list[bytes] = []
    if target_geolocation_evidence is not None:
        predicted = cast("dict[str, object]", target_geolocation_evidence["predicted"])
        ranging = cast("dict[str, object]", target_geolocation_evidence["range_solution"])
        source_frame_id = f"hil-frame-{TRACK_STATUS_SAMPLE_COUNT}"
        source_captured_at_s = now - 0.02 + (TRACK_STATUS_SAMPLE_COUNT - 1) / TRACK_STATUS_RATE_HZ
        produced_at_s = source_captured_at_s + 0.01
        range_status = RangeStatusMessage(
            status_id="77777777-7777-4777-8777-777777777777",
            sequence=210,
            target_id=target_id,
            calibration_id="gazebo-geolocation-evidence",
            source_frame_id=source_frame_id,
            source_captured_at_s=source_captured_at_s,
            produced_at_s=produced_at_s,
            validity=RangeValidity(str(ranging["validity"])),
            reasons=tuple(str(value) for value in cast("list[object]", ranging["reasons"])),
            sources=tuple(str(value) for value in cast("list[object]", ranging["sources"])),
            rejected_sources=tuple(
                str(value) for value in cast("list[object]", ranging["rejected_sources"])
            ),
            slant_range_m=float(ranging["slant_range_m"]),
            ground_range_m=float(ranging["ground_range_m"]),
            slant_range_ci95_m=tuple(
                float(value) for value in cast("list[object]", ranging["slant_range_ci95_m"])
            ),
            ground_range_ci95_m=tuple(
                float(value) for value in cast("list[object]", ranging["ground_range_ci95_m"])
            ),
            relative_bearing_deg=float(ranging["relative_bearing_deg"]),
            absolute_bearing_deg=float(ranging["absolute_bearing_deg"]),
            bearing_sigma_deg=float(ranging["bearing_sigma_deg"]),
            north_offset_m=float(ranging["north_offset_m"]),
            east_offset_m=float(ranging["east_offset_m"]),
            data_freshness_s=float(ranging["data_freshness_s"]),
            sensor_consistency=float(ranging["sensor_consistency"]),
            fusion_profile=str(ranging["fusion_profile"]),
            vehicle_profile=str(ranging["vehicle_profile"]),
            navigation_state=str(ranging["navigation_state"]),
            motion_regime=(
                "static" if str(ranging["motion_regime"]) == "stationary"
                else str(ranging["motion_regime"])
            ),
        )
        geolocation_status = TargetGeolocationStatusMessage(
            sequence=211,
            target_id=target_id,
            source_frame_id=source_frame_id,
            source_captured_at_s=source_captured_at_s,
            produced_at_s=produced_at_s,
            available=True,
            reason="gps_qualified",
            latitude_deg=float(predicted["latitude_deg"]),
            longitude_deg=float(predicted["longitude_deg"]),
            horizontal_sigma_m=float(predicted["horizontal_sigma_m"]),
        )
        geolocation_payloads = [
            codec.encode_range_status(range_status),
            codec.encode_target_geolocation_status(geolocation_status),
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
        label=target_label,
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
            label=target_label,
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
            *geolocation_payloads,
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
        raise HilFailure("the signed operator HIL driver is restricted to 127.0.0.1")
    if not 1024 <= args.port <= 65535:
        raise HilFailure("HIL UDP port must be in 1024..65535")
    if not 0 <= args.local_port <= 65535:
        raise HilFailure("local operator UDP port must be in 0..65535")
    key_text = os.environ.get(args.key_env, "")
    key = key_text.encode("utf-8")
    if len(key) < 32:
        raise HilFailure(f"{args.key_env} must contain at least 32 UTF-8 bytes")
    signing_key_text = os.environ.get(args.signing_key_hex_env, "").strip()
    try:
        signing_key = bytes.fromhex(signing_key_text)
    except ValueError as error:
        raise HilFailure(f"{args.signing_key_hex_env} must contain 64 hexadecimal digits") from error
    if len(signing_key) != 32 or signing_key.hex() != signing_key_text.lower():
        raise HilFailure(f"{args.signing_key_hex_env} must contain 64 hexadecimal digits")

    target_geolocation_evidence = _load_target_geolocation_evidence(
        args.target_geolocation_evidence,
        args.expected_release_label,
    )
    geometry = VideoGeometry(args.stream_id, args.width, args.height, args.rotation)
    codec = OperatorTunnelCodec(hmac_key=key, geometries=(geometry,))
    destination = (args.host, args.port)
    endpoint = OperatorMavlinkEndpoint(
        local_system_id=1,
        local_component_id=191,
        remote_system_id=255,
        remote_component_id=190,
    )
    adapter = OperatorMavlinkTunnelAdapter(
        codec,
        endpoint,
        signing_key=signing_key,
        signing_link_id=1,
        initial_signing_timestamp=_signing_timestamp(),
    )
    heartbeat_serializer = _signed_serializer(signing_key, system_id=1, component_id=191)
    autopilot_serializer = _signed_serializer(signing_key, system_id=1, component_id=1)
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
    channel.bind(("127.0.0.1", args.local_port))
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
                    channel.sendto(jetson_heartbeat.pack(heartbeat_serializer), destination)
                    jetson_component_heartbeats_sent += 1
                else:
                    channel.sendto(autopilot_heartbeat.pack(autopilot_serializer), destination)
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
            try:
                payload = adapter.extract_authenticated_operator_payload(
                    datagram,
                    ignore_unrelated_message=True,
                )
            except Exception as error:
                raise HilFailure(f"QGC signed operator datagram rejected: {error}") from error
            if payload is None:
                continue
            inbound_tunnel_count += 1
            packet = codec.decode(payload)

            if packet.message_type is WireMessageType.TARGET_SELECTION:
                if not isinstance(packet.message, TargetSelectionCommand):
                    raise HilFailure("decoded target selection has the wrong type")
                decoded_selection = packet.message
                if (
                    decoded_selection.action not in (SelectionAction.SELECT, SelectionAction.SELECT_TRK)
                    or decoded_selection.bbox is None
                ):
                    raise HilFailure("QGC did not send the expected SELECT/TRK rectangle")
                selection = decoded_selection
                acknowledgement = SelectionAck(
                    decoded_selection.command_id,
                    True,
                    SelectionAckReason.ACCEPTED,
                    decoded_selection.sequence,
                )
                channel.sendto(
                    adapter.wrap_authenticated_operator_payload(
                        codec.encode_ack(
                            acknowledgement,
                            sequence=201,
                            sent_at_s=time.time(),
                        )
                    ),
                    destination,
                )
                status_payloads, challenge = _status_payloads(
                    codec,
                    geometry,
                    decoded_selection,
                    target_geolocation_evidence,
                )
                for status_payload in status_payloads:
                    time.sleep(0.05)
                    channel.sendto(adapter.wrap_authenticated_operator_payload(status_payload), destination)
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
                channel.sendto(
                    adapter.wrap_authenticated_operator_payload(
                        codec.encode_authorization_ack(
                            acknowledgement,
                            sequence=206,
                            sent_at_s=time.time(),
                        )
                    ),
                    destination,
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
                    "range_status_sent": target_geolocation_evidence is not None,
                    "target_geolocation_status_sent": target_geolocation_evidence is not None,
                    "target_geolocation_evidence": target_geolocation_evidence,
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

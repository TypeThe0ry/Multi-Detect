from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .alerts import (
    AlertAuthenticationError,
    AlertPublisher,
    AuthenticatedUdpAlertReceiver,
    JsonLineAlertPublisher,
    RetryingAcknowledgedAlertPublisher,
    SqliteAlertDeduplicationStore,
    SqliteAlertOutbox,
    UdpAcknowledgedAlertTransport,
)
from .audit import AuditLog
from .config import MissionConfig
from .domain import BoundingBox, VehicleTelemetry
from .evaluation import (
    JsonlPredictionWriter,
    evaluate_detections,
    evaluation_document,
    load_ground_truth_jsonl,
    load_prediction_jsonl,
)
from .live import LiveMissionRunner, LiveRunConfig
from .mission import MissionController
from .model_manifest import (
    PINNED_LEGACY_CHECKPOINT_SHA256,
    PINNED_LEGACY_CHECKPOINT_SIZE_BYTES,
    VerifiedModelArtifact,
    create_candidate_model_manifest,
    verify_checkpoint_bytes,
    verify_model_manifest,
    write_candidate_model_manifest,
)
from .operator_bridge import LiveOperatorBridge
from .operator_link import (
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from .operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
from .operator_protocol import (
    MAX_TUNNEL_PAYLOAD_BYTES,
    OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
    OperatorTunnelCodec,
)
from .operator_tracking import OperatorTargetLock, TargetLockConfig
from .operator_transport import SelectionCommandServer, SelectionRetryClient
from .operator_udp import UdpOperatorSelectionClient, UdpOperatorSelectionServer
from .payload_inventory import (
    ConfiguredSimulationPayloadInventoryProvider,
    FailClosedPayloadInventoryProvider,
    FilePayloadInventoryProvider,
    load_payload_inventory_snapshot,
    verify_payload_inventory,
)
from .pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from .replay import load_jsonl_replay
from .synthetic_model import create_synthetic_hil_model_bundle
from .telemetry import FailClosedTelemetryProvider
from .vision import (
    CaptureConfig,
    DetectorEnsemble,
    OnnxNx6Config,
    OnnxNx6Detector,
    OpenCVFrameSource,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-detect",
        description="Safety-first non-hazardous mission orchestration and live perception.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate a mission JSON file")
    validate.add_argument("config", type=Path)

    replay = subparsers.add_parser("replay", help="run detections through the simulated loop")
    replay.add_argument("config", type=Path)
    replay.add_argument("frames", type=Path)
    replay.add_argument(
        "--simulate-authorized-cycle",
        action="store_true",
        help=(
            "explicitly act as a demo operator and complete one FakePayloadPort transaction; "
            "never controls hardware"
        ),
    )
    replay.add_argument("--operator-id", default="demo-operator")
    replay.add_argument("--audit-out", type=Path)

    payload_check = subparsers.add_parser(
        "payload-inventory-check",
        help="validate a read-only HIL payload inventory report against a mission",
    )
    payload_check.add_argument("config", type=Path)
    payload_check.add_argument("report", type=Path)
    payload_check.add_argument("--now-s", type=float, required=True)

    evaluation = subparsers.add_parser(
        "evaluate-detections",
        help="compare prediction JSONL with frame-aligned ground truth",
    )
    evaluation.add_argument("ground_truth", type=Path)
    evaluation.add_argument("predictions", type=Path)
    evaluation.add_argument("--iou-threshold", type=float, default=0.5)
    evaluation.add_argument("--confidence-threshold", type=float, default=0.25)
    payload_check.add_argument("--hmac-key-env")
    payload_check.add_argument("--expected-key-id")

    camera_check = subparsers.add_parser(
        "camera-check", help="open a local/RTSP source, read one frame, and discard it"
    )
    _add_capture_arguments(camera_check)
    camera_check.add_argument("--frames", type=int, default=1)

    model_check = subparsers.add_parser(
        "model-check",
        help="validate a post-NMS ONNX Nx6 contract and benchmark synthetic inference",
    )
    model_check.add_argument("--onnx-model", type=Path, required=True)
    model_check.add_argument("--model-manifest", type=Path)
    model_check.add_argument(
        "--model-role",
        choices=("fire_candidate", "safety_object_evidence"),
        default="fire_candidate",
    )
    model_check.add_argument("--require-production-approved", action="store_true")
    model_check.add_argument("--class-names", default="fire,smoke")
    model_check.add_argument("--input-width", type=int, default=640)
    model_check.add_argument("--input-height", type=int, default=640)
    model_check.add_argument("--confidence-threshold", type=float, default=0.25)
    model_check.add_argument(
        "--output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
        default="letterbox_xyxy_px",
    )
    model_check.add_argument("--provider", action="append", default=[])
    model_check.add_argument("--trt-engine-cache", type=Path)
    model_check.add_argument("--warmup-iterations", type=int, default=3)
    model_check.add_argument("--benchmark-iterations", type=int, default=20)

    manifest_init = subparsers.add_parser(
        "model-manifest-init",
        help="create a quarantined candidate manifest bound to a local ONNX artifact",
    )
    manifest_init.add_argument("--onnx-model", type=Path, required=True)
    manifest_init.add_argument("--out", type=Path, required=True)
    manifest_init.add_argument("--model-id", required=True)
    manifest_init.add_argument("--model-version", required=True)
    manifest_init.add_argument("--source-description", required=True)
    manifest_init.add_argument(
        "--model-role",
        choices=("fire_candidate", "safety_object_evidence"),
        default="fire_candidate",
    )
    manifest_init.add_argument("--class-names", default="fire,smoke")
    manifest_init.add_argument("--input-width", type=int, default=640)
    manifest_init.add_argument("--input-height", type=int, default=640)
    manifest_init.add_argument(
        "--output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
        required=True,
    )
    manifest_init.add_argument("--force", action="store_true")

    synthetic_model = subparsers.add_parser(
        "synthetic-model-init",
        help="create a constant-output Nx6 model for software HIL only; never for production",
    )
    synthetic_model.add_argument("--out-dir", type=Path, required=True)
    synthetic_model.add_argument("--input-width", type=int, default=640)
    synthetic_model.add_argument("--input-height", type=int, default=640)
    synthetic_model.add_argument("--force", action="store_true")

    checkpoint_verify = subparsers.add_parser(
        "legacy-checkpoint-verify",
        help="verify checkpoint bytes without importing torch or deserializing pickle",
    )
    checkpoint_verify.add_argument("checkpoint", type=Path)
    checkpoint_verify.add_argument(
        "--expected-size-bytes",
        type=int,
        default=PINNED_LEGACY_CHECKPOINT_SIZE_BYTES,
    )
    checkpoint_verify.add_argument(
        "--expected-sha256",
        default=PINNED_LEGACY_CHECKPOINT_SHA256,
    )

    pixhawk_check = subparsers.add_parser(
        "pixhawk-check",
        help="sample a Pixhawk MAVLink link read-only; never transmits commands or requests",
    )
    pixhawk_check.add_argument("--endpoint", required=True)
    pixhawk_check.add_argument("--baud", type=int, default=57_600)
    pixhawk_check.add_argument("--samples", type=int, default=10)
    pixhawk_check.add_argument("--interval-seconds", type=float, default=0.2)
    pixhawk_check.add_argument("--require-fresh-link", action="store_true")

    alert_receiver = subparsers.add_parser(
        "alert-udp-receiver",
        help="receive authenticated fire alerts and return correlated UDP acknowledgements",
    )
    alert_receiver.add_argument("--bind-host", default="127.0.0.1")
    alert_receiver.add_argument("--port", type=int, default=14_600)
    alert_receiver.add_argument("--hmac-key-env", required=True)
    alert_receiver.add_argument("--receiver-id", required=True)
    alert_receiver.add_argument("--expected-sender-id", required=True)
    alert_receiver.add_argument("--max-messages", type=int, default=1)
    alert_receiver.add_argument("--receive-timeout-seconds", type=float)
    alert_receiver.add_argument("--maximum-clock-skew-seconds", type=float, default=30.0)
    alert_receiver.add_argument("--max-rejected-packets", type=int, default=100)

    subparsers.add_parser(
        "operator-link-demo",
        help=(
            "run an authenticated G20-to-Jetson TUNNEL payload loopback with simulated loss; "
            "never controls hardware"
        ),
    )

    operator_server = subparsers.add_parser(
        "operator-udp-server",
        help="run the Jetson-side signed selection/ACK UDP diagnostic endpoint",
    )
    operator_server.add_argument("--bind-host", default="0.0.0.0")
    operator_server.add_argument("--port", type=int, default=14_580)
    operator_server.add_argument("--operator-hmac-key-env", required=True)
    operator_server.add_argument("--mavlink-signing-key-hex-env", required=True)
    operator_server.add_argument("--stream-id", default="camera-main")
    operator_server.add_argument("--width", type=int, default=1280)
    operator_server.add_argument("--height", type=int, default=720)
    operator_server.add_argument("--rotation", type=int, default=0)
    operator_server.add_argument("--local-system-id", type=int, default=1)
    operator_server.add_argument("--local-component-id", type=int, default=191)
    operator_server.add_argument("--remote-system-id", type=int, default=255)
    operator_server.add_argument("--remote-component-id", type=int, default=190)
    operator_server.add_argument("--receive-timeout-seconds", type=float, default=30.0)
    operator_server.add_argument("--max-datagrams", type=int, default=1)

    operator_client = subparsers.add_parser(
        "operator-udp-select",
        help="send one signed selection to a Jetson UDP diagnostic endpoint and require its ACK",
    )
    operator_client.add_argument("--host", required=True)
    operator_client.add_argument("--port", type=int, default=14_580)
    operator_client.add_argument("--operator-hmac-key-env", required=True)
    operator_client.add_argument("--mavlink-signing-key-hex-env", required=True)
    operator_client.add_argument("--stream-id", default="camera-main")
    operator_client.add_argument("--width", type=int, default=1280)
    operator_client.add_argument("--height", type=int, default=720)
    operator_client.add_argument("--rotation", type=int, default=0)
    operator_client.add_argument("--x1", type=float, default=0.32)
    operator_client.add_argument("--y1", type=float, default=0.21)
    operator_client.add_argument("--x2", type=float, default=0.61)
    operator_client.add_argument("--y2", type=float, default=0.72)
    operator_client.add_argument("--ttl-seconds", type=float, default=3.0)
    operator_client.add_argument("--retry-interval-seconds", type=float, default=0.25)
    operator_client.add_argument("--maximum-attempts", type=int, default=3)
    operator_client.add_argument("--local-system-id", type=int, default=255)
    operator_client.add_argument("--local-component-id", type=int, default=190)
    operator_client.add_argument("--remote-system-id", type=int, default=1)
    operator_client.add_argument("--remote-component-id", type=int, default=191)
    alert_receiver.add_argument("--deduplication-db", type=Path)

    live = subparsers.add_parser(
        "live-camera",
        help="local/RTSP capture -> ONNX Nx6 -> safety/authorization UI; no physical release",
    )
    live.add_argument("config", type=Path)
    _add_capture_arguments(live)
    live.add_argument("--onnx-model", type=Path, required=True)
    live.add_argument("--model-manifest", type=Path)
    live.add_argument("--class-names", default="fire,smoke")
    live.add_argument("--safety-onnx-model", type=Path)
    live.add_argument("--safety-model-manifest", type=Path)
    live.add_argument("--require-production-approved-models", action="store_true")
    live.add_argument(
        "--allow-synthetic-hil-model",
        action="store_true",
        help="explicitly allow a constant-output synthetic model for local software HIL only",
    )
    live.add_argument("--safety-class-names", default="person,firefighter")
    live.add_argument("--input-width", type=int, default=640)
    live.add_argument("--input-height", type=int, default=640)
    live.add_argument("--confidence-threshold", type=float, default=0.25)
    live.add_argument(
        "--output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
        default="letterbox_xyxy_px",
    )
    live.add_argument(
        "--safety-output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
    )
    live.add_argument("--provider", action="append", default=[])
    live.add_argument("--trt-engine-cache", type=Path)
    live.add_argument("--pixhawk-endpoint")
    live.add_argument("--pixhawk-baud", type=int, default=57_600)
    live.add_argument("--observe-pixhawk-lifecycle", action="store_true")
    live.add_argument("--task-area-mission-sequence", type=int)
    live.add_argument("--allowed-auto-mode", action="append", default=[])
    live.add_argument("--operator-id", default="local-operator")
    live.add_argument("--max-frames", type=int)
    live.add_argument("--alert-banner-seconds", type=float, default=5.0)
    live.add_argument("--performance-window-frames", type=int, default=600)
    live.add_argument("--alert-outbox", type=Path)
    live.add_argument("--alert-udp-host")
    live.add_argument("--alert-udp-port", type=int, default=14_600)
    live.add_argument("--alert-hmac-key-env")
    live.add_argument("--alert-sender-id", default="aircraft-1")
    live.add_argument("--alert-receiver-id", default="ground-station-1")
    live.add_argument("--alert-ack-timeout-seconds", type=float, default=1.0)
    live.add_argument("--alert-delivery-attempts", type=int, default=3)
    live.add_argument("--alert-maximum-clock-skew-seconds", type=float, default=30.0)
    live.add_argument("--payload-inventory-report", type=Path)
    live.add_argument("--payload-inventory-hmac-key-env")
    live.add_argument("--payload-inventory-key-id")
    live.add_argument(
        "--operator-udp-port",
        type=int,
        help="enable signed remote target selection/status metadata; never enables control",
    )
    live.add_argument("--operator-udp-bind-host", default="0.0.0.0")
    live.add_argument("--operator-hmac-key-env")
    live.add_argument("--mavlink-signing-key-hex-env")
    live.add_argument("--operator-stream-id", default="camera-main")
    live.add_argument("--operator-source-width", type=int, default=1280)
    live.add_argument("--operator-source-height", type=int, default=720)
    live.add_argument("--operator-source-rotation", type=int, default=0)
    live.add_argument("--operator-acquisition-timeout-seconds", type=float, default=1.0)
    live.add_argument("--operator-lost-after-seconds", type=float, default=0.75)
    live.add_argument("--operator-local-system-id", type=int, default=1)
    live.add_argument("--operator-local-component-id", type=int, default=191)
    live.add_argument("--operator-remote-system-id", type=int, default=255)
    live.add_argument("--operator-remote-component-id", type=int, default=190)
    live.add_argument(
        "--simulate-payload-cycle",
        action="store_true",
        help="enable operator-triggered FakePayloadPort HIL cycle; never controls hardware",
    )
    live.add_argument("--no-display", action="store_true")
    live.add_argument("--audit-out", type=Path)
    live.add_argument("--prediction-log-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            return _validate_config(args.config)
        if args.command == "replay":
            return _run_replay(
                config_path=args.config,
                replay_path=args.frames,
                simulate_authorized_cycle=args.simulate_authorized_cycle,
                operator_id=args.operator_id,
                audit_out=args.audit_out,
            )
        if args.command == "camera-check":
            return _camera_check(_capture_config_from_args(args), frame_count=args.frames)
        if args.command == "payload-inventory-check":
            return _payload_inventory_check(args)
        if args.command == "evaluate-detections":
            return _evaluate_detection_logs(args)
        if args.command == "model-check":
            return _run_model_check(args)
        if args.command == "model-manifest-init":
            return _run_model_manifest_init(args)
        if args.command == "synthetic-model-init":
            return _run_synthetic_model_init(args)
        if args.command == "legacy-checkpoint-verify":
            return _run_legacy_checkpoint_verify(args)
        if args.command == "pixhawk-check":
            return _run_pixhawk_check(args)
        if args.command == "alert-udp-receiver":
            return _run_alert_udp_receiver(args)
        if args.command == "operator-link-demo":
            return _run_operator_link_demo()
        if args.command == "operator-udp-server":
            return _run_operator_udp_server(args)
        if args.command == "operator-udp-select":
            return _run_operator_udp_select(args)
        if args.command == "live-camera":
            return _run_live_camera(args)
    except (OSError, ValueError, RuntimeError) as exc:
        _emit(
            {
                "event": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "simulation_only": args.command != "live-camera",
                "hardware_control_enabled": False,
            },
            stream=sys.stderr,
        )
        return 1
    parser.error(f"unsupported command: {args.command}")
    return 2


def _validate_config(path: Path) -> int:
    config = MissionConfig.from_json(path)
    _emit(
        {
            "event": "config_valid",
            "mission_id": config.mission_id,
            "mission_type": config.mission_type.value,
            "platform_mode": config.platform_mode.value,
            "mission_capability": (
                "deployment_capable" if config.deployment_capable else "patrol_only"
            ),
            "payload_installed": config.payload_installed,
            "payload_count": len(config.payloads),
            "deployment_capable": config.deployment_capable,
            "human_authorization_required": config.human_authorization_required,
            "simulation_only": True,
        }
    )
    return 0


def _run_operator_link_demo() -> int:
    """Exercise selection retry, idempotent ACK and tracking metadata without a radio."""

    geometry = VideoGeometry("camera-main", 1280, 720)
    codec = OperatorTunnelCodec(
        hmac_key=b"operator-link-loopback-only-key-material-v1",
        geometries=(geometry,),
    )
    mavlink_signing_key = b"M" * 32
    g20_mavlink = OperatorMavlinkTunnelAdapter(
        codec,
        OperatorMavlinkEndpoint(255, 190, 1, 191),
        signing_key=mavlink_signing_key,
        signing_link_id=3,
        initial_signing_timestamp=1_000_000,
    )
    jetson_mavlink = OperatorMavlinkTunnelAdapter(
        codec,
        OperatorMavlinkEndpoint(1, 191, 255, 190),
        signing_key=mavlink_signing_key,
        signing_link_id=4,
        initial_signing_timestamp=2_000_000,
    )
    command = TargetSelectionCommand(
        command_id=str(UUID("11111111-1111-4111-8111-111111111111")),
        session_id=str(UUID("22222222-2222-4222-8222-222222222222")),
        sequence=1,
        action=SelectionAction.SELECT,
        geometry=geometry,
        issued_at_s=1_000.0,
        expires_at_s=1_003.0,
        bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
        displayed_frame_id="g20-frame-500",
    )
    client = SelectionRetryClient(codec, command)
    server = SelectionCommandServer(codec, SelectionCommandGuard(geometry))

    first = client.poll(now_s=1_000.0)
    if first is None:  # pragma: no cover - internal invariant
        raise RuntimeError("operator-link demo did not produce the first packet")
    first_mavlink_frame = g20_mavlink.wrap_authenticated_operator_payload(first)
    _emit(
        {
            "event": "operator_selection_encoded",
            "command_id": command.command_id,
            "payload_type": OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
            "payload_bytes": len(first),
            "maximum_payload_bytes": MAX_TUNNEL_PAYLOAD_BYTES,
            "mavlink2_signed": True,
            "mavlink_frame_bytes": len(first_mavlink_frame),
            "bbox": command.bbox.rounded() if command.bbox else None,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )
    _emit(
        {
            "event": "simulated_command_packet_lost",
            "attempt": client.attempts,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )

    retry = client.poll(now_s=1_000.25)
    if retry is None or retry != first:  # pragma: no cover - internal invariant
        raise RuntimeError("operator-link retry was not an identical packet")
    retry = jetson_mavlink.extract_authenticated_operator_payload(
        g20_mavlink.wrap_authenticated_operator_payload(retry)
    )
    accepted = server.handle_selection(
        retry,
        received_at_s=1_000.26,
        acknowledgement_sequence=1,
    )
    _emit(
        {
            "event": "jetson_selection_evaluated",
            "accepted": accepted.acceptance.allowed,
            "duplicate": accepted.duplicate,
            "attempt": client.attempts,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )
    _emit(
        {
            "event": "simulated_ack_packet_lost",
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )

    final_retry = client.poll(now_s=1_000.50)
    if final_retry is None:  # pragma: no cover - internal invariant
        raise RuntimeError("operator-link demo did not produce the final retry")
    final_retry = jetson_mavlink.extract_authenticated_operator_payload(
        g20_mavlink.wrap_authenticated_operator_payload(final_retry)
    )
    duplicate = server.handle_selection(
        final_retry,
        received_at_s=1_000.51,
        acknowledgement_sequence=2,
    )
    acknowledgement_frame = jetson_mavlink.wrap_authenticated_operator_payload(
        duplicate.acknowledgement_payload
    )
    acknowledgement = client.handle_acknowledgement(
        g20_mavlink.extract_authenticated_operator_payload(acknowledgement_frame)
    )
    _emit(
        {
            "event": "g20_selection_acknowledged",
            "accepted": acknowledgement.accepted,
            "reason": acknowledgement.reason.name.lower(),
            "attempts": client.attempts,
            "jetson_detected_duplicate": duplicate.duplicate,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )

    status = TrackStatusMessage(
        status_id=str(UUID("33333333-3333-4333-8333-333333333333")),
        selection_command_id=command.command_id,
        sequence=1,
        geometry=geometry,
        state=TrackingState.TRACKING,
        target_id="track-42",
        bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
        label="flame",
        confidence=0.91,
        tracking_quality=0.87,
        source_frame_id="jetson-frame-700",
        source_captured_at_s=1_000.52,
        produced_at_s=1_000.55,
        relative_bearing_deg=-4.2,
        estimated_range_m=82.0,
    )
    encoded_status = codec.encode_track_status(status)
    status_frame = jetson_mavlink.wrap_authenticated_operator_payload(encoded_status)
    received_status = codec.decode(
        g20_mavlink.extract_authenticated_operator_payload(status_frame)
    ).message
    if not isinstance(received_status, TrackStatusMessage):  # pragma: no cover
        raise RuntimeError("operator-link demo returned the wrong status type")
    _emit(
        {
            "event": "g20_track_status_received",
            "state": received_status.state.value,
            "target_id": received_status.target_id,
            "label": received_status.label,
            "confidence": received_status.confidence,
            "tracking_quality": received_status.tracking_quality,
            "bbox": received_status.bbox.rounded() if received_status.bbox else None,
            "payload_bytes": len(encoded_status),
            "mavlink_frame_bytes": len(status_frame),
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )
    _emit(
        {
            "event": "operator_link_demo_finished",
            "selection_delivered": acknowledgement.accepted,
            "tracking_status_received": True,
            "physical_payload_interface_present": False,
            "autopilot_write_enabled": False,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_operator_udp_server(args: argparse.Namespace) -> int:
    if args.max_datagrams <= 0:
        raise ValueError("--max-datagrams must be positive")
    geometry = VideoGeometry(args.stream_id, args.width, args.height, args.rotation)
    adapter = _operator_mavlink_adapter(
        geometry=geometry,
        operator_hmac_key_env=args.operator_hmac_key_env,
        mavlink_signing_key_hex_env=args.mavlink_signing_key_hex_env,
        local_system_id=args.local_system_id,
        local_component_id=args.local_component_id,
        remote_system_id=args.remote_system_id,
        remote_component_id=args.remote_component_id,
    )
    with UdpOperatorSelectionServer(
        bind_host=args.bind_host,
        port=args.port,
        mavlink=adapter,
        guard=SelectionCommandGuard(geometry),
        receive_timeout_s=args.receive_timeout_seconds,
    ) as server:
        _emit(
            {
                "event": "operator_udp_server_ready",
                "bind_host": server.bound_address[0],
                "port": server.bound_address[1],
                "stream_id": geometry.stream_id,
                "local_system_id": args.local_system_id,
                "local_component_id": args.local_component_id,
                "maximum_datagrams": args.max_datagrams,
                "hardware_control_enabled": False,
            }
        )
        accepted_count = 0
        for _ in range(args.max_datagrams):
            result, peer = server.serve_once()
            accepted_count += int(result.acceptance.allowed)
            _emit(
                {
                    "event": "operator_udp_selection_processed",
                    "command_id": result.command.command_id,
                    "accepted": result.acceptance.allowed,
                    "reasons": list(result.acceptance.reasons),
                    "duplicate": result.duplicate,
                    "peer_host": peer[0],
                    "peer_port": peer[1],
                    "hardware_control_enabled": False,
                }
            )
    _emit(
        {
            "event": "operator_udp_server_finished",
            "datagram_count": args.max_datagrams,
            "accepted_count": accepted_count,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_operator_udp_select(args: argparse.Namespace) -> int:
    geometry = VideoGeometry(args.stream_id, args.width, args.height, args.rotation)
    adapter = _operator_mavlink_adapter(
        geometry=geometry,
        operator_hmac_key_env=args.operator_hmac_key_env,
        mavlink_signing_key_hex_env=args.mavlink_signing_key_hex_env,
        local_system_id=args.local_system_id,
        local_component_id=args.local_component_id,
        remote_system_id=args.remote_system_id,
        remote_component_id=args.remote_component_id,
    )
    issued_at_s = time.time()
    command = TargetSelectionCommand(
        command_id=str(uuid4()),
        session_id=str(uuid4()),
        sequence=1,
        action=SelectionAction.SELECT,
        geometry=geometry,
        issued_at_s=issued_at_s,
        expires_at_s=issued_at_s + args.ttl_seconds,
        bbox=BoundingBox(args.x1, args.y1, args.x2, args.y2),
        displayed_frame_id="udp-diagnostic",
    )
    receipt = UdpOperatorSelectionClient(
        host=args.host,
        port=args.port,
        mavlink=adapter,
        retry_interval_s=args.retry_interval_seconds,
        maximum_attempts=args.maximum_attempts,
    ).deliver(command)
    _emit(
        {
            "event": "operator_udp_selection_acknowledged",
            "command_id": command.command_id,
            "accepted": receipt.acknowledgement.accepted,
            "reason": receipt.acknowledgement.reason.name.lower(),
            "attempts": receipt.attempts,
            "elapsed_ms": receipt.elapsed_s * 1000.0,
            "remote_host": receipt.remote[0],
            "remote_port": receipt.remote[1],
            "hardware_control_enabled": False,
        }
    )
    return 0 if receipt.acknowledgement.accepted else 1


def _operator_mavlink_adapter(
    *,
    geometry: VideoGeometry,
    operator_hmac_key_env: str | None,
    mavlink_signing_key_hex_env: str | None,
    local_system_id: int,
    local_component_id: int,
    remote_system_id: int,
    remote_component_id: int,
) -> OperatorMavlinkTunnelAdapter:
    application_key = _hmac_key_from_env(operator_hmac_key_env)
    if application_key is None or len(application_key) < 32:
        raise ValueError("operator-link HMAC key must contain at least 32 bytes")
    if mavlink_signing_key_hex_env is None:
        raise ValueError("remote operator link requires --mavlink-signing-key-hex-env")
    signing_key = _mavlink_signing_key_from_env(mavlink_signing_key_hex_env)
    endpoint = OperatorMavlinkEndpoint(
        local_system_id,
        local_component_id,
        remote_system_id,
        remote_component_id,
    )
    return OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=application_key, geometries=(geometry,)),
        endpoint,
        signing_key=signing_key,
        signing_link_id=local_component_id,
        initial_signing_timestamp=_current_mavlink_signing_timestamp(),
    )


def _mavlink_signing_key_from_env(variable_name: str) -> bytes:
    name = variable_name.strip()
    if not name:
        raise ValueError("MAVLink signing-key environment variable name cannot be empty")
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"MAVLink signing-key environment variable is missing: {name}")
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        raise ValueError("MAVLink signing key must be exactly 64 hexadecimal characters")
    return bytes.fromhex(value)


def _current_mavlink_signing_timestamp() -> int:
    mavlink_epoch_unix_s = 1_420_070_400
    return max(1, int((time.time() - mavlink_epoch_unix_s) * 100_000))


def _add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--source",
        help="camera index such as 0, or an RTSP URI (prefer --source-env for credentials)",
    )
    source_group.add_argument(
        "--source-env",
        metavar="ENV_VAR",
        help="read the camera source from an environment variable without placing it in argv",
    )
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--rtsp-transport", choices=("tcp", "udp"), default="tcp")
    parser.add_argument("--backend", choices=("auto", "dshow", "msmf", "ffmpeg"), default="auto")
    parser.add_argument("--reconnect-attempts", type=int, default=3)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=0.25)


def _capture_config_from_args(args: argparse.Namespace) -> CaptureConfig:
    raw_source = _camera_source_from_args(args)
    if not raw_source:
        raise ValueError("camera source cannot be empty")
    source: int | str = int(raw_source) if raw_source.isdigit() else raw_source
    return CaptureConfig(
        source=source,
        width=args.width,
        height=args.height,
        fps=args.fps,
        rtsp_transport=args.rtsp_transport,
        backend=args.backend,
        reconnect_attempts=args.reconnect_attempts,
        reconnect_delay_seconds=args.reconnect_delay_seconds,
    )


def _camera_source_from_args(args: argparse.Namespace) -> str:
    variable_name = args.source_env
    if variable_name is None:
        return str(args.source if args.source is not None else "0").strip()
    name = variable_name.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("camera source environment variable name is invalid")
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"camera source environment variable is missing: {name}")
    return value.strip()


def _parse_class_names(raw: str) -> tuple[str, ...]:
    labels = tuple(label.strip() for label in raw.split(",") if label.strip())
    if not labels:
        raise ValueError("class names must contain at least one comma-separated label")
    return labels


def _hmac_key_from_env(variable_name: str | None) -> bytes | None:
    if variable_name is None:
        return None
    name = variable_name.strip()
    if not name:
        raise ValueError("HMAC key environment variable name cannot be empty")
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"HMAC key environment variable is missing: {name}")
    return value.encode("utf-8")


def _required_alert_hmac_key_from_env(variable_name: str | None) -> bytes:
    if variable_name is None:
        raise ValueError("authenticated UDP alerts require --alert-hmac-key-env")
    key = _hmac_key_from_env(variable_name)
    assert key is not None
    if len(key) < 32:
        raise ValueError("alert HMAC key must contain at least 32 bytes")
    return key


def _verify_optional_model_manifest(
    *,
    manifest_path: Path | None,
    model_path: Path,
    class_names: tuple[str, ...],
    output_coordinates: str,
    require_production_approved: bool,
    expected_model_role: str,
) -> VerifiedModelArtifact | None:
    if manifest_path is None:
        if require_production_approved:
            raise ValueError("a model manifest is required by the production-approval gate")
        return None
    return verify_model_manifest(
        manifest_path,
        model_path,
        expected_class_names=class_names,
        expected_output_coordinates=output_coordinates,
        expected_model_role=expected_model_role,
        require_production_approved=require_production_approved,
    )


def _telemetry_document(telemetry: VehicleTelemetry) -> dict[str, object]:
    return {
        "latitude_deg": _finite_or_none(telemetry.latitude_deg),
        "longitude_deg": _finite_or_none(telemetry.longitude_deg),
        "altitude_agl_m": _finite_or_none(telemetry.altitude_agl_m),
        "heading_deg": _finite_or_none(telemetry.heading_deg),
        "ground_speed_mps": _finite_or_none(telemetry.ground_speed_mps),
        "roll_deg": _finite_or_none(telemetry.roll_deg),
        "pitch_deg": _finite_or_none(telemetry.pitch_deg),
        "battery_remaining_pct": _finite_or_none(telemetry.battery_remaining_pct),
        "satellites_visible": telemetry.satellites_visible,
        "armed": telemetry.armed,
        "flight_mode": telemetry.flight_mode,
        "mission_sequence": telemetry.mission_sequence,
        "link_healthy": telemetry.link_healthy,
        "position_healthy": telemetry.position_healthy,
        "geofence_healthy": telemetry.geofence_healthy,
        "in_allowed_zone": telemetry.in_allowed_zone,
        "flight_mode_allows_deploy": telemetry.flight_mode_allows_deploy,
        "release_zone_clear": telemetry.release_zone_clear,
    }


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _camera_check(capture_config: CaptureConfig, *, frame_count: int = 1) -> int:
    if frame_count <= 0:
        raise ValueError("camera-check frame count must be positive")
    latencies_ms: list[float] = []
    started_s = time.perf_counter()
    with OpenCVFrameSource(capture_config) as source:
        captured = None
        expected_size: tuple[int, int] | None = None
        for _ in range(frame_count):
            frame_started_s = time.perf_counter()
            captured = source.read()
            latencies_ms.append((time.perf_counter() - frame_started_s) * 1_000.0)
            size = (captured.width, captured.height)
            if expected_size is None:
                expected_size = size
            elif size != expected_size:
                raise RuntimeError(
                    f"camera resolution changed during check: {expected_size} -> {size}"
                )
        reconnect_count = source.reconnect_count
    assert captured is not None
    elapsed_s = max(time.perf_counter() - started_s, 1e-9)
    _emit(
        {
            "event": "camera_frame_received",
            "source_kind": "rtsp" if capture_config.is_rtsp else "local_device",
            "width": captured.width,
            "height": captured.height,
            "frame_id": captured.frame_id,
            "frame_count": frame_count,
            "average_fps": frame_count / elapsed_s,
            "capture_latency_p50_ms": _sequence_percentile(latencies_ms, 0.50),
            "capture_latency_p95_ms": _sequence_percentile(latencies_ms, 0.95),
            "reconnect_count": reconnect_count,
            "image_saved": False,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _payload_inventory_check(args: argparse.Namespace) -> int:
    config = MissionConfig.from_json(args.config)
    snapshot = load_payload_inventory_snapshot(
        args.report,
        hmac_key=_hmac_key_from_env(args.hmac_key_env),
        expected_key_id=args.expected_key_id,
    )
    verification = verify_payload_inventory(config, snapshot, now_s=args.now_s)
    _emit(
        {
            "event": "payload_inventory_checked",
            "mission_id": config.mission_id,
            "source_id": verification.source_id,
            "allowed": verification.allowed,
            "reasons": verification.reasons,
            "simulation_only": verification.simulation_only,
            "authenticated": snapshot.authenticated,
            "hardware_control_enabled": False,
        }
    )
    return 0 if verification.allowed else 1


def _evaluate_detection_logs(args: argparse.Namespace) -> int:
    report = evaluate_detections(
        load_ground_truth_jsonl(args.ground_truth),
        load_prediction_jsonl(args.predictions),
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.confidence_threshold,
    )
    _emit(
        {
            "event": "detection_evaluation_completed",
            **evaluation_document(report),
        }
    )
    return 0


def _run_model_check(args: argparse.Namespace) -> int:
    if args.warmup_iterations < 0 or args.benchmark_iterations <= 0:
        raise ValueError(
            "model-check iterations must be non-negative warmup and positive benchmark"
        )
    if not args.onnx_model.is_file():
        raise ValueError(f"ONNX model file does not exist: {args.onnx_model}")
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("model-check requires the vision dependencies") from exc
    class_names = _parse_class_names(args.class_names)
    verified = _verify_optional_model_manifest(
        manifest_path=args.model_manifest,
        model_path=args.onnx_model,
        class_names=class_names,
        output_coordinates=args.output_coordinates,
        require_production_approved=args.require_production_approved,
        expected_model_role=args.model_role,
    )
    detector = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=args.onnx_model,
            class_names=class_names,
            input_width=args.input_width,
            input_height=args.input_height,
            confidence_threshold=args.confidence_threshold,
            providers=tuple(args.provider),
            trt_engine_cache_path=args.trt_engine_cache,
            output_coordinates=args.output_coordinates,
            model_version=verified.model_version if verified is not None else None,
        )
    )
    image = np.zeros((args.input_height, args.input_width, 3), dtype=np.uint8)
    for _ in range(args.warmup_iterations):
        detector.detect(image)
    latencies_ms: list[float] = []
    detection_count = 0
    for _ in range(args.benchmark_iterations):
        started_s = time.perf_counter()
        detection_count = len(detector.detect(image))
        latencies_ms.append((time.perf_counter() - started_s) * 1_000.0)
    _emit(
        {
            "event": "onnx_model_validated",
            "model_path": str(args.onnx_model.resolve()),
            "model_sha256": _sha256_file(args.onnx_model),
            "post_nms_output_contract": "Nx6",
            "input_width": args.input_width,
            "input_height": args.input_height,
            "class_names": list(detector.class_names),
            "active_providers": list(detector.provider_names),
            "warmup_iterations": args.warmup_iterations,
            "benchmark_iterations": args.benchmark_iterations,
            "synthetic_detection_count_last_iteration": detection_count,
            "latency_p50_ms": _sequence_percentile(latencies_ms, 0.50),
            "latency_p95_ms": _sequence_percentile(latencies_ms, 0.95),
            "accuracy_validated": False,
            "manifest_validated": verified is not None,
            "manifest_status": verified.status if verified is not None else None,
            "production_approved": (
                verified.production_approved if verified is not None else False
            ),
            "synthetic_hil_only": (verified.synthetic_hil_only if verified is not None else False),
            "model_role": verified.model_role if verified is not None else args.model_role,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_model_manifest_init(args: argparse.Namespace) -> int:
    document = create_candidate_model_manifest(
        args.onnx_model,
        model_id=args.model_id,
        model_version=args.model_version,
        class_names=_parse_class_names(args.class_names),
        input_width=args.input_width,
        input_height=args.input_height,
        output_coordinates=args.output_coordinates,
        source_description=args.source_description,
        model_role=args.model_role,
    )
    destination = write_candidate_model_manifest(
        args.out,
        document,
        overwrite=args.force,
    )
    _emit(
        {
            "event": "candidate_model_manifest_created",
            "manifest_path": str(destination.resolve()),
            "model_path": str(args.onnx_model.resolve()),
            "model_sha256": document["export"]["artifact_sha256"],
            "status": "quarantined",
            "model_role": args.model_role,
            "production_approved": False,
            "accuracy_validated": False,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_synthetic_model_init(args: argparse.Namespace) -> int:
    model_path, manifest_path = create_synthetic_hil_model_bundle(
        args.out_dir,
        input_width=args.input_width,
        input_height=args.input_height,
        overwrite=args.force,
    )
    verified = verify_model_manifest(
        manifest_path,
        model_path,
        expected_class_names=("fire", "smoke"),
        expected_output_coordinates="normalized_xyxy",
        expected_model_role="fire_candidate",
    )
    _emit(
        {
            "event": "synthetic_hil_model_created",
            "model_path": str(model_path.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "model_sha256": verified.artifact_sha256,
            "status": verified.status,
            "synthetic_hil_only": verified.synthetic_hil_only,
            "production_approved": verified.production_approved,
            "accuracy_validated": False,
            "constant_detection": [0.25, 0.25, 0.55, 0.55, 0.95, 0],
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_legacy_checkpoint_verify(args: argparse.Namespace) -> int:
    verification = verify_checkpoint_bytes(
        args.checkpoint,
        expected_size_bytes=args.expected_size_bytes,
        expected_sha256=args.expected_sha256,
    )
    _emit(
        {
            "event": "legacy_checkpoint_bytes_verified",
            "checkpoint_path": str(verification.path.resolve()),
            "actual_size_bytes": verification.actual_size_bytes,
            "actual_sha256": verification.actual_sha256,
            "expected_size_bytes": verification.expected_size_bytes,
            "expected_sha256": verification.expected_sha256,
            "size_matches": verification.size_matches,
            "sha256_matches": verification.sha256_matches,
            "matches_audited_checkpoint": verification.matches,
            "deserialized": False,
            "safe_to_run_directly": False,
            "requires_isolated_export": True,
            "hardware_control_enabled": False,
        }
    )
    return 0 if verification.matches else 1


def _run_pixhawk_check(args: argparse.Namespace) -> int:
    if args.samples <= 0:
        raise ValueError("pixhawk-check samples must be positive")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds < 0:
        raise ValueError("pixhawk-check interval must be a finite non-negative number")
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(endpoint=args.endpoint, baud=args.baud)
    )
    snapshots = []
    try:
        for index in range(args.samples):
            snapshots.append(provider.snapshot(now_s=time.monotonic()))
            if index + 1 < args.samples and args.interval_seconds > 0:
                time.sleep(args.interval_seconds)
    finally:
        provider.close()
    latest = snapshots[-1]
    fresh_link_samples = sum(snapshot.link_healthy is True for snapshot in snapshots)
    fresh_position_samples = sum(snapshot.position_healthy is True for snapshot in snapshots)
    if args.require_fresh_link and fresh_link_samples == 0:
        raise RuntimeError("Pixhawk link produced no fresh heartbeat during the check")
    _emit(
        {
            "event": "pixhawk_read_only_check_finished",
            "endpoint": args.endpoint,
            "baud": args.baud,
            "sample_count": len(snapshots),
            "fresh_link_sample_count": fresh_link_samples,
            "fresh_position_sample_count": fresh_position_samples,
            "latest": _telemetry_document(latest),
            "read_only": provider.is_read_only,
            "messages_transmitted": 0,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_alert_udp_receiver(args: argparse.Namespace) -> int:
    if args.max_messages <= 0:
        raise ValueError("maximum received message count must be positive")
    if args.max_rejected_packets < 0:
        raise ValueError("maximum rejected packet count cannot be negative")
    key = _required_alert_hmac_key_from_env(args.hmac_key_env)
    received_count = 0
    rejected_count = 0
    deduplication_store = (
        SqliteAlertDeduplicationStore(args.deduplication_db)
        if args.deduplication_db is not None
        else None
    )
    try:
        with AuthenticatedUdpAlertReceiver(
            bind_host=args.bind_host,
            port=args.port,
            hmac_key=key,
            receiver_id=args.receiver_id,
            expected_sender_id=args.expected_sender_id,
            receive_timeout_seconds=args.receive_timeout_seconds,
            maximum_clock_skew_seconds=args.maximum_clock_skew_seconds,
            deduplication_store=deduplication_store,
        ) as receiver:
            _emit(
                {
                    "event": "alert_udp_receiver_started",
                    "bind_host": args.bind_host,
                    "port": receiver.local_address[1],
                    "receiver_id": args.receiver_id,
                    "expected_sender_id": args.expected_sender_id,
                    "authenticated": True,
                    "persistent_deduplication": deduplication_store is not None,
                    "hardware_control_enabled": False,
                }
            )
            while received_count < args.max_messages:
                try:
                    received = receiver.receive()
                except AlertAuthenticationError as exc:
                    rejected_count += 1
                    _emit(
                        {
                            "event": "alert_udp_packet_rejected",
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "rejected_count": rejected_count,
                            "hardware_control_enabled": False,
                        },
                        stream=sys.stderr,
                    )
                    if rejected_count > args.max_rejected_packets:
                        raise RuntimeError(
                            "maximum rejected UDP alert packet count exceeded"
                        ) from exc
                    continue
                received_count += 1
                _emit(
                    {
                        "event": "authenticated_fire_alert_received",
                        "sender_id": received.sender_id,
                        "duplicate": received.duplicate,
                        "alert": received.document,
                        "hardware_control_enabled": False,
                    }
                )
    finally:
        if deduplication_store is not None:
            deduplication_store.close()
    _emit(
        {
            "event": "alert_udp_receiver_finished",
            "received_count": received_count,
            "rejected_count": rejected_count,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _alert_publisher_from_args(args: argparse.Namespace) -> AlertPublisher:
    if args.alert_udp_host is None:
        if args.alert_hmac_key_env is not None:
            raise ValueError("--alert-hmac-key-env requires --alert-udp-host")
        return JsonLineAlertPublisher(sys.stdout)
    transport = UdpAcknowledgedAlertTransport(
        host=args.alert_udp_host,
        port=args.alert_udp_port,
        hmac_key=_required_alert_hmac_key_from_env(args.alert_hmac_key_env),
        sender_id=args.alert_sender_id,
        receiver_id=args.alert_receiver_id,
        acknowledgement_timeout_seconds=args.alert_ack_timeout_seconds,
        maximum_clock_skew_seconds=args.alert_maximum_clock_skew_seconds,
    )
    return RetryingAcknowledgedAlertPublisher(
        transport,
        maximum_attempts=args.alert_delivery_attempts,
    )


def _run_live_camera(args: argparse.Namespace) -> int:
    config = MissionConfig.from_json(args.config)
    if args.safety_model_manifest is not None and args.safety_onnx_model is None:
        raise ValueError("--safety-model-manifest requires --safety-onnx-model")
    if args.simulate_payload_cycle and args.payload_inventory_report is not None:
        raise ValueError(
            "--simulate-payload-cycle cannot be combined with --payload-inventory-report"
        )
    if args.payload_inventory_report is None and (
        args.payload_inventory_hmac_key_env is not None or args.payload_inventory_key_id is not None
    ):
        raise ValueError("payload inventory authentication options require a report path")
    if args.simulate_payload_cycle and not config.deployment_capable:
        raise ValueError(
            "--simulate-payload-cycle requires a configuration with an installed payload"
        )
    if args.observe_pixhawk_lifecycle and not args.pixhawk_endpoint:
        raise ValueError("--observe-pixhawk-lifecycle requires --pixhawk-endpoint")
    if args.observe_pixhawk_lifecycle and args.task_area_mission_sequence is None:
        raise ValueError("--observe-pixhawk-lifecycle requires --task-area-mission-sequence")
    if args.operator_udp_port is None and (
        args.operator_hmac_key_env is not None or args.mavlink_signing_key_hex_env is not None
    ):
        raise ValueError("remote operator key options require --operator-udp-port")
    providers = tuple(args.provider)
    class_names = _parse_class_names(args.class_names)
    verified_fire_model = _verify_optional_model_manifest(
        manifest_path=args.model_manifest,
        model_path=args.onnx_model,
        class_names=class_names,
        output_coordinates=args.output_coordinates,
        require_production_approved=args.require_production_approved_models,
        expected_model_role="fire_candidate",
    )
    if verified_fire_model is not None and verified_fire_model.synthetic_hil_only:
        if not args.allow_synthetic_hil_model:
            raise ValueError(
                "synthetic HIL model requires the explicit --allow-synthetic-hil-model flag"
            )
    elif args.allow_synthetic_hil_model:
        raise ValueError(
            "--allow-synthetic-hil-model requires a manifest marked synthetic_hil_only"
        )
    detectors = [
        OnnxNx6Detector(
            OnnxNx6Config(
                model_path=args.onnx_model,
                class_names=class_names,
                input_width=args.input_width,
                input_height=args.input_height,
                confidence_threshold=args.confidence_threshold,
                providers=providers,
                trt_engine_cache_path=args.trt_engine_cache,
                output_coordinates=args.output_coordinates,
                model_version=(
                    verified_fire_model.model_version if verified_fire_model is not None else None
                ),
            )
        )
    ]
    verified_safety_model: VerifiedModelArtifact | None = None
    if args.safety_onnx_model is not None:
        safety_class_names = _parse_class_names(args.safety_class_names)
        verified_safety_model = _verify_optional_model_manifest(
            manifest_path=args.safety_model_manifest,
            model_path=args.safety_onnx_model,
            class_names=safety_class_names,
            output_coordinates=(args.safety_output_coordinates or args.output_coordinates),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="safety_object_evidence",
        )
        detectors.append(
            OnnxNx6Detector(
                OnnxNx6Config(
                    model_path=args.safety_onnx_model,
                    class_names=safety_class_names,
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=args.confidence_threshold,
                    providers=providers,
                    trt_engine_cache_path=args.trt_engine_cache,
                    output_coordinates=(args.safety_output_coordinates or args.output_coordinates),
                    model_version=(
                        verified_safety_model.model_version
                        if verified_safety_model is not None
                        else None
                    ),
                )
            )
        )
    detector = DetectorEnsemble(detectors)
    telemetry = (
        PixhawkReadOnlyTelemetryProvider(
            PixhawkReadOnlyConfig(endpoint=args.pixhawk_endpoint, baud=args.pixhawk_baud)
        )
        if args.pixhawk_endpoint
        else FailClosedTelemetryProvider()
    )
    audit_log = (
        AuditLog(
            stream_path=args.audit_out,
            max_in_memory_events=10_000,
            fsync_every_events=100,
            fsync_event_prefixes=(
                "alert.",
                "authorization.",
                "mission.transition",
                "operator.",
                "payload.",
            ),
            stream_append=True,
        )
        if args.audit_out is not None
        else None
    )
    if args.simulate_payload_cycle:
        payload_inventory_provider = ConfiguredSimulationPayloadInventoryProvider(config)
    elif args.payload_inventory_report is not None:
        payload_inventory_provider = FilePayloadInventoryProvider(
            args.payload_inventory_report,
            hmac_key=_hmac_key_from_env(args.payload_inventory_hmac_key_env),
            expected_key_id=args.payload_inventory_key_id,
        )
    else:
        payload_inventory_provider = FailClosedPayloadInventoryProvider()
    controller = MissionController(
        config,
        audit_log=audit_log,
        payload_inventory_provider=payload_inventory_provider,
    )
    alert_outbox = SqliteAlertOutbox(args.alert_outbox) if args.alert_outbox else None
    if alert_outbox is not None:
        alert_outbox.prune_delivered(keep_latest=10_000)
    prediction_writer = (
        JsonlPredictionWriter(args.prediction_log_out)
        if args.prediction_log_out is not None
        else None
    )
    alert_publisher = _alert_publisher_from_args(args)
    operator_bridge = None
    if args.operator_udp_port is not None:
        operator_geometry = VideoGeometry(
            args.operator_stream_id,
            args.operator_source_width,
            args.operator_source_height,
            args.operator_source_rotation,
        )
        operator_adapter = _operator_mavlink_adapter(
            geometry=operator_geometry,
            operator_hmac_key_env=args.operator_hmac_key_env,
            mavlink_signing_key_hex_env=args.mavlink_signing_key_hex_env,
            local_system_id=args.operator_local_system_id,
            local_component_id=args.operator_local_component_id,
            remote_system_id=args.operator_remote_system_id,
            remote_component_id=args.operator_remote_component_id,
        )
        operator_transport = UdpOperatorSelectionServer(
            bind_host=args.operator_udp_bind_host,
            port=args.operator_udp_port,
            mavlink=operator_adapter,
            guard=SelectionCommandGuard(operator_geometry),
        )
        operator_bridge = LiveOperatorBridge(
            operator_transport,
            OperatorTargetLock(
                operator_geometry,
                TargetLockConfig(
                    frozenset(config.target_classes),
                    acquisition_timeout_s=args.operator_acquisition_timeout_seconds,
                    lost_after_s=args.operator_lost_after_seconds,
                ),
            ),
        )
    runner = LiveMissionRunner(
        mission=controller,
        frame_source=OpenCVFrameSource(_capture_config_from_args(args)),
        detector=detector,
        telemetry_provider=telemetry,
        alert_publisher=alert_publisher,
        alert_outbox=alert_outbox,
        prediction_writer=prediction_writer,
        operator_bridge=operator_bridge,
        config=LiveRunConfig(
            operator_id=args.operator_id,
            max_frames=args.max_frames,
            display=not args.no_display,
            alert_banner_seconds=args.alert_banner_seconds,
            performance_window_frames=args.performance_window_frames,
            simulate_payload_cycle=args.simulate_payload_cycle,
            observe_pixhawk_lifecycle=args.observe_pixhawk_lifecycle,
            task_area_mission_sequence=args.task_area_mission_sequence,
            allowed_auto_modes=(
                tuple(args.allowed_auto_mode)
                if args.allowed_auto_mode
                else ("AUTO", "MISSION", "AUTO_MISSION")
            ),
        ),
    )
    _emit(
        {
            "event": "live_camera_started",
            "model_providers": [provider for item in detectors for provider in item.provider_names],
            "pixhawk_read_only": bool(args.pixhawk_endpoint),
            "mission_lifecycle": (
                "pixhawk_observed" if args.observe_pixhawk_lifecycle else "immediate_simulation"
            ),
            "physical_release_supported": False,
            "person_safety_model_coverage": detector.covers_labels(config.person_labels),
            "fire_model_manifest_validated": verified_fire_model is not None,
            "fire_model_role": (
                verified_fire_model.model_role if verified_fire_model is not None else "unverified"
            ),
            "fire_model_production_approved": (
                verified_fire_model.production_approved
                if verified_fire_model is not None
                else False
            ),
            "fire_model_synthetic_hil_only": (
                verified_fire_model.synthetic_hil_only if verified_fire_model is not None else False
            ),
            "safety_model_manifest_validated": verified_safety_model is not None,
            "safety_model_role": (
                verified_safety_model.model_role
                if verified_safety_model is not None
                else "unverified"
            ),
            "safety_model_production_approved": (
                verified_safety_model.production_approved
                if verified_safety_model is not None
                else False
            ),
            "alert_transport": (
                "authenticated_udp" if args.alert_udp_host is not None else "json_lines"
            ),
            "remote_operator_udp_enabled": operator_bridge is not None,
            "remote_operator_control_enabled": False,
        }
    )
    try:
        result = runner.run()
    finally:
        if alert_outbox is not None:
            alert_outbox.close()
        if audit_log is not None:
            audit_log.close()
        if prediction_writer is not None:
            prediction_writer.close()
    _emit(
        {
            "event": "live_camera_finished",
            "processed_frames": result.processed_frames,
            "phase": result.final_phase.value,
            "authorizations": result.authorization_count,
            "alerts_delivered": result.alert_delivery_count,
            "alert_delivery_failures": result.alert_delivery_failure_count,
            "average_fps": result.average_fps,
            "capture_latency_p50_ms": result.capture_latency_p50_ms,
            "capture_latency_p95_ms": result.capture_latency_p95_ms,
            "inference_latency_p50_ms": result.inference_latency_p50_ms,
            "inference_latency_p95_ms": result.inference_latency_p95_ms,
            "camera_reconnect_count": result.camera_reconnect_count,
            "alerts_retried": result.retried_alert_count,
            "simulated_payload_cycles": result.simulated_payload_cycle_count,
            "remote_selections": result.remote_selection_count,
            "remote_tracking_statuses": result.remote_tracking_status_count,
            "remote_transport_errors": result.remote_transport_error_count,
            "audit_written": args.audit_out is not None,
            "prediction_log_written": args.prediction_log_out is not None,
            "physical_release_supported": False,
        }
    )
    return 0


def _run_replay(
    *,
    config_path: Path,
    replay_path: Path,
    simulate_authorized_cycle: bool,
    operator_id: str,
    audit_out: Path | None,
) -> int:
    config = MissionConfig.from_json(config_path)
    if simulate_authorized_cycle and not config.deployment_capable:
        raise ValueError(
            "--simulate-authorized-cycle requires a configuration with an installed payload"
        )
    frames = load_jsonl_replay(replay_path)
    if not frames:
        raise ValueError("replay contains no frames")
    controller = MissionController(config)
    first_timestamp = frames[0].captured_at_s
    controller.launch(now_s=max(0.0, first_timestamp - 2.0))
    controller.arrive_task_area(now_s=max(0.0, first_timestamp - 1.0))
    completed_demo_cycle = False
    alert_count = 0

    _emit(
        {
            "event": "replay_started",
            "mission_id": config.mission_id,
            "frame_count": len(frames),
            "simulation_only": True,
            "hardware_interfaces_present": False,
        }
    )
    for frame in frames:
        outcome = controller.process_observation(frame, now_s=frame.captured_at_s)
        _emit(
            {
                "event": "frame_evaluated",
                "frame_id": frame.frame_id,
                "phase": outcome.phase.value,
                "track_count": len(outcome.tracks),
                "decisions": [
                    {
                        "target_id": decision.target_id,
                        "allowed": decision.allowed,
                        "priority_score": decision.priority_score,
                        "denial_reasons": decision.denial_reasons,
                    }
                    for decision in outcome.decisions
                ],
                "alerts": [
                    {
                        "alert_id": alert.alert_id,
                        "target_id": alert.target_id,
                        "target_revision": alert.target_revision,
                        "frame_id": alert.frame_id,
                        "label": alert.label,
                        "confidence": alert.confidence,
                        "bbox": alert.bbox.rounded(),
                        "observed_at_s": alert.observed_at_s,
                        "aircraft_position": {
                            "latitude_deg": (
                                alert.aircraft_latitude_deg
                                if math.isfinite(alert.aircraft_latitude_deg)
                                else None
                            ),
                            "longitude_deg": (
                                alert.aircraft_longitude_deg
                                if math.isfinite(alert.aircraft_longitude_deg)
                                else None
                            ),
                            "altitude_agl_m": (
                                alert.aircraft_altitude_agl_m
                                if math.isfinite(alert.aircraft_altitude_agl_m)
                                else None
                            ),
                        },
                    }
                    for alert in outcome.alerts
                ],
                "simulation_only": True,
            }
        )
        alert_count += len(outcome.alerts)
        for alert in outcome.alerts:
            _emit(
                {
                    "event": "fire_alert_confirmed",
                    "alert_id": alert.alert_id,
                    "mission_id": alert.mission_id,
                    "frame_id": alert.frame_id,
                    "target_id": alert.target_id,
                    "target_revision": alert.target_revision,
                    "label": alert.label,
                    "confidence": alert.confidence,
                    "bbox": alert.bbox.rounded(),
                    "observed_at_s": alert.observed_at_s,
                    "delivery": "local_console_only",
                    "simulation_only": True,
                }
            )
        challenge = outcome.challenge
        if challenge is None:
            continue
        _emit(
            {
                "event": "authorization_required",
                "challenge_id": challenge.challenge_id,
                "target_id": challenge.target_id,
                "target_revision": challenge.target_revision,
                "payload_slot_id": challenge.payload_slot_id,
                "scene_digest": challenge.scene_digest,
                "ruleset_version": challenge.ruleset_version,
                "expires_at_s": challenge.expires_at_s,
                "nonce_redacted": True,
                "simulation_only": True,
            }
        )
        if not simulate_authorized_cycle:
            break

        approved_at = frame.captured_at_s + 0.1
        controller.approve_authorization(
            challenge_id=challenge.challenge_id,
            nonce=challenge.nonce,
            operator_id=operator_id,
            now_s=approved_at,
        )
        _emit(
            {
                "event": "demo_operator_approved",
                "challenge_id": challenge.challenge_id,
                "operator_id": operator_id,
                "simulation_only": True,
            }
        )
        release_id = controller.request_simulated_deployment(now_s=approved_at + 0.1)
        controller.report_simulated_execution(release_id=release_id, now_s=approved_at + 0.2)
        controller.report_independent_confirmation(
            release_id=release_id,
            source_id="demo-independent-bay-sensor",
            now_s=approved_at + 0.3,
        )
        _emit(
            {
                "event": "simulated_release_confirmed",
                "release_id": release_id,
                "payload_slot_id": challenge.payload_slot_id,
                "remaining_payload_count": controller.payload.remaining_payload_count,
                "simulation_only": True,
            }
        )
        completed_demo_cycle = True
        break

    if audit_out is not None:
        audit_out.parent.mkdir(parents=True, exist_ok=True)
        controller.write_audit_jsonl(audit_out)
        _emit(
            {
                "event": "audit_written",
                "path": str(audit_out.resolve()),
                "event_count": len(controller.audit),
                "simulation_only": True,
            }
        )
    status = controller.status()
    _emit(
        {
            "event": "replay_finished",
            "phase": status.phase.value,
            "remaining_payload_count": status.remaining_payload_count,
            "payload_installed": config.payload_installed,
            "mission_capability": (
                "deployment_capable" if config.deployment_capable else "patrol_only"
            ),
            "alert_count": alert_count,
            "pending_authorization": status.pending_challenge_id is not None,
            "simulated_cycle_completed": completed_demo_cycle,
            "fake_release_request_count": controller.fake_payload_port.request_count,
            "simulation_only": True,
        }
    )
    return 0


def _emit(document: dict[str, Any], *, stream: Any = None) -> None:
    destination = sys.stdout if stream is None else stream
    print(
        json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        file=destination,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sequence_percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

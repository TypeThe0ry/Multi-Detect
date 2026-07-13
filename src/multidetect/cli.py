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
from .camera_bench import CameraBenchConfig, run_camera_bench
from .config import MissionConfig
from .deployment_planner import FixedWingReleaseWindowPlanner
from .domain import (
    BoundingBox,
    DeploymentWindowStatus,
    FrameObservation,
    MissionPhase,
    RuleCheck,
    TrackSnapshot,
    VehicleTelemetry,
    Verdict,
)
from .evaluation import (
    JsonlPredictionWriter,
    evaluate_detections,
    evaluation_document,
    load_ground_truth_jsonl,
    load_prediction_jsonl,
)
from .gr01_bench import Gr01BenchConfig, run_gr01_link_bench
from .integration_evidence import INTEGRATION_PROFILES, check_integration_evidence_bundle
from .jetson_bench import JetsonVisionBenchConfig, run_jetson_vision_bench
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
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    SafetyStatusMessage,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
    operator_identifier_token,
)
from .operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
from .operator_protocol import (
    MAX_TUNNEL_PAYLOAD_BYTES,
    OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
    OperatorTunnelCodec,
)
from .operator_tracking import (
    FIRE_CANDIDATE_TRACK_LABELS,
    OperatorTargetLock,
    TargetLockConfig,
)
from .operator_transport import (
    SelectionCommandServer,
    SelectionRetryClient,
    ServerAuthorizationDecisionResult,
    ServerSelectionResult,
)
from .operator_udp import (
    UdpOperatorSelectionClient,
    UdpOperatorSelectionServer,
    UdpOperatorSessionClient,
)
from .payload_bench_evidence import check_inert_payload_hardware_bench
from .payload_confirmation_hil import PayloadConfirmationHilCodec
from .payload_confirmation_udp import UdpPayloadConfirmationHilReceiver
from .payload_hil_cycle import InertPayloadHilCycleCoordinator
from .payload_hil_mission import MissionPayloadHilAdapter
from .payload_hil_protocol import PayloadHilCodec
from .payload_hil_udp import UdpPayloadHilClient
from .payload_inventory import (
    ConfiguredSimulationPayloadInventoryProvider,
    FailClosedPayloadInventoryProvider,
    FilePayloadInventoryProvider,
    load_payload_inventory_snapshot,
    verify_payload_inventory,
)
from .pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from .pixhawk_bench import (
    PixhawkBenchConfig,
    load_qgc_telemetry_snapshot,
    run_pixhawk_v6x_bench,
)
from .replay import load_jsonl_replay
from .synthetic_model import create_synthetic_hil_model_bundle
from .telemetry import AuthenticatedZoneTelemetryProvider, FailClosedTelemetryProvider
from .vision import (
    BrightNeutralLightVetoFilter,
    CaptureConfig,
    ClassConfidenceFilter,
    DetectorEnsemble,
    OnnxNx6Config,
    OnnxNx6Detector,
    OpenCVFrameSource,
    PersonOverlapVetoFilter,
    TemporalDetectionFilter,
)
from .zone_evidence import FileZoneEvidenceProvider

COCO80_CLASS_NAMES = tuple(
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,"
    "stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,"
    "backpack,umbrella,handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,"
    "baseball bat,baseball glove,skateboard,surfboard,tennis racket,bottle,wine glass,cup,"
    "fork,knife,spoon,bowl,banana,apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,"
    "cake,chair,couch,potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,"
    "cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,scissors,teddy bear,"
    "hair drier,toothbrush".split(",")
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-detect",
        description="Safety-first non-hazardous mission orchestration and live perception.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate a mission JSON file")
    validate.add_argument("config", type=Path)

    release_window = subparsers.add_parser(
        "release-window-check",
        help="compute an advisory fixed-wing HIL release window without control output",
    )
    release_window.add_argument("config", type=Path)
    release_window.add_argument("--x1", type=float, required=True)
    release_window.add_argument("--y1", type=float, required=True)
    release_window.add_argument("--x2", type=float, required=True)
    release_window.add_argument("--y2", type=float, required=True)
    release_window.add_argument("--altitude-agl-m", type=float, required=True)
    release_window.add_argument("--ground-speed-mps", type=float, required=True)
    release_window.add_argument("--pitch-deg", type=float, required=True)
    release_window.add_argument("--label", default="flame")
    release_window.add_argument("--target-id", default="hil-target")
    release_window.add_argument("--target-revision", type=int, default=1)
    release_window.add_argument("--now-s", type=float, default=0.0)

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

    payload_bench = subparsers.add_parser(
        "inert-payload-bench-check",
        help="verify separately signed controller/sensor logs from an inert hardware bench",
    )
    payload_bench.add_argument("--controller-log", type=Path, required=True)
    payload_bench.add_argument("--sensor-log", type=Path, required=True)
    payload_bench.add_argument("--controller-hmac-key-env", required=True)
    payload_bench.add_argument("--sensor-hmac-key-env", required=True)
    payload_bench.add_argument("--bench-id", required=True)
    payload_bench.add_argument("--controller-id", required=True)
    payload_bench.add_argument("--sensor-id", required=True)
    payload_bench.add_argument("--controller-key-id", required=True)
    payload_bench.add_argument("--sensor-key-id", required=True)
    payload_bench.add_argument("--minimum-confirmed-cycles", type=int, default=20)
    payload_bench.add_argument("--maximum-age-hours", type=float, default=168.0)
    payload_bench.add_argument("--inert-load-only", action="store_true")
    payload_bench.add_argument("--people-excluded-from-test-area", action="store_true")
    payload_bench.add_argument("--out", type=Path, required=True)

    camera_check = subparsers.add_parser(
        "camera-check", help="open a local/RTSP source, read one frame, and discard it"
    )
    _add_capture_arguments(camera_check)
    camera_check.add_argument("--frames", type=int, default=1)

    camera_bench = subparsers.add_parser(
        "camera-bench",
        help="soak a local/RTSP camera and write redacted staged hardware evidence",
    )
    _add_capture_arguments(camera_bench)
    camera_bench.add_argument("--minimum-frames", type=int, default=300)
    camera_bench.add_argument("--minimum-duration-seconds", type=float, default=60.0)
    camera_bench.add_argument("--maximum-duration-seconds", type=float, default=120.0)
    camera_bench.add_argument("--out", type=Path, required=True)

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

    jetson_bench = subparsers.add_parser(
        "jetson-vision-bench",
        help="soak RTSP capture plus ONNX inference on Jetson and write hardware evidence",
    )
    _add_capture_arguments(jetson_bench)
    jetson_bench.add_argument("--onnx-model", type=Path, required=True)
    jetson_bench.add_argument("--model-manifest", type=Path, required=True)
    jetson_bench.add_argument("--class-names", default="fire,smoke")
    jetson_bench.add_argument("--input-width", type=int, default=640)
    jetson_bench.add_argument("--input-height", type=int, default=640)
    jetson_bench.add_argument("--confidence-threshold", type=float, default=0.10)
    jetson_bench.add_argument(
        "--output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
        default="normalized_xyxy",
    )
    jetson_bench.add_argument("--provider", action="append", default=[])
    jetson_bench.add_argument("--trt-engine-cache", type=Path)
    jetson_bench.add_argument("--minimum-frames", type=int, default=1000)
    jetson_bench.add_argument("--minimum-duration-seconds", type=float, default=1800.0)
    jetson_bench.add_argument("--maximum-duration-seconds", type=float, default=2100.0)
    jetson_bench.add_argument("--maximum-temperature-c", type=float, default=95.0)
    jetson_bench.add_argument("--out", type=Path, required=True)

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

    pixhawk_bench = subparsers.add_parser(
        "pixhawk-v6x-bench",
        help="compare read-only Pixhawk telemetry with a stationary QGC snapshot",
    )
    pixhawk_bench.add_argument("--endpoint", required=True)
    pixhawk_bench.add_argument("--baud", type=int, default=57_600)
    pixhawk_bench.add_argument("--qgc-snapshot", type=Path, required=True)
    pixhawk_bench.add_argument("--minimum-samples", type=int, default=100)
    pixhawk_bench.add_argument("--sample-interval-seconds", type=float, default=0.2)
    pixhawk_bench.add_argument("--stale-after-seconds", type=float, default=1.0)
    pixhawk_bench.add_argument("--maximum-qgc-age-seconds", type=float, default=120.0)
    pixhawk_bench.add_argument("--out", type=Path, required=True)

    evidence_check = subparsers.add_parser(
        "integration-evidence-check",
        help="verify hashed staged HIL/hardware evidence without granting production approval",
    )
    evidence_check.add_argument("bundle", type=Path)
    evidence_check.add_argument("--profile", choices=tuple(INTEGRATION_PROFILES), required=True)
    evidence_check.add_argument("--maximum-hardware-age-hours", type=float, default=168.0)
    evidence_check.add_argument("--out", type=Path)

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
    operator_server.add_argument("--bind-host", default="127.0.0.1")
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
    operator_server.add_argument("--exit-after-accepted-selections", type=int)
    operator_server.add_argument(
        "--authorization-hil",
        action="store_true",
        help=(
            "after an accepted selection, publish one synthetic authorization challenge and "
            "accept a signed approve/deny decision; protocol HIL only"
        ),
    )
    operator_server.add_argument("--authorization-window-seconds", type=float, default=5.0)

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
    operator_client.add_argument("--wait-track-status-seconds", type=float, default=0.0)
    operator_client.add_argument("--local-system-id", type=int, default=255)
    operator_client.add_argument("--local-component-id", type=int, default=190)
    operator_client.add_argument("--remote-system-id", type=int, default=1)
    operator_client.add_argument("--remote-component-id", type=int, default=191)

    operator_authorize = subparsers.add_parser(
        "operator-udp-authorize",
        help=(
            "select a diagnostic target, receive a signed synthetic challenge, and return one "
            "bound approve/deny decision; never sends a payload or flight command"
        ),
    )
    operator_authorize.add_argument("--host", required=True)
    operator_authorize.add_argument("--port", type=int, default=14_580)
    operator_authorize.add_argument("--operator-hmac-key-env", required=True)
    operator_authorize.add_argument("--mavlink-signing-key-hex-env", required=True)
    operator_authorize.add_argument("--stream-id", default="camera-main")
    operator_authorize.add_argument("--width", type=int, default=1280)
    operator_authorize.add_argument("--height", type=int, default=720)
    operator_authorize.add_argument("--rotation", type=int, default=0)
    operator_authorize.add_argument("--x1", type=float, default=0.32)
    operator_authorize.add_argument("--y1", type=float, default=0.21)
    operator_authorize.add_argument("--x2", type=float, default=0.61)
    operator_authorize.add_argument("--y2", type=float, default=0.72)
    operator_authorize.add_argument("--selection-ttl-seconds", type=float, default=3.0)
    operator_authorize.add_argument("--authorization-ttl-seconds", type=float, default=2.0)
    operator_authorize.add_argument("--authorization-timeout-seconds", type=float, default=5.0)
    operator_authorize.add_argument("--retry-interval-seconds", type=float, default=0.25)
    operator_authorize.add_argument("--maximum-attempts", type=int, default=3)
    operator_authorize.add_argument(
        "--decision",
        choices=("approve", "deny"),
        default="deny",
        help="defaults to deny so approval must always be explicit",
    )
    operator_authorize.add_argument("--operator-id", required=True)
    operator_authorize.add_argument("--local-system-id", type=int, default=255)
    operator_authorize.add_argument("--local-component-id", type=int, default=190)
    operator_authorize.add_argument("--remote-system-id", type=int, default=1)
    operator_authorize.add_argument("--remote-component-id", type=int, default=191)
    alert_receiver.add_argument("--deduplication-db", type=Path)

    gr01_bench = subparsers.add_parser(
        "gr01-link-bench",
        help="measure signed G20/Jetson UDP round trips through a GR01 or software baseline",
    )
    gr01_bench.add_argument("--host", required=True)
    gr01_bench.add_argument("--port", type=int, default=14_580)
    gr01_bench.add_argument("--operator-hmac-key-env", required=True)
    gr01_bench.add_argument("--mavlink-signing-key-hex-env", required=True)
    gr01_bench.add_argument("--stream-id", default="camera-main")
    gr01_bench.add_argument("--width", type=int, default=1280)
    gr01_bench.add_argument("--height", type=int, default=720)
    gr01_bench.add_argument("--rotation", type=int, default=0)
    gr01_bench.add_argument("--minimum-round-trips", type=int, default=100)
    gr01_bench.add_argument("--command-ttl-seconds", type=float, default=3.0)
    gr01_bench.add_argument("--retry-interval-seconds", type=float, default=0.5)
    gr01_bench.add_argument("--maximum-attempts", type=int, default=3)
    gr01_bench.add_argument("--maximum-packet-loss-rate", type=float, default=0.01)
    gr01_bench.add_argument("--maximum-ack-latency-p95-ms", type=float, default=500.0)
    gr01_bench.add_argument("--hardware-mode", action="store_true")
    gr01_bench.add_argument("--hardware-id")
    gr01_bench.add_argument("--local-system-id", type=int, default=255)
    gr01_bench.add_argument("--local-component-id", type=int, default=190)
    gr01_bench.add_argument("--remote-system-id", type=int, default=1)
    gr01_bench.add_argument("--remote-component-id", type=int, default=191)
    gr01_bench.add_argument("--out", type=Path, required=True)

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
    live.add_argument("--safety-model-coco80", action="store_true")
    live.add_argument("--safety-confidence-threshold", type=float, default=0.30)
    live.add_argument("--input-width", type=int, default=640)
    live.add_argument("--input-height", type=int, default=640)
    live.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.10,
        help=(
            "candidate display/tracking threshold; mission confirmation still uses the higher "
            "minimum_confidence from the mission config"
        ),
    )
    live.add_argument("--flame-confidence-threshold", type=float, default=0.72)
    live.add_argument("--smoke-confidence-threshold", type=float, default=0.60)
    live.add_argument("--candidate-stability-frames", type=int, default=6)
    live.add_argument("--person-veto-fire-coverage", type=float, default=0.40)
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
    live.add_argument("--zone-evidence-report", type=Path)
    live.add_argument("--zone-evidence-hmac-key-env")
    live.add_argument("--zone-evidence-key-id")
    live.add_argument("--zone-evidence-max-position-delta-m", type=float, default=25.0)
    live.add_argument(
        "--operator-udp-port",
        type=int,
        help=(
            "enable signed remote target selection/status/authorization metadata; never enables "
            "flight commands or direct payload control"
        ),
    )
    live.add_argument("--operator-udp-bind-host", default="127.0.0.1")
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
    live.add_argument(
        "--auto-simulate-payload-cycle",
        action="store_true",
        help=(
            "after a valid authorization reaches DEPLOYMENT_READY, automatically execute exactly "
            "one simulated/inert HIL cycle; requires --simulate-payload-cycle"
        ),
    )
    live.add_argument(
        "--inert-payload-hil",
        action="store_true",
        help="route the explicit simulated cycle through two authenticated inert HIL channels",
    )
    live.add_argument("--payload-hil-controller-host", default="127.0.0.1")
    live.add_argument("--payload-hil-controller-port", type=int)
    live.add_argument("--payload-hil-controller-module-id")
    live.add_argument("--payload-hil-request-key-env")
    live.add_argument("--payload-hil-request-key-id")
    live.add_argument("--payload-hil-result-key-env")
    live.add_argument("--payload-hil-result-key-id")
    live.add_argument("--payload-hil-response-timeout-seconds", type=float, default=0.5)
    live.add_argument("--payload-hil-maximum-attempts", type=int, default=3)
    live.add_argument("--payload-hil-request-ttl-seconds", type=float, default=1.0)
    live.add_argument("--payload-hil-result-max-age-seconds", type=float, default=1.0)
    live.add_argument("--payload-confirmation-bind-host", default="127.0.0.1")
    live.add_argument("--payload-confirmation-port", type=int)
    live.add_argument("--payload-confirmation-key-env")
    live.add_argument("--payload-confirmation-key-id")
    live.add_argument("--payload-confirmation-sensor-id", action="append", default=[])
    live.add_argument("--payload-confirmation-timeout-seconds", type=float, default=2.0)
    live.add_argument("--payload-confirmation-max-age-seconds", type=float, default=1.0)
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
        if args.command == "release-window-check":
            return _release_window_check(args)
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
        if args.command == "camera-bench":
            return _run_camera_bench(args)
        if args.command == "payload-inventory-check":
            return _payload_inventory_check(args)
        if args.command == "inert-payload-bench-check":
            return _run_inert_payload_bench_check(args)
        if args.command == "evaluate-detections":
            return _evaluate_detection_logs(args)
        if args.command == "model-check":
            return _run_model_check(args)
        if args.command == "jetson-vision-bench":
            return _run_jetson_vision_bench(args)
        if args.command == "model-manifest-init":
            return _run_model_manifest_init(args)
        if args.command == "synthetic-model-init":
            return _run_synthetic_model_init(args)
        if args.command == "legacy-checkpoint-verify":
            return _run_legacy_checkpoint_verify(args)
        if args.command == "pixhawk-check":
            return _run_pixhawk_check(args)
        if args.command == "pixhawk-v6x-bench":
            return _run_pixhawk_v6x_bench(args)
        if args.command == "integration-evidence-check":
            return _run_integration_evidence_check(args)
        if args.command == "alert-udp-receiver":
            return _run_alert_udp_receiver(args)
        if args.command == "operator-link-demo":
            return _run_operator_link_demo()
        if args.command == "operator-udp-server":
            return _run_operator_udp_server(args)
        if args.command == "operator-udp-select":
            return _run_operator_udp_select(args)
        if args.command == "operator-udp-authorize":
            return _run_operator_udp_authorize(args)
        if args.command == "gr01-link-bench":
            return _run_gr01_link_bench(args)
        if args.command == "live-camera":
            return _run_live_camera(args)
    except (OSError, ValueError, RuntimeError) as exc:
        _emit(
            {
                "event": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "simulation_only": args.command
                not in {
                    "live-camera",
                    "camera-check",
                    "camera-bench",
                    "jetson-vision-bench",
                    "pixhawk-check",
                    "pixhawk-v6x-bench",
                    "gr01-link-bench",
                    "inert-payload-bench-check",
                },
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


def _run_integration_evidence_check(args: argparse.Namespace) -> int:
    document = check_integration_evidence_bundle(
        args.bundle,
        profile=args.profile,
        maximum_hardware_age_hours=args.maximum_hardware_age_hours,
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit(document)
    return 0 if document["passed"] else 1


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
    mission_status = MissionStatusMessage(
        status_id=str(UUID("44444444-4444-4444-8444-444444444444")),
        sequence=2,
        mission_id="fire-fixed-wing-demo",
        phase=MissionPhase.AWAITING_AUTHORIZATION,
        authorization_state=AuthorizationDisplayState.PENDING,
        release_window=DeploymentWindowStatus.WAIT,
        safety_allowed=False,
        remaining_payload_count=4,
        total_payload_count=4,
        target_id="track-42",
        active_payload_slot_id="payload-1",
        target_confidence=0.91,
        relative_bearing_deg=-4.2,
        estimated_range_m=82.0,
        cross_track_error_m=1.4,
        along_track_error_m=19.3,
        release_lead_distance_m=62.7,
        produced_at_s=1_000.56,
    )
    encoded_mission_status = codec.encode_mission_status(mission_status)
    mission_status_frame = jetson_mavlink.wrap_authenticated_operator_payload(
        encoded_mission_status
    )
    received_mission_status = codec.decode(
        g20_mavlink.extract_authenticated_operator_payload(mission_status_frame)
    ).message
    if not isinstance(received_mission_status, MissionStatusMessage):  # pragma: no cover
        raise RuntimeError("operator-link demo returned the wrong mission-status type")
    _emit(
        {
            "event": "g20_mission_status_received",
            "phase": received_mission_status.phase.value,
            "authorization_state": received_mission_status.authorization_state.value,
            "release_window": (
                received_mission_status.release_window.value
                if received_mission_status.release_window is not None
                else None
            ),
            "safety_allowed": received_mission_status.safety_allowed,
            "payload_bytes": len(encoded_mission_status),
            "mavlink_frame_bytes": len(mission_status_frame),
            "advisory_only": received_mission_status.advisory_only,
            "flight_control_enabled": received_mission_status.flight_control_enabled,
            "physical_release_enabled": received_mission_status.physical_release_enabled,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )
    safety_status = SafetyStatusMessage(
        status_id=str(UUID("55555555-5555-4555-8555-555555555555")),
        sequence=3,
        mission_id="fire-fixed-wing-demo",
        target_id="track-42",
        ruleset_version="safety-rules-fixed-wing-hil-v1",
        checks=(
            RuleCheck("target.confirmed_track", Verdict.PASS, "confirmed"),
            RuleCheck("navigation.allowed_zone", Verdict.UNKNOWN, "unknown"),
            RuleCheck("deployment.person_exclusion", Verdict.DENY, "person nearby"),
        ),
        produced_at_s=1_000.57,
    )
    encoded_safety_status = codec.encode_safety_status(safety_status)
    safety_status_frame = jetson_mavlink.wrap_authenticated_operator_payload(encoded_safety_status)
    received_safety_status = codec.decode(
        g20_mavlink.extract_authenticated_operator_payload(safety_status_frame)
    ).message
    if not isinstance(received_safety_status, SafetyStatusMessage):  # pragma: no cover
        raise RuntimeError("operator-link demo returned the wrong safety-status type")
    _emit(
        {
            "event": "g20_safety_status_received",
            "target_id": received_safety_status.target_id,
            "ruleset_version": received_safety_status.ruleset_version,
            "pass_count": received_safety_status.pass_count,
            "deny_count": received_safety_status.deny_count,
            "unknown_count": received_safety_status.unknown_count,
            "allowed": received_safety_status.allowed,
            "checks": [
                {"rule_id": check.rule_id, "verdict": check.verdict.value}
                for check in received_safety_status.checks
            ],
            "payload_bytes": len(encoded_safety_status),
            "mavlink_frame_bytes": len(safety_status_frame),
            "advisory_only": received_safety_status.advisory_only,
            "flight_control_enabled": received_safety_status.flight_control_enabled,
            "physical_release_enabled": received_safety_status.physical_release_enabled,
            "simulation_only": True,
            "hardware_control_enabled": False,
        }
    )
    _emit(
        {
            "event": "operator_link_demo_finished",
            "selection_delivered": acknowledgement.accepted,
            "tracking_status_received": True,
            "mission_status_received": True,
            "safety_status_received": True,
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
    if args.exit_after_accepted_selections is not None and (
        args.exit_after_accepted_selections <= 0
        or args.exit_after_accepted_selections > args.max_datagrams
    ):
        raise ValueError(
            "--exit-after-accepted-selections must be positive and no greater than max datagrams"
        )
    if args.authorization_hil and args.max_datagrams < 2:
        raise ValueError("--authorization-hil requires --max-datagrams of at least 2")
    if (
        not math.isfinite(args.authorization_window_seconds)
        or args.authorization_window_seconds <= 0.0
    ):
        raise ValueError("--authorization-window-seconds must be finite and positive")
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
        accepted_selection_count = 0
        unique_accepted_selection_count = 0
        accepted_authorization_count = 0
        challenge_published = False
        datagram_count = 0
        for _ in range(args.max_datagrams):
            result, peer = server.serve_once()
            datagram_count += 1
            accepted_count += int(result.acceptance.allowed)
            if isinstance(result, ServerSelectionResult):
                accepted_selection_count += int(result.acceptance.allowed)
                unique_accepted_selection_count += int(
                    result.acceptance.allowed and not result.duplicate
                )
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
                if (
                    args.authorization_hil
                    and result.acceptance.allowed
                    and not result.duplicate
                    and not challenge_published
                ):
                    now_s = time.time()
                    challenge = AuthorizationChallengeStatusMessage(
                        challenge_token=operator_identifier_token(str(uuid4())),
                        mission_token=operator_identifier_token("diagnostic-fire-mission"),
                        target_token=operator_identifier_token(result.command.command_id),
                        scene_token=operator_identifier_token("diagnostic-scene-snapshot"),
                        ruleset_token=operator_identifier_token("diagnostic-rules-v1"),
                        payload_slot_token=operator_identifier_token("inert-hil-slot-1"),
                        target_revision=1,
                        created_at_s=now_s,
                        expires_at_s=now_s + args.authorization_window_seconds,
                        sequence=1,
                        produced_at_s=now_s,
                    )
                    server.publish_authorization_challenge(challenge, peer=peer)
                    challenge_published = True
                    _emit(
                        {
                            "event": "operator_udp_authorization_challenge_published",
                            "challenge_token": f"{challenge.challenge_token:016x}",
                            "target_revision": challenge.target_revision,
                            "expires_at_s": challenge.expires_at_s,
                            "nonce_transmitted": False,
                            "synthetic_protocol_hil": True,
                            "payload_release_enabled": False,
                            "hardware_control_enabled": False,
                        }
                    )
            elif isinstance(result, ServerAuthorizationDecisionResult):
                accepted_authorization_count += int(result.acceptance.allowed)
                _emit(
                    {
                        "event": "operator_udp_authorization_decision_processed",
                        "command_token": f"{result.command.command_token:016x}",
                        "decision": result.command.decision.value,
                        "operator_token": f"{result.command.operator_token:016x}",
                        "accepted": result.acceptance.allowed,
                        "reasons": list(result.acceptance.reasons),
                        "duplicate": result.duplicate,
                        "peer_host": peer[0],
                        "peer_port": peer[1],
                        "mission_state_changed": False,
                        "payload_release_requested": False,
                        "hardware_control_enabled": False,
                    }
                )
            else:  # pragma: no cover - closed result union
                raise RuntimeError("operator UDP server returned an unsupported result type")
            if (
                args.exit_after_accepted_selections is not None
                and unique_accepted_selection_count >= args.exit_after_accepted_selections
            ):
                break
    _emit(
        {
            "event": "operator_udp_server_finished",
            "datagram_count": datagram_count,
            "accepted_count": accepted_count,
            "accepted_selection_count": accepted_selection_count,
            "unique_accepted_selection_count": unique_accepted_selection_count,
            "accepted_authorization_count": accepted_authorization_count,
            "authorization_hil": args.authorization_hil,
            "payload_release_requested": False,
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
    if args.wait_track_status_seconds < 0.0:
        raise ValueError("--wait-track-status-seconds cannot be negative")
    remote_status = None
    if args.wait_track_status_seconds > 0.0:
        with UdpOperatorSessionClient(
            host=args.host,
            port=args.port,
            mavlink=adapter,
            retry_interval_s=args.retry_interval_seconds,
            maximum_attempts=args.maximum_attempts,
        ) as session:
            receipt = session.deliver(command)
            if receipt.acknowledgement.accepted:
                remote_status = session.receive_track_status(
                    timeout_s=args.wait_track_status_seconds
                )
    else:
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
    if remote_status is not None:
        _emit(
            {
                "event": "operator_udp_track_status_received",
                "command_id": remote_status.selection_command_id,
                "state": remote_status.state.value,
                "target_id": remote_status.target_id,
                "label": remote_status.label,
                "confidence": remote_status.confidence,
                "tracking_quality": remote_status.tracking_quality,
                "bbox": remote_status.bbox.rounded() if remote_status.bbox else None,
                "hardware_control_enabled": False,
            }
        )
    return 0 if receipt.acknowledgement.accepted else 1


def _run_gr01_link_bench(args: argparse.Namespace) -> int:
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
    config = Gr01BenchConfig(
        minimum_round_trips=args.minimum_round_trips,
        command_ttl_seconds=args.command_ttl_seconds,
        maximum_packet_loss_rate=args.maximum_packet_loss_rate,
        maximum_ack_latency_p95_ms=args.maximum_ack_latency_p95_ms,
        hardware_mode=args.hardware_mode,
        hardware_id=args.hardware_id,
    )
    with UdpOperatorSessionClient(
        host=args.host,
        port=args.port,
        mavlink=adapter,
        retry_interval_s=args.retry_interval_seconds,
        maximum_attempts=args.maximum_attempts,
    ) as session:
        document = run_gr01_link_bench(session, geometry, config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit(document)
    return 0 if document["passed"] else 1


def _run_operator_udp_authorize(args: argparse.Namespace) -> int:
    if args.authorization_timeout_seconds <= 0.0:
        raise ValueError("--authorization-timeout-seconds must be positive")
    if args.selection_ttl_seconds <= 0.0:
        raise ValueError("--selection-ttl-seconds must be positive")
    if args.authorization_ttl_seconds <= 0.0:
        raise ValueError("--authorization-ttl-seconds must be positive")
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
    session_token_text = str(uuid4())
    selection = TargetSelectionCommand(
        command_id=str(uuid4()),
        session_id=session_token_text,
        sequence=1,
        action=SelectionAction.SELECT,
        geometry=geometry,
        issued_at_s=issued_at_s,
        expires_at_s=issued_at_s + args.selection_ttl_seconds,
        bbox=BoundingBox(args.x1, args.y1, args.x2, args.y2),
        displayed_frame_id="udp-authorization-diagnostic",
    )
    with UdpOperatorSessionClient(
        host=args.host,
        port=args.port,
        mavlink=adapter,
        retry_interval_s=args.retry_interval_seconds,
        maximum_attempts=args.maximum_attempts,
    ) as session:
        selection_receipt = session.deliver(selection)
        _emit(
            {
                "event": "operator_udp_authorization_selection_acknowledged",
                "command_id": selection.command_id,
                "accepted": selection_receipt.acknowledgement.accepted,
                "reason": selection_receipt.acknowledgement.reason.name.lower(),
                "attempts": selection_receipt.attempts,
                "elapsed_ms": selection_receipt.elapsed_s * 1000.0,
                "hardware_control_enabled": False,
            }
        )
        if not selection_receipt.acknowledgement.accepted:
            return 1
        challenge = session.receive_authorization_challenge(
            timeout_s=args.authorization_timeout_seconds
        )
        _emit(
            {
                "event": "operator_udp_authorization_challenge_received",
                "challenge_token": f"{challenge.challenge_token:016x}",
                "target_revision": challenge.target_revision,
                "expires_at_s": challenge.expires_at_s,
                "nonce_received": False,
                "hardware_control_enabled": False,
            }
        )
        decision_issued_at_s = time.time()
        decision_expires_at_s = min(
            decision_issued_at_s + args.authorization_ttl_seconds,
            challenge.expires_at_s,
        )
        if decision_expires_at_s <= decision_issued_at_s:
            raise RuntimeError("authorization challenge expired before the decision was created")
        decision = AuthorizationDecisionCommand(
            command_token=operator_identifier_token(str(uuid4())),
            session_token=operator_identifier_token(session_token_text),
            challenge_token=challenge.challenge_token,
            mission_token=challenge.mission_token,
            target_token=challenge.target_token,
            scene_token=challenge.scene_token,
            ruleset_token=challenge.ruleset_token,
            payload_slot_token=challenge.payload_slot_token,
            target_revision=challenge.target_revision,
            decision=(
                AuthorizationDecision.APPROVE
                if args.decision == "approve"
                else AuthorizationDecision.DENY
            ),
            operator_token=operator_identifier_token(args.operator_id),
            sequence=2,
            issued_at_s=decision_issued_at_s,
            expires_at_s=decision_expires_at_s,
        )
        decision_receipt = session.deliver_authorization_decision(decision)
    _emit(
        {
            "event": "operator_udp_authorization_decision_acknowledged",
            "command_token": f"{decision.command_token:016x}",
            "decision": decision.decision.value,
            "accepted": decision_receipt.acknowledgement.accepted,
            "reason": decision_receipt.acknowledgement.reason.name.lower(),
            "attempts": decision_receipt.attempts,
            "elapsed_ms": decision_receipt.elapsed_s * 1000.0,
            "protocol_hil_only": True,
            "mission_state_changed": False,
            "payload_release_requested": False,
            "flight_command_enabled": False,
            "hardware_control_enabled": False,
        }
    )
    return 0 if decision_receipt.acknowledgement.accepted else 1


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
    if key is None:  # Kept explicit so validation is identical under ``python -O``.
        raise RuntimeError("alert HMAC key lookup returned no key")
    if len(key) < 32:
        raise ValueError("alert HMAC key must contain at least 32 bytes")
    return key


def _required_zone_evidence_hmac_key_from_env(variable_name: str | None) -> bytes:
    if variable_name is None:
        raise ValueError("authenticated zone evidence requires --zone-evidence-hmac-key-env")
    key = _hmac_key_from_env(variable_name)
    if key is None:  # Kept explicit so validation is identical under ``python -O``.
        raise RuntimeError("zone evidence HMAC key lookup returned no key")
    if len(key) < 32:
        raise ValueError("zone evidence HMAC key must contain at least 32 bytes")
    return key


def _required_payload_hil_hmac_key_from_env(
    variable_name: str | None,
    *,
    purpose: str,
) -> bytes:
    if variable_name is None:
        raise ValueError(f"payload HIL {purpose} requires an environment key option")
    key = _hmac_key_from_env(variable_name)
    if key is None:
        raise RuntimeError(f"payload HIL {purpose} key lookup returned no key")
    if len(key) < 32:
        raise ValueError(f"payload HIL {purpose} key must contain at least 32 bytes")
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
    if captured is None:  # ``frame_count`` validation above should make this unreachable.
        raise RuntimeError("camera check completed without capturing a frame")
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


def _run_camera_bench(args: argparse.Namespace) -> int:
    capture_config = _capture_config_from_args(args)
    bench_config = CameraBenchConfig(
        minimum_frames=args.minimum_frames,
        minimum_duration_seconds=args.minimum_duration_seconds,
        maximum_duration_seconds=args.maximum_duration_seconds,
    )
    source = OpenCVFrameSource(capture_config)
    try:
        document = run_camera_bench(source, capture_config, bench_config)
    finally:
        source.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit(document)
    return 0 if document["passed"] else 1


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


def _run_inert_payload_bench_check(args: argparse.Namespace) -> int:
    controller_key = _hmac_key_from_env(args.controller_hmac_key_env)
    sensor_key = _hmac_key_from_env(args.sensor_hmac_key_env)
    if controller_key is None or len(controller_key) < 32:
        raise ValueError("payload bench controller HMAC key must contain at least 32 bytes")
    if sensor_key is None or len(sensor_key) < 32:
        raise ValueError("payload bench sensor HMAC key must contain at least 32 bytes")
    document = check_inert_payload_hardware_bench(
        controller_log=args.controller_log,
        sensor_log=args.sensor_log,
        controller_hmac_key=controller_key,
        sensor_hmac_key=sensor_key,
        bench_id=args.bench_id,
        controller_id=args.controller_id,
        sensor_id=args.sensor_id,
        controller_key_id=args.controller_key_id,
        sensor_key_id=args.sensor_key_id,
        inert_load_only=args.inert_load_only,
        people_excluded_from_test_area=args.people_excluded_from_test_area,
        minimum_confirmed_cycles=args.minimum_confirmed_cycles,
        maximum_age_hours=args.maximum_age_hours,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit(document)
    return 0 if document["passed"] else 1


def _release_window_check(args: argparse.Namespace) -> int:
    config = MissionConfig.from_json(args.config)
    planner_config = config.fixed_wing_release_window
    if planner_config is None:
        raise ValueError("mission does not configure a fixed-wing HIL release window")
    if args.target_revision < 0:
        raise ValueError("target revision cannot be negative")
    bbox = BoundingBox(args.x1, args.y1, args.x2, args.y2)
    track = TrackSnapshot(
        track_id=args.target_id,
        revision=args.target_revision,
        label=args.label,
        bbox=bbox,
        first_seen_at_s=args.now_s,
        last_seen_at_s=args.now_s,
        observation_count=1,
        consecutive_observations=1,
        confidence_floor=1.0,
        confidence_mean=1.0,
        maximum_gap_s=0.0,
        area_growth_rate=0.0,
        thermal_corroborated=False,
        confirmed=False,
    )
    frame = FrameObservation(
        frame_id="release-window-hil-frame",
        captured_at_s=args.now_s,
        detections=(),
        telemetry=VehicleTelemetry(
            altitude_agl_m=args.altitude_agl_m,
            roll_deg=float("nan"),
            pitch_deg=args.pitch_deg,
            ground_speed_mps=args.ground_speed_mps,
            in_allowed_zone=None,
            geofence_healthy=None,
            position_healthy=None,
            link_healthy=None,
            flight_mode_allows_deploy=None,
            release_zone_clear=None,
        ),
    )
    solution = FixedWingReleaseWindowPlanner(
        planner_config,
        allowed_target_labels=config.target_classes,
    ).plan(track=track, frame=frame, now_s=args.now_s)
    _emit(
        {
            "event": "fixed_wing_release_window_checked",
            "mission_id": config.mission_id,
            "target_id": solution.target_id,
            "target_revision": solution.target_revision,
            "status": solution.status.value,
            "reasons": solution.reasons,
            "calibration_id": solution.calibration_id,
            "relative_bearing_deg": solution.relative_bearing_deg,
            "depression_angle_deg": solution.depression_angle_deg,
            "estimated_ground_range_m": solution.estimated_ground_range_m,
            "cross_track_error_m": solution.cross_track_error_m,
            "along_track_error_m": solution.along_track_error_m,
            "payload_descent_time_s": solution.payload_descent_time_s,
            "release_lead_distance_m": solution.release_lead_distance_m,
            "advisory_only": solution.advisory_only,
            "safety_rules_evaluated": False,
            "authorization_created": False,
            "flight_control_enabled": solution.flight_control_enabled,
            "physical_release_enabled": solution.physical_release_enabled,
        }
    )
    return 0


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


def _run_jetson_vision_bench(args: argparse.Namespace) -> int:
    class_names = _parse_class_names(args.class_names)
    verified = _verify_optional_model_manifest(
        manifest_path=args.model_manifest,
        model_path=args.onnx_model,
        class_names=class_names,
        output_coordinates=args.output_coordinates,
        require_production_approved=False,
        expected_model_role="fire_candidate",
    )
    if verified is None:
        raise RuntimeError("Jetson vision bench requires a verified model manifest")
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
            model_version=verified.model_version,
        )
    )
    capture_config = _capture_config_from_args(args)
    bench_config = JetsonVisionBenchConfig(
        minimum_frames=args.minimum_frames,
        minimum_duration_seconds=args.minimum_duration_seconds,
        maximum_duration_seconds=args.maximum_duration_seconds,
        maximum_temperature_c=args.maximum_temperature_c,
    )
    source = OpenCVFrameSource(capture_config)
    try:
        document = run_jetson_vision_bench(source, detector, bench_config)
    finally:
        source.close()
    document.update(
        {
            "model_sha256": _sha256_file(args.onnx_model),
            "model_version": verified.model_version,
            "model_role": verified.model_role,
            "manifest_status": verified.status,
            "manifest_production_approved": verified.production_approved,
            "class_names": list(class_names),
        }
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit(document)
    return 0 if document["passed"] else 1


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
            "messages_transmitted": provider.messages_transmitted,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_pixhawk_v6x_bench(args: argparse.Namespace) -> int:
    qgc_snapshot = load_qgc_telemetry_snapshot(args.qgc_snapshot)
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            endpoint=args.endpoint,
            baud=args.baud,
            stale_after_seconds=args.stale_after_seconds,
        )
    )
    try:
        document = run_pixhawk_v6x_bench(
            provider,
            qgc_snapshot,
            PixhawkBenchConfig(
                minimum_samples=args.minimum_samples,
                sample_interval_seconds=args.sample_interval_seconds,
                maximum_qgc_age_seconds=args.maximum_qgc_age_seconds,
            ),
        )
    finally:
        provider.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit(document)
    return 0 if document["passed"] else 1


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
    zone_evidence_hmac_key: bytes | None = None
    payload_hil_request_key: bytes | None = None
    payload_hil_result_key: bytes | None = None
    payload_confirmation_key: bytes | None = None
    if args.safety_model_manifest is not None and args.safety_onnx_model is None:
        raise ValueError("--safety-model-manifest requires --safety-onnx-model")
    if args.simulate_payload_cycle and args.payload_inventory_report is not None:
        raise ValueError(
            "--simulate-payload-cycle cannot be combined with --payload-inventory-report"
        )
    if args.auto_simulate_payload_cycle and not args.simulate_payload_cycle:
        raise ValueError("--auto-simulate-payload-cycle requires --simulate-payload-cycle")
    if args.payload_inventory_report is None and (
        args.payload_inventory_hmac_key_env is not None or args.payload_inventory_key_id is not None
    ):
        raise ValueError("payload inventory authentication options require a report path")
    if args.zone_evidence_report is None and (
        args.zone_evidence_hmac_key_env is not None or args.zone_evidence_key_id is not None
    ):
        raise ValueError("zone evidence authentication options require a report path")
    if args.zone_evidence_report is not None:
        if not args.pixhawk_endpoint:
            raise ValueError("--zone-evidence-report requires --pixhawk-endpoint")
        if not isinstance(args.zone_evidence_key_id, str) or not args.zone_evidence_key_id.strip():
            raise ValueError("--zone-evidence-report requires --zone-evidence-key-id")
        zone_evidence_hmac_key = _required_zone_evidence_hmac_key_from_env(
            args.zone_evidence_hmac_key_env
        )
    if args.simulate_payload_cycle and not config.deployment_capable:
        raise ValueError(
            "--simulate-payload-cycle requires a configuration with an installed payload"
        )
    payload_hil_specific_options = (
        args.payload_hil_controller_port,
        args.payload_hil_controller_module_id,
        args.payload_hil_request_key_env,
        args.payload_hil_request_key_id,
        args.payload_hil_result_key_env,
        args.payload_hil_result_key_id,
        args.payload_confirmation_port,
        args.payload_confirmation_key_env,
        args.payload_confirmation_key_id,
        *args.payload_confirmation_sensor_id,
    )
    if not args.inert_payload_hil and any(
        value is not None and value != "" for value in payload_hil_specific_options
    ):
        raise ValueError("payload HIL channel options require --inert-payload-hil")
    if args.inert_payload_hil:
        if not args.simulate_payload_cycle:
            raise ValueError("--inert-payload-hil requires --simulate-payload-cycle")
        required_hil_values = {
            "--payload-hil-controller-port": args.payload_hil_controller_port,
            "--payload-hil-controller-module-id": args.payload_hil_controller_module_id,
            "--payload-hil-request-key-env": args.payload_hil_request_key_env,
            "--payload-hil-request-key-id": args.payload_hil_request_key_id,
            "--payload-hil-result-key-env": args.payload_hil_result_key_env,
            "--payload-hil-result-key-id": args.payload_hil_result_key_id,
            "--payload-confirmation-port": args.payload_confirmation_port,
            "--payload-confirmation-key-env": args.payload_confirmation_key_env,
            "--payload-confirmation-key-id": args.payload_confirmation_key_id,
        }
        missing = [name for name, value in required_hil_values.items() if value is None]
        if missing:
            raise ValueError("--inert-payload-hil requires " + ", ".join(missing))
        if not 1 <= args.payload_hil_controller_port <= 65535:
            raise ValueError("payload HIL controller port must be in [1, 65535]")
        if not 1 <= args.payload_confirmation_port <= 65535:
            raise ValueError("payload confirmation port must be in [1, 65535]")
        if args.payload_hil_controller_host.strip().lower() not in {"127.0.0.1", "localhost"}:
            raise ValueError("inert payload HIL controller must use a loopback host")
        if args.payload_confirmation_bind_host.strip().lower() not in {
            "127.0.0.1",
            "localhost",
        }:
            raise ValueError("inert payload confirmation must bind to loopback")
        if not args.payload_hil_controller_module_id.strip():
            raise ValueError("payload HIL controller module ID cannot be empty")
        if not args.payload_confirmation_sensor_id:
            raise ValueError("--inert-payload-hil requires --payload-confirmation-sensor-id")
        sensor_ids = frozenset(item.strip() for item in args.payload_confirmation_sensor_id)
        if any(not item for item in sensor_ids):
            raise ValueError("payload confirmation sensor IDs cannot be empty")
        if args.payload_hil_controller_module_id.strip() in sensor_ids:
            raise ValueError("payload controller and confirmation sensor IDs must differ")
        payload_hil_request_key = _required_payload_hil_hmac_key_from_env(
            args.payload_hil_request_key_env,
            purpose="request",
        )
        payload_hil_result_key = _required_payload_hil_hmac_key_from_env(
            args.payload_hil_result_key_env,
            purpose="result",
        )
        payload_confirmation_key = _required_payload_hil_hmac_key_from_env(
            args.payload_confirmation_key_env,
            purpose="confirmation",
        )
        if len({payload_hil_request_key, payload_hil_result_key, payload_confirmation_key}) != 3:
            raise ValueError("payload HIL request, result and confirmation keys must differ")
        key_ids = {
            args.payload_hil_request_key_id.strip(),
            args.payload_hil_result_key_id.strip(),
            args.payload_confirmation_key_id.strip(),
        }
        if "" in key_ids or len(key_ids) != 3:
            raise ValueError("payload HIL request, result and confirmation key IDs must differ")
    if args.observe_pixhawk_lifecycle and not args.pixhawk_endpoint:
        raise ValueError("--observe-pixhawk-lifecycle requires --pixhawk-endpoint")
    if args.observe_pixhawk_lifecycle and args.task_area_mission_sequence is None:
        raise ValueError("--observe-pixhawk-lifecycle requires --task-area-mission-sequence")
    if args.operator_udp_port is None and (
        args.operator_hmac_key_env is not None or args.mavlink_signing_key_hex_env is not None
    ):
        raise ValueError("remote operator key options require --operator-udp-port")
    if args.safety_model_coco80 and args.safety_onnx_model is None:
        raise ValueError("--safety-model-coco80 requires --safety-onnx-model")
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
    verified_safety_model: VerifiedModelArtifact | None = None
    safety_class_names: tuple[str, ...] | None = None
    if args.safety_onnx_model is not None:
        safety_class_names = (
            COCO80_CLASS_NAMES
            if args.safety_model_coco80
            else _parse_class_names(args.safety_class_names)
        )
        verified_safety_model = _verify_optional_model_manifest(
            manifest_path=args.safety_model_manifest,
            model_path=args.safety_onnx_model,
            class_names=safety_class_names,
            output_coordinates=(args.safety_output_coordinates or args.output_coordinates),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="safety_object_evidence",
        )
    synthetic_models = tuple(
        artifact
        for artifact in (verified_fire_model, verified_safety_model)
        if artifact is not None and artifact.synthetic_hil_only
    )
    if synthetic_models and not args.allow_synthetic_hil_model:
        raise ValueError(
            "synthetic HIL model requires the explicit --allow-synthetic-hil-model flag"
        )
    if args.allow_synthetic_hil_model and not synthetic_models:
        raise ValueError(
            "--allow-synthetic-hil-model requires a manifest marked synthetic_hil_only"
        )

    # Load executable graph artifacts only after every supplied manifest has passed its role,
    # hash, coordinate and optional production-approval gates.
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
    if args.safety_onnx_model is not None:
        if safety_class_names is None:
            raise RuntimeError("safety model class names were not initialized")
        detectors.append(
            OnnxNx6Detector(
                OnnxNx6Config(
                    model_path=args.safety_onnx_model,
                    class_names=safety_class_names,
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=args.safety_confidence_threshold,
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
    detector: Any = ClassConfidenceFilter(
        DetectorEnsemble(detectors),
        {
            "fire": args.flame_confidence_threshold,
            "flame": args.flame_confidence_threshold,
            "smoke": args.smoke_confidence_threshold,
            "person": args.safety_confidence_threshold,
            "firefighter": args.safety_confidence_threshold,
        },
        default_threshold=None,
    )
    detector = BrightNeutralLightVetoFilter(detector)
    if args.safety_onnx_model is not None:
        detector = PersonOverlapVetoFilter(
            detector,
            minimum_fire_coverage=args.person_veto_fire_coverage,
        )
    detector = TemporalDetectionFilter(
        detector,
        labels=FIRE_CANDIDATE_TRACK_LABELS,
        minimum_consecutive_frames=args.candidate_stability_frames,
    )
    person_safety_model_coverage = detector.covers_labels(config.person_labels)
    person_safety_evidence_qualified = (
        person_safety_model_coverage and verified_safety_model is not None
    )
    telemetry = (
        PixhawkReadOnlyTelemetryProvider(
            PixhawkReadOnlyConfig(endpoint=args.pixhawk_endpoint, baud=args.pixhawk_baud)
        )
        if args.pixhawk_endpoint
        else FailClosedTelemetryProvider()
    )
    if args.zone_evidence_report is not None:
        if zone_evidence_hmac_key is None or args.zone_evidence_key_id is None:
            raise RuntimeError("zone evidence authentication was not initialized")
        telemetry = AuthenticatedZoneTelemetryProvider(
            telemetry,
            FileZoneEvidenceProvider(
                args.zone_evidence_report,
                hmac_key=zone_evidence_hmac_key,
                expected_key_id=args.zone_evidence_key_id,
            ),
            mission_id=config.mission_id,
            maximum_age_s=config.safety.sensor_data_max_age_seconds,
            maximum_position_delta_m=args.zone_evidence_max_position_delta_m,
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
                    frozenset(config.target_classes) | FIRE_CANDIDATE_TRACK_LABELS,
                    acquisition_timeout_s=args.operator_acquisition_timeout_seconds,
                    lost_after_s=args.operator_lost_after_seconds,
                ),
            ),
        )
    payload_hil_cycle = None
    if args.inert_payload_hil:
        if (
            payload_hil_request_key is None
            or payload_hil_result_key is None
            or payload_confirmation_key is None
            or args.payload_hil_controller_port is None
            or args.payload_confirmation_port is None
            or args.payload_hil_controller_module_id is None
            or args.payload_hil_request_key_id is None
            or args.payload_hil_result_key_id is None
            or args.payload_confirmation_key_id is None
        ):
            raise RuntimeError("payload HIL configuration was not initialized")
        request_codec = PayloadHilCodec(
            hmac_key=payload_hil_request_key,
            expected_key_id=args.payload_hil_request_key_id,
        )
        result_codec = PayloadHilCodec(
            hmac_key=payload_hil_result_key,
            expected_key_id=args.payload_hil_result_key_id,
        )
        confirmation_codec = PayloadConfirmationHilCodec(
            hmac_key=payload_confirmation_key,
            expected_key_id=args.payload_confirmation_key_id,
        )
        confirmation_receiver = UdpPayloadConfirmationHilReceiver(
            bind_host=args.payload_confirmation_bind_host,
            port=args.payload_confirmation_port,
        )
        try:
            payload_hil_cycle = InertPayloadHilCycleCoordinator(
                mission=controller,
                controller_adapter=MissionPayloadHilAdapter(
                    mission=controller,
                    client=UdpPayloadHilClient(
                        host=args.payload_hil_controller_host,
                        port=args.payload_hil_controller_port,
                        request_codec=request_codec,
                        result_codec=result_codec,
                        response_timeout_s=args.payload_hil_response_timeout_seconds,
                        maximum_attempts=args.payload_hil_maximum_attempts,
                    ),
                    module_id=args.payload_hil_controller_module_id,
                    request_key_id=args.payload_hil_request_key_id,
                    request_ttl_s=args.payload_hil_request_ttl_seconds,
                    maximum_result_age_s=args.payload_hil_result_max_age_seconds,
                ),
                confirmation_receiver=confirmation_receiver,
                confirmation_codec=confirmation_codec,
                controller_module_id=args.payload_hil_controller_module_id,
                allowed_confirmation_sensor_ids=frozenset(
                    item.strip() for item in args.payload_confirmation_sensor_id
                ),
                confirmation_timeout_s=args.payload_confirmation_timeout_seconds,
                confirmation_maximum_age_s=args.payload_confirmation_max_age_seconds,
            )
        except Exception:
            confirmation_receiver.close()
            raise
    runner = LiveMissionRunner(
        mission=controller,
        frame_source=OpenCVFrameSource(_capture_config_from_args(args)),
        detector=detector,
        telemetry_provider=telemetry,
        alert_publisher=alert_publisher,
        alert_outbox=alert_outbox,
        prediction_writer=prediction_writer,
        operator_bridge=operator_bridge,
        payload_hil_cycle=payload_hil_cycle,
        config=LiveRunConfig(
            operator_id=args.operator_id,
            max_frames=args.max_frames,
            display=not args.no_display,
            alert_banner_seconds=args.alert_banner_seconds,
            performance_window_frames=args.performance_window_frames,
            simulate_payload_cycle=args.simulate_payload_cycle,
            auto_simulate_payload_cycle=args.auto_simulate_payload_cycle,
            observe_pixhawk_lifecycle=args.observe_pixhawk_lifecycle,
            task_area_mission_sequence=args.task_area_mission_sequence,
            allowed_auto_modes=(
                tuple(args.allowed_auto_mode)
                if args.allowed_auto_mode
                else ("AUTO", "MISSION", "AUTO_MISSION")
            ),
            person_safety_evidence_qualified=person_safety_evidence_qualified,
        ),
    )
    _emit(
        {
            "event": "live_camera_started",
            "model_providers": [provider for item in detectors for provider in item.provider_names],
            "pixhawk_read_only": bool(args.pixhawk_endpoint),
            "zone_evidence_enabled": args.zone_evidence_report is not None,
            "zone_evidence_control_enabled": False,
            "mission_lifecycle": (
                "pixhawk_observed" if args.observe_pixhawk_lifecycle else "immediate_simulation"
            ),
            "physical_release_supported": False,
            "person_safety_model_coverage": person_safety_model_coverage,
            "person_safety_evidence_qualified": person_safety_evidence_qualified,
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
            "remote_operator_authorization_enabled": (
                operator_bridge is not None and config.deployment_capable
            ),
            "remote_operator_flight_control_enabled": False,
            "remote_operator_direct_payload_control_enabled": False,
            "inert_payload_hil_enabled": payload_hil_cycle is not None,
            "auto_simulated_payload_cycle_enabled": args.auto_simulate_payload_cycle,
            "payload_hil_physical_release_enabled": False,
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
            "local_selections": result.local_selection_count,
            "local_tracking_statuses": result.local_tracking_status_count,
            "remote_selections": result.remote_selection_count,
            "remote_tracking_statuses": result.remote_tracking_status_count,
            "remote_mission_statuses": result.remote_mission_status_count,
            "remote_safety_statuses": result.remote_safety_status_count,
            "remote_transport_errors": result.remote_transport_error_count,
            "audit_written": args.audit_out is not None,
            "prediction_log_written": args.prediction_log_out is not None,
            "physical_release_supported": False,
            "inert_payload_hil_enabled": payload_hil_cycle is not None,
            "auto_simulated_payload_cycle_enabled": args.auto_simulate_payload_cycle,
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
                        "deployment_window": (
                            None
                            if decision.deployment_window is None
                            else {
                                "status": decision.deployment_window.status.value,
                                "reasons": decision.deployment_window.reasons,
                                "calibration_id": decision.deployment_window.calibration_id,
                                "relative_bearing_deg": (
                                    decision.deployment_window.relative_bearing_deg
                                ),
                                "cross_track_error_m": (
                                    decision.deployment_window.cross_track_error_m
                                ),
                                "along_track_error_m": (
                                    decision.deployment_window.along_track_error_m
                                ),
                                "release_lead_distance_m": (
                                    decision.deployment_window.release_lead_distance_m
                                ),
                                "advisory_only": decision.deployment_window.advisory_only,
                                "flight_control_enabled": (
                                    decision.deployment_window.flight_control_enabled
                                ),
                                "physical_release_enabled": (
                                    decision.deployment_window.physical_release_enabled
                                ),
                            }
                        ),
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

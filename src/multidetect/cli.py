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

from .aircraft_appearance import HandcraftedAircraftAppearanceEncoder
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
from .appearance_reid import (
    NVIDIA_TAO_REID_V1_2_SHA256,
    OnnxPersonReIdConfig,
    OnnxPersonReIdEncoder,
)
from .approach_hil import ApproachHilController
from .approach_live import LiveApproachHilCoordinator
from .audit import AuditLog
from .camera_bench import CameraBenchConfig, run_camera_bench
from .config import MissionConfig
from .deployment_planner import FixedWingReleaseWindowPlanner, PrimaryRangeEvidence
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
from .engine_provenance import verify_engine_provenance
from .evaluation import (
    JsonlPredictionWriter,
    evaluate_detections,
    evaluation_document,
    load_ground_truth_jsonl,
    load_prediction_jsonl,
)
from .fixed_wing_aim_control import (
    FixedWingAimConfig,
    FixedWingAimController,
    FixedWingAimExecutor,
    PixhawkFlightControlConfig,
    PixhawkFlightControlProvider,
)
from .gr01_bench import Gr01BenchConfig, run_gr01_link_bench
from .integration_evidence import INTEGRATION_PROFILES, check_integration_evidence_bundle
from .jetson_bench import JetsonVisionBenchConfig, run_jetson_vision_bench
from .live import LiveMissionRunner, LiveRangingConfig, LiveRunConfig
from .mission import MissionController
from .model_manifest import (
    PINNED_LEGACY_CHECKPOINT_SHA256,
    PINNED_LEGACY_CHECKPOINT_SIZE_BYTES,
    VerifiedModelArtifact,
    create_candidate_model_manifest,
    create_semantic_context_model_manifest,
    verify_checkpoint_bytes,
    verify_model_manifest,
    write_candidate_model_manifest,
)
from .monocular_acceptance import (
    MonocularAvoidanceAcceptanceConfig,
    run_monocular_avoidance_acceptance,
)
from .monocular_avoidance import MonocularAvoidanceConfig, OpenCVSparseFlowAvoidance
from .multimodal_ranging import (
    MultiModalRangingEngine,
    RangeSolution,
    RangeValidity,
    load_camera_calibration,
)
from .operator_bridge import LiveOperatorBridge
from .operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
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
from .patrol_advisory import (
    AdvisoryValidity,
    PatrolAdvisoryConfig,
    PatrolAdvisoryEngine,
    PatrolPhase,
    ReturnObserveDirection,
)
from .patrol_reacquisition_acceptance import run_patrol_reacquisition_acceptance
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
from .payload_target_live import LivePayloadTargetCoordinator
from .pixhawk import (
    PIXHAWK_AUTOPILOT_IDS,
    PIXHAWK_VEHICLE_TYPE_IDS,
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)
from .pixhawk_bench import (
    PixhawkBenchConfig,
    load_qgc_telemetry_snapshot,
    run_pixhawk_v6x_bench,
)
from .pixhawk_link_audit import V6XLinkAuditExpectations, audit_v6x_link_topology
from .pixhawk_parameters import (
    PixhawkParameterBackupClient,
    PixhawkParameterBackupConfig,
    compare_pixhawk_parameter_snapshots,
    load_verified_pixhawk_parameter_snapshot,
    write_pixhawk_parameter_diff,
    write_pixhawk_parameter_report,
    write_pixhawk_parameter_snapshot,
)
from .reid_acceptance import (
    ReIdModelAcceptanceConfig,
    ReIdTensorRtAcceptanceConfig,
    run_reid_model_acceptance,
    run_reid_tensorrt_acceptance,
)
from .replay import load_jsonl_replay, primary_range_evidence_from_frame
from .rgb_fire_corroboration import (
    IndependentRgbFireCorroborationConfig,
    IndependentRgbFireCorroborator,
)
from .rtsp_evidence_recording import (
    RtspEvidenceRecordingConfig,
    record_rtsp_evidence,
    rtsp_evidence_recording_document,
)
from .selection_target_pool import UnifiedSelectionTargetPool
from .semantic_environment import (
    CITYSEMSEGFORMER_LABELS,
    AsyncSemanticContextRunner,
    OnnxCategoricalSemanticContext,
    OnnxSemanticContextConfig,
)
from .short_term_acceptance import (
    ShortTermTrackingAcceptanceConfig,
    run_short_term_tracking_acceptance,
)
from .short_term_tracking import OpenCVShortTermTargetTracker, ShortTermTrackingConfig
from .synthetic_model import create_synthetic_hil_model_bundle
from .telemetry import AuthenticatedZoneTelemetryProvider, FailClosedTelemetryProvider
from .tensorrt_session import TensorRtEmbeddingSession, TensorRtSemanticSession
from .tracking_evaluation import (
    IdentityTrackingEvaluationReport,
    JsonlIdentityPredictionWriter,
    evaluate_identity_tracking,
    load_identity_ground_truth_jsonl,
    load_identity_prediction_jsonl,
    tracking_evaluation_document,
)
from .tracking_review import (
    prepare_tracking_review_bundle,
    tracking_review_bundle_document,
)
from .unified_acceptance import (
    UnifiedTrackingAcceptanceConfig,
    run_unified_tracking_acceptance,
)
from .unified_tracking import UnifiedTargetPool, UnifiedTargetPoolConfig, UnifiedTrackState
from .vehicle_reid import (
    OPENVINO_VEHICLE_REID_0001_SHA384,
    OnnxVehicleReIdConfig,
    OnnxVehicleReIdEncoder,
)
from .vision import (
    BrightNeutralLightVetoFilter,
    BufferedFrameSource,
    CaptureConfig,
    ClassConfidenceFilter,
    DetectorEnsemble,
    FrameCadencedDetector,
    LabelAllowListFilter,
    LabelRemapDetector,
    MultiSourceConfidenceFilter,
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    PersonOverlapVetoFilter,
    SameLabelDetectionFusion,
    TemporalDetectionFilter,
    TiledDetectionConfig,
    TiledDetectionFusion,
    VehicleFurnitureOverlapVetoFilter,
    frame_source_from_config,
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
PRIORITY_DETECTION_CLASS_NAMES = (
    "person",
    "firefighter",
    "airplane",
    "bicycle",
    "car",
    "motorcycle",
    "bus",
    "train",
    "truck",
    "boat",
)
AIRCRAFT_DETECTION_CLASS_NAMES = frozenset({"airplane"})
VEHICLE_DETECTION_CLASS_NAMES = frozenset(
    label
    for label in PRIORITY_DETECTION_CLASS_NAMES
    if label not in {"person", "firefighter", *AIRCRAFT_DETECTION_CLASS_NAMES}
)
VEHICLE_TEMPORAL_LABEL_ALIASES = {
    label: "vehicle"
    for label in (
        *VEHICLE_DETECTION_CLASS_NAMES,
        "vehicle",
        "van",
        "motorbike",
        "motor",
        "tricycle",
        "awning-tricycle",
        "awning_tricycle",
    )
}
ENVIRONMENT_RISK_CLASS_NAMES = (
    "power_line",
    "flammable_tank",
)
# Automatic UI candidates deliberately exclude indoor furniture and consumer
# objects.  The common COCO detector still emits those labels upstream so the
# vehicle/furniture veto can use them to reject false cars; manual operator
# rectangles remain available for arbitrary objects.
NON_SELECTABLE_AUTOMATIC_LABELS = frozenset(
    {
        "chair",
        "couch",
        "bed",
        "dining table",
        "toilet",
        "tv",
        "laptop",
        "mouse",
        "remote",
        "keyboard",
        "cell phone",
        "potted plant",
        "book",
        "clock",
        "vase",
    }
)
VISDRONE_PRIORITY_CLASS_NAMES = (
    "pedestrian",
    "people",
    "bicycle",
    "car",
    "van",
    "truck",
    "tricycle",
    "awning-tricycle",
    "bus",
    "motor",
)
VISDRONE_PRIORITY_LABEL_MAP = (
    "pedestrian=person,people=person,van=car,tricycle=motorcycle,"
    "awning-tricycle=motorcycle,motor=motorcycle"
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
    release_window.add_argument("--heading-deg", type=float, required=True)
    release_window.add_argument("--velocity-north-mps", type=float, required=True)
    release_window.add_argument("--velocity-east-mps", type=float, required=True)
    release_window.add_argument("--airspeed-mps", type=float, required=True)
    release_window.add_argument("--wind-north-mps", type=float, required=True)
    release_window.add_argument("--wind-east-mps", type=float, required=True)
    release_window.add_argument("--target-north-m", type=float, required=True)
    release_window.add_argument("--target-east-m", type=float, required=True)
    release_window.add_argument("--range-ci-low-m", type=float, required=True)
    release_window.add_argument("--range-ci-high-m", type=float, required=True)
    release_window.add_argument("--bearing-sigma-deg", type=float, required=True)
    release_window.add_argument("--range-sensor-consistency", type=float, required=True)
    release_window.add_argument("--range-calibration-id", required=True)
    release_window.add_argument("--range-target-id", default="hil-unified-target")
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
    tracking_evaluation = subparsers.add_parser(
        "evaluate-tracking",
        help="evaluate identity-annotated tracking JSONL without opening hardware",
    )
    tracking_evaluation.add_argument("ground_truth", type=Path)
    tracking_evaluation.add_argument("predictions", type=Path)
    tracking_evaluation.add_argument("--iou-threshold", type=float, default=0.5)
    tracking_evaluation.add_argument("--confidence-threshold", type=float, default=0.1)
    tracking_evaluation.add_argument("--maximum-timestamp-delta-seconds", type=float, default=0.05)
    tracking_evaluation.add_argument(
        "--maximum-occlusion-recovery-seconds", type=float, default=0.5
    )
    tracking_evaluation.add_argument(
        "--maximum-out-of-frame-recovery-seconds", type=float, default=2.0
    )
    tracking_evaluation.add_argument(
        "--dataset-provenance",
        choices=("unverified", "synthetic_demo", "lab_recording", "deployment_recording"),
        default="unverified",
    )
    tracking_evaluation.add_argument("--source-video", type=Path)
    tracking_evaluation.add_argument("--annotations-reviewed", action="store_true")
    tracking_evaluation.add_argument("--minimum-idf1", type=float)
    tracking_evaluation.add_argument("--maximum-id-switch-count", type=int)
    tracking_evaluation.add_argument("--minimum-occlusion-recovery-rate", type=float)
    tracking_evaluation.add_argument("--minimum-out-of-frame-recovery-rate", type=float)
    tracking_evaluation.add_argument("--maximum-occlusion-recovery-p95-seconds", type=float)
    tracking_evaluation.add_argument("--maximum-out-of-frame-recovery-p95-seconds", type=float)
    tracking_evaluation.add_argument("--out", type=Path)
    unified_bench = subparsers.add_parser(
        "unified-tracking-bench",
        help="benchmark the metadata-only multi-target core without camera, models, or hardware",
    )
    unified_bench.add_argument("--track-count", type=int, default=10)
    unified_bench.add_argument("--benchmark-frames", type=int, default=3000)
    unified_bench.add_argument("--minimum-metadata-rate-hz", type=float, default=15.0)
    unified_bench.add_argument("--maximum-switch-latency-ms", type=float, default=200.0)
    unified_bench.add_argument("--maximum-short-occlusion-seconds", type=float, default=0.5)
    unified_bench.add_argument("--maximum-reacquisition-seconds", type=float, default=2.0)
    unified_bench.add_argument("--out", type=Path, required=True)
    patrol_reacquisition_sitl = subparsers.add_parser(
        "patrol-reacquisition-sitl",
        help=(
            "validate mode-1 occlusion, LOST, ReID recovery and revisit advice against an "
            "owned isolated PX4 SITL telemetry stream; opens no camera and sends no MAVLink"
        ),
    )
    patrol_reacquisition_sitl.add_argument("--endpoint", required=True)
    patrol_reacquisition_sitl.add_argument("--baud", type=int, default=57_600)
    patrol_reacquisition_sitl.add_argument("--samples", type=int, default=40)
    patrol_reacquisition_sitl.add_argument("--interval-seconds", type=float, default=0.1)
    patrol_reacquisition_sitl.add_argument(
        "--acknowledge-owned-disposable-sitl",
        action="store_true",
    )
    patrol_reacquisition_sitl.add_argument("--out", type=Path, required=True)
    short_term_bench = subparsers.add_parser(
        "short-term-tracking-bench",
        help="benchmark image-level flow/template recovery without camera, models, or hardware",
    )
    short_term_bench.add_argument("--track-count", type=int, default=10)
    short_term_bench.add_argument("--benchmark-frames", type=int, default=300)
    short_term_bench.add_argument("--frame-rate-hz", type=float, default=30.0)
    short_term_bench.add_argument("--analysis-width", type=int, default=320)
    short_term_bench.add_argument("--frame-stride", type=int, default=2)
    short_term_bench.add_argument(
        "--maximum-processing-latency-p95-ms",
        type=float,
        default=66.7,
    )
    short_term_bench.add_argument("--minimum-end-to-end-rate-hz", type=float, default=15.0)
    short_term_bench.add_argument("--maximum-recovery-seconds", type=float, default=0.5)
    short_term_bench.add_argument("--out", type=Path, required=True)
    avoidance_bench = subparsers.add_parser(
        "monocular-avoidance-bench",
        help="benchmark advisory-only OpenCV flow/RANSAC avoidance without hardware",
    )
    avoidance_bench.add_argument("--benchmark-frames", type=int, default=300)
    avoidance_bench.add_argument("--frame-rate-hz", type=float, default=30.0)
    avoidance_bench.add_argument("--analysis-width", type=int, default=320)
    avoidance_bench.add_argument(
        "--maximum-processing-latency-p95-ms",
        type=float,
        default=66.7,
    )
    avoidance_bench.add_argument("--minimum-end-to-end-rate-hz", type=float, default=15.0)
    avoidance_bench.add_argument("--out", type=Path, required=True)
    reid_bench = subparsers.add_parser(
        "reid-onnx-cpu-bench",
        help="validate pinned person/vehicle ReID ONNX models on CPU without hardware",
    )
    reid_bench.add_argument("--person-model", type=Path, required=True)
    reid_bench.add_argument("--vehicle-model", type=Path, required=True)
    reid_bench.add_argument("--person-count", type=int, default=4)
    reid_bench.add_argument("--vehicle-count", type=int, default=4)
    reid_bench.add_argument("--iterations", type=int, default=2)
    reid_bench.add_argument("--realtime-frame-budget-ms", type=float, default=66.7)
    reid_bench.add_argument("--out", type=Path, required=True)
    reid_tensorrt_bench = subparsers.add_parser(
        "reid-tensorrt-bench",
        help="gate target-built person/vehicle ReID TensorRT engines without camera or Pixhawk",
    )
    reid_tensorrt_bench.add_argument("--person-model", type=Path, required=True)
    reid_tensorrt_bench.add_argument("--vehicle-model", type=Path, required=True)
    reid_tensorrt_bench.add_argument("--person-engine", type=Path, required=True)
    reid_tensorrt_bench.add_argument("--vehicle-engine", type=Path, required=True)
    reid_tensorrt_bench.add_argument("--person-count", type=int, default=4)
    reid_tensorrt_bench.add_argument("--vehicle-count", type=int, default=4)
    reid_tensorrt_bench.add_argument("--iterations", type=int, default=20)
    reid_tensorrt_bench.add_argument("--realtime-frame-budget-ms", type=float, default=66.7)
    reid_tensorrt_bench.add_argument("--out", type=Path, required=True)
    tracking_review = subparsers.add_parser(
        "prepare-tracking-review",
        help="prepare a hash-bound, deliberately unreviewed identity annotation bundle",
    )
    tracking_review.add_argument("predictions", type=Path)
    tracking_review.add_argument("source_video", type=Path)
    tracking_review.add_argument("source_video_manifest", type=Path)
    tracking_review.add_argument("output_directory", type=Path)
    tracking_review.add_argument("--overwrite", action="store_true")
    rtsp_recording = subparsers.add_parser(
        "record-rtsp-evidence",
        help="record credential-redacted H.265 RTSP stream-copy evidence without re-encoding",
    )
    rtsp_recording.add_argument("--source-env", required=True)
    rtsp_recording.add_argument("--session-id", required=True)
    rtsp_recording.add_argument("--out-video", type=Path, required=True)
    rtsp_recording.add_argument("--manifest-out", type=Path, required=True)
    rtsp_recording.add_argument("--duration-seconds", type=float, default=30.0)
    rtsp_recording.add_argument("--latency-ms", type=int, default=100)
    rtsp_recording.add_argument("--finalize-timeout-seconds", type=float, default=5.0)
    rtsp_recording.add_argument("--overwrite", action="store_true")
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
    model_check.add_argument(
        "--onnx-model",
        type=Path,
        required=True,
        help="post-NMS ONNX model or Jetson TensorRT .engine/.plan artifact",
    )
    model_check.add_argument("--model-manifest", type=Path)
    model_check.add_argument(
        "--model-role",
        choices=(
            "fire_candidate",
            "fire_verifier",
            "safety_object_evidence",
            "environment_risk_evidence",
        ),
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
    jetson_bench.add_argument(
        "--onnx-model",
        type=Path,
        required=True,
        help="post-NMS ONNX model or Jetson TensorRT .engine/.plan artifact",
    )
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
    jetson_bench.add_argument("--minimum-frames", type=int, default=54_000)
    jetson_bench.add_argument("--minimum-duration-seconds", type=float, default=3600.0)
    jetson_bench.add_argument("--maximum-duration-seconds", type=float, default=3900.0)
    jetson_bench.add_argument("--maximum-temperature-c", type=float, default=95.0)
    jetson_bench.add_argument("--minimum-processing-fps", type=float, default=15.0)
    jetson_bench.add_argument(
        "--maximum-inference-latency-p95-ms",
        type=float,
        default=66.7,
    )
    jetson_bench.add_argument(
        "--maximum-capture-queue-high-watermark",
        type=int,
        default=1,
    )
    jetson_bench.add_argument("--maximum-memory-growth-mb", type=float, default=256.0)
    jetson_bench.add_argument("--memory-warmup-seconds", type=float, default=60.0)
    jetson_bench.add_argument("--out", type=Path, required=True)

    manifest_init = subparsers.add_parser(
        "model-manifest-init",
        help="create a quarantined candidate manifest bound to a local model artifact",
    )
    manifest_init.add_argument(
        "--onnx-model",
        "--model-artifact",
        dest="onnx_model",
        type=Path,
        required=True,
    )
    manifest_init.add_argument("--out", type=Path, required=True)
    manifest_init.add_argument("--model-id", required=True)
    manifest_init.add_argument("--model-version", required=True)
    manifest_init.add_argument("--source-description", required=True)
    manifest_init.add_argument(
        "--model-role",
        choices=(
            "fire_candidate",
            "fire_verifier",
            "safety_object_evidence",
            "environment_risk_evidence",
        ),
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
    manifest_init.add_argument(
        "--native-output-format",
        choices=("post_nms_N_x_6", "ultralytics_raw_xywh_class_scores"),
        default="post_nms_N_x_6",
    )
    manifest_init.add_argument("--force", action="store_true")

    semantic_manifest_init = subparsers.add_parser(
        "semantic-model-manifest-init",
        help="create a quarantined categorical semantic-context manifest bound to ONNX",
    )
    semantic_manifest_init.add_argument("--onnx-model", type=Path, required=True)
    semantic_manifest_init.add_argument("--out", type=Path, required=True)
    semantic_manifest_init.add_argument("--model-id", required=True)
    semantic_manifest_init.add_argument("--model-version", required=True)
    semantic_manifest_init.add_argument("--source-description", required=True)
    semantic_manifest_init.add_argument("--input-width", type=int, default=1820)
    semantic_manifest_init.add_argument("--input-height", type=int, default=1024)
    semantic_manifest_init.add_argument("--output-name", default="output")
    semantic_manifest_init.add_argument("--force", action="store_true")

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
    pixhawk_check.add_argument(
        "--endpoint",
        required=True,
        help="serial/network endpoint, or auto for fail-closed USB discovery",
    )
    pixhawk_check.add_argument("--baud", type=int, default=57_600)
    pixhawk_check.add_argument("--samples", type=int, default=10)
    pixhawk_check.add_argument("--interval-seconds", type=float, default=0.2)
    pixhawk_check.add_argument("--require-fresh-link", action="store_true")
    pixhawk_check.add_argument("--require-fresh-position", action="store_true")
    pixhawk_check.add_argument("--expected-system-id", type=int)
    pixhawk_check.add_argument(
        "--expected-autopilot",
        choices=tuple(sorted(PIXHAWK_AUTOPILOT_IDS)),
    )
    pixhawk_check.add_argument(
        "--expected-vehicle-type",
        choices=tuple(sorted(PIXHAWK_VEHICLE_TYPE_IDS)),
    )
    pixhawk_check.add_argument("--require-operational-state", action="store_true")

    pixhawk_parameters = subparsers.add_parser(
        "pixhawk-param-backup",
        help=(
            "send one explicitly acknowledged PARAM_REQUEST_LIST and atomically back up "
            "returned values; never writes parameters or sends flight commands"
        ),
    )
    pixhawk_parameters.add_argument("--endpoint", required=True)
    pixhawk_parameters.add_argument("--baud", type=int, default=57_600)
    pixhawk_parameters.add_argument("--target-system-id", type=int, required=True)
    pixhawk_parameters.add_argument("--target-component-id", type=int, default=1)
    pixhawk_parameters.add_argument(
        "--parameter-encoding",
        choices=("bytewise", "c_cast"),
        required=True,
        help="PX4 uses bytewise; choose c_cast only for an autopilot known to use C casting",
    )
    pixhawk_parameters.add_argument("--timeout-seconds", type=float, default=60.0)
    pixhawk_parameters.add_argument("--idle-timeout-seconds", type=float, default=3.0)
    pixhawk_parameters.add_argument("--minimum-parameters", type=int, default=100)
    pixhawk_parameters.add_argument("--out", type=Path, required=True)
    pixhawk_parameters.add_argument("--force", action="store_true")
    pixhawk_parameters.add_argument(
        "--acknowledge-active-read-request",
        action="store_true",
        help=(
            "required acknowledgement that this command transmits exactly one active "
            "parameter-list read request"
        ),
    )

    pixhawk_parameter_verify = subparsers.add_parser(
        "pixhawk-param-verify",
        help="offline-verify a complete parameter backup and its self-consistency hash",
    )
    pixhawk_parameter_verify.add_argument("snapshot", type=Path)
    pixhawk_parameter_verify.add_argument("--out", type=Path)
    pixhawk_parameter_verify.add_argument("--force", action="store_true")

    pixhawk_parameter_diff = subparsers.add_parser(
        "pixhawk-param-diff",
        help="offline-compare two verified backups and reject unlisted parameter changes",
    )
    pixhawk_parameter_diff.add_argument("before", type=Path)
    pixhawk_parameter_diff.add_argument("after", type=Path)
    pixhawk_parameter_diff.add_argument("--allow-change", action="append", default=[])
    pixhawk_parameter_diff.add_argument("--require-change", action="append", default=[])
    pixhawk_parameter_diff.add_argument("--out", type=Path, required=True)
    pixhawk_parameter_diff.add_argument("--force", action="store_true")

    pixhawk_link_audit = subparsers.add_parser(
        "pixhawk-link-audit",
        help=(
            "offline-audit independent GR01/TELEM1, Jetson/Ethernet and optional "
            "Jetson/TELEM2 link configuration from a verified PX4 parameter backup"
        ),
    )
    pixhawk_link_audit.add_argument("snapshot", type=Path)
    pixhawk_link_audit.add_argument("--gr01-telem1-baud", type=int, default=115_200)
    pixhawk_link_audit.add_argument("--jetson-telem2-baud", type=int, default=921_600)
    pixhawk_link_audit.add_argument("--ethernet-udp-port", type=int, default=14_550)
    pixhawk_link_audit.add_argument("--require-uart-fallback", action="store_true")
    pixhawk_link_audit.add_argument("--out", type=Path)
    pixhawk_link_audit.add_argument("--force", action="store_true")

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
    alert_receiver.add_argument("--deduplication-capacity", type=int, default=10_000)

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
    live.add_argument(
        "--onnx-model",
        type=Path,
        required=True,
        help="post-NMS ONNX model or Jetson TensorRT .engine/.plan artifact",
    )
    live.add_argument("--model-manifest", type=Path)
    live.add_argument("--class-names", default="fire,smoke")
    live.add_argument(
        "--primary-model-frame-stride",
        type=int,
        default=1,
        help=(
            "run the primary fire/smoke detector every Nth camera frame; selected targets "
            "retain visual continuity through the short-term tracker"
        ),
    )
    live.add_argument("--primary-model-frame-phase", type=int, default=0)
    live.add_argument(
        "--rgb-fire-verifier-model",
        type=Path,
        help=(
            "independent post-NMS RGB fire verifier ONNX/TensorRT model; its output only "
            "corroborates primary fire boxes and never creates targets"
        ),
    )
    live.add_argument("--rgb-fire-verifier-model-manifest", type=Path)
    live.add_argument("--rgb-fire-verifier-class-names", default="fire,smoke")
    live.add_argument("--rgb-fire-verifier-confidence-threshold", type=float, default=0.65)
    live.add_argument("--rgb-fire-verifier-minimum-iou", type=float, default=0.30)
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
    live.add_argument(
        "--safety-model-format",
        choices=("post_nms_nx6", "ultralytics_raw"),
        default="post_nms_nx6",
        help=(
            "use ultralytics_raw for an end2end=False, nms=False export; host-side NMS "
            "avoids TensorRT 8.6 TopK incompatibilities"
        ),
    )
    live.add_argument("--safety-model-iou-threshold", type=float, default=0.45)
    live.add_argument("--safety-model-maximum-detections", type=int, default=300)
    live.add_argument("--safety-model-frame-stride", type=int, default=1)
    live.add_argument("--safety-model-frame-phase", type=int, default=0)
    live.add_argument("--safety-confidence-threshold", type=float, default=0.30)
    live.add_argument("--safety-priority-confidence-threshold", type=float, default=0.25)
    live.add_argument("--safety-fallback-confidence-threshold", type=float, default=0.35)
    live.add_argument("--safety-tile-columns", type=int, default=1)
    live.add_argument("--safety-tile-rows", type=int, default=1)
    live.add_argument("--safety-tile-overlap", type=float, default=0.15)
    live.add_argument("--safety-tile-scan-interval-frames", type=int, default=3)
    live.add_argument("--safety-tile-fusion-iou-threshold", type=float, default=0.30)
    live.add_argument("--safety-tile-confidence-threshold", type=float, default=0.40)
    live.add_argument(
        "--safety-tile-label-confidence-thresholds",
        default="airplane=0.82",
        help="comma-separated class=confidence overrides used only on tiled detections",
    )
    live.add_argument("--safety-tile-maximum-box-area", type=float, default=0.04)
    live.add_argument(
        "--safety-tile-labels",
        default=",".join(PRIORITY_DETECTION_CLASS_NAMES),
        help="comma-separated priority classes eligible for tiled small-object discovery",
    )
    live.add_argument("--priority-onnx-model", type=Path)
    live.add_argument("--priority-model-manifest", type=Path)
    live.add_argument("--priority-class-names", default=",".join(VISDRONE_PRIORITY_CLASS_NAMES))
    live.add_argument("--priority-label-map", default=VISDRONE_PRIORITY_LABEL_MAP)
    live.add_argument("--priority-input-width", type=int, default=960)
    live.add_argument("--priority-input-height", type=int, default=960)
    live.add_argument("--priority-confidence-threshold", type=float, default=0.30)
    live.add_argument("--priority-person-confidence-threshold", type=float, default=0.30)
    live.add_argument(
        "--priority-vehicle-confidence-threshold",
        type=float,
        default=0.60,
        help="minimum vehicle confidence across both common and priority detectors",
    )
    live.add_argument(
        "--car-single-source-confidence-threshold",
        type=float,
        default=0.80,
        help="minimum car confidence when common and priority detectors do not agree",
    )
    live.add_argument(
        "--priority-label-confidence-thresholds",
        default="truck=0.80",
        help=(
            "comma-separated source-label=confidence overrides applied by the "
            "priority detector before runtime label remapping"
        ),
    )
    live.add_argument(
        "--priority-vehicle-stability-frames",
        type=int,
        default=3,
        help="consecutive scheduled vehicle detections required before fusion",
    )
    live.add_argument("--priority-model-iou-threshold", type=float, default=0.45)
    live.add_argument("--priority-model-maximum-detections", type=int, default=300)
    live.add_argument("--priority-model-frame-stride", type=int, default=1)
    live.add_argument("--priority-model-frame-phase", type=int, default=0)
    live.add_argument(
        "--lock-model-force-every-frame",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "force every matching learned detector on each LCK frame; disable to retain "
            "scheduled detector cadence while the short-term tracker supplies visual continuity"
        ),
    )
    live.add_argument("--environment-onnx-model", type=Path)
    live.add_argument("--environment-model-manifest", type=Path)
    live.add_argument(
        "--environment-class-names",
        default=",".join(ENVIRONMENT_RISK_CLASS_NAMES),
    )
    live.add_argument("--environment-confidence-threshold", type=float, default=0.40)
    live.add_argument(
        "--semantic-context-onnx-model",
        type=Path,
        help=(
            "hash-manifest-bound categorical scene segmentation ONNX; runs on a bounded "
            "low-rate worker and never enters target identity or control"
        ),
    )
    live.add_argument("--semantic-context-model-manifest", type=Path)
    live.add_argument("--semantic-context-engine", type=Path)
    live.add_argument("--semantic-context-engine-provenance", type=Path)
    live.add_argument(
        "--semantic-context-trtexec",
        type=Path,
        default=Path("/usr/src/tensorrt/bin/trtexec"),
    )
    live.add_argument("--semantic-context-minimum-interval-seconds", type=float, default=0.5)
    live.add_argument("--semantic-context-maximum-age-seconds", type=float, default=2.0)
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
    live.add_argument("--fire-minimum-bright-warm-fraction", type=float, default=0.0)
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
    live.add_argument(
        "--rgb-fire-verifier-output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
    )
    live.add_argument(
        "--environment-output-coordinates",
        choices=("letterbox_xyxy_px", "normalized_xyxy"),
    )
    live.add_argument("--provider", action="append", default=[])
    live.add_argument("--trt-engine-cache", type=Path)
    live.add_argument("--pixhawk-endpoint")
    live.add_argument("--pixhawk-baud", type=int, default=57_600)
    live.add_argument("--pixhawk-system-id", type=int)
    live.add_argument(
        "--pixhawk-expected-autopilot",
        choices=tuple(sorted(PIXHAWK_AUTOPILOT_IDS)),
    )
    live.add_argument(
        "--pixhawk-expected-vehicle-type",
        choices=tuple(sorted(PIXHAWK_VEHICLE_TYPE_IDS)),
    )
    live.add_argument("--require-pixhawk-operational-state", action="store_true")
    live.add_argument("--observe-pixhawk-lifecycle", action="store_true")
    live.add_argument("--task-area-mission-sequence", type=int)
    live.add_argument("--allowed-auto-mode", action="append", default=[])
    live.add_argument("--operator-id", default="local-operator")
    live.add_argument("--max-frames", type=int)
    live.add_argument("--alert-banner-seconds", type=float, default=5.0)
    live.add_argument("--performance-window-frames", type=int, default=600)
    live.add_argument(
        "--monocular-avoidance",
        action="store_true",
        help=(
            "enable lightweight monocular collision-risk advisory; emits no flight-control "
            "commands and provides no metric depth"
        ),
    )
    live.add_argument("--avoidance-analysis-width", type=int, default=640)
    live.add_argument("--avoidance-minimum-features", type=int, default=24)
    live.add_argument("--avoidance-caution-ttc-seconds", type=float, default=3.0)
    live.add_argument("--avoidance-avoid-ttc-seconds", type=float, default=1.5)
    live.add_argument("--avoidance-maximum-data-age-seconds", type=float, default=0.25)
    live.add_argument(
        "--multimodal-ranging",
        action="store_true",
        help=(
            "enable read-only primary-target ranging from an explicit camera calibration and "
            "timestamped Pixhawk observations; never enables flight or payload control"
        ),
    )
    live.add_argument(
        "--mode3-aim",
        "--approach-hil",
        dest="approach_hil",
        action="store_true",
        help=(
            "enable Mode-3 target-bound centering state and signed execution confirmation; "
            "requires operator UDP, unified target pool, monocular avoidance and multimodal ranging"
        ),
    )
    live.add_argument(
        "--fixed-wing-aim-control",
        action="store_true",
        help="enable real Mode-3 LCK attitude-target control through the qualified Pixhawk link",
    )
    live.add_argument("--aim-maximum-target-age-seconds", type=float, default=0.30)
    live.add_argument("--aim-maximum-attitude-age-seconds", type=float, default=0.50)
    live.add_argument("--aim-minimum-airspeed-mps", type=float, default=12.0)
    live.add_argument("--aim-minimum-altitude-agl-m", type=float, default=8.0)
    live.add_argument("--aim-maximum-abs-roll-deg", type=float, default=20.0)
    live.add_argument("--aim-maximum-abs-pitch-deg", type=float, default=15.0)
    live.add_argument("--aim-maximum-roll-correction-deg", type=float, default=10.0)
    live.add_argument("--aim-maximum-pitch-correction-deg", type=float, default=6.0)
    live.add_argument("--aim-roll-gain", type=float, default=0.70)
    live.add_argument("--aim-pitch-gain", type=float, default=0.70)
    live.add_argument("--aim-maximum-roll-slew-deg-s", type=float, default=35.0)
    live.add_argument("--aim-maximum-pitch-slew-deg-s", type=float, default=25.0)
    live.add_argument("--aim-prestream-setpoints", type=int, default=10)
    live.add_argument("--aim-control-mode", default="OFFBOARD")
    live.add_argument("--aim-return-mode", default="AUTO")
    live.add_argument("--aim-rc-input-rate-hz", type=float, default=20.0)
    live.add_argument("--aim-rc-input-maximum-age-seconds", type=float, default=0.30)
    live.add_argument("--aim-rc-cancel-threshold-us", type=int, default=50)
    live.add_argument(
        "--payload-target-hil",
        action="store_true",
        help=(
            "enable Mode-2 fire-aimpoint resolution and target-bound continuous-slide "
            "confirmation; gates authorization metadata but never enables physical release"
        ),
    )
    live.add_argument(
        "--ranging-calibration",
        type=Path,
        help="strict schema-v1 JSON camera intrinsics, distortion and installation calibration",
    )
    live.add_argument("--ranging-agl-sigma-m", type=float, default=1.5)
    live.add_argument("--ranging-roll-sigma-deg", type=float, default=0.3)
    live.add_argument("--ranging-pitch-sigma-deg", type=float, default=0.3)
    live.add_argument("--ranging-heading-sigma-deg", type=float, default=1.0)
    live.add_argument("--ranging-target-center-sigma-px", type=float, default=2.0)
    live.add_argument(
        "--unified-target-pool",
        action="store_true",
        help=(
            "maintain a bounded metadata-only multi-target bank; this does not enable "
            "flight control or physical payload output"
        ),
    )
    live.add_argument("--unified-target-pool-maximum-tracks", type=int, default=64)
    live.add_argument(
        "--unified-target-pool-locked-reacquisition-seconds",
        type=float,
        default=5.0,
        help=(
            "retain an exclusive LCK identity in active reacquisition for this long; "
            "normal DET/TRK tracks keep the shorter target-pool timeout"
        ),
    )
    live.add_argument(
        "--unified-target-pool-minimum-association-confidence",
        type=float,
        default=0.10,
    )
    live.add_argument(
        "--unified-target-pool-priority-minimum-new-track-confidence",
        type=float,
        default=0.25,
    )
    live.add_argument(
        "--unified-target-pool-minimum-new-track-confidence",
        type=float,
        default=0.35,
    )
    live.add_argument(
        "--unified-target-pool-high-confidence-threshold",
        type=float,
        default=0.55,
    )
    live.add_argument(
        "--unified-target-pool-person-maximum-appearance-distance",
        type=float,
        help=(
            "optional person/firefighter ReID association gate; leaves vehicle and aircraft "
            "appearance gates at their shared defaults"
        ),
    )
    live.add_argument(
        "--unified-target-pool-person-strict-reid-distance",
        type=float,
        help="optional person/firefighter strict ReID recovery gate",
    )
    live.add_argument("--unified-target-pool-kalman-process-noise", type=float, default=0.04)
    live.add_argument(
        "--unified-target-pool-kalman-measurement-noise",
        type=float,
        default=0.0004,
    )
    live.add_argument("--unified-target-pool-kalman-gate-sigma", type=float, default=4.0)
    live.add_argument(
        "--unified-target-pool-kalman-maximum-horizon-seconds",
        type=float,
        default=2.0,
    )
    live.add_argument(
        "--patrol-advisory",
        action="store_true",
        help=(
            "derive mode-1 patrol and return-to-observe metadata; emits no route or "
            "flight-control commands"
        ),
    )
    live.add_argument("--patrol-maximum-bank-angle-deg", type=float, default=25.0)
    live.add_argument("--patrol-minimum-ground-speed-mps", type=float, default=5.0)
    live.add_argument("--patrol-maximum-evidence-age-seconds", type=float, default=2.0)
    live.add_argument(
        "--person-reid-onnx",
        type=Path,
        help="hash-pinned NVIDIA TAO person ReID ONNX artifact",
    )
    live.add_argument(
        "--person-reid-engine",
        type=Path,
        help="Jetson-built TensorRT engine for the pinned person ReID ONNX artifact",
    )
    live.add_argument("--person-reid-maximum-batch-size", type=int, default=10)
    live.add_argument(
        "--person-reid-frame-stride",
        type=int,
        default=2,
        help="run person ReID every N frames while stable; recovery and time gates override it",
    )
    live.add_argument(
        "--vehicle-reid-onnx",
        type=Path,
        help="hash-pinned OpenVINO vehicle-reid-0001 ONNX artifact",
    )
    live.add_argument(
        "--vehicle-reid-engine",
        type=Path,
        help="Jetson-built TensorRT engine for the pinned vehicle ReID ONNX artifact",
    )
    live.add_argument("--vehicle-reid-maximum-batch-size", type=int, default=8)
    live.add_argument(
        "--vehicle-reid-frame-stride",
        type=int,
        default=2,
        help="run vehicle ReID every N frames while stable; recovery and time gates override it",
    )
    live.add_argument(
        "--reid-maximum-interval-seconds",
        type=float,
        default=0.1,
        help="maximum wall-clock interval between enabled ReID passes",
    )
    live.add_argument(
        "--allow-nonrealtime-reid",
        action="store_true",
        help=(
            "lab-only override permitting ReID without TensorRT; runtime status remains "
            "non-realtime and no deployment claim is allowed"
        ),
    )
    live.add_argument(
        "--short-term-tracking",
        action="store_true",
        help=(
            "enable local optical-flow/template prediction hints; hints never count as "
            "identity observations and never enable flight control"
        ),
    )
    live.add_argument("--short-term-analysis-width", type=int, default=640)
    live.add_argument("--short-term-maximum-tracks", type=int, default=16)
    live.add_argument("--short-term-minimum-flow-points", type=int, default=6)
    live.add_argument("--short-term-minimum-box-size-px", type=int, default=12)
    live.add_argument("--short-term-frame-stride", type=int, default=1)
    live.add_argument(
        "--short-term-template-minimum-correlation",
        type=float,
        default=0.72,
    )
    live.add_argument("--short-term-search-expansion", type=float, default=2.5)
    live.add_argument(
        "--short-term-occluded-search-multiplier",
        type=float,
        default=1.5,
    )
    live.add_argument(
        "--short-term-reacquiring-search-multiplier",
        type=float,
        default=2.0,
    )
    live.add_argument("--short-term-maximum-search-expansion", type=float, default=6.0)
    live.add_argument(
        "--short-term-maximum-retained-template-age-seconds",
        type=float,
        default=2.0,
    )
    live.add_argument(
        "--warmup-iterations",
        type=int,
        default=1,
        help="initialize each model provider before opening the live camera",
    )
    live.add_argument(
        "--capture-queue-frames",
        type=int,
        default=4,
        help=(
            "ordered capture FIFO capacity; 0 restores sequential capture/inference for diagnostics"
        ),
    )
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
    live.add_argument(
        "--identity-tracking-log-out",
        type=Path,
        help=(
            "write frame-aligned unified target IDs and states for offline identity evaluation; "
            "requires --unified-target-pool"
        ),
    )
    live.add_argument(
        "--identity-tracking-session-id",
        help="shared UUID binding the identity log to the matching RTSP evidence recording",
    )
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
        if args.command == "evaluate-tracking":
            return _evaluate_tracking_logs(args)
        if args.command == "unified-tracking-bench":
            return _run_unified_tracking_bench(args)
        if args.command == "patrol-reacquisition-sitl":
            return _run_patrol_reacquisition_sitl(args)
        if args.command == "short-term-tracking-bench":
            return _run_short_term_tracking_bench(args)
        if args.command == "monocular-avoidance-bench":
            return _run_monocular_avoidance_bench(args)
        if args.command == "reid-onnx-cpu-bench":
            return _run_reid_onnx_cpu_bench(args)
        if args.command == "reid-tensorrt-bench":
            return _run_reid_tensorrt_bench(args)
        if args.command == "prepare-tracking-review":
            return _prepare_tracking_review(args)
        if args.command == "record-rtsp-evidence":
            return _record_rtsp_evidence(args)
        if args.command == "model-check":
            return _run_model_check(args)
        if args.command == "jetson-vision-bench":
            return _run_jetson_vision_bench(args)
        if args.command == "model-manifest-init":
            return _run_model_manifest_init(args)
        if args.command == "semantic-model-manifest-init":
            return _run_semantic_model_manifest_init(args)
        if args.command == "synthetic-model-init":
            return _run_synthetic_model_init(args)
        if args.command == "legacy-checkpoint-verify":
            return _run_legacy_checkpoint_verify(args)
        if args.command == "pixhawk-check":
            return _run_pixhawk_check(args)
        if args.command == "pixhawk-param-backup":
            return _run_pixhawk_param_backup(args)
        if args.command == "pixhawk-param-verify":
            return _run_pixhawk_param_verify(args)
        if args.command == "pixhawk-param-diff":
            return _run_pixhawk_param_diff(args)
        if args.command == "pixhawk-link-audit":
            return _run_pixhawk_link_audit(args)
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
                    "record-rtsp-evidence",
                    "reid-tensorrt-bench",
                    "jetson-vision-bench",
                    "pixhawk-check",
                    "pixhawk-param-backup",
                    "pixhawk-param-verify",
                    "pixhawk-param-diff",
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
    patrol_status = PatrolStatusMessage(
        status_id=str(UUID("66666666-6666-4666-8666-666666666666")),
        sequence=4,
        mission_id="fire-patrol-demo",
        phase=PatrolPhase.LOST,
        primary_target_id="track-42",
        target_state=UnifiedTrackState.LOST,
        bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
        label="flame",
        confidence=0.91,
        tracking_quality=0.2,
        total_track_count=10,
        locked_track_count=2,
        source_frame_id="jetson-frame-700",
        source_captured_at_s=1_000.52,
        produced_at_s=1_000.58,
        return_direction=ReturnObserveDirection.LEFT,
        return_validity=AdvisoryValidity.DEGRADED,
        return_evidence_age_s=0.5,
        estimated_minimum_turn_radius_m=75.0,
    )
    encoded_patrol_status = codec.encode_patrol_status(patrol_status)
    patrol_status_frame = jetson_mavlink.wrap_authenticated_operator_payload(encoded_patrol_status)
    received_patrol_status = codec.decode(
        g20_mavlink.extract_authenticated_operator_payload(patrol_status_frame)
    ).message
    if not isinstance(received_patrol_status, PatrolStatusMessage):  # pragma: no cover
        raise RuntimeError("operator-link demo returned the wrong patrol-status type")
    _emit(
        {
            "event": "g20_patrol_status_received",
            "phase": received_patrol_status.phase.value,
            "target_state": (
                received_patrol_status.target_state.value
                if received_patrol_status.target_state is not None
                else None
            ),
            "total_track_count": received_patrol_status.total_track_count,
            "locked_track_count": received_patrol_status.locked_track_count,
            "return_direction": received_patrol_status.return_direction.value,
            "return_validity": received_patrol_status.return_validity.value,
            "payload_bytes": len(encoded_patrol_status),
            "mavlink_frame_bytes": len(patrol_status_frame),
            "advisory_only": received_patrol_status.advisory_only,
            "flight_control_enabled": received_patrol_status.flight_control_enabled,
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
            "patrol_status_received": True,
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
    parser.add_argument("--rtsp-codec", choices=("h264", "h265"), default="h265")
    parser.add_argument(
        "--backend",
        choices=("auto", "dshow", "msmf", "ffmpeg", "gstreamer"),
        default="auto",
    )
    parser.add_argument("--gstreamer-hardware-decode", action="store_true")
    parser.add_argument("--gstreamer-latency-ms", type=int, default=100)
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
        rtsp_codec=args.rtsp_codec,
        backend=args.backend,
        gstreamer_hardware_decode=args.gstreamer_hardware_decode,
        gstreamer_latency_ms=args.gstreamer_latency_ms,
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


def _parse_label_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("label map entries must use source=destination")
        source, destination = (value.strip().lower() for value in item.split("=", 1))
        if not source or not destination:
            raise ValueError("label map entries cannot be empty")
        mapping[source] = destination
    return mapping


def _parse_label_confidence_thresholds(raw: str) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise ValueError("tile confidence entries must use class=confidence")
        raw_label, raw_value = (value.strip() for value in item.split("=", 1))
        label = raw_label.lower()
        if not label:
            raise ValueError("tile confidence labels cannot be empty")
        try:
            threshold = float(raw_value)
        except ValueError as exc:
            raise ValueError("tile confidence values must be numeric") from exc
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("tile confidence values must be in [0, 1]")
        thresholds[label] = threshold
    return thresholds


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
    expected_native_output_format: str | None = None,
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
        expected_native_output_format=expected_native_output_format,
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
    with frame_source_from_config(capture_config) as source:
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
            "source_kind": (
                "synthetic"
                if capture_config.is_synthetic
                else "rtsp"
                if capture_config.is_rtsp
                else "local_device"
            ),
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
    source = frame_source_from_config(capture_config)
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
            heading_deg=args.heading_deg,
            velocity_north_mps=args.velocity_north_mps,
            velocity_east_mps=args.velocity_east_mps,
            airspeed_mps=args.airspeed_mps,
            wind_north_mps=args.wind_north_mps,
            wind_east_mps=args.wind_east_mps,
            velocity_observed_at_s=args.now_s,
            airspeed_observed_at_s=args.now_s,
            wind_observed_at_s=args.now_s,
        ),
    )
    ground_range_m = math.hypot(args.target_north_m, args.target_east_m)
    relative_bearing_deg = math.degrees(math.atan2(args.target_east_m, args.target_north_m))
    range_solution = RangeSolution(
        target_id=args.range_target_id,
        frame_id=frame.frame_id,
        calibration_id=args.range_calibration_id,
        evaluated_at_s=args.now_s,
        validity=RangeValidity.VALID,
        reasons=("multimodal_range_consistent",),
        sources=("camera_ground", "laser"),
        rejected_sources=(),
        slant_range_m=ground_range_m,
        ground_range_m=ground_range_m,
        slant_range_ci95_m=(args.range_ci_low_m, args.range_ci_high_m),
        ground_range_ci95_m=(args.range_ci_low_m, args.range_ci_high_m),
        relative_bearing_deg=relative_bearing_deg,
        absolute_bearing_deg=(args.heading_deg + relative_bearing_deg) % 360.0,
        bearing_sigma_deg=args.bearing_sigma_deg,
        north_offset_m=args.target_north_m,
        east_offset_m=args.target_east_m,
        data_freshness_s=0.0,
        sensor_consistency=args.range_sensor_consistency,
    )
    evidence = PrimaryRangeEvidence(
        source_target_id=args.range_target_id,
        source_frame_id=frame.frame_id,
        source_captured_at_s=args.now_s,
        source_label=track.label,
        source_bbox=track.bbox,
        solution=range_solution,
    )
    solution = FixedWingReleaseWindowPlanner(
        planner_config,
        allowed_target_labels=config.target_classes,
    ).plan(
        track=track,
        frame=frame,
        now_s=args.now_s,
        ranging_evidence=evidence,
    )
    _emit(
        {
            "event": "fixed_wing_release_window_checked",
            "mission_id": config.mission_id,
            "target_id": solution.target_id,
            "target_revision": solution.target_revision,
            "status": solution.status.value,
            "timing_status": solution.timing_status.value,
            "reasons": solution.reasons,
            "calibration_id": solution.calibration_id,
            "relative_bearing_deg": solution.relative_bearing_deg,
            "depression_angle_deg": solution.depression_angle_deg,
            "estimated_ground_range_m": solution.estimated_ground_range_m,
            "cross_track_error_m": solution.cross_track_error_m,
            "along_track_error_m": solution.along_track_error_m,
            "payload_descent_time_s": solution.payload_descent_time_s,
            "release_lead_distance_m": solution.release_lead_distance_m,
            "target_north_offset_m": solution.target_north_offset_m,
            "target_east_offset_m": solution.target_east_offset_m,
            "impact_north_offset_m": solution.impact_north_offset_m,
            "impact_east_offset_m": solution.impact_east_offset_m,
            "error_ellipse_major_m": solution.error_ellipse_major_m,
            "error_ellipse_minor_m": solution.error_ellipse_minor_m,
            "error_ellipse_orientation_deg": solution.error_ellipse_orientation_deg,
            "ground_range_ci95_m": solution.ground_range_ci95_m,
            "range_target_id": solution.range_target_id,
            "range_frame_id": solution.range_frame_id,
            "range_sensor_consistency": solution.range_sensor_consistency,
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


def _evaluate_tracking_logs(args: argparse.Namespace) -> int:
    if args.source_video is not None and not args.source_video.is_file():
        raise ValueError(f"tracking source video does not exist: {args.source_video}")
    report = evaluate_identity_tracking(
        load_identity_ground_truth_jsonl(args.ground_truth),
        load_identity_prediction_jsonl(args.predictions),
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.confidence_threshold,
        maximum_timestamp_delta_s=args.maximum_timestamp_delta_seconds,
        maximum_occlusion_recovery_s=args.maximum_occlusion_recovery_seconds,
        maximum_out_of_frame_recovery_s=args.maximum_out_of_frame_recovery_seconds,
    )
    failure_reasons = _tracking_acceptance_failure_reasons(args, report)
    acceptance_evaluated = any(
        value is not None
        for value in (
            args.minimum_idf1,
            args.maximum_id_switch_count,
            args.minimum_occlusion_recovery_rate,
            args.minimum_out_of_frame_recovery_rate,
            args.maximum_occlusion_recovery_p95_seconds,
            args.maximum_out_of_frame_recovery_p95_seconds,
        )
    )
    document = {
        "event": "identity_tracking_evaluation_completed",
        "dataset_provenance": args.dataset_provenance,
        "ground_truth_sha256": _sha256_file(args.ground_truth),
        "predictions_sha256": _sha256_file(args.predictions),
        "source_video_sha256": (
            _sha256_file(args.source_video) if args.source_video is not None else None
        ),
        "annotations_reviewed": args.annotations_reviewed,
        "deployment_domain_evidence_complete": (
            args.dataset_provenance == "deployment_recording"
            and args.source_video is not None
            and args.annotations_reviewed
        ),
        "acceptance_evaluated": acceptance_evaluated,
        "passed": (not failure_reasons if acceptance_evaluated else None),
        "failure_reasons": failure_reasons,
        **tracking_evaluation_document(report),
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _emit(document)
    return 2 if failure_reasons else 0


def _run_unified_tracking_bench(args: argparse.Namespace) -> int:
    started_s = time.perf_counter()
    report = run_unified_tracking_acceptance(
        UnifiedTrackingAcceptanceConfig(
            track_count=args.track_count,
            benchmark_frames=args.benchmark_frames,
            minimum_metadata_rate_hz=args.minimum_metadata_rate_hz,
            maximum_switch_latency_ms=args.maximum_switch_latency_ms,
            maximum_short_occlusion_s=args.maximum_short_occlusion_seconds,
            maximum_reacquisition_s=args.maximum_reacquisition_seconds,
        )
    )
    document = {
        "event": "unified_tracking_core_benchmark_completed",
        "schema_version": 1,
        "measured_at_unix_s": time.time(),
        "command_wall_time_s": time.perf_counter() - started_s,
        "passed": True,
        "camera_opened": False,
        "model_inference_executed": False,
        "pixhawk_opened": False,
        "metadata_only": True,
        **report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(document)
    return 0


def _run_patrol_reacquisition_sitl(args: argparse.Namespace) -> int:
    if not args.acknowledge_owned_disposable_sitl:
        raise ValueError("patrol-reacquisition-sitl requires --acknowledge-owned-disposable-sitl")
    endpoint_match = re.fullmatch(
        r"udpin:(?:0\.0\.0\.0|127\.0\.0\.1):(\d{1,5})",
        args.endpoint,
    )
    if endpoint_match is None:
        raise ValueError("patrol-reacquisition-sitl requires an isolated local udpin endpoint")
    endpoint_port = int(endpoint_match.group(1))
    if not 1 <= endpoint_port <= 65_535:
        raise ValueError("patrol-reacquisition-sitl endpoint port is invalid")
    if endpoint_port == 14_550:
        raise ValueError("patrol-reacquisition-sitl refuses protected ground-station UDP 14550")
    if args.samples <= 0:
        raise ValueError("patrol-reacquisition-sitl samples must be positive")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds < 0.0:
        raise ValueError("patrol-reacquisition-sitl interval must be a finite non-negative number")

    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            endpoint=args.endpoint,
            baud=args.baud,
            expected_system_id=1,
            expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS["px4"],
            expected_vehicle_type_id=PIXHAWK_VEHICLE_TYPE_IDS["fixed_wing"],
            require_operational_state=True,
        )
    )
    snapshots: list[VehicleTelemetry] = []
    try:
        for index in range(args.samples):
            snapshots.append(provider.snapshot(now_s=time.monotonic()))
            if index + 1 < args.samples and args.interval_seconds > 0.0:
                time.sleep(args.interval_seconds)
        sampled_at_s = time.monotonic()
        diagnostics = provider.diagnostics(now_s=sampled_at_s)
    finally:
        provider.close()
    operational_snapshots = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.link_healthy is True
        and snapshot.position_healthy is True
        and snapshot.armed is True
        and snapshot.flight_mode == "MISSION"
        and math.isfinite(snapshot.ground_speed_mps)
        and snapshot.ground_speed_mps >= 5.0
    )
    latest = operational_snapshots[-1] if operational_snapshots else snapshots[-1]
    requirements = {
        "qualified_px4_fixed_wing": provider.qualification.passed is True,
        "fresh_link": latest.link_healthy is True,
        "fresh_position": latest.position_healthy is True,
        "armed": latest.armed is True,
        "mission_mode": latest.flight_mode == "MISSION",
        "ground_speed_at_least_5_mps": (
            math.isfinite(latest.ground_speed_mps) and latest.ground_speed_mps >= 5.0
        ),
        "receive_only": provider.messages_transmitted == 0,
    }
    failed = tuple(name for name, passed in requirements.items() if not passed)
    if failed:
        raise RuntimeError(
            "owned PX4 SITL telemetry did not satisfy patrol-reacquisition gates: "
            + ", ".join(failed)
        )

    report = run_patrol_reacquisition_acceptance(latest)
    document = {
        "event": "patrol_reacquisition_sitl_acceptance_completed",
        "schema_version": 1,
        "measured_at_unix_s": time.time(),
        "passed": True,
        "scope": {
            "owned_disposable_sitl_acknowledged": True,
            "isolated_local_udp_port": endpoint_port,
            "protected_ground_station_port_contacted": False,
            "camera_opened": False,
            "network_camera_contacted": False,
            "model_inference_executed": False,
            "application_mavlink_messages_transmitted": provider.messages_transmitted,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
        },
        "requirements": requirements,
        "pixhawk": {
            **diagnostics,
            "latest": _telemetry_document(latest),
        },
        "scenario": report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(document)
    return 0


def _run_short_term_tracking_bench(args: argparse.Namespace) -> int:
    started_s = time.perf_counter()
    report = run_short_term_tracking_acceptance(
        ShortTermTrackingAcceptanceConfig(
            track_count=args.track_count,
            benchmark_frames=args.benchmark_frames,
            frame_rate_hz=args.frame_rate_hz,
            analysis_width=args.analysis_width,
            frame_stride=args.frame_stride,
            maximum_processing_latency_p95_ms=(args.maximum_processing_latency_p95_ms),
            minimum_end_to_end_rate_hz=args.minimum_end_to_end_rate_hz,
            maximum_recovery_s=args.maximum_recovery_seconds,
        )
    )
    document = {
        "event": "short_term_image_tracking_benchmark_completed",
        "schema_version": 1,
        "measured_at_unix_s": time.time(),
        "command_wall_time_s": time.perf_counter() - started_s,
        "passed": True,
        **report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(document)
    return 0


def _run_monocular_avoidance_bench(args: argparse.Namespace) -> int:
    started_s = time.perf_counter()
    report = run_monocular_avoidance_acceptance(
        MonocularAvoidanceAcceptanceConfig(
            benchmark_frames=args.benchmark_frames,
            frame_rate_hz=args.frame_rate_hz,
            analysis_width=args.analysis_width,
            maximum_processing_latency_p95_ms=(args.maximum_processing_latency_p95_ms),
            minimum_end_to_end_rate_hz=args.minimum_end_to_end_rate_hz,
        )
    )
    document = {
        "event": "monocular_avoidance_image_benchmark_completed",
        "schema_version": 1,
        "measured_at_unix_s": time.time(),
        "command_wall_time_s": time.perf_counter() - started_s,
        "passed": True,
        **report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(document)
    return 0


def _run_reid_onnx_cpu_bench(args: argparse.Namespace) -> int:
    started_s = time.perf_counter()
    report = run_reid_model_acceptance(
        ReIdModelAcceptanceConfig(
            person_model_path=args.person_model,
            vehicle_model_path=args.vehicle_model,
            person_count=args.person_count,
            vehicle_count=args.vehicle_count,
            iterations=args.iterations,
            realtime_frame_budget_ms=args.realtime_frame_budget_ms,
        )
    )
    document = {
        "event": "reid_onnx_cpu_benchmark_completed",
        "schema_version": 1,
        "measured_at_unix_s": time.time(),
        "command_wall_time_s": time.perf_counter() - started_s,
        "passed": True,
        **report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(document)
    return 0


def _run_reid_tensorrt_bench(args: argparse.Namespace) -> int:
    started_s = time.perf_counter()
    report = run_reid_tensorrt_acceptance(
        ReIdTensorRtAcceptanceConfig(
            person_model_path=args.person_model,
            vehicle_model_path=args.vehicle_model,
            person_engine_path=args.person_engine,
            vehicle_engine_path=args.vehicle_engine,
            person_count=args.person_count,
            vehicle_count=args.vehicle_count,
            iterations=args.iterations,
            realtime_frame_budget_ms=args.realtime_frame_budget_ms,
        )
    )
    passed = bool(
        report["target_tensorrt_runtime_validated"]
        and report["repeat_stability_validated"]
        and report["realtime_budget_passed"]
    )
    document = {
        "event": "reid_tensorrt_benchmark_completed",
        "schema_version": 1,
        "measured_at_unix_s": time.time(),
        "command_wall_time_s": time.perf_counter() - started_s,
        "passed": passed,
        **report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(document)
    return 0 if passed else 2


def _prepare_tracking_review(args: argparse.Namespace) -> int:
    report = prepare_tracking_review_bundle(
        args.predictions,
        args.source_video,
        args.source_video_manifest,
        args.output_directory,
        overwrite=args.overwrite,
    )
    document = tracking_review_bundle_document(
        report,
        predictions_path=args.predictions,
        source_video_path=args.source_video,
        source_video_manifest_path=args.source_video_manifest,
    )
    document["manifest_path"] = str(report.manifest_path)
    _emit(document)
    return 0


def _record_rtsp_evidence(args: argparse.Namespace) -> int:
    report = record_rtsp_evidence(
        RtspEvidenceRecordingConfig(
            source_env=args.source_env,
            session_id=args.session_id,
            output_video=args.out_video,
            manifest_out=args.manifest_out,
            duration_s=args.duration_seconds,
            latency_ms=args.latency_ms,
            finalize_timeout_s=args.finalize_timeout_seconds,
            overwrite=args.overwrite,
        )
    )
    _emit(rtsp_evidence_recording_document(report))
    return 0 if report.passed else 2


def _tracking_acceptance_failure_reasons(
    args: argparse.Namespace,
    report: IdentityTrackingEvaluationReport,
) -> list[str]:
    probability_thresholds = (
        ("minimum_idf1", args.minimum_idf1),
        ("minimum_occlusion_recovery_rate", args.minimum_occlusion_recovery_rate),
        ("minimum_out_of_frame_recovery_rate", args.minimum_out_of_frame_recovery_rate),
    )
    for name, value in probability_thresholds:
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError(f"{name} must be finite and in [0, 1]")
    if args.maximum_id_switch_count is not None and args.maximum_id_switch_count < 0:
        raise ValueError("maximum_id_switch_count must be non-negative")
    for name, value in (
        (
            "maximum_occlusion_recovery_p95_seconds",
            args.maximum_occlusion_recovery_p95_seconds,
        ),
        (
            "maximum_out_of_frame_recovery_p95_seconds",
            args.maximum_out_of_frame_recovery_p95_seconds,
        ),
    ):
        if value is not None and (not math.isfinite(value) or value < 0.0):
            raise ValueError(f"{name} must be finite and non-negative")

    failures: list[str] = []
    if args.minimum_idf1 is not None and (
        report.overall.idf1 is None or report.overall.idf1 < args.minimum_idf1
    ):
        failures.append("overall IDF1 is below the configured minimum")
    if (
        args.maximum_id_switch_count is not None
        and report.overall.id_switch_count > args.maximum_id_switch_count
    ):
        failures.append("identity switch count exceeds the configured maximum")
    for name, metrics, minimum_rate, maximum_p95 in (
        (
            "occlusion",
            report.occlusion_recovery,
            args.minimum_occlusion_recovery_rate,
            args.maximum_occlusion_recovery_p95_seconds,
        ),
        (
            "out-of-frame",
            report.out_of_frame_recovery,
            args.minimum_out_of_frame_recovery_rate,
            args.maximum_out_of_frame_recovery_p95_seconds,
        ),
    ):
        if minimum_rate is not None and (
            metrics.recovery_rate is None or metrics.recovery_rate < minimum_rate
        ):
            failures.append(f"{name} recovery rate is below the configured minimum")
        if maximum_p95 is not None and (
            metrics.recovery_latency_p95_s is None or metrics.recovery_latency_p95_s > maximum_p95
        ):
            failures.append(f"{name} recovery P95 exceeds the configured maximum")
    return failures


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
        minimum_processing_fps=args.minimum_processing_fps,
        maximum_inference_latency_p95_ms=args.maximum_inference_latency_p95_ms,
        maximum_capture_queue_high_watermark=args.maximum_capture_queue_high_watermark,
        maximum_memory_growth_mb=args.maximum_memory_growth_mb,
        memory_warmup_seconds=args.memory_warmup_seconds,
    )
    source = frame_source_from_config(capture_config)
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
        native_output_format=args.native_output_format,
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


def _run_semantic_model_manifest_init(args: argparse.Namespace) -> int:
    document = create_semantic_context_model_manifest(
        args.onnx_model,
        model_id=args.model_id,
        model_version=args.model_version,
        class_names=CITYSEMSEGFORMER_LABELS,
        input_width=args.input_width,
        input_height=args.input_height,
        output_name=args.output_name,
        source_description=args.source_description,
    )
    destination = write_candidate_model_manifest(
        args.out,
        document,
        overwrite=args.force,
    )
    _emit(
        {
            "event": "semantic_context_model_manifest_created",
            "manifest_path": str(destination.resolve()),
            "model_path": str(args.onnx_model.resolve()),
            "model_sha256": document["export"]["artifact_sha256"],
            "status": "quarantined",
            "model_role": "semantic_scene_context",
            "output_format": "categorical_H_W_1",
            "confidence_available": False,
            "production_approved": False,
            "advisory_only": True,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
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
        PixhawkReadOnlyConfig(
            endpoint=args.endpoint,
            baud=args.baud,
            expected_system_id=args.expected_system_id,
            expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS.get(args.expected_autopilot),
            expected_vehicle_type_id=PIXHAWK_VEHICLE_TYPE_IDS.get(args.expected_vehicle_type),
            require_operational_state=args.require_operational_state,
        )
    )
    snapshots = []
    fresh_transport_link_samples = 0
    try:
        for index in range(args.samples):
            now_s = time.monotonic()
            snapshot = provider.snapshot(now_s=now_s)
            snapshots.append(snapshot)
            transport_health = getattr(provider, "transport_link_healthy", None)
            if callable(transport_health):
                transport_fresh = transport_health(now_s=now_s)
            else:
                transport_fresh = snapshot.link_healthy
            fresh_transport_link_samples += transport_fresh is True
            if index + 1 < args.samples and args.interval_seconds > 0:
                time.sleep(args.interval_seconds)
    finally:
        provider.close()
    latest = snapshots[-1]
    fresh_link_samples = sum(snapshot.link_healthy is True for snapshot in snapshots)
    fresh_position_samples = sum(snapshot.position_healthy is True for snapshot in snapshots)
    identity = getattr(provider, "heartbeat_identity", None)
    identity_document = (
        identity.to_document()
        if identity is not None and callable(getattr(identity, "to_document", None))
        else None
    )
    qualification = getattr(provider, "qualification", None)
    qualification_document = (
        qualification.to_document()
        if qualification is not None and callable(getattr(qualification, "to_document", None))
        else {"required": False, "passed": None, "reasons": ()}
    )
    gate_failures: list[str] = []
    if args.require_fresh_link and fresh_transport_link_samples == 0:
        gate_failures.append("no fresh autopilot heartbeat was received")
    if args.require_fresh_position and fresh_position_samples == 0:
        gate_failures.append("no fresh global position was received")
    if qualification_document["required"] and qualification_document["passed"] is not True:
        gate_failures.extend(str(item) for item in qualification_document["reasons"])
    gate_passed = not gate_failures
    _emit(
        {
            "event": "pixhawk_read_only_check_finished",
            "endpoint": getattr(provider, "resolved_endpoint", None) or args.endpoint,
            "configured_endpoint": args.endpoint,
            "baud": args.baud,
            "sample_count": len(snapshots),
            "fresh_link_sample_count": fresh_link_samples,
            "fresh_transport_link_sample_count": fresh_transport_link_samples,
            "fresh_position_sample_count": fresh_position_samples,
            "heartbeat_identity": identity_document,
            "qualification": qualification_document,
            "requirements": {
                "fresh_link": args.require_fresh_link,
                "fresh_position": args.require_fresh_position,
                "expected_system_id": args.expected_system_id,
                "expected_autopilot": args.expected_autopilot,
                "expected_vehicle_type": args.expected_vehicle_type,
                "operational_state": args.require_operational_state,
            },
            "gate_passed": gate_passed,
            "gate_failures": gate_failures,
            "messages_received": getattr(provider, "messages_received", None),
            "rejected_system_messages": getattr(
                provider,
                "rejected_system_messages",
                None,
            ),
            "ignored_non_autopilot_heartbeats": getattr(
                provider,
                "ignored_non_autopilot_heartbeats",
                None,
            ),
            "message_type_counts": getattr(provider, "message_type_counts", None),
            "latest": _telemetry_document(latest),
            "read_only": provider.is_read_only,
            "messages_transmitted": provider.messages_transmitted,
            "hardware_control_enabled": False,
        }
    )
    return 0 if gate_passed else 1


def _run_pixhawk_param_backup(args: argparse.Namespace) -> int:
    if not args.acknowledge_active_read_request:
        raise ValueError(
            "pixhawk-param-backup requires --acknowledge-active-read-request; "
            "no MAVLink request was sent"
        )
    if args.out.exists() and not args.force:
        raise FileExistsError(f"Pixhawk parameter backup already exists: {args.out}")
    client = PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            endpoint=args.endpoint,
            parameter_encoding=args.parameter_encoding,
            active_read_request_acknowledged=args.acknowledge_active_read_request,
            baud=args.baud,
            target_system_id=args.target_system_id,
            target_component_id=args.target_component_id,
            timeout_seconds=args.timeout_seconds,
            idle_timeout_seconds=args.idle_timeout_seconds,
            minimum_parameters=args.minimum_parameters,
        )
    )
    try:
        snapshot = client.capture()
    finally:
        client.close()
    write_pixhawk_parameter_snapshot(args.out, snapshot, force=args.force)
    document = snapshot.to_document()
    document["output_path"] = str(args.out.resolve())
    _emit(document)
    return 0 if snapshot.passed else 1


def _run_pixhawk_param_verify(args: argparse.Namespace) -> int:
    if args.out is not None and args.out.exists() and not args.force:
        raise FileExistsError(f"Pixhawk parameter verification report already exists: {args.out}")
    snapshot = load_verified_pixhawk_parameter_snapshot(args.snapshot)
    document: dict[str, object] = {
        "schema_version": 1,
        "event": "pixhawk_parameter_backup_verified",
        "snapshot_path": str(args.snapshot.resolve()),
        "parameter_encoding": snapshot.parameter_encoding,
        "target_system_id": snapshot.target_system_id,
        "target_component_id": snapshot.target_component_id,
        "parameter_count": len(snapshot.parameters),
        "parameter_list_sha256": snapshot.parameter_list_sha256,
        "self_consistency_hash_verified": True,
        "cryptographically_authenticated": False,
        "complete": snapshot.complete,
        "passed": snapshot.passed,
        "messages_transmitted": 0,
        "hardware_control_enabled": False,
    }
    if args.out is not None:
        write_pixhawk_parameter_report(args.out, document, force=args.force)
        document["output_path"] = str(args.out.resolve())
    _emit(document)
    return 0


def _run_pixhawk_param_diff(args: argparse.Namespace) -> int:
    if args.out.exists() and not args.force:
        raise FileExistsError(f"Pixhawk parameter diff already exists: {args.out}")
    before = load_verified_pixhawk_parameter_snapshot(args.before)
    after = load_verified_pixhawk_parameter_snapshot(args.after)
    required_changes = frozenset(args.require_change)
    allowed_changes = frozenset((*args.allow_change, *required_changes))
    document = compare_pixhawk_parameter_snapshots(
        before,
        after,
        allowed_changes=allowed_changes,
        required_changes=required_changes,
    )
    document["before_path"] = str(args.before.resolve())
    document["after_path"] = str(args.after.resolve())
    write_pixhawk_parameter_diff(args.out, document, force=args.force)
    document["output_path"] = str(args.out.resolve())
    _emit(document)
    return 0 if document["gate_passed"] else 1


def _run_pixhawk_link_audit(args: argparse.Namespace) -> int:
    if args.out is not None and args.out.exists() and not args.force:
        raise FileExistsError(f"Pixhawk link audit already exists: {args.out}")
    snapshot = load_verified_pixhawk_parameter_snapshot(args.snapshot)
    document = audit_v6x_link_topology(
        snapshot,
        V6XLinkAuditExpectations(
            gr01_telem1_baud=args.gr01_telem1_baud,
            jetson_uart_telem2_baud=args.jetson_telem2_baud,
            ethernet_udp_port=args.ethernet_udp_port,
            require_uart_fallback=args.require_uart_fallback,
        ),
    )
    document["snapshot_path"] = str(args.snapshot.resolve())
    if args.out is not None:
        write_pixhawk_parameter_report(args.out, document, force=args.force)
        document["output_path"] = str(args.out.resolve())
    _emit(document)
    return 0 if document["gate_passed"] else 1


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
    if args.deduplication_capacity <= 0:
        raise ValueError("deduplication capacity must be positive")
    key = _required_alert_hmac_key_from_env(args.hmac_key_env)
    received_count = 0
    rejected_count = 0
    deduplication_store = (
        SqliteAlertDeduplicationStore(
            args.deduplication_db,
            capacity=args.deduplication_capacity,
        )
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
            deduplication_capacity=args.deduplication_capacity,
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
                    "deduplication_capacity": args.deduplication_capacity,
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
    if not 0 <= args.capture_queue_frames <= 256:
        raise ValueError("--capture-queue-frames must be between 0 and 256")
    if not 0 <= args.warmup_iterations <= 100:
        raise ValueError("--warmup-iterations must be between 0 and 100")
    if args.person_reid_onnx is not None and not args.unified_target_pool:
        raise ValueError("--person-reid-onnx requires --unified-target-pool")
    if args.person_reid_engine is not None and args.person_reid_onnx is None:
        raise ValueError("--person-reid-engine requires --person-reid-onnx")
    if (
        args.person_reid_onnx is not None
        and args.person_reid_engine is None
        and not args.allow_nonrealtime_reid
    ):
        raise ValueError(
            "live person ReID requires --person-reid-engine; "
            "use --allow-nonrealtime-reid only for explicit lab experiments"
        )
    if args.vehicle_reid_onnx is not None and not args.unified_target_pool:
        raise ValueError("--vehicle-reid-onnx requires --unified-target-pool")
    if args.vehicle_reid_engine is not None and args.vehicle_reid_onnx is None:
        raise ValueError("--vehicle-reid-engine requires --vehicle-reid-onnx")
    if (
        args.vehicle_reid_onnx is not None
        and args.vehicle_reid_engine is None
        and not args.allow_nonrealtime_reid
    ):
        raise ValueError(
            "live vehicle ReID requires --vehicle-reid-engine; "
            "use --allow-nonrealtime-reid only for explicit lab experiments"
        )
    if args.allow_nonrealtime_reid and not (
        (args.person_reid_onnx is not None and args.person_reid_engine is None)
        or (args.vehicle_reid_onnx is not None and args.vehicle_reid_engine is None)
    ):
        raise ValueError(
            "--allow-nonrealtime-reid requires a ReID ONNX without its TensorRT engine"
        )
    if args.patrol_advisory and not args.unified_target_pool:
        raise ValueError("--patrol-advisory requires --unified-target-pool")
    if args.short_term_tracking and not args.unified_target_pool:
        raise ValueError("--short-term-tracking requires --unified-target-pool")
    if args.identity_tracking_log_out is not None and not args.unified_target_pool:
        raise ValueError("--identity-tracking-log-out requires --unified-target-pool")
    if args.identity_tracking_log_out is not None and args.identity_tracking_session_id is None:
        raise ValueError("--identity-tracking-log-out requires --identity-tracking-session-id")
    if args.identity_tracking_session_id is not None and args.identity_tracking_log_out is None:
        raise ValueError("--identity-tracking-session-id requires --identity-tracking-log-out")
    if args.multimodal_ranging:
        if not args.unified_target_pool:
            raise ValueError("--multimodal-ranging requires --unified-target-pool")
        if not args.pixhawk_endpoint:
            raise ValueError("--multimodal-ranging requires --pixhawk-endpoint")
        if args.ranging_calibration is None:
            raise ValueError("--multimodal-ranging requires --ranging-calibration")
    elif args.ranging_calibration is not None:
        raise ValueError("--ranging-calibration requires --multimodal-ranging")
    if args.approach_hil:
        missing = tuple(
            option
            for enabled, option in (
                (args.operator_udp_port is not None, "--operator-udp-port"),
                (args.unified_target_pool, "--unified-target-pool"),
                (args.monocular_avoidance, "--monocular-avoidance"),
                (args.multimodal_ranging, "--multimodal-ranging"),
            )
            if not enabled
        )
        if missing:
            raise ValueError("--mode3-aim requires " + ", ".join(missing))
    if args.fixed_wing_aim_control:
        missing = tuple(
            option
            for enabled, option in (
                (args.approach_hil, "--mode3-aim"),
                (args.pixhawk_endpoint is not None, "--pixhawk-endpoint"),
            )
            if not enabled
        )
        if missing:
            raise ValueError("--fixed-wing-aim-control requires " + ", ".join(missing))
    if args.payload_target_hil:
        missing = tuple(
            option
            for enabled, option in (
                (args.operator_udp_port is not None, "--operator-udp-port"),
                (args.unified_target_pool, "--unified-target-pool"),
                (args.rgb_fire_verifier_model is not None, "--rgb-fire-verifier-model"),
            )
            if not enabled
        )
        if missing:
            raise ValueError("--payload-target-hil requires " + ", ".join(missing))
        if not config.deployment_capable:
            raise ValueError("--payload-target-hil requires a deployment-capable mission")
    if args.payload_target_hil and args.approach_hil:
        raise ValueError(
            "Mode-2 payload targeting and Mode-3 fixed-wing aiming are mutually exclusive"
        )
    if not 1 <= args.person_reid_maximum_batch_size <= 10:
        raise ValueError("--person-reid-maximum-batch-size must be between 1 and 10")
    if not 1 <= args.vehicle_reid_maximum_batch_size <= 10:
        raise ValueError("--vehicle-reid-maximum-batch-size must be between 1 and 10")
    if not 1 <= args.person_reid_frame_stride <= 30:
        raise ValueError("--person-reid-frame-stride must be between 1 and 30")
    if not 1 <= args.vehicle_reid_frame_stride <= 30:
        raise ValueError("--vehicle-reid-frame-stride must be between 1 and 30")
    if not math.isfinite(args.reid_maximum_interval_seconds) or not (
        0.01 <= args.reid_maximum_interval_seconds <= 2.0
    ):
        raise ValueError("--reid-maximum-interval-seconds must be between 0.01 and 2 seconds")
    if args.onnx_model.suffix.lower() in {".engine", ".plan"} and args.model_manifest is None:
        raise ValueError("TensorRT fire model requires a hash-bound --model-manifest")
    if args.rgb_fire_verifier_model is not None:
        if args.rgb_fire_verifier_model_manifest is None:
            raise ValueError(
                "--rgb-fire-verifier-model requires a hash-bound --rgb-fire-verifier-model-manifest"
            )
        if args.model_manifest is None:
            raise ValueError(
                "independent RGB fire corroboration requires a hash-bound primary --model-manifest"
            )
    if args.rgb_fire_verifier_model_manifest is not None and args.rgb_fire_verifier_model is None:
        raise ValueError("--rgb-fire-verifier-model-manifest requires --rgb-fire-verifier-model")
    if not math.isfinite(args.rgb_fire_verifier_confidence_threshold) or not (
        0.0 <= args.rgb_fire_verifier_confidence_threshold <= 1.0
    ):
        raise ValueError("--rgb-fire-verifier-confidence-threshold must be in [0, 1]")
    if not math.isfinite(args.rgb_fire_verifier_minimum_iou) or not (
        0.0 < args.rgb_fire_verifier_minimum_iou <= 1.0
    ):
        raise ValueError("--rgb-fire-verifier-minimum-iou must be in (0, 1]")
    if (
        args.safety_onnx_model is not None
        and args.safety_onnx_model.suffix.lower() in {".engine", ".plan"}
        and args.safety_model_manifest is None
    ):
        raise ValueError("TensorRT safety model requires a hash-bound --safety-model-manifest")
    if (
        args.priority_onnx_model is not None
        and args.priority_onnx_model.suffix.lower() in {".engine", ".plan"}
        and args.priority_model_manifest is None
    ):
        raise ValueError("TensorRT priority model requires a hash-bound --priority-model-manifest")
    if (
        args.environment_onnx_model is not None
        and args.environment_onnx_model.suffix.lower() in {".engine", ".plan"}
        and args.environment_model_manifest is None
    ):
        raise ValueError(
            "TensorRT environment model requires a hash-bound --environment-model-manifest"
        )
    zone_evidence_hmac_key: bytes | None = None
    payload_hil_request_key: bytes | None = None
    payload_hil_result_key: bytes | None = None
    payload_confirmation_key: bytes | None = None
    if args.safety_model_manifest is not None and args.safety_onnx_model is None:
        raise ValueError("--safety-model-manifest requires --safety-onnx-model")
    if args.priority_model_manifest is not None and args.priority_onnx_model is None:
        raise ValueError("--priority-model-manifest requires --priority-onnx-model")
    if args.environment_model_manifest is not None and args.environment_onnx_model is None:
        raise ValueError("--environment-model-manifest requires --environment-onnx-model")
    if (
        args.semantic_context_onnx_model is None
        and args.semantic_context_model_manifest is not None
    ):
        raise ValueError("--semantic-context-model-manifest requires --semantic-context-onnx-model")
    if (
        args.semantic_context_onnx_model is not None
        and args.semantic_context_model_manifest is None
    ):
        raise ValueError(
            "--semantic-context-onnx-model requires a hash-bound --semantic-context-model-manifest"
        )
    if (
        args.semantic_context_onnx_model is not None
        and args.semantic_context_onnx_model.suffix.lower() != ".onnx"
    ):
        raise ValueError("semantic context currently requires an ONNX artifact")
    if (args.semantic_context_engine is None) != (args.semantic_context_engine_provenance is None):
        raise ValueError(
            "--semantic-context-engine and --semantic-context-engine-provenance "
            "must be supplied together"
        )
    if args.semantic_context_engine is not None and args.semantic_context_onnx_model is None:
        raise ValueError("--semantic-context-engine requires --semantic-context-onnx-model")
    for name, value in (
        (
            "--semantic-context-minimum-interval-seconds",
            args.semantic_context_minimum_interval_seconds,
        ),
        (
            "--semantic-context-maximum-age-seconds",
            args.semantic_context_maximum_age_seconds,
        ),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and positive")
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
    if args.observe_pixhawk_lifecycle:
        required_qualification_options = {
            "--pixhawk-system-id": args.pixhawk_system_id,
            "--pixhawk-expected-autopilot": args.pixhawk_expected_autopilot,
            "--pixhawk-expected-vehicle-type": args.pixhawk_expected_vehicle_type,
        }
        missing_qualification_options = [
            name for name, value in required_qualification_options.items() if value is None
        ]
        if not args.require_pixhawk_operational_state:
            missing_qualification_options.append("--require-pixhawk-operational-state")
        if missing_qualification_options:
            raise ValueError(
                "--observe-pixhawk-lifecycle requires " + ", ".join(missing_qualification_options)
            )
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
    verified_rgb_fire_verifier: VerifiedModelArtifact | None = None
    rgb_fire_verifier_class_names: tuple[str, ...] | None = None
    if args.rgb_fire_verifier_model is not None:
        rgb_fire_verifier_class_names = _parse_class_names(args.rgb_fire_verifier_class_names)
        verified_rgb_fire_verifier = _verify_optional_model_manifest(
            manifest_path=args.rgb_fire_verifier_model_manifest,
            model_path=args.rgb_fire_verifier_model,
            class_names=rgb_fire_verifier_class_names,
            output_coordinates=(
                args.rgb_fire_verifier_output_coordinates or args.output_coordinates
            ),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="fire_verifier",
        )
        if verified_fire_model is None or verified_rgb_fire_verifier is None:
            raise RuntimeError("independent RGB fire model manifests were not verified")
        if verified_fire_model.artifact_sha256 == verified_rgb_fire_verifier.artifact_sha256:
            raise ValueError(
                "primary fire detector and independent RGB verifier must use different artifacts"
            )
        verifier_labels = {
            "flame" if label.strip().lower() == "fire" else label.strip().lower()
            for label in rgb_fire_verifier_class_names
        }
        if config.require_independent_rgb_corroboration and not (
            verifier_labels.intersection(config.target_classes)
        ):
            raise ValueError(
                "independent RGB verifier does not cover any configured fire target class"
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
            expected_native_output_format=(
                "ultralytics_raw_xywh_class_scores"
                if args.safety_model_format == "ultralytics_raw"
                else "post_nms_N_x_6"
            ),
        )
    verified_priority_model: VerifiedModelArtifact | None = None
    priority_class_names: tuple[str, ...] | None = None
    priority_label_map: dict[str, str] = {}
    priority_label_confidence_overrides: dict[str, float] = {}
    if args.priority_onnx_model is not None:
        priority_class_names = _parse_class_names(args.priority_class_names)
        priority_label_map = _parse_label_map(args.priority_label_map)
        unknown_sources = set(priority_label_map).difference(
            label.strip().lower() for label in priority_class_names
        )
        if unknown_sources:
            raise ValueError(
                "priority label map contains unknown source classes: "
                + ", ".join(sorted(unknown_sources))
            )
        priority_label_confidence_overrides = _parse_label_confidence_thresholds(
            args.priority_label_confidence_thresholds
        )
        unknown_override_labels = set(priority_label_confidence_overrides).difference(
            label.strip().lower() for label in priority_class_names
        )
        if unknown_override_labels:
            raise ValueError(
                "priority label confidence overrides contain unknown source classes: "
                + ", ".join(sorted(unknown_override_labels))
            )
        verified_priority_model = _verify_optional_model_manifest(
            manifest_path=args.priority_model_manifest,
            model_path=args.priority_onnx_model,
            class_names=priority_class_names,
            output_coordinates="letterbox_xyxy_px",
            require_production_approved=args.require_production_approved_models,
            expected_model_role="safety_object_evidence",
            expected_native_output_format="ultralytics_raw_xywh_class_scores",
        )
    verified_environment_model: VerifiedModelArtifact | None = None
    environment_class_names: tuple[str, ...] | None = None
    if args.environment_onnx_model is not None:
        environment_class_names = _parse_class_names(args.environment_class_names)
        reserved_environment_labels = {
            "fire",
            "flame",
            "smoke",
            "hotspot",
            "person",
            "firefighter",
            "car",
            "bus",
            "truck",
            "vehicle",
        }
        conflicting = reserved_environment_labels.intersection(environment_class_names)
        if conflicting:
            raise ValueError(
                "environment model labels overlap protected fire/person/vehicle domains: "
                + ", ".join(sorted(conflicting))
            )
        verified_environment_model = _verify_optional_model_manifest(
            manifest_path=args.environment_model_manifest,
            model_path=args.environment_onnx_model,
            class_names=environment_class_names,
            output_coordinates=(args.environment_output_coordinates or args.output_coordinates),
            require_production_approved=args.require_production_approved_models,
            expected_model_role="environment_risk_evidence",
        )
    verified_semantic_context_model: VerifiedModelArtifact | None = None
    if args.semantic_context_onnx_model is not None:
        verified_semantic_context_model = verify_model_manifest(
            args.semantic_context_model_manifest,
            args.semantic_context_onnx_model,
            expected_class_names=CITYSEMSEGFORMER_LABELS,
            expected_model_role="semantic_scene_context",
            expected_output_format="categorical_H_W_1",
            require_production_approved=args.require_production_approved_models,
        )
        if args.semantic_context_engine is not None:
            verify_engine_provenance(
                provenance=args.semantic_context_engine_provenance,
                engine=args.semantic_context_engine,
                source_model=args.semantic_context_onnx_model,
                trtexec=args.semantic_context_trtexec,
            )
    synthetic_models = tuple(
        artifact
        for artifact in (
            verified_fire_model,
            verified_rgb_fire_verifier,
            verified_safety_model,
            verified_priority_model,
            verified_environment_model,
            verified_semantic_context_model,
        )
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
    primary_detector = FrameCadencedDetector(
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
        ),
        frame_stride=args.primary_model_frame_stride,
        frame_phase=args.primary_model_frame_phase,
    )
    detectors = [primary_detector]
    rgb_fire_verifier = None
    rgb_fire_corroborator = None
    rgb_fire_verifier_evidence_qualified = False
    if args.rgb_fire_verifier_model is not None:
        if (
            rgb_fire_verifier_class_names is None
            or verified_fire_model is None
            or verified_rgb_fire_verifier is None
        ):
            raise RuntimeError("independent RGB fire verifier was not initialized")
        rgb_fire_verifier = OnnxNx6Detector(
            OnnxNx6Config(
                model_path=args.rgb_fire_verifier_model,
                class_names=rgb_fire_verifier_class_names,
                input_width=args.input_width,
                input_height=args.input_height,
                confidence_threshold=args.rgb_fire_verifier_confidence_threshold,
                providers=providers,
                trt_engine_cache_path=args.trt_engine_cache,
                output_coordinates=(
                    args.rgb_fire_verifier_output_coordinates or args.output_coordinates
                ),
                model_version=verified_rgb_fire_verifier.model_version,
            )
        )
        rgb_fire_verifier.warmup(iterations=args.warmup_iterations)
        rgb_fire_verifier_evidence_qualified = bool(
            (
                verified_fire_model.production_approved
                and verified_rgb_fire_verifier.production_approved
            )
            or (
                args.allow_synthetic_hil_model
                and verified_fire_model.synthetic_hil_only
                and verified_rgb_fire_verifier.synthetic_hil_only
            )
        )
        rgb_fire_corroborator = IndependentRgbFireCorroborator(
            IndependentRgbFireCorroborationConfig(
                minimum_iou=args.rgb_fire_verifier_minimum_iou,
                minimum_verifier_confidence=(args.rgb_fire_verifier_confidence_threshold),
                evidence_qualified=rgb_fire_verifier_evidence_qualified,
                primary_artifact_sha256=verified_fire_model.artifact_sha256,
                verifier_artifact_sha256=(verified_rgb_fire_verifier.artifact_sha256),
            )
        )
    if args.safety_onnx_model is not None:
        if safety_class_names is None:
            raise RuntimeError("safety model class names were not initialized")
        priority_detection_labels = frozenset(
            label.strip().lower() for label in _parse_class_names(args.safety_tile_labels)
        )
        safety_candidate_confidence = min(
            args.safety_confidence_threshold,
            args.safety_priority_confidence_threshold,
            args.safety_fallback_confidence_threshold,
            args.safety_tile_confidence_threshold,
        )
        if args.safety_model_format == "ultralytics_raw":
            safety_detector = OnnxRawYoloDetector(
                OnnxRawYoloConfig(
                    model_path=args.safety_onnx_model,
                    class_names=safety_class_names,
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=safety_candidate_confidence,
                    iou_threshold=args.safety_model_iou_threshold,
                    maximum_detections=args.safety_model_maximum_detections,
                    providers=providers,
                    trt_engine_cache_path=args.trt_engine_cache,
                    model_version=(
                        verified_safety_model.model_version
                        if verified_safety_model is not None
                        else None
                    ),
                )
            )
        else:
            safety_detector = OnnxNx6Detector(
                OnnxNx6Config(
                    model_path=args.safety_onnx_model,
                    class_names=safety_class_names,
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=safety_candidate_confidence,
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
        if args.safety_tile_columns * args.safety_tile_rows > 1:
            safety_detector = TiledDetectionFusion(
                safety_detector,
                TiledDetectionConfig(
                    columns=args.safety_tile_columns,
                    rows=args.safety_tile_rows,
                    overlap_fraction=args.safety_tile_overlap,
                    scan_interval_frames=args.safety_tile_scan_interval_frames,
                    fusion_iou_threshold=args.safety_tile_fusion_iou_threshold,
                    tile_confidence_threshold=args.safety_tile_confidence_threshold,
                    tile_confidence_by_label=_parse_label_confidence_thresholds(
                        args.safety_tile_label_confidence_thresholds
                    ),
                    tile_labels=priority_detection_labels,
                    maximum_tile_box_area=args.safety_tile_maximum_box_area,
                    maximum_detections=args.safety_model_maximum_detections,
                ),
            )
        # The common COCO detector also emits vehicle classes. Apply the same
        # vehicle gate here as on the priority detector so a low-confidence shoe,
        # chair leg, or reflection cannot bypass the priority-model threshold.
        safety_detector = TemporalDetectionFilter(
            safety_detector,
            labels=VEHICLE_DETECTION_CLASS_NAMES,
            minimum_consecutive_frames=args.priority_vehicle_stability_frames,
            iou_threshold=0.25,
            maximum_missed_frames=1,
            label_aliases=VEHICLE_TEMPORAL_LABEL_ALIASES,
        )
        safety_detector = FrameCadencedDetector(
            safety_detector,
            frame_stride=args.safety_model_frame_stride,
            frame_phase=args.safety_model_frame_phase,
        )
        detectors.append(safety_detector)
    if args.priority_onnx_model is not None:
        if priority_class_names is None:
            raise RuntimeError("priority model class names were not initialized")
        priority_source_thresholds = {
            "pedestrian": args.priority_person_confidence_threshold,
            "people": args.priority_person_confidence_threshold,
            "bicycle": args.priority_vehicle_confidence_threshold,
            "car": args.priority_vehicle_confidence_threshold,
            "van": args.priority_vehicle_confidence_threshold,
            "truck": args.priority_vehicle_confidence_threshold,
            "tricycle": args.priority_vehicle_confidence_threshold,
            "awning-tricycle": args.priority_vehicle_confidence_threshold,
            "bus": args.priority_vehicle_confidence_threshold,
            "motor": args.priority_vehicle_confidence_threshold,
        }
        priority_source_thresholds.update(priority_label_confidence_overrides)
        priority_candidate_confidence = min(
            args.priority_confidence_threshold,
            args.priority_person_confidence_threshold,
            args.priority_vehicle_confidence_threshold,
            *priority_label_confidence_overrides.values(),
        )
        priority_detector = LabelRemapDetector(
            ClassConfidenceFilter(
                OnnxRawYoloDetector(
                    OnnxRawYoloConfig(
                        model_path=args.priority_onnx_model,
                        class_names=priority_class_names,
                        input_width=args.priority_input_width,
                        input_height=args.priority_input_height,
                        confidence_threshold=priority_candidate_confidence,
                        iou_threshold=args.priority_model_iou_threshold,
                        maximum_detections=args.priority_model_maximum_detections,
                        providers=providers,
                        trt_engine_cache_path=args.trt_engine_cache,
                        model_version=(
                            verified_priority_model.model_version
                            if verified_priority_model is not None
                            else None
                        ),
                    ),
                ),
                priority_source_thresholds,
                default_threshold=args.priority_confidence_threshold,
            ),
            priority_label_map,
            fusion_iou_threshold=args.priority_model_iou_threshold,
        )
        priority_detector = TemporalDetectionFilter(
            priority_detector,
            labels=frozenset({"bicycle", "car", "motorcycle", "bus", "truck"}),
            minimum_consecutive_frames=args.priority_vehicle_stability_frames,
            iou_threshold=0.25,
            maximum_missed_frames=1,
            label_aliases=VEHICLE_TEMPORAL_LABEL_ALIASES,
        )
        priority_detector = FrameCadencedDetector(
            priority_detector,
            frame_stride=args.priority_model_frame_stride,
            frame_phase=args.priority_model_frame_phase,
        )
        detectors.append(priority_detector)
    if args.environment_onnx_model is not None:
        if environment_class_names is None:
            raise RuntimeError("environment model class names were not initialized")
        detectors.append(
            OnnxNx6Detector(
                OnnxNx6Config(
                    model_path=args.environment_onnx_model,
                    class_names=environment_class_names,
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=args.environment_confidence_threshold,
                    providers=providers,
                    trt_engine_cache_path=args.trt_engine_cache,
                    output_coordinates=(
                        args.environment_output_coordinates or args.output_coordinates
                    ),
                    model_version=(
                        verified_environment_model.model_version
                        if verified_environment_model is not None
                        else None
                    ),
                )
            )
        )
    for model_detector in detectors:
        model_detector.warmup(iterations=args.warmup_iterations)
    class_thresholds = {
        "fire": args.flame_confidence_threshold,
        "flame": args.flame_confidence_threshold,
        "smoke": args.smoke_confidence_threshold,
    }
    if safety_class_names is not None:
        priority_detection_labels = frozenset(
            label.strip().lower() for label in _parse_class_names(args.safety_tile_labels)
        )
        for label in safety_class_names:
            normalized_label = label.strip().lower()
            threshold = (
                max(
                    args.safety_priority_confidence_threshold,
                    args.priority_vehicle_confidence_threshold,
                )
                if normalized_label in VEHICLE_DETECTION_CLASS_NAMES
                else args.safety_priority_confidence_threshold
                if normalized_label in priority_detection_labels
                else args.safety_fallback_confidence_threshold
            )
            class_thresholds.setdefault(normalized_label, threshold)
    if environment_class_names is not None:
        class_thresholds.update(
            {
                label.strip().lower(): args.environment_confidence_threshold
                for label in environment_class_names
            }
        )
    if priority_class_names is not None:
        for label in priority_class_names:
            source = label.strip().lower()
            destination = priority_label_map.get(source, source)
            class_thresholds.setdefault(destination, args.priority_confidence_threshold)
    detector_ensemble: Any = DetectorEnsemble(
        detectors,
        force_locked_cadence=args.lock_model_force_every_frame,
    )
    if args.safety_onnx_model is not None and args.priority_onnx_model is not None:
        detector_ensemble = MultiSourceConfidenceFilter(
            detector_ensemble,
            labels=frozenset({"car"}),
            iou_threshold=args.priority_model_iou_threshold,
            single_source_confidence=args.car_single_source_confidence_threshold,
        )
    fused_ensemble = SameLabelDetectionFusion(
        detector_ensemble,
        iou_threshold=args.priority_model_iou_threshold,
        maximum_detections=max(
            args.safety_model_maximum_detections,
            args.priority_model_maximum_detections,
        ),
    )
    detector: Any = ClassConfidenceFilter(
        fused_ensemble,
        class_thresholds,
        default_threshold=None,
    )
    detector = VehicleFurnitureOverlapVetoFilter(detector)
    automatic_candidate_labels = (
        frozenset(
            label.strip().lower()
            for label in (
                *FIRE_CANDIDATE_TRACK_LABELS,
                *PRIORITY_DETECTION_CLASS_NAMES,
                *ENVIRONMENT_RISK_CLASS_NAMES,
                *config.target_classes,
                *config.person_labels,
            )
            if label.strip()
        )
        - NON_SELECTABLE_AUTOMATIC_LABELS
    )
    detector = LabelAllowListFilter(detector, labels=automatic_candidate_labels)
    detector = BrightNeutralLightVetoFilter(
        detector,
        minimum_bright_warm_fraction=args.fire_minimum_bright_warm_fraction,
    )
    if args.safety_onnx_model is not None:
        detector = PersonOverlapVetoFilter(
            detector,
            minimum_fire_coverage=args.person_veto_fire_coverage,
        )
    detector = TemporalDetectionFilter(
        detector,
        labels=FIRE_CANDIDATE_TRACK_LABELS,
        minimum_consecutive_frames=args.candidate_stability_frames,
        # The legacy fire model alternates between `fire` and `flame`; contour
        # flicker also makes strict box IoU unstable on a real RGB stream.
        label_aliases={"fire": "flame"},
        maximum_center_distance=0.10,
        minimum_area_ratio=0.12,
        # The primary detector can deliberately skip frames in the Jetson live
        # profile. Preserve temporal fire evidence through those bounded gaps;
        # only fresh inference frames increase the consecutive-hit count.
        maximum_missed_frames=max(1, args.primary_model_frame_stride),
    )
    person_safety_model_coverage = detector.covers_labels(config.person_labels)
    person_safety_evidence_qualified = (
        person_safety_model_coverage and verified_safety_model is not None
    )
    environment_model_coverage = detector.covers_labels(ENVIRONMENT_RISK_CLASS_NAMES)
    environment_evidence_qualified = (
        environment_model_coverage and verified_environment_model is not None
    )
    pixhawk_config = (
        PixhawkReadOnlyConfig(
            endpoint=args.pixhawk_endpoint,
            baud=args.pixhawk_baud,
            expected_system_id=(
                args.pixhawk_system_id
                if args.pixhawk_system_id is not None
                else 1
                if args.fixed_wing_aim_control
                else None
            ),
            expected_autopilot_id=(
                PIXHAWK_AUTOPILOT_IDS.get(args.pixhawk_expected_autopilot)
                if args.pixhawk_expected_autopilot is not None
                else PIXHAWK_AUTOPILOT_IDS["px4"]
                if args.fixed_wing_aim_control
                else None
            ),
            expected_vehicle_type_id=(
                PIXHAWK_VEHICLE_TYPE_IDS.get(args.pixhawk_expected_vehicle_type)
                if args.pixhawk_expected_vehicle_type is not None
                else (
                    PIXHAWK_VEHICLE_TYPE_IDS["fixed_wing"] if args.fixed_wing_aim_control else None
                )
            ),
            require_operational_state=(
                args.require_pixhawk_operational_state or args.fixed_wing_aim_control
            ),
        )
        if args.pixhawk_endpoint
        else None
    )
    pixhawk_telemetry = (
        PixhawkFlightControlProvider(
            PixhawkFlightControlConfig(
                pixhawk_config,
                rc_input_rate_hz=args.aim_rc_input_rate_hz,
            )
        )
        if args.fixed_wing_aim_control and pixhawk_config is not None
        else PixhawkReadOnlyTelemetryProvider(pixhawk_config)
        if pixhawk_config is not None
        else None
    )
    telemetry = pixhawk_telemetry or FailClosedTelemetryProvider()
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
    identity_prediction_writer = (
        JsonlIdentityPredictionWriter(
            args.identity_tracking_log_out,
            session_id=args.identity_tracking_session_id,
        )
        if args.identity_tracking_log_out is not None
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
                    frozenset(config.target_classes)
                    | FIRE_CANDIDATE_TRACK_LABELS
                    | frozenset(PRIORITY_DETECTION_CLASS_NAMES),
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
    capture_source = frame_source_from_config(_capture_config_from_args(args))
    frame_source = (
        BufferedFrameSource(capture_source, capacity=args.capture_queue_frames)
        if args.capture_queue_frames > 0
        else capture_source
    )
    monocular_avoidance = (
        OpenCVSparseFlowAvoidance(
            MonocularAvoidanceConfig(
                minimum_feature_count=args.avoidance_minimum_features,
                caution_ttc_s=args.avoidance_caution_ttc_seconds,
                avoid_ttc_s=args.avoidance_avoid_ttc_seconds,
                maximum_data_age_s=args.avoidance_maximum_data_age_seconds,
                analysis_width=args.avoidance_analysis_width,
            )
        )
        if args.monocular_avoidance
        else None
    )
    semantic_context_runner = None
    if args.semantic_context_onnx_model is not None:
        semantic_context_session = (
            TensorRtSemanticSession(args.semantic_context_engine)
            if args.semantic_context_engine is not None
            else None
        )
        try:
            semantic_context_model = OnnxCategoricalSemanticContext(
                OnnxSemanticContextConfig(
                    args.semantic_context_onnx_model,
                    providers=providers,
                ),
                session=semantic_context_session,
            )
            semantic_context_model.warmup()
        except BaseException:
            if semantic_context_session is not None:
                semantic_context_session.close()
            raise
        semantic_context_runner = AsyncSemanticContextRunner(
            semantic_context_model,
            minimum_interval_s=args.semantic_context_minimum_interval_seconds,
        )
    unified_target_pool = (
        UnifiedTargetPool(
            UnifiedTargetPoolConfig(
                maximum_tracks=args.unified_target_pool_maximum_tracks,
                locked_reacquisition_timeout_s=(
                    args.unified_target_pool_locked_reacquisition_seconds
                ),
                minimum_association_confidence=(
                    args.unified_target_pool_minimum_association_confidence
                ),
                priority_minimum_new_track_confidence=(
                    args.unified_target_pool_priority_minimum_new_track_confidence
                ),
                minimum_new_track_confidence=(
                    args.unified_target_pool_minimum_new_track_confidence
                ),
                high_confidence_threshold=(args.unified_target_pool_high_confidence_threshold),
                person_maximum_appearance_distance=(
                    args.unified_target_pool_person_maximum_appearance_distance
                ),
                person_strict_reid_distance=(args.unified_target_pool_person_strict_reid_distance),
                kalman_process_noise=args.unified_target_pool_kalman_process_noise,
                kalman_measurement_noise=(args.unified_target_pool_kalman_measurement_noise),
                kalman_gate_sigma=args.unified_target_pool_kalman_gate_sigma,
                kalman_max_prediction_horizon_s=(
                    args.unified_target_pool_kalman_maximum_horizon_seconds
                ),
            )
        )
        if args.unified_target_pool
        else None
    )
    # Aircraft labels have no deployed ONNX ReID model.  The compact descriptor
    # fills only that disjoint class domain and is used for LCK/LOST identity
    # recovery without taking a GPU inference slot from the primary detector.
    aircraft_appearance_encoder = (
        HandcraftedAircraftAppearanceEncoder() if unified_target_pool is not None else None
    )
    ranging_engine = MultiModalRangingEngine() if args.multimodal_ranging else None
    ranging_config = None
    ranging_calibration_sha256 = None
    if args.multimodal_ranging:
        if args.ranging_calibration is None:  # Defensive guard for direct Namespace callers.
            raise ValueError("--multimodal-ranging requires --ranging-calibration")
        ranging_calibration = load_camera_calibration(args.ranging_calibration)
        ranging_calibration_sha256 = _sha256_file(args.ranging_calibration)
        ranging_config = LiveRangingConfig(
            calibration=ranging_calibration,
            altitude_agl_sigma_m=args.ranging_agl_sigma_m,
            roll_sigma_deg=args.ranging_roll_sigma_deg,
            pitch_sigma_deg=args.ranging_pitch_sigma_deg,
            heading_sigma_deg=args.ranging_heading_sigma_deg,
            target_center_sigma_px=args.ranging_target_center_sigma_px,
        )
    short_term_tracker = (
        OpenCVShortTermTargetTracker(
            ShortTermTrackingConfig(
                analysis_width=args.short_term_analysis_width,
                maximum_tracks=args.short_term_maximum_tracks,
                minimum_flow_points=args.short_term_minimum_flow_points,
                minimum_box_size_px=args.short_term_minimum_box_size_px,
                frame_stride=args.short_term_frame_stride,
                template_minimum_correlation=(args.short_term_template_minimum_correlation),
                search_expansion=args.short_term_search_expansion,
                occluded_search_multiplier=(args.short_term_occluded_search_multiplier),
                reacquiring_search_multiplier=(args.short_term_reacquiring_search_multiplier),
                maximum_search_expansion=args.short_term_maximum_search_expansion,
                maximum_retained_template_age_s=(
                    args.short_term_maximum_retained_template_age_seconds
                ),
            )
        )
        if args.short_term_tracking
        else None
    )
    selection_target_pool = (
        UnifiedSelectionTargetPool(unified_target_pool)
        if unified_target_pool is not None and operator_bridge is not None
        else None
    )
    approach_hil_coordinator = (
        LiveApproachHilCoordinator(
            controller=ApproachHilController(),
            calibration=ranging_config.calibration,
            flight_control_enabled=args.fixed_wing_aim_control,
        )
        if args.approach_hil and ranging_config is not None
        else None
    )
    fixed_wing_aim_executor = (
        FixedWingAimExecutor(
            FixedWingAimController(
                ranging_config.calibration,
                FixedWingAimConfig(
                    maximum_target_age_s=args.aim_maximum_target_age_seconds,
                    maximum_attitude_age_s=args.aim_maximum_attitude_age_seconds,
                    minimum_airspeed_mps=args.aim_minimum_airspeed_mps,
                    minimum_altitude_agl_m=args.aim_minimum_altitude_agl_m,
                    maximum_abs_roll_deg=args.aim_maximum_abs_roll_deg,
                    maximum_abs_pitch_deg=args.aim_maximum_abs_pitch_deg,
                    maximum_roll_correction_deg=args.aim_maximum_roll_correction_deg,
                    maximum_pitch_correction_deg=args.aim_maximum_pitch_correction_deg,
                    roll_gain=args.aim_roll_gain,
                    pitch_gain=args.aim_pitch_gain,
                    maximum_roll_slew_deg_s=args.aim_maximum_roll_slew_deg_s,
                    maximum_pitch_slew_deg_s=args.aim_maximum_pitch_slew_deg_s,
                    prestream_setpoints=args.aim_prestream_setpoints,
                    control_mode=args.aim_control_mode,
                    return_mode=args.aim_return_mode,
                    rc_input_maximum_age_s=args.aim_rc_input_maximum_age_seconds,
                    rc_cancel_threshold_us=args.aim_rc_cancel_threshold_us,
                ),
            ),
            pixhawk_telemetry,
        )
        if (
            args.fixed_wing_aim_control
            and ranging_config is not None
            and isinstance(pixhawk_telemetry, PixhawkFlightControlProvider)
        )
        else None
    )
    payload_target_coordinator = LivePayloadTargetCoordinator() if args.payload_target_hil else None
    patrol_advisory_engine = (
        PatrolAdvisoryEngine(
            PatrolAdvisoryConfig(
                maximum_bank_angle_deg=args.patrol_maximum_bank_angle_deg,
                minimum_ground_speed_mps=args.patrol_minimum_ground_speed_mps,
                maximum_evidence_age_s=args.patrol_maximum_evidence_age_seconds,
            )
        )
        if args.patrol_advisory
        else None
    )
    person_reid_session = None
    person_reid_encoder = None
    if args.person_reid_onnx is not None:
        if _sha256_file(args.person_reid_onnx) != NVIDIA_TAO_REID_V1_2_SHA256:
            raise ValueError("person ReID ONNX artifact does not match the pinned NVIDIA hash")
        try:
            if args.person_reid_engine is not None:
                person_reid_session = TensorRtEmbeddingSession(
                    args.person_reid_engine,
                    maximum_batch_size=args.person_reid_maximum_batch_size,
                )
            person_reid_encoder = OnnxPersonReIdEncoder(
                OnnxPersonReIdConfig(
                    model_path=args.person_reid_onnx,
                    maximum_batch_size=args.person_reid_maximum_batch_size,
                    providers=providers,
                ),
                session=person_reid_session,
            )
            person_reid_encoder.warmup(batch_size=1)
        except BaseException:
            if person_reid_session is not None:
                person_reid_session.close()
            raise
    vehicle_reid_session = None
    vehicle_reid_encoder = None
    if args.vehicle_reid_onnx is not None:
        if _sha384_file(args.vehicle_reid_onnx) != OPENVINO_VEHICLE_REID_0001_SHA384:
            if person_reid_session is not None:
                person_reid_session.close()
            raise ValueError("vehicle ReID ONNX artifact does not match the pinned OpenVINO hash")
        try:
            if args.vehicle_reid_engine is not None:
                vehicle_reid_session = TensorRtEmbeddingSession(
                    args.vehicle_reid_engine,
                    maximum_batch_size=args.vehicle_reid_maximum_batch_size,
                    input_height=208,
                    input_width=208,
                    feature_size=512,
                )
            vehicle_reid_encoder = OnnxVehicleReIdEncoder(
                OnnxVehicleReIdConfig(
                    model_path=args.vehicle_reid_onnx,
                    maximum_batch_size=args.vehicle_reid_maximum_batch_size,
                    providers=providers,
                ),
                session=vehicle_reid_session,
            )
            vehicle_reid_encoder.warmup(batch_size=1)
        except BaseException:
            if vehicle_reid_session is not None:
                vehicle_reid_session.close()
            if person_reid_session is not None:
                person_reid_session.close()
            raise
    runner = LiveMissionRunner(
        mission=controller,
        frame_source=frame_source,
        detector=detector,
        telemetry_provider=telemetry,
        alert_publisher=alert_publisher,
        alert_outbox=alert_outbox,
        prediction_writer=prediction_writer,
        identity_prediction_writer=identity_prediction_writer,
        operator_bridge=operator_bridge,
        payload_hil_cycle=payload_hil_cycle,
        monocular_avoidance=monocular_avoidance,
        unified_target_pool=unified_target_pool,
        person_reid_encoder=person_reid_encoder,
        vehicle_reid_encoder=vehicle_reid_encoder,
        aircraft_appearance_encoder=aircraft_appearance_encoder,
        patrol_advisory_engine=patrol_advisory_engine,
        short_term_tracker=short_term_tracker,
        selection_target_pool=selection_target_pool,
        ranging_engine=ranging_engine,
        ranging_config=ranging_config,
        approach_hil_coordinator=approach_hil_coordinator,
        fixed_wing_aim_executor=fixed_wing_aim_executor,
        payload_target_coordinator=payload_target_coordinator,
        semantic_context_runner=semantic_context_runner,
        rgb_fire_verifier=rgb_fire_verifier,
        rgb_fire_corroborator=rgb_fire_corroborator,
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
            semantic_context_maximum_age_s=args.semantic_context_maximum_age_seconds,
            person_reid_frame_stride=args.person_reid_frame_stride,
            vehicle_reid_frame_stride=args.vehicle_reid_frame_stride,
            reid_maximum_interval_s=args.reid_maximum_interval_seconds,
        ),
    )
    _emit(
        {
            "event": "live_camera_started",
            "model_providers": [provider for item in detectors for provider in item.provider_names],
            "pixhawk_read_only": bool(args.pixhawk_endpoint) and not args.fixed_wing_aim_control,
            "fixed_wing_aim_control_enabled": args.fixed_wing_aim_control,
            "fixed_camera_observation": selection_target_pool is not None,
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
            "rgb_fire_verifier_configured": rgb_fire_verifier is not None,
            "rgb_fire_verifier_manifest_validated": (verified_rgb_fire_verifier is not None),
            "rgb_fire_verifier_model_role": (
                verified_rgb_fire_verifier.model_role
                if verified_rgb_fire_verifier is not None
                else "not_configured"
            ),
            "rgb_fire_verifier_production_approved": (
                verified_rgb_fire_verifier.production_approved
                if verified_rgb_fire_verifier is not None
                else False
            ),
            "rgb_fire_verifier_evidence_qualified": (rgb_fire_verifier_evidence_qualified),
            "rgb_fire_verifier_required_by_mission": (config.require_independent_rgb_corroboration),
            "rgb_fire_verifier_minimum_iou": args.rgb_fire_verifier_minimum_iou,
            "rgb_fire_verifier_output_creates_targets": False,
            "rgb_fire_verifier_flight_control_enabled": False,
            "rgb_fire_verifier_physical_release_enabled": False,
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
            "environment_model_manifest_validated": verified_environment_model is not None,
            "environment_model_role": (
                verified_environment_model.model_role
                if verified_environment_model is not None
                else "unverified"
            ),
            "environment_model_production_approved": (
                verified_environment_model.production_approved
                if verified_environment_model is not None
                else False
            ),
            "environment_model_required_labels": list(ENVIRONMENT_RISK_CLASS_NAMES),
            "environment_model_coverage": environment_model_coverage,
            "environment_evidence_qualified": environment_evidence_qualified,
            "environment_model_flight_control_enabled": False,
            "environment_model_physical_release_enabled": False,
            "semantic_context_enabled": semantic_context_runner is not None,
            "semantic_context_model_manifest_validated": (
                verified_semantic_context_model is not None
            ),
            "semantic_context_model_role": (
                verified_semantic_context_model.model_role
                if verified_semantic_context_model is not None
                else "unverified"
            ),
            "semantic_context_model_production_approved": (
                verified_semantic_context_model.production_approved
                if verified_semantic_context_model is not None
                else False
            ),
            "semantic_context_model_providers": (
                list(semantic_context_model.provider_names)
                if semantic_context_runner is not None
                else []
            ),
            "semantic_context_tensorrt_engine_enabled": (args.semantic_context_engine is not None),
            "semantic_context_engine_provenance_validated": (
                args.semantic_context_engine_provenance is not None
            ),
            "semantic_context_minimum_interval_s": (args.semantic_context_minimum_interval_seconds),
            "semantic_context_maximum_age_s": args.semantic_context_maximum_age_seconds,
            "semantic_context_queue_capacity": 1,
            "semantic_context_confidence_available": False,
            "semantic_context_target_pool_identity_authority": False,
            "semantic_context_advisory_only": True,
            "semantic_context_flight_control_enabled": False,
            "semantic_context_physical_release_enabled": False,
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
            "capture_queue_frames": args.capture_queue_frames,
            "capture_queue_intentional_drop_policy": "none",
            "model_warmup_iterations": args.warmup_iterations,
            "monocular_avoidance_enabled": monocular_avoidance is not None,
            "monocular_avoidance_advisory_only": True,
            "monocular_avoidance_metric_depth_available": False,
            "monocular_avoidance_flight_control_enabled": False,
            "unified_target_pool_enabled": unified_target_pool is not None,
            "unified_target_pool_maximum_tracks": (
                args.unified_target_pool_maximum_tracks if unified_target_pool is not None else 0
            ),
            "unified_target_pool_locked_reacquisition_seconds": (
                args.unified_target_pool_locked_reacquisition_seconds
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_minimum_association_confidence": (
                args.unified_target_pool_minimum_association_confidence
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_minimum_new_track_confidence": (
                args.unified_target_pool_minimum_new_track_confidence
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_high_confidence_threshold": (
                args.unified_target_pool_high_confidence_threshold
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_kalman_process_noise": (
                args.unified_target_pool_kalman_process_noise
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_kalman_measurement_noise": (
                args.unified_target_pool_kalman_measurement_noise
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_kalman_gate_sigma": (
                args.unified_target_pool_kalman_gate_sigma
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_kalman_maximum_horizon_seconds": (
                args.unified_target_pool_kalman_maximum_horizon_seconds
                if unified_target_pool is not None
                else None
            ),
            "unified_target_pool_metadata_only": True,
            "unified_target_pool_flight_control_enabled": False,
            "identity_tracking_log_enabled": identity_prediction_writer is not None,
            "identity_tracking_session_id": args.identity_tracking_session_id,
            "identity_tracking_log_contains_pixels": False,
            "identity_tracking_log_flight_control_enabled": False,
            "person_reid_enabled": person_reid_encoder is not None,
            "person_reid_providers": (
                list(person_reid_encoder.provider_names) if person_reid_encoder is not None else []
            ),
            "person_reid_allowed_labels": ["firefighter", "person"],
            "person_reid_vehicle_identity_enabled": False,
            "person_reid_frame_stride": args.person_reid_frame_stride,
            "person_reid_frame_phase": 0,
            "person_reid_runtime_class": (
                "tensorrt"
                if person_reid_encoder is not None and args.person_reid_engine is not None
                else "onnx_nonrealtime"
                if person_reid_encoder is not None
                else "off"
            ),
            "vehicle_reid_enabled": vehicle_reid_encoder is not None,
            "vehicle_reid_providers": (
                list(vehicle_reid_encoder.provider_names)
                if vehicle_reid_encoder is not None
                else []
            ),
            "vehicle_reid_allowed_labels": ["bus", "car", "truck", "vehicle"],
            "vehicle_reid_person_identity_enabled": False,
            "vehicle_reid_motorcycle_identity_enabled": False,
            "vehicle_reid_frame_stride": args.vehicle_reid_frame_stride,
            "vehicle_reid_frame_phase": (1 if args.vehicle_reid_frame_stride > 1 else 0),
            "reid_maximum_interval_seconds": args.reid_maximum_interval_seconds,
            "reid_recovery_overrides_cadence": True,
            "vehicle_reid_runtime_class": (
                "tensorrt"
                if vehicle_reid_encoder is not None and args.vehicle_reid_engine is not None
                else "onnx_nonrealtime"
                if vehicle_reid_encoder is not None
                else "off"
            ),
            "reid_nonrealtime_override_enabled": args.allow_nonrealtime_reid,
            "reid_realtime_admission_passed": (
                (
                    (person_reid_encoder is None or args.person_reid_engine is not None)
                    and (vehicle_reid_encoder is None or args.vehicle_reid_engine is not None)
                )
                if person_reid_encoder is not None or vehicle_reid_encoder is not None
                else None
            ),
            "reid_synchronous_inference": (
                person_reid_encoder is not None or vehicle_reid_encoder is not None
            ),
            "reid_frame_backlog_risk_accepted": args.allow_nonrealtime_reid,
            "reid_flight_control_enabled": False,
            "patrol_advisory_enabled": patrol_advisory_engine is not None,
            "patrol_advisory_operator_confirmation_required": True,
            "patrol_advisory_sitl_validation_required": True,
            "patrol_advisory_flight_control_enabled": False,
            "short_term_tracking_enabled": short_term_tracker is not None,
            "short_term_tracking_metadata_only": True,
            "short_term_tracking_identity_authority": False,
            "short_term_tracking_flight_control_enabled": False,
            "selection_target_pool_enabled": selection_target_pool is not None,
            "selection_target_pool_metadata_only": True,
            "selection_target_pool_flight_control_enabled": False,
            "multimodal_ranging_enabled": ranging_engine is not None,
            "multimodal_ranging_calibration_id": (
                ranging_config.calibration.calibration_id if ranging_config is not None else None
            ),
            "multimodal_ranging_calibration_sha256": ranging_calibration_sha256,
            "multimodal_ranging_absolute_methods": (
                ["camera_ground"] if ranging_engine is not None else []
            ),
            "multimodal_ranging_independent_direct_range_available": False,
            "multimodal_ranging_valid_possible": False,
            "multimodal_ranging_advisory_only": True,
            "multimodal_ranging_flight_control_enabled": False,
            "multimodal_ranging_physical_release_enabled": False,
            "approach_hil_enabled": approach_hil_coordinator is not None,
            "approach_hil_advisory_only": True,
            "approach_hil_sitl_hil_only": True,
            "approach_hil_flight_control_enabled": False,
            "approach_hil_physical_release_enabled": False,
            "payload_target_hil_enabled": payload_target_coordinator is not None,
            "payload_target_hil_requires_selection": True,
            "payload_target_hil_requires_continuous_slide": True,
            "payload_target_hil_flight_control_enabled": False,
            "payload_target_hil_physical_release_enabled": False,
        }
    )
    pixhawk_diagnostics: dict[str, object] | None = None
    try:
        result = runner.run()
        if pixhawk_telemetry is not None:
            diagnostics_at_s = time.monotonic()
            pixhawk_diagnostics = pixhawk_telemetry.diagnostics(now_s=diagnostics_at_s)
            if audit_log is not None:
                audit_log.append(
                    "live.pixhawk_read_only_summary",
                    diagnostics_at_s,
                    pixhawk_diagnostics,
                )
    finally:
        if vehicle_reid_session is not None:
            vehicle_reid_session.close()
        if person_reid_session is not None:
            person_reid_session.close()
        if alert_outbox is not None:
            alert_outbox.close()
        if audit_log is not None:
            audit_log.close()
        if prediction_writer is not None:
            prediction_writer.close()
        if identity_prediction_writer is not None:
            identity_prediction_writer.close()
    _emit(
        {
            "event": "live_camera_finished",
            "processed_frames": result.processed_frames,
            "phase": result.final_phase.value,
            "authorizations": result.authorization_count,
            "alerts_delivered": result.alert_delivery_count,
            "alert_delivery_failures": result.alert_delivery_failure_count,
            "average_fps": result.average_fps,
            "steady_source_fps": result.steady_source_fps,
            "steady_processing_fps": result.steady_processing_fps,
            "startup_to_first_frame_seconds": result.startup_to_first_frame_seconds,
            "capture_latency_p50_ms": result.capture_latency_p50_ms,
            "capture_latency_p95_ms": result.capture_latency_p95_ms,
            "frame_age_at_inference_p50_ms": result.frame_age_at_inference_p50_ms,
            "frame_age_at_inference_p95_ms": result.frame_age_at_inference_p95_ms,
            "inference_latency_p50_ms": result.inference_latency_p50_ms,
            "inference_latency_p95_ms": result.inference_latency_p95_ms,
            "rgb_fire_verifier_assessments": result.rgb_fire_verifier_assessment_count,
            "rgb_fire_verifier_skipped_no_candidate_frames": (
                result.rgb_fire_verifier_skipped_no_candidate_frame_count
            ),
            "rgb_fire_verifier_inferences": result.rgb_fire_verifier_inference_count,
            "rgb_fire_verifier_failures": result.rgb_fire_verifier_failure_count,
            "rgb_fire_verifier_unavailable_frames": (
                result.rgb_fire_verifier_unavailable_frame_count
            ),
            "rgb_fire_verifier_unqualified_frames": (
                result.rgb_fire_verifier_unqualified_frame_count
            ),
            "rgb_fire_verifier_corroborated_frames": (
                result.rgb_fire_verifier_corroborated_frame_count
            ),
            "rgb_fire_verifier_corroborated_detections": (
                result.rgb_fire_verifier_corroborated_detection_count
            ),
            "rgb_fire_verifier_latency_p50_ms": (result.rgb_fire_verifier_latency_p50_ms),
            "rgb_fire_verifier_latency_p95_ms": (result.rgb_fire_verifier_latency_p95_ms),
            "rgb_fire_verifier_output_creates_targets": False,
            "rgb_fire_verifier_flight_control_enabled": False,
            "rgb_fire_verifier_physical_release_enabled": False,
            "camera_reconnect_count": result.camera_reconnect_count,
            "capture_queue_high_watermark": result.capture_queue_high_watermark,
            "capture_queue_backpressure_count": result.capture_queue_backpressure_count,
            "captured_frame_count": result.captured_frame_count,
            "alerts_retried": result.retried_alert_count,
            "simulated_payload_cycles": result.simulated_payload_cycle_count,
            "local_selections": result.local_selection_count,
            "local_tracking_statuses": result.local_tracking_status_count,
            "remote_selections": result.remote_selection_count,
            "remote_tracking_statuses": result.remote_tracking_status_count,
            "remote_mission_statuses": result.remote_mission_status_count,
            "remote_safety_statuses": result.remote_safety_status_count,
            "remote_patrol_statuses": result.remote_patrol_status_count,
            "remote_range_statuses": result.remote_range_status_count,
            "remote_release_statuses": result.remote_release_status_count,
            "remote_approach_challenges": result.remote_approach_challenge_count,
            "remote_approach_statuses": result.remote_approach_status_count,
            "remote_target_pool_statuses": result.remote_target_pool_status_count,
            "remote_scene_context_statuses": result.remote_scene_context_status_count,
            "remote_approach_confirmations": result.remote_approach_confirmation_count,
            "remote_payload_target_challenges": result.remote_payload_target_challenge_count,
            "remote_payload_target_statuses": result.remote_payload_target_status_count,
            "remote_payload_target_confirmations": (
                result.remote_payload_target_confirmation_count
            ),
            "payload_target_errors": result.payload_target_error_count,
            "approach_hil_aborts": result.approach_hil_abort_count,
            "approach_hil_errors": result.approach_hil_error_count,
            "approach_hil_advisory_only": True,
            "approach_hil_flight_control_enabled": False,
            "approach_hil_physical_release_enabled": False,
            "remote_transport_errors": result.remote_transport_error_count,
            "monocular_avoidance_assessments": (result.monocular_avoidance_assessment_count),
            "monocular_avoidance_invalid": result.monocular_avoidance_invalid_count,
            "monocular_avoidance_caution": result.monocular_avoidance_caution_count,
            "monocular_avoidance_avoid": result.monocular_avoidance_avoid_count,
            "monocular_avoidance_errors": result.monocular_avoidance_error_count,
            "monocular_avoidance_latency_p50_ms": (result.monocular_avoidance_latency_p50_ms),
            "monocular_avoidance_latency_p95_ms": (result.monocular_avoidance_latency_p95_ms),
            "monocular_avoidance_advisory_only": True,
            "monocular_avoidance_flight_control_enabled": False,
            "unified_target_pool_updates": result.unified_target_pool_update_count,
            "unified_target_pool_errors": result.unified_target_pool_error_count,
            "unified_target_pool_maximum_tracks": (result.unified_target_pool_maximum_track_count),
            "unified_target_pool_created_tracks": (result.unified_target_pool_created_track_count),
            "unified_target_pool_recovered_tracks": (
                result.unified_target_pool_recovered_track_count
            ),
            "unified_target_pool_lost_tracks": result.unified_target_pool_lost_track_count,
            "unified_target_pool_association_p50_ms": (
                result.unified_target_pool_association_p50_ms
            ),
            "unified_target_pool_association_p95_ms": (
                result.unified_target_pool_association_p95_ms
            ),
            "person_reid_failures": result.person_reid_failure_count,
            "person_reid_inferences": result.person_reid_inference_count,
            "person_reid_skipped_frames": result.person_reid_skipped_frame_count,
            "person_reid_no_candidate_frames": (result.person_reid_no_candidate_frame_count),
            "person_reid_forced_recoveries": result.person_reid_forced_recovery_count,
            "person_reid_latency_p50_ms": result.person_reid_latency_p50_ms,
            "person_reid_latency_p95_ms": result.person_reid_latency_p95_ms,
            "vehicle_reid_failures": result.vehicle_reid_failure_count,
            "vehicle_reid_inferences": result.vehicle_reid_inference_count,
            "vehicle_reid_skipped_frames": result.vehicle_reid_skipped_frame_count,
            "vehicle_reid_no_candidate_frames": (result.vehicle_reid_no_candidate_frame_count),
            "vehicle_reid_forced_recoveries": result.vehicle_reid_forced_recovery_count,
            "vehicle_reid_latency_p50_ms": result.vehicle_reid_latency_p50_ms,
            "vehicle_reid_latency_p95_ms": result.vehicle_reid_latency_p95_ms,
            "patrol_advisory_assessments": result.patrol_advisory_assessment_count,
            "patrol_return_to_observe": result.patrol_return_to_observe_count,
            "patrol_advisory_errors": result.patrol_advisory_error_count,
            "patrol_advisory_flight_control_enabled": False,
            "unified_target_pool_metadata_only": True,
            "unified_target_pool_flight_control_enabled": False,
            "short_term_tracking_updates": result.short_term_tracking_update_count,
            "short_term_tracking_invalid": result.short_term_tracking_invalid_count,
            "short_term_tracking_errors": result.short_term_tracking_error_count,
            "short_term_tracking_optical_flow_hints": (
                result.short_term_tracking_optical_flow_hint_count
            ),
            "short_term_tracking_template_hints": (result.short_term_tracking_template_hint_count),
            "short_term_tracking_accepted_hints": (result.short_term_tracking_accepted_hint_count),
            "short_term_tracking_rejected_hints": (result.short_term_tracking_rejected_hint_count),
            "short_term_tracking_latency_p50_ms": (result.short_term_tracking_latency_p50_ms),
            "short_term_tracking_latency_p95_ms": (result.short_term_tracking_latency_p95_ms),
            "short_term_tracking_metadata_only": True,
            "short_term_tracking_identity_authority": False,
            "short_term_tracking_flight_control_enabled": False,
            "selection_target_pool_syncs": result.selection_target_pool_sync_count,
            "selection_target_pool_bindings": result.selection_target_pool_binding_count,
            "selection_target_pool_pending": result.selection_target_pool_pending_count,
            "selection_target_pool_cancels": result.selection_target_pool_cancel_count,
            "selection_target_pool_errors": result.selection_target_pool_error_count,
            "selection_target_pool_metadata_only": True,
            "selection_target_pool_flight_control_enabled": False,
            "multimodal_ranging_assessments": result.ranging_assessment_count,
            "multimodal_ranging_valid": result.ranging_valid_count,
            "multimodal_ranging_degraded": result.ranging_degraded_count,
            "multimodal_ranging_invalid": result.ranging_invalid_count,
            "multimodal_ranging_errors": result.ranging_error_count,
            "multimodal_ranging_latency_p50_ms": result.ranging_latency_p50_ms,
            "multimodal_ranging_latency_p95_ms": result.ranging_latency_p95_ms,
            "multimodal_ranging_advisory_only": True,
            "multimodal_ranging_flight_control_enabled": False,
            "multimodal_ranging_physical_release_enabled": False,
            "semantic_context_submitted_frames": (result.semantic_context_submitted_frame_count),
            "semantic_context_interval_skipped_frames": (
                result.semantic_context_interval_skipped_frame_count
            ),
            "semantic_context_replaced_pending_frames": (
                result.semantic_context_replaced_pending_frame_count
            ),
            "semantic_context_valid_frames": result.semantic_context_valid_frame_count,
            "semantic_context_invalid_frames": result.semantic_context_invalid_frame_count,
            "semantic_context_submit_errors": result.semantic_context_submit_error_count,
            "semantic_context_stale_count": result.semantic_context_stale_count,
            "semantic_context_latency_p50_ms": result.semantic_context_latency_p50_ms,
            "semantic_context_latency_p95_ms": result.semantic_context_latency_p95_ms,
            "semantic_context_shutdown_clean": result.semantic_context_shutdown_clean,
            "semantic_context_queue_capacity": 1,
            "semantic_context_advisory_only": True,
            "semantic_context_target_pool_identity_authority": False,
            "semantic_context_flight_control_enabled": False,
            "semantic_context_physical_release_enabled": False,
            "audit_written": args.audit_out is not None,
            "prediction_log_written": args.prediction_log_out is not None,
            "identity_tracking_log_written": args.identity_tracking_log_out is not None,
            "identity_tracking_session_id": args.identity_tracking_session_id,
            "identity_tracking_log_frames": result.identity_tracking_log_frame_count,
            "identity_tracking_log_errors": result.identity_tracking_log_error_count,
            "identity_tracking_log_complete": (
                args.identity_tracking_log_out is None
                or (
                    result.identity_tracking_log_frame_count == result.processed_frames
                    and result.identity_tracking_log_error_count == 0
                )
            ),
            "physical_release_supported": False,
            "inert_payload_hil_enabled": payload_hil_cycle is not None,
            "auto_simulated_payload_cycle_enabled": args.auto_simulate_payload_cycle,
            "pixhawk": pixhawk_diagnostics,
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
        outcome = controller.process_observation(
            frame,
            now_s=frame.captured_at_s,
            primary_range_evidence=primary_range_evidence_from_frame(frame),
        )
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


def _sha384_file(path: Path) -> str:
    digest = hashlib.sha384()
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

#!/usr/bin/env bash
set -euo pipefail

# Jetson production launcher. Perception/telemetry run continuously.  The signed
# Mode-3 confirmation channel and real attitude control have separate switches so
# QGC confirmation remains available without silently enabling actuator output.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
CONFIG="${MISSION_CONFIG:-${ROOT}/configs/missions/fire_patrol.demo.json}"
MODEL="${FIRE_MODEL_PATH:-${ROOT}/models/fire-smoke-v5-trt86/best.opset17.fp16.engine}"
MANIFEST="${FIRE_MODEL_MANIFEST:-${ROOT}/models/fire-smoke-v5-trt86/best.opset17.fp16.manifest.json}"
FIRE_FLAME_CONFIDENCE_THRESHOLD="${FIRE_FLAME_CONFIDENCE_THRESHOLD:-0.25}"
# The FIRESENSE public smoke regression retained zero stable smoke candidates
# across 7,153 decoded negative frames at 0.30 while improving the known-positive
# clip's stable smoke-frame coverage versus the former 0.60 default.
FIRE_SMOKE_CONFIDENCE_THRESHOLD="${FIRE_SMOKE_CONFIDENCE_THRESHOLD:-0.30}"
FIRE_CANDIDATE_STABILITY_FRAMES="${FIRE_CANDIDATE_STABILITY_FRAMES:-3}"
# Run primary fire inference on alternating frames. The target pool and
# short-term tracker still update on every captured frame, so selected boxes can
# publish at the ground-overlay cadence without inventing detector observations.
PRIMARY_FIRE_MODEL_FRAME_STRIDE="${PRIMARY_FIRE_MODEL_FRAME_STRIDE:-2}"
PRIMARY_FIRE_MODEL_FRAME_PHASE="${PRIMARY_FIRE_MODEL_FRAME_PHASE:-0}"
# Keep learned LCK detectors on their configured cadence by default. The
# short-term tracker owns intermediate visual continuity; set 1 for a diagnostic
# all-detectors-per-frame run.
LOCK_MODEL_FORCE_EVERY_FRAME="${LOCK_MODEL_FORCE_EVERY_FRAME:-0}"
# Disabled by default.  Public-video evidence supports a staged 0.001 sweep,
# but low-light deployment-domain fire validation is still required before enabling it.
FIRE_MINIMUM_BRIGHT_WARM_FRACTION="${FIRE_MINIMUM_BRIGHT_WARM_FRACTION:-0.0}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${ROOT}/artifacts/deployment/jetson-bench}"
MAX_FRAMES="${MAX_FRAMES:-0}"
CAMERA_RECONNECT_ATTEMPTS="${CAMERA_RECONNECT_ATTEMPTS:-30}"
CAMERA_RECONNECT_DELAY_SECONDS="${CAMERA_RECONNECT_DELAY_SECONDS:-1}"
GSTREAMER_LATENCY_MS="${GSTREAMER_LATENCY_MS:-50}"
CAPTURE_QUEUE_FRAMES="${CAPTURE_QUEUE_FRAMES:-1}"
OPERATOR_UDP_ENABLED="${OPERATOR_UDP_ENABLED:-auto}"
MONOCULAR_AVOIDANCE_ENABLED="${MONOCULAR_AVOIDANCE_ENABLED:-1}"
UNIFIED_TARGET_POOL_ENABLED="${UNIFIED_TARGET_POOL_ENABLED:-1}"
PATROL_ADVISORY_ENABLED="${PATROL_ADVISORY_ENABLED:-1}"
COMMON_OBJECT_DETECTOR_ENABLED="${COMMON_OBJECT_DETECTOR_ENABLED:-auto}"
PRIORITY_OBJECT_DETECTOR_ENABLED="${PRIORITY_OBJECT_DETECTOR_ENABLED:-auto}"
PERSON_REID_ENABLED="${PERSON_REID_ENABLED:-0}"
VEHICLE_REID_ENABLED="${VEHICLE_REID_ENABLED:-0}"
ENVIRONMENT_RISK_DETECTOR_ENABLED="${ENVIRONMENT_RISK_DETECTOR_ENABLED:-0}"
SEMANTIC_CONTEXT_ENABLED="${SEMANTIC_CONTEXT_ENABLED:-0}"
SHORT_TERM_TRACKING_ENABLED="${SHORT_TERM_TRACKING_ENABLED:-1}"
MULTIMODAL_RANGING_ENABLED="${MULTIMODAL_RANGING_ENABLED:-0}"
MODE3_CONFIRMATION_ENABLED="${MODE3_CONFIRMATION_ENABLED:-0}"
MODE3_AIM_CONTROL_ENABLED="${MODE3_AIM_CONTROL_ENABLED:-0}"
RANGING_CALIBRATION_PATH="${RANGING_CALIBRATION_PATH:-}"
COMMON_OBJECT_ONNX_PATH="${COMMON_OBJECT_ONNX_PATH:-${ROOT}/models/coco-yolo26n-traditional/yolo26n-traditional.onnx}"
COMMON_OBJECT_MODEL_PATH="${COMMON_OBJECT_MODEL_PATH:-${ROOT}/models/coco-yolo26n-traditional/yolo26n-traditional.b1.fp16.trt86.engine}"
COMMON_OBJECT_MODEL_MANIFEST="${COMMON_OBJECT_MODEL_MANIFEST:-${COMMON_OBJECT_MODEL_PATH}.manifest.json}"
COMMON_OBJECT_ENGINE_PROVENANCE="${COMMON_OBJECT_ENGINE_PROVENANCE:-${COMMON_OBJECT_MODEL_PATH}.provenance.json}"
COMMON_OBJECT_CONFIDENCE_THRESHOLD="${COMMON_OBJECT_CONFIDENCE_THRESHOLD:-0.30}"
COMMON_OBJECT_PRIORITY_CONFIDENCE_THRESHOLD="${COMMON_OBJECT_PRIORITY_CONFIDENCE_THRESHOLD:-0.25}"
COMMON_OBJECT_FALLBACK_CONFIDENCE_THRESHOLD="${COMMON_OBJECT_FALLBACK_CONFIDENCE_THRESHOLD:-0.35}"
COMMON_OBJECT_IOU_THRESHOLD="${COMMON_OBJECT_IOU_THRESHOLD:-0.45}"
COMMON_OBJECT_MAXIMUM_DETECTIONS="${COMMON_OBJECT_MAXIMUM_DETECTIONS:-300}"
COMMON_OBJECT_FRAME_STRIDE="${COMMON_OBJECT_FRAME_STRIDE:-4}"
COMMON_OBJECT_FRAME_PHASE="${COMMON_OBJECT_FRAME_PHASE:-0}"
COMMON_OBJECT_TILE_COLUMNS="${COMMON_OBJECT_TILE_COLUMNS:-2}"
COMMON_OBJECT_TILE_ROWS="${COMMON_OBJECT_TILE_ROWS:-1}"
COMMON_OBJECT_TILE_OVERLAP="${COMMON_OBJECT_TILE_OVERLAP:-0.15}"
COMMON_OBJECT_TILE_SCAN_INTERVAL_FRAMES="${COMMON_OBJECT_TILE_SCAN_INTERVAL_FRAMES:-3}"
COMMON_OBJECT_TILE_FUSION_IOU_THRESHOLD="${COMMON_OBJECT_TILE_FUSION_IOU_THRESHOLD:-0.30}"
COMMON_OBJECT_TILE_CONFIDENCE_THRESHOLD="${COMMON_OBJECT_TILE_CONFIDENCE_THRESHOLD:-0.40}"
COMMON_OBJECT_TILE_LABEL_CONFIDENCE_THRESHOLDS="${COMMON_OBJECT_TILE_LABEL_CONFIDENCE_THRESHOLDS:-airplane=0.82}"
COMMON_OBJECT_TILE_MAXIMUM_BOX_AREA="${COMMON_OBJECT_TILE_MAXIMUM_BOX_AREA:-0.04}"
COMMON_OBJECT_TILE_LABELS="${COMMON_OBJECT_TILE_LABELS:-person,airplane,bicycle,car,motorcycle,bus,train,truck,boat}"
PRIORITY_OBJECT_ONNX_PATH="${PRIORITY_OBJECT_ONNX_PATH:-${ROOT}/models/visdrone-yolo26n-e30-960/best.onnx}"
PRIORITY_OBJECT_MODEL_PATH="${PRIORITY_OBJECT_MODEL_PATH:-${ROOT}/models/visdrone-yolo26n-e30-960/best.b1.fp16.trt86.engine}"
PRIORITY_OBJECT_MODEL_MANIFEST="${PRIORITY_OBJECT_MODEL_MANIFEST:-${PRIORITY_OBJECT_MODEL_PATH}.manifest.json}"
PRIORITY_OBJECT_ENGINE_PROVENANCE="${PRIORITY_OBJECT_ENGINE_PROVENANCE:-${PRIORITY_OBJECT_MODEL_PATH}.provenance.json}"
PRIORITY_OBJECT_CLASS_NAMES="${PRIORITY_OBJECT_CLASS_NAMES:-pedestrian,people,bicycle,car,van,truck,tricycle,awning-tricycle,bus,motor}"
PRIORITY_OBJECT_LABEL_MAP="${PRIORITY_OBJECT_LABEL_MAP:-pedestrian=person,people=person,van=car,tricycle=motorcycle,awning-tricycle=motorcycle,motor=motorcycle}"
PRIORITY_OBJECT_CONFIDENCE_THRESHOLD="${PRIORITY_OBJECT_CONFIDENCE_THRESHOLD:-0.30}"
PRIORITY_OBJECT_PERSON_CONFIDENCE_THRESHOLD="${PRIORITY_OBJECT_PERSON_CONFIDENCE_THRESHOLD:-0.30}"
PRIORITY_OBJECT_VEHICLE_CONFIDENCE_THRESHOLD="${PRIORITY_OBJECT_VEHICLE_CONFIDENCE_THRESHOLD:-0.60}"
PRIORITY_OBJECT_CAR_SINGLE_SOURCE_CONFIDENCE_THRESHOLD="${PRIORITY_OBJECT_CAR_SINGLE_SOURCE_CONFIDENCE_THRESHOLD:-0.80}"
PRIORITY_OBJECT_LABEL_CONFIDENCE_THRESHOLDS="${PRIORITY_OBJECT_LABEL_CONFIDENCE_THRESHOLDS:-truck=0.80}"
PRIORITY_OBJECT_VEHICLE_STABILITY_FRAMES="${PRIORITY_OBJECT_VEHICLE_STABILITY_FRAMES:-3}"
PRIORITY_OBJECT_IOU_THRESHOLD="${PRIORITY_OBJECT_IOU_THRESHOLD:-0.45}"
PRIORITY_OBJECT_MAXIMUM_DETECTIONS="${PRIORITY_OBJECT_MAXIMUM_DETECTIONS:-300}"
PRIORITY_OBJECT_FRAME_STRIDE="${PRIORITY_OBJECT_FRAME_STRIDE:-8}"
PRIORITY_OBJECT_FRAME_PHASE="${PRIORITY_OBJECT_FRAME_PHASE:-2}"
PERSON_REID_ONNX_PATH="${PERSON_REID_ONNX_PATH:-${ROOT}/models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.onnx}"
PERSON_REID_ENGINE_PATH="${PERSON_REID_ENGINE_PATH:-${ROOT}/models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.b1-b10.fp16.trt86.engine}"
PERSON_REID_ENGINE_SHA256_PATH="${PERSON_REID_ENGINE_SHA256_PATH:-${PERSON_REID_ENGINE_PATH}.sha256}"
PERSON_REID_ENGINE_PROVENANCE="${PERSON_REID_ENGINE_PROVENANCE:-${PERSON_REID_ENGINE_PATH}.provenance.json}"
VEHICLE_REID_ONNX_PATH="${VEHICLE_REID_ONNX_PATH:-${ROOT}/models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.onnx}"
VEHICLE_REID_ENGINE_PATH="${VEHICLE_REID_ENGINE_PATH:-${ROOT}/models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.b1-b8.fp16.trt86.engine}"
VEHICLE_REID_ENGINE_SHA256_PATH="${VEHICLE_REID_ENGINE_SHA256_PATH:-${VEHICLE_REID_ENGINE_PATH}.sha256}"
VEHICLE_REID_ENGINE_PROVENANCE="${VEHICLE_REID_ENGINE_PROVENANCE:-${VEHICLE_REID_ENGINE_PATH}.provenance.json}"
ENVIRONMENT_ONNX_PATH="${ENVIRONMENT_ONNX_PATH:-}"
ENVIRONMENT_ENGINE_PATH="${ENVIRONMENT_ENGINE_PATH:-}"
ENVIRONMENT_MODEL_MANIFEST="${ENVIRONMENT_MODEL_MANIFEST:-}"
ENVIRONMENT_ENGINE_PROVENANCE="${ENVIRONMENT_ENGINE_PROVENANCE:-}"
ENVIRONMENT_CLASS_NAMES="${ENVIRONMENT_CLASS_NAMES:-power_line,flammable_tank}"
ENVIRONMENT_CONFIDENCE_THRESHOLD="${ENVIRONMENT_CONFIDENCE_THRESHOLD:-0.40}"
SEMANTIC_CONTEXT_ONNX_PATH="${SEMANTIC_CONTEXT_ONNX_PATH:-}"
SEMANTIC_CONTEXT_MODEL_MANIFEST="${SEMANTIC_CONTEXT_MODEL_MANIFEST:-}"
SEMANTIC_CONTEXT_ENGINE_PATH="${SEMANTIC_CONTEXT_ENGINE_PATH:-}"
SEMANTIC_CONTEXT_ENGINE_PROVENANCE="${SEMANTIC_CONTEXT_ENGINE_PROVENANCE:-}"
SEMANTIC_CONTEXT_ENGINE_SHA256_PATH="${SEMANTIC_CONTEXT_ENGINE_SHA256_PATH:-}"
SEMANTIC_CONTEXT_MINIMUM_INTERVAL_SECONDS="${SEMANTIC_CONTEXT_MINIMUM_INTERVAL_SECONDS:-0.5}"
SEMANTIC_CONTEXT_MAXIMUM_AGE_SECONDS="${SEMANTIC_CONTEXT_MAXIMUM_AGE_SECONDS:-2.0}"
TRTEXEC="${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}"

# The camera manual specifies this credential-free H.265 main stream. Override
# CAMERA_SOURCE in the process environment if the installation changes.
export CAMERA_SOURCE="${CAMERA_SOURCE:-rtsp://192.168.144.108:554/stream=0}"

for path in "${PYTHON}" "${CONFIG}" "${MODEL}" "${MANIFEST}"; do
    if [[ ! -e "${path}" ]]; then
        printf 'Required Jetson runtime path is missing: %s\n' "${path}" >&2
        exit 2
    fi
done

mkdir -p "${EVIDENCE_DIR}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
audit_out="${EVIDENCE_DIR}/jetson-live-${timestamp}.audit.jsonl"
prediction_out="${EVIDENCE_DIR}/jetson-live-${timestamp}.predictions.jsonl"
identity_tracking_out="${EVIDENCE_DIR}/jetson-live-${timestamp}.identity-tracks.jsonl"
tracking_evidence_session_id="${TRACKING_EVIDENCE_SESSION_ID:-$("${PYTHON}" -c 'import uuid; print(uuid.uuid4())')}"

args=(
    -m multidetect live-camera "${CONFIG}"
    --source-env CAMERA_SOURCE
    --rtsp-transport tcp
    --rtsp-codec h265
    --backend gstreamer
    --gstreamer-hardware-decode
    --gstreamer-latency-ms "${GSTREAMER_LATENCY_MS}"
    --reconnect-attempts "${CAMERA_RECONNECT_ATTEMPTS}"
    --reconnect-delay-seconds "${CAMERA_RECONNECT_DELAY_SECONDS}"
    --onnx-model "${MODEL}"
    --model-manifest "${MANIFEST}"
    --class-names flame,smoke
    --primary-model-frame-stride "${PRIMARY_FIRE_MODEL_FRAME_STRIDE}"
    --primary-model-frame-phase "${PRIMARY_FIRE_MODEL_FRAME_PHASE}"
    --output-coordinates letterbox_xyxy_px
    --confidence-threshold 0.10
    --flame-confidence-threshold "${FIRE_FLAME_CONFIDENCE_THRESHOLD}"
    --smoke-confidence-threshold "${FIRE_SMOKE_CONFIDENCE_THRESHOLD}"
    --fire-minimum-bright-warm-fraction "${FIRE_MINIMUM_BRIGHT_WARM_FRACTION}"
    --candidate-stability-frames "${FIRE_CANDIDATE_STABILITY_FRAMES}"
    --capture-queue-frames "${CAPTURE_QUEUE_FRAMES}"
    --pixhawk-endpoint udp:0.0.0.0:14550
    --pixhawk-baud 921600
    --pixhawk-system-id 1
    --pixhawk-expected-autopilot px4
    --pixhawk-expected-vehicle-type fixed_wing
    --no-display
    --audit-out "${audit_out}"
    --prediction-log-out "${prediction_out}"
)

if [[ "${LOCK_MODEL_FORCE_EVERY_FRAME}" == "1" ]]; then
    args+=(--lock-model-force-every-frame)
elif [[ "${LOCK_MODEL_FORCE_EVERY_FRAME}" == "0" ]]; then
    args+=(--no-lock-model-force-every-frame)
else
    printf 'LOCK_MODEL_FORCE_EVERY_FRAME must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${MONOCULAR_AVOIDANCE_ENABLED}" == "1" ]]; then
    args+=(
        --monocular-avoidance
        --avoidance-analysis-width "${AVOIDANCE_ANALYSIS_WIDTH:-320}"
        --avoidance-minimum-features "${AVOIDANCE_MINIMUM_FEATURES:-24}"
        --avoidance-caution-ttc-seconds "${AVOIDANCE_CAUTION_TTC_SECONDS:-3.0}"
        --avoidance-avoid-ttc-seconds "${AVOIDANCE_AVOID_TTC_SECONDS:-1.5}"
        --avoidance-maximum-data-age-seconds "${AVOIDANCE_MAXIMUM_DATA_AGE_SECONDS:-0.25}"
    )
elif [[ "${MONOCULAR_AVOIDANCE_ENABLED}" != "0" ]]; then
    printf 'MONOCULAR_AVOIDANCE_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${UNIFIED_TARGET_POOL_ENABLED}" == "1" ]]; then
    args+=(
        --unified-target-pool
        --identity-tracking-log-out "${identity_tracking_out}"
        --identity-tracking-session-id "${tracking_evidence_session_id}"
        --unified-target-pool-maximum-tracks "${UNIFIED_TARGET_POOL_MAXIMUM_TRACKS:-64}"
        --unified-target-pool-locked-reacquisition-seconds "${UNIFIED_TARGET_POOL_LOCKED_REACQUISITION_SECONDS:-5.0}"
        --unified-target-pool-minimum-association-confidence "${UNIFIED_TARGET_POOL_MINIMUM_ASSOCIATION_CONFIDENCE:-0.10}"
        --unified-target-pool-priority-minimum-new-track-confidence "${UNIFIED_TARGET_POOL_PRIORITY_MINIMUM_NEW_TRACK_CONFIDENCE:-0.25}"
        --unified-target-pool-minimum-new-track-confidence "${UNIFIED_TARGET_POOL_MINIMUM_NEW_TRACK_CONFIDENCE:-0.35}"
        --unified-target-pool-high-confidence-threshold "${UNIFIED_TARGET_POOL_HIGH_CONFIDENCE_THRESHOLD:-0.55}"
        --unified-target-pool-person-maximum-appearance-distance "${UNIFIED_TARGET_POOL_PERSON_MAXIMUM_APPEARANCE_DISTANCE:-0.70}"
        --unified-target-pool-person-strict-reid-distance "${UNIFIED_TARGET_POOL_PERSON_STRICT_REID_DISTANCE:-0.22}"
        --unified-target-pool-kalman-process-noise "${UNIFIED_TARGET_POOL_KALMAN_PROCESS_NOISE:-0.04}"
        --unified-target-pool-kalman-measurement-noise "${UNIFIED_TARGET_POOL_KALMAN_MEASUREMENT_NOISE:-0.0004}"
        --unified-target-pool-kalman-gate-sigma "${UNIFIED_TARGET_POOL_KALMAN_GATE_SIGMA:-4.0}"
        --unified-target-pool-kalman-maximum-horizon-seconds "${UNIFIED_TARGET_POOL_KALMAN_MAXIMUM_HORIZON_SECONDS:-2.0}"
    )
elif [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "0" ]]; then
    printf 'UNIFIED_TARGET_POOL_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${MULTIMODAL_RANGING_ENABLED}" == "1" ]]; then
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'MULTIMODAL_RANGING_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    if [[ -z "${RANGING_CALIBRATION_PATH}" || ! -f "${RANGING_CALIBRATION_PATH}" ]]; then
        printf 'RANGING_CALIBRATION_PATH must name an existing calibrated camera JSON file.\n' >&2
        exit 2
    fi
    args+=(
        --multimodal-ranging
        --ranging-calibration "${RANGING_CALIBRATION_PATH}"
        --ranging-agl-sigma-m "${RANGING_AGL_SIGMA_M:-1.5}"
        --ranging-roll-sigma-deg "${RANGING_ROLL_SIGMA_DEG:-0.3}"
        --ranging-pitch-sigma-deg "${RANGING_PITCH_SIGMA_DEG:-0.3}"
        --ranging-heading-sigma-deg "${RANGING_HEADING_SIGMA_DEG:-1.0}"
        --ranging-target-center-sigma-px "${RANGING_TARGET_CENTER_SIGMA_PX:-2.0}"
    )
elif [[ "${MULTIMODAL_RANGING_ENABLED}" != "0" ]]; then
    printf 'MULTIMODAL_RANGING_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${PATROL_ADVISORY_ENABLED}" == "1" ]]; then
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'PATROL_ADVISORY_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    args+=(
        --patrol-advisory
        --patrol-maximum-bank-angle-deg "${PATROL_MAXIMUM_BANK_ANGLE_DEG:-25.0}"
        --patrol-minimum-ground-speed-mps "${PATROL_MINIMUM_GROUND_SPEED_MPS:-5.0}"
        --patrol-maximum-evidence-age-seconds \
            "${PATROL_MAXIMUM_EVIDENCE_AGE_SECONDS:-2.0}"
    )
elif [[ "${PATROL_ADVISORY_ENABLED}" != "0" ]]; then
    printf 'PATROL_ADVISORY_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${COMMON_OBJECT_DETECTOR_ENABLED}" == "auto" ]]; then
    COMMON_OBJECT_DETECTOR_ENABLED=1
    for path in \
        "${COMMON_OBJECT_MODEL_PATH}" \
        "${COMMON_OBJECT_ONNX_PATH}" \
        "${COMMON_OBJECT_MODEL_MANIFEST}" \
        "${COMMON_OBJECT_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            COMMON_OBJECT_DETECTOR_ENABLED=0
            break
        fi
    done
fi

if [[ "${COMMON_OBJECT_DETECTOR_ENABLED}" == "1" ]]; then
    for path in \
        "${COMMON_OBJECT_MODEL_PATH}" \
        "${COMMON_OBJECT_ONNX_PATH}" \
        "${COMMON_OBJECT_MODEL_MANIFEST}" \
        "${COMMON_OBJECT_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            printf 'Required common-object detector artifact is missing: %s\n' "${path}" >&2
            exit 2
        fi
    done
    "${PYTHON}" -m multidetect.engine_provenance verify \
        --engine "${COMMON_OBJECT_MODEL_PATH}" \
        --source-model "${COMMON_OBJECT_ONNX_PATH}" \
        --trtexec "${TRTEXEC}" \
        --provenance "${COMMON_OBJECT_ENGINE_PROVENANCE}"
    args+=(
        --safety-onnx-model "${COMMON_OBJECT_MODEL_PATH}"
        --safety-model-manifest "${COMMON_OBJECT_MODEL_MANIFEST}"
        --safety-model-coco80
        --safety-model-format ultralytics_raw
        --safety-confidence-threshold "${COMMON_OBJECT_CONFIDENCE_THRESHOLD}"
        --safety-priority-confidence-threshold "${COMMON_OBJECT_PRIORITY_CONFIDENCE_THRESHOLD}"
        --safety-fallback-confidence-threshold "${COMMON_OBJECT_FALLBACK_CONFIDENCE_THRESHOLD}"
        --safety-model-iou-threshold "${COMMON_OBJECT_IOU_THRESHOLD}"
        --safety-model-maximum-detections "${COMMON_OBJECT_MAXIMUM_DETECTIONS}"
        --safety-model-frame-stride "${COMMON_OBJECT_FRAME_STRIDE}"
        --safety-model-frame-phase "${COMMON_OBJECT_FRAME_PHASE}"
        --safety-tile-columns "${COMMON_OBJECT_TILE_COLUMNS}"
        --safety-tile-rows "${COMMON_OBJECT_TILE_ROWS}"
        --safety-tile-overlap "${COMMON_OBJECT_TILE_OVERLAP}"
        --safety-tile-scan-interval-frames "${COMMON_OBJECT_TILE_SCAN_INTERVAL_FRAMES}"
        --safety-tile-fusion-iou-threshold "${COMMON_OBJECT_TILE_FUSION_IOU_THRESHOLD}"
        --safety-tile-confidence-threshold "${COMMON_OBJECT_TILE_CONFIDENCE_THRESHOLD}"
        --safety-tile-label-confidence-thresholds "${COMMON_OBJECT_TILE_LABEL_CONFIDENCE_THRESHOLDS}"
        --safety-tile-maximum-box-area "${COMMON_OBJECT_TILE_MAXIMUM_BOX_AREA}"
        --safety-tile-labels "${COMMON_OBJECT_TILE_LABELS}"
    )
elif [[ "${COMMON_OBJECT_DETECTOR_ENABLED}" != "0" ]]; then
    printf 'COMMON_OBJECT_DETECTOR_ENABLED must be auto, 0 or 1.\n' >&2
    exit 2
fi

if [[ "${PRIORITY_OBJECT_DETECTOR_ENABLED}" == "auto" ]]; then
    PRIORITY_OBJECT_DETECTOR_ENABLED=1
    for path in \
        "${PRIORITY_OBJECT_MODEL_PATH}" \
        "${PRIORITY_OBJECT_ONNX_PATH}" \
        "${PRIORITY_OBJECT_MODEL_MANIFEST}" \
        "${PRIORITY_OBJECT_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            PRIORITY_OBJECT_DETECTOR_ENABLED=0
            break
        fi
    done
fi

if [[ "${PRIORITY_OBJECT_DETECTOR_ENABLED}" == "1" ]]; then
    for path in \
        "${PRIORITY_OBJECT_MODEL_PATH}" \
        "${PRIORITY_OBJECT_ONNX_PATH}" \
        "${PRIORITY_OBJECT_MODEL_MANIFEST}" \
        "${PRIORITY_OBJECT_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            printf 'Required priority-object detector artifact is missing: %s\n' "${path}" >&2
            exit 2
        fi
    done
    "${PYTHON}" -m multidetect.engine_provenance verify \
        --engine "${PRIORITY_OBJECT_MODEL_PATH}" \
        --source-model "${PRIORITY_OBJECT_ONNX_PATH}" \
        --trtexec "${TRTEXEC}" \
        --provenance "${PRIORITY_OBJECT_ENGINE_PROVENANCE}"
    args+=(
        --priority-onnx-model "${PRIORITY_OBJECT_MODEL_PATH}"
        --priority-model-manifest "${PRIORITY_OBJECT_MODEL_MANIFEST}"
        --priority-class-names "${PRIORITY_OBJECT_CLASS_NAMES}"
        --priority-label-map "${PRIORITY_OBJECT_LABEL_MAP}"
        --priority-input-width 960
        --priority-input-height 960
        --priority-confidence-threshold "${PRIORITY_OBJECT_CONFIDENCE_THRESHOLD}"
        --priority-person-confidence-threshold "${PRIORITY_OBJECT_PERSON_CONFIDENCE_THRESHOLD}"
        --priority-vehicle-confidence-threshold "${PRIORITY_OBJECT_VEHICLE_CONFIDENCE_THRESHOLD}"
        --car-single-source-confidence-threshold "${PRIORITY_OBJECT_CAR_SINGLE_SOURCE_CONFIDENCE_THRESHOLD}"
        --priority-label-confidence-thresholds "${PRIORITY_OBJECT_LABEL_CONFIDENCE_THRESHOLDS}"
        --priority-vehicle-stability-frames "${PRIORITY_OBJECT_VEHICLE_STABILITY_FRAMES}"
        --priority-model-iou-threshold "${PRIORITY_OBJECT_IOU_THRESHOLD}"
        --priority-model-maximum-detections "${PRIORITY_OBJECT_MAXIMUM_DETECTIONS}"
        --priority-model-frame-stride "${PRIORITY_OBJECT_FRAME_STRIDE}"
        --priority-model-frame-phase "${PRIORITY_OBJECT_FRAME_PHASE}"
    )
elif [[ "${PRIORITY_OBJECT_DETECTOR_ENABLED}" != "0" ]]; then
    printf 'PRIORITY_OBJECT_DETECTOR_ENABLED must be auto, 0 or 1.\n' >&2
    exit 2
fi

if [[ "${SHORT_TERM_TRACKING_ENABLED}" == "1" ]]; then
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'SHORT_TERM_TRACKING_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    args+=(
        --short-term-tracking
        --short-term-analysis-width "${SHORT_TERM_ANALYSIS_WIDTH:-320}"
        --short-term-maximum-tracks "${SHORT_TERM_MAXIMUM_TRACKS:-16}"
        --short-term-minimum-flow-points "${SHORT_TERM_MINIMUM_FLOW_POINTS:-6}"
        --short-term-minimum-box-size-px "${SHORT_TERM_MINIMUM_BOX_SIZE_PX:-8}"
        --short-term-frame-stride "${SHORT_TERM_FRAME_STRIDE:-1}"
        --short-term-template-minimum-correlation \
            "${SHORT_TERM_TEMPLATE_MINIMUM_CORRELATION:-0.72}"
        --short-term-search-expansion "${SHORT_TERM_SEARCH_EXPANSION:-2.5}"
        --short-term-occluded-search-multiplier \
            "${SHORT_TERM_OCCLUDED_SEARCH_MULTIPLIER:-1.5}"
        --short-term-reacquiring-search-multiplier \
            "${SHORT_TERM_REACQUIRING_SEARCH_MULTIPLIER:-2.0}"
        --short-term-maximum-search-expansion \
            "${SHORT_TERM_MAXIMUM_SEARCH_EXPANSION:-6.0}"
        --short-term-maximum-retained-template-age-seconds \
            "${SHORT_TERM_MAXIMUM_RETAINED_TEMPLATE_AGE_SECONDS:-2.0}"
    )
elif [[ "${SHORT_TERM_TRACKING_ENABLED}" != "0" ]]; then
    printf 'SHORT_TERM_TRACKING_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${PERSON_REID_ENABLED}" == "1" ]]; then
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'PERSON_REID_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    for path in \
        "${PERSON_REID_ONNX_PATH}" \
        "${PERSON_REID_ENGINE_PATH}" \
        "${PERSON_REID_ENGINE_SHA256_PATH}" \
        "${PERSON_REID_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            printf 'Required person ReID artifact is missing: %s\n' "${path}" >&2
            exit 2
        fi
    done
    if ! sha256sum --check --status "${PERSON_REID_ENGINE_SHA256_PATH}"; then
        printf 'Person ReID TensorRT engine failed its SHA-256 integrity check.\n' >&2
        exit 2
    fi
    "${PYTHON}" -m multidetect.engine_provenance verify \
        --engine "${PERSON_REID_ENGINE_PATH}" \
        --source-model "${PERSON_REID_ONNX_PATH}" \
        --trtexec "${TRTEXEC}" \
        --provenance "${PERSON_REID_ENGINE_PROVENANCE}"
    args+=(
        --person-reid-onnx "${PERSON_REID_ONNX_PATH}"
        --person-reid-engine "${PERSON_REID_ENGINE_PATH}"
        --person-reid-maximum-batch-size "${PERSON_REID_MAXIMUM_BATCH_SIZE:-10}"
        --person-reid-frame-stride "${PERSON_REID_FRAME_STRIDE:-2}"
    )
elif [[ "${PERSON_REID_ENABLED}" != "0" ]]; then
    printf 'PERSON_REID_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${VEHICLE_REID_ENABLED}" == "1" ]]; then
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'VEHICLE_REID_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    for path in \
        "${VEHICLE_REID_ONNX_PATH}" \
        "${VEHICLE_REID_ENGINE_PATH}" \
        "${VEHICLE_REID_ENGINE_SHA256_PATH}" \
        "${VEHICLE_REID_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            printf 'Required vehicle ReID artifact is missing: %s\n' "${path}" >&2
            exit 2
        fi
    done
    if ! sha256sum --check --status "${VEHICLE_REID_ENGINE_SHA256_PATH}"; then
        printf 'Vehicle ReID TensorRT engine failed its SHA-256 integrity check.\n' >&2
        exit 2
    fi
    "${PYTHON}" -m multidetect.engine_provenance verify \
        --engine "${VEHICLE_REID_ENGINE_PATH}" \
        --source-model "${VEHICLE_REID_ONNX_PATH}" \
        --trtexec "${TRTEXEC}" \
        --provenance "${VEHICLE_REID_ENGINE_PROVENANCE}"
    args+=(
        --vehicle-reid-onnx "${VEHICLE_REID_ONNX_PATH}"
        --vehicle-reid-engine "${VEHICLE_REID_ENGINE_PATH}"
        --vehicle-reid-maximum-batch-size "${VEHICLE_REID_MAXIMUM_BATCH_SIZE:-8}"
        --vehicle-reid-frame-stride "${VEHICLE_REID_FRAME_STRIDE:-2}"
    )
elif [[ "${VEHICLE_REID_ENABLED}" != "0" ]]; then
    printf 'VEHICLE_REID_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${PERSON_REID_ENABLED}" == "1" || "${VEHICLE_REID_ENABLED}" == "1" ]]; then
    args+=(--reid-maximum-interval-seconds "${REID_MAXIMUM_INTERVAL_SECONDS:-0.1}")
fi

if [[ "${ENVIRONMENT_RISK_DETECTOR_ENABLED}" == "1" ]]; then
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'ENVIRONMENT_RISK_DETECTOR_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    for variable in \
        ENVIRONMENT_ONNX_PATH \
        ENVIRONMENT_ENGINE_PATH \
        ENVIRONMENT_MODEL_MANIFEST \
        ENVIRONMENT_ENGINE_PROVENANCE; do
        if [[ -z "${!variable}" ]]; then
            printf '%s must be explicitly configured for the environment detector.\n' "${variable}" >&2
            exit 2
        fi
    done
    for path in \
        "${ENVIRONMENT_ONNX_PATH}" \
        "${ENVIRONMENT_ENGINE_PATH}" \
        "${ENVIRONMENT_MODEL_MANIFEST}" \
        "${ENVIRONMENT_ENGINE_PROVENANCE}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            printf 'Required environment-risk detector artifact is missing: %s\n' "${path}" >&2
            exit 2
        fi
    done
    "${PYTHON}" -m multidetect.engine_provenance verify \
        --engine "${ENVIRONMENT_ENGINE_PATH}" \
        --source-model "${ENVIRONMENT_ONNX_PATH}" \
        --trtexec "${TRTEXEC}" \
        --provenance "${ENVIRONMENT_ENGINE_PROVENANCE}"
    args+=(
        --environment-onnx-model "${ENVIRONMENT_ENGINE_PATH}"
        --environment-model-manifest "${ENVIRONMENT_MODEL_MANIFEST}"
        --environment-class-names "${ENVIRONMENT_CLASS_NAMES}"
        --environment-confidence-threshold "${ENVIRONMENT_CONFIDENCE_THRESHOLD}"
    )
elif [[ "${ENVIRONMENT_RISK_DETECTOR_ENABLED}" != "0" ]]; then
    printf 'ENVIRONMENT_RISK_DETECTOR_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${SEMANTIC_CONTEXT_ENABLED}" == "1" ]]; then
    for variable in \
        SEMANTIC_CONTEXT_ONNX_PATH \
        SEMANTIC_CONTEXT_MODEL_MANIFEST \
        SEMANTIC_CONTEXT_ENGINE_PATH \
        SEMANTIC_CONTEXT_ENGINE_PROVENANCE \
        SEMANTIC_CONTEXT_ENGINE_SHA256_PATH; do
        if [[ -z "${!variable}" ]]; then
            printf '%s must be explicitly configured for semantic context.\n' "${variable}" >&2
            exit 2
        fi
    done
    for path in \
        "${SEMANTIC_CONTEXT_ONNX_PATH}" \
        "${SEMANTIC_CONTEXT_MODEL_MANIFEST}" \
        "${SEMANTIC_CONTEXT_ENGINE_PATH}" \
        "${SEMANTIC_CONTEXT_ENGINE_PROVENANCE}" \
        "${SEMANTIC_CONTEXT_ENGINE_SHA256_PATH}" \
        "${TRTEXEC}"; do
        if [[ ! -f "${path}" ]]; then
            printf 'Required semantic-context artifact is missing: %s\n' "${path}" >&2
            exit 2
        fi
    done
    if [[ "${SEMANTIC_CONTEXT_ONNX_PATH}" != *.onnx ]]; then
        printf 'SEMANTIC_CONTEXT_ONNX_PATH must reference an ONNX artifact.\n' >&2
        exit 2
    fi
    if ! sha256sum --check --status "${SEMANTIC_CONTEXT_ENGINE_SHA256_PATH}"; then
        printf 'Semantic-context TensorRT engine failed its SHA-256 integrity check.\n' >&2
        exit 2
    fi
    "${PYTHON}" -m multidetect.engine_provenance verify \
        --engine "${SEMANTIC_CONTEXT_ENGINE_PATH}" \
        --source-model "${SEMANTIC_CONTEXT_ONNX_PATH}" \
        --trtexec "${TRTEXEC}" \
        --provenance "${SEMANTIC_CONTEXT_ENGINE_PROVENANCE}"
    args+=(
        --semantic-context-onnx-model "${SEMANTIC_CONTEXT_ONNX_PATH}"
        --semantic-context-model-manifest "${SEMANTIC_CONTEXT_MODEL_MANIFEST}"
        --semantic-context-engine "${SEMANTIC_CONTEXT_ENGINE_PATH}"
        --semantic-context-engine-provenance "${SEMANTIC_CONTEXT_ENGINE_PROVENANCE}"
        --semantic-context-trtexec "${TRTEXEC}"
        --semantic-context-minimum-interval-seconds "${SEMANTIC_CONTEXT_MINIMUM_INTERVAL_SECONDS}"
        --semantic-context-maximum-age-seconds "${SEMANTIC_CONTEXT_MAXIMUM_AGE_SECONDS}"
    )
elif [[ "${SEMANTIC_CONTEXT_ENABLED}" != "0" ]]; then
    printf 'SEMANTIC_CONTEXT_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${OPERATOR_UDP_ENABLED}" == "auto" ]]; then
    if [[ -n "${MULTIDETECT_OPERATOR_KEY:-}" && -n "${MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX:-}" ]]; then
        OPERATOR_UDP_ENABLED=1
    else
        OPERATOR_UDP_ENABLED=0
    fi
fi

if [[ "${OPERATOR_UDP_ENABLED}" == "1" ]]; then
    : "${MULTIDETECT_OPERATOR_KEY:?MULTIDETECT_OPERATOR_KEY is required when OPERATOR_UDP_ENABLED=1}"
    : "${MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX:?MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX is required when OPERATOR_UDP_ENABLED=1}"
    args+=(
        --operator-udp-port "${OPERATOR_UDP_PORT:-14580}"
        --operator-udp-bind-host "${OPERATOR_UDP_BIND_HOST:-0.0.0.0}"
        --operator-hmac-key-env MULTIDETECT_OPERATOR_KEY
        --mavlink-signing-key-hex-env MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX
        --operator-stream-id "${MULTIDETECT_OPERATOR_STREAM_ID:-camera-main}"
        --operator-source-width "${MULTIDETECT_OPERATOR_STREAM_WIDTH:-1280}"
        --operator-source-height "${MULTIDETECT_OPERATOR_STREAM_HEIGHT:-720}"
        --operator-source-rotation "${MULTIDETECT_OPERATOR_STREAM_ROTATION:-0}"
        --operator-local-system-id 1
        --operator-local-component-id 191
        --operator-remote-system-id 255
        --operator-remote-component-id 190
    )
elif [[ "${OPERATOR_UDP_ENABLED}" != "0" ]]; then
    printf 'OPERATOR_UDP_ENABLED must be auto, 0 or 1.\n' >&2
    exit 2
fi

if [[ "${MODE3_CONFIRMATION_ENABLED}" == "1" ]]; then
    if [[ "${OPERATOR_UDP_ENABLED}" != "1" ]]; then
        printf 'MODE3_CONFIRMATION_ENABLED=1 requires OPERATOR_UDP_ENABLED=1.\n' >&2
        exit 2
    fi
    if [[ "${UNIFIED_TARGET_POOL_ENABLED}" != "1" ]]; then
        printf 'MODE3_CONFIRMATION_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1.\n' >&2
        exit 2
    fi
    if [[ "${MONOCULAR_AVOIDANCE_ENABLED}" != "1" ]]; then
        printf 'MODE3_CONFIRMATION_ENABLED=1 requires MONOCULAR_AVOIDANCE_ENABLED=1.\n' >&2
        exit 2
    fi
    if [[ "${MULTIMODAL_RANGING_ENABLED}" != "1" ]]; then
        printf 'MODE3_CONFIRMATION_ENABLED=1 requires MULTIMODAL_RANGING_ENABLED=1.\n' >&2
        exit 2
    fi
    args+=(--mode3-aim)
elif [[ "${MODE3_CONFIRMATION_ENABLED}" != "0" ]]; then
    printf 'MODE3_CONFIRMATION_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${MODE3_AIM_CONTROL_ENABLED}" == "1" ]]; then
    if [[ "${MODE3_CONFIRMATION_ENABLED}" != "1" ]]; then
        printf 'MODE3_AIM_CONTROL_ENABLED=1 requires MODE3_CONFIRMATION_ENABLED=1.\n' >&2
        exit 2
    fi
    args+=(
        --fixed-wing-aim-control
        --aim-maximum-target-age-seconds "${AIM_MAXIMUM_TARGET_AGE_SECONDS:-0.30}"
        --aim-maximum-attitude-age-seconds "${AIM_MAXIMUM_ATTITUDE_AGE_SECONDS:-0.50}"
        --aim-minimum-airspeed-mps "${AIM_MINIMUM_AIRSPEED_MPS:-12.0}"
        --aim-minimum-altitude-agl-m "${AIM_MINIMUM_ALTITUDE_AGL_M:-8.0}"
        --aim-maximum-abs-roll-deg "${AIM_MAXIMUM_ABS_ROLL_DEG:-20.0}"
        --aim-maximum-abs-pitch-deg "${AIM_MAXIMUM_ABS_PITCH_DEG:-15.0}"
        --aim-maximum-roll-correction-deg "${AIM_MAXIMUM_ROLL_CORRECTION_DEG:-10.0}"
        --aim-maximum-pitch-correction-deg "${AIM_MAXIMUM_PITCH_CORRECTION_DEG:-6.0}"
        --aim-roll-gain "${AIM_ROLL_GAIN:-0.70}"
        --aim-pitch-gain "${AIM_PITCH_GAIN:-0.70}"
        --aim-maximum-roll-slew-deg-s "${AIM_MAXIMUM_ROLL_SLEW_DEG_S:-35.0}"
        --aim-maximum-pitch-slew-deg-s "${AIM_MAXIMUM_PITCH_SLEW_DEG_S:-25.0}"
        --aim-prestream-setpoints "${AIM_PRESTREAM_SETPOINTS:-10}"
        --aim-control-mode "${AIM_CONTROL_MODE:-OFFBOARD}"
        --aim-return-mode "${AIM_RETURN_MODE:-AUTO}"
        --aim-rc-input-rate-hz "${AIM_RC_INPUT_RATE_HZ:-20.0}"
        --aim-rc-input-maximum-age-seconds "${AIM_RC_INPUT_MAXIMUM_AGE_SECONDS:-0.30}"
        --aim-rc-cancel-threshold-us "${AIM_RC_CANCEL_THRESHOLD_US:-50}"
    )
elif [[ "${MODE3_AIM_CONTROL_ENABLED}" != "0" ]]; then
    printf 'MODE3_AIM_CONTROL_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${MAX_FRAMES}" =~ ^[1-9][0-9]*$ ]]; then
    args+=(--max-frames "${MAX_FRAMES}")
elif [[ "${MAX_FRAMES}" != "0" ]]; then
    printf 'MAX_FRAMES must be 0 or a positive integer.\n' >&2
    exit 2
fi

printf 'Starting Jetson RTSP recognition with V6X telemetry.\n'
printf 'Unified target pool: %s; person ReID: %s (metadata only).\n' \
    "${UNIFIED_TARGET_POOL_ENABLED}" "${PERSON_REID_ENABLED}"
printf 'Common COCO80 object detector: %s (metadata and safety evidence only).\n' \
    "${COMMON_OBJECT_DETECTOR_ENABLED}"
printf 'VisDrone priority object detector: %s (metadata and safety evidence only).\n' \
    "${PRIORITY_OBJECT_DETECTOR_ENABLED}"
printf 'Environment-risk detector: %s (metadata only; isolated label domain).\n' \
    "${ENVIRONMENT_RISK_DETECTOR_ENABLED}"
printf 'Road/building semantic context: %s (bounded low-rate advisory only).\n' \
    "${SEMANTIC_CONTEXT_ENABLED}"
printf 'Monocular avoidance / optical flow: %s.\n' "${MONOCULAR_AVOIDANCE_ENABLED}"
printf 'Short-term optical-flow/template tracking: %s (prediction hints only).\n' \
    "${SHORT_TERM_TRACKING_ENABLED}"
printf 'Timestamped multimodal ranging: %s.\n' \
    "${MULTIMODAL_RANGING_ENABLED}"
printf 'Mode-3 signed execution confirmation: %s.\n' "${MODE3_CONFIRMATION_ENABLED}"
printf 'Mode-3 fixed-wing attitude control: %s.\n' "${MODE3_AIM_CONTROL_ENABLED}"
printf 'Audit: %s\nPredictions: %s\n' "${audit_out}" "${prediction_out}"
exec "${PYTHON}" "${args[@]}"

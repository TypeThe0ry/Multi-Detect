#!/usr/bin/env bash
set -euo pipefail

# Bench launcher for the target Jetson. It receives Pixhawk MAVLink only and
# contains no flight-control, mission-write, actuator, or payload command path.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
CONFIG="${MISSION_CONFIG:-${ROOT}/configs/missions/fire_patrol.demo.json}"
MODEL="${FIRE_MODEL_PATH:-${ROOT}/models/fire-smoke-v5-trt86/best.opset17.fp16.engine}"
MANIFEST="${FIRE_MODEL_MANIFEST:-${ROOT}/models/fire-smoke-v5-trt86/best.opset17.fp16.manifest.json}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${ROOT}/artifacts/deployment/jetson-bench}"
MAX_FRAMES="${MAX_FRAMES:-0}"
CAMERA_RECONNECT_ATTEMPTS="${CAMERA_RECONNECT_ATTEMPTS:-30}"
CAMERA_RECONNECT_DELAY_SECONDS="${CAMERA_RECONNECT_DELAY_SECONDS:-1}"
OPERATOR_UDP_ENABLED="${OPERATOR_UDP_ENABLED:-0}"

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

args=(
    -m multidetect live-camera "${CONFIG}"
    --source-env CAMERA_SOURCE
    --rtsp-transport tcp
    --rtsp-codec h265
    --backend gstreamer
    --gstreamer-hardware-decode
    --gstreamer-latency-ms 100
    --reconnect-attempts "${CAMERA_RECONNECT_ATTEMPTS}"
    --reconnect-delay-seconds "${CAMERA_RECONNECT_DELAY_SECONDS}"
    --onnx-model "${MODEL}"
    --model-manifest "${MANIFEST}"
    --class-names flame,smoke
    --output-coordinates letterbox_xyxy_px
    --confidence-threshold 0.10
    --flame-confidence-threshold 0.72
    --smoke-confidence-threshold 0.60
    --candidate-stability-frames 6
    --capture-queue-frames 4
    --pixhawk-endpoint udp:0.0.0.0:14550
    --pixhawk-baud 921600
    --pixhawk-system-id 1
    --pixhawk-expected-autopilot px4
    --pixhawk-expected-vehicle-type fixed_wing
    --no-display
    --audit-out "${audit_out}"
    --prediction-log-out "${prediction_out}"
)

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
    printf 'OPERATOR_UDP_ENABLED must be 0 or 1.\n' >&2
    exit 2
fi

if [[ "${MAX_FRAMES}" =~ ^[1-9][0-9]*$ ]]; then
    args+=(--max-frames "${MAX_FRAMES}")
elif [[ "${MAX_FRAMES}" != "0" ]]; then
    printf 'MAX_FRAMES must be 0 or a positive integer.\n' >&2
    exit 2
fi

printf 'Starting Jetson RTSP recognition with read-only V6X telemetry.\n'
printf 'Audit: %s\nPredictions: %s\n' "${audit_out}" "${prediction_out}"
exec "${PYTHON}" "${args[@]}"

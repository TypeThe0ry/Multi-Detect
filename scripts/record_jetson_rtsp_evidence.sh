#!/usr/bin/env bash
set -euo pipefail

# Independent evidence recorder. It never starts flight control, payload control,
# the detector, or QGC, and it passes only an environment-variable name to Python.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${ROOT}/artifacts/tracking}"
DURATION_SECONDS="${DURATION_SECONDS:-300}"
GSTREAMER_LATENCY_MS="${GSTREAMER_LATENCY_MS:-100}"
SOURCE_ENV_NAME="${SOURCE_ENV_NAME:-CAMERA_SOURCE}"
FRAME_STRIDE="${FRAME_STRIDE:-1}"
FRAME_LIMIT="${FRAME_LIMIT:-}"
tracking_evidence_session_id="${TRACKING_EVIDENCE_SESSION_ID:-}"

if [[ ! -x "${PYTHON}" ]]; then
    printf 'Python runtime is unavailable: %s\n' "${PYTHON}" >&2
    exit 2
fi
if [[ -z "${tracking_evidence_session_id}" ]]; then
    printf 'TRACKING_EVIDENCE_SESSION_ID is required and must match the live tracker session.\n' >&2
    exit 2
fi
if [[ -z "${!SOURCE_ENV_NAME:-}" ]]; then
    printf 'RTSP source environment variable is not configured: %s\n' "${SOURCE_ENV_NAME}" >&2
    exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
session_root="${SESSION_ROOT:-${EVIDENCE_DIR}/jetson-rtsp-${timestamp}}"
video_out="${session_root}/capture.mkv"
manifest_out="${session_root}/capture.manifest.json"
raw_replay_out="${session_root}/raw-replay"
inventory_out="${session_root}/transfer-inventory.json"

if [[ -e "${session_root}" ]]; then
    printf 'Evidence session root already exists; refusing overwrite: %s\n' "${session_root}" >&2
    exit 2
fi
mkdir -p "${session_root}"

on_exit() {
    status="$?"
    if [[ "${status}" != "0" ]]; then
        printf 'RTSP evidence session stopped with exit %s; partial evidence retained at: %s\n' \
            "${status}" "${session_root}" >&2
    fi
}
trap on_exit EXIT

"${PYTHON}" -m multidetect record-rtsp-evidence \
    --source-env "${SOURCE_ENV_NAME}" \
    --session-id "${tracking_evidence_session_id}" \
    --out-video "${video_out}" \
    --manifest-out "${manifest_out}" \
    --duration-seconds "${DURATION_SECONDS}" \
    --latency-ms "${GSTREAMER_LATENCY_MS}"

conversion_args=(
    "${manifest_out}"
    "${raw_replay_out}"
    --recording-id "rtsp-${tracking_evidence_session_id}"
    --frame-stride "${FRAME_STRIDE}"
)
if [[ -n "${FRAME_LIMIT}" ]]; then
    conversion_args+=(--frame-limit "${FRAME_LIMIT}")
fi
"${PYTHON}" -m multidetect.rtsp_evidence_replay "${conversion_args[@]}"

"${PYTHON}" -m multidetect.evidence_bundle_transfer snapshot \
    "${session_root}" \
    "${inventory_out}" \
    --bundle-id "${tracking_evidence_session_id}"

trap - EXIT
printf 'RTSP_EVIDENCE_SESSION_READY=%s\n' "${session_root}"
printf 'RTSP_EVIDENCE_TRANSFER_INVENTORY=%s\n' "${inventory_out}"

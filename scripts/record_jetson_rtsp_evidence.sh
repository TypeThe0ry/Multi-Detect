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

mkdir -p "${EVIDENCE_DIR}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
video_out="${EVIDENCE_DIR}/jetson-rtsp-${timestamp}.mkv"
manifest_out="${EVIDENCE_DIR}/jetson-rtsp-${timestamp}.manifest.json"

exec "${PYTHON}" -m multidetect record-rtsp-evidence \
    --source-env "${SOURCE_ENV_NAME}" \
    --session-id "${tracking_evidence_session_id}" \
    --out-video "${video_out}" \
    --manifest-out "${manifest_out}" \
    --duration-seconds "${DURATION_SECONDS}" \
    --latency-ms "${GSTREAMER_LATENCY_MS}"

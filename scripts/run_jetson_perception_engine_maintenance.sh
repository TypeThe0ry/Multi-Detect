#!/usr/bin/env bash
set -euo pipefail

# Ground-maintenance-only orchestration for the common-object and two ReID
# TensorRT engines. This script deliberately never stops or restarts recognition,
# writes flight-control data, or touches an actuator/payload interface.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
TRTEXEC="${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${ROOT}/artifacts/evaluation}"
ACK_EXPECTED="recognition-stopped-ground-maintenance-only"
ACK_ACTUAL="${MULTIDETECT_REID_MAINTENANCE_ACK:-}"
LOCK_PATH="${MULTIDETECT_REID_MAINTENANCE_LOCK:-/tmp/multidetect-reid-maintenance.lock}"

PERSON_ONNX="${PERSON_REID_ONNX:-${ROOT}/models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.onnx}"
PERSON_ENGINE="${PERSON_REID_ENGINE:-${ROOT}/models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.b1-b10.fp16.trt86.engine}"
VEHICLE_ONNX="${VEHICLE_REID_ONNX:-${ROOT}/models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.onnx}"
VEHICLE_ENGINE="${VEHICLE_REID_ENGINE:-${ROOT}/models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.b1-b8.fp16.trt86.engine}"

if [[ "${ACK_ACTUAL}" != "${ACK_EXPECTED}" ]]; then
    printf 'Refusing perception-engine maintenance without the exact ground-maintenance acknowledgement.\n' >&2
    printf 'Set MULTIDETECT_REID_MAINTENANCE_ACK=%s only after recognition is intentionally stopped.\n' \
        "${ACK_EXPECTED}" >&2
    exit 2
fi

for path in \
    "${PYTHON}" \
    "${TRTEXEC}" \
    "${ROOT}/scripts/build_jetson_common_detector_engine.sh" \
    "${ROOT}/scripts/build_jetson_reid_engine.sh" \
    "${ROOT}/scripts/build_jetson_vehicle_reid_engine.sh"; do
    if [[ ! -e "${path}" ]]; then
        printf 'Required maintenance path is missing: %s\n' "${path}" >&2
        exit 2
    fi
done

if pgrep -f 'multidetect live-camera' >/dev/null; then
    printf 'Live recognition is still running; maintenance did not start.\n' >&2
    printf 'This script does not stop or restart that process.\n' >&2
    exit 3
fi

if ! command -v flock >/dev/null 2>&1; then
    printf 'flock is required to serialize the maintenance window.\n' >&2
    exit 2
fi
exec 9>"${LOCK_PATH}"
if ! flock -n 9; then
    printf 'Another perception-engine maintenance process already owns %s.\n' "${LOCK_PATH}" >&2
    exit 4
fi

mkdir -p "${EVIDENCE_DIR}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
reid_report="${EVIDENCE_DIR}/jetson-reid-tensorrt-acceptance-${timestamp}.json"

"${ROOT}/scripts/build_jetson_common_detector_engine.sh"
"${ROOT}/scripts/build_jetson_reid_engine.sh"
"${ROOT}/scripts/build_jetson_vehicle_reid_engine.sh"

sha256sum --check --status "${PERSON_ENGINE}.sha256"
sha256sum --check --status "${VEHICLE_ENGINE}.sha256"
"${PYTHON}" -m multidetect.engine_provenance verify \
    --engine "${PERSON_ENGINE}" \
    --source-model "${PERSON_ONNX}" \
    --trtexec "${TRTEXEC}" \
    --provenance "${PERSON_ENGINE}.provenance.json"
"${PYTHON}" -m multidetect.engine_provenance verify \
    --engine "${VEHICLE_ENGINE}" \
    --source-model "${VEHICLE_ONNX}" \
    --trtexec "${TRTEXEC}" \
    --provenance "${VEHICLE_ENGINE}.provenance.json"

"${PYTHON}" -m multidetect reid-tensorrt-bench \
    --person-model "${PERSON_ONNX}" \
    --vehicle-model "${VEHICLE_ONNX}" \
    --person-engine "${PERSON_ENGINE}" \
    --vehicle-engine "${VEHICLE_ENGINE}" \
    --person-count "${PERSON_REID_BENCH_COUNT:-4}" \
    --vehicle-count "${VEHICLE_REID_BENCH_COUNT:-4}" \
    --iterations "${REID_TENSORRT_BENCH_ITERATIONS:-20}" \
    --realtime-frame-budget-ms "${REID_TENSORRT_FRAME_BUDGET_MS:-66.7}" \
    --out "${reid_report}"

printf 'Perception engine maintenance and ReID TensorRT gate passed.\n'
printf 'Evidence: %s\n' "${reid_report}"
printf 'Recognition remains stopped; this script intentionally performs no restart.\n'
printf 'Flight control and physical payload release remain disabled.\n'

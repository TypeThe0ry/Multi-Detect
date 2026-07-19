#!/usr/bin/env bash
set -euo pipefail

# Builds an Orin-local TensorRT engine. It never starts, stops, or changes the
# live service and contains no flight-control or payload interface.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
ONNX="${PERSON_REID_ONNX:-${ROOT}/models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.onnx}"
ENGINE="${PERSON_REID_ENGINE:-${ROOT}/models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.b1-b10.fp16.trt86.engine}"
TRTEXEC="${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}"
PROVENANCE="${PERSON_REID_ENGINE_PROVENANCE:-${ENGINE}.provenance.json}"
EXPECTED_SHA256="0e21d09278508ec835955f422a9fdd3cd59b2a6ecdef98d705f388f33cebac2b"

for path in "${PYTHON}" "${ONNX}" "${TRTEXEC}"; do
    if [[ ! -e "${path}" ]]; then
        printf 'Required ReID build path is missing: %s\n' "${path}" >&2
        exit 2
    fi
done

actual_sha256="$(sha256sum "${ONNX}" | cut -d' ' -f1)"
if [[ "${actual_sha256}" != "${EXPECTED_SHA256}" ]]; then
    printf 'Person ReID ONNX SHA-256 does not match the pinned NVIDIA artifact.\n' >&2
    exit 2
fi

if pgrep -f 'multidetect live-camera' >/dev/null && [[ "${ALLOW_CONCURRENT_ENGINE_BUILD:-0}" != "1" ]]; then
    printf 'Live recognition is running; refusing a concurrent TensorRT engine build.\n' >&2
    printf 'Use a planned service window. Do not override this on an airborne system.\n' >&2
    exit 3
fi

mkdir -p "$(dirname "${ENGINE}")"
temporary_engine="${ENGINE}.building"
build_log="${ENGINE}.build.log"
rm -f "${temporary_engine}"

"${TRTEXEC}" \
    --onnx="${ONNX}" \
    --saveEngine="${temporary_engine}" \
    --minShapes=input:1x3x256x128 \
    --optShapes=input:8x3x256x128 \
    --maxShapes=input:10x3x256x128 \
    --fp16 \
    --workspace=1024 \
    --builderOptimizationLevel=3 \
    --profilingVerbosity=detailed \
    --skipInference \
    2>&1 | tee "${build_log}"

if [[ ! -s "${temporary_engine}" ]]; then
    printf 'TensorRT did not produce a non-empty person ReID engine.\n' >&2
    exit 4
fi

mv "${temporary_engine}" "${ENGINE}"
sha256sum "${ENGINE}" >"${ENGINE}.sha256"
"${PYTHON}" -m multidetect.engine_provenance write \
    --engine "${ENGINE}" \
    --source-model "${ONNX}" \
    --source-hash-algorithm sha256 \
    --expected-source-digest "${EXPECTED_SHA256}" \
    --precision fp16 \
    --min-shapes input:1x3x256x128 \
    --opt-shapes input:8x3x256x128 \
    --max-shapes input:10x3x256x128 \
    --trtexec "${TRTEXEC}" \
    --out "${PROVENANCE}"
printf 'Person ReID TensorRT engine built: %s\n' "${ENGINE}"
printf 'Target runtime provenance written: %s\n' "${PROVENANCE}"
printf 'This artifact provides perception metadata only; flight control remains disabled.\n'

#!/usr/bin/env bash
set -euo pipefail

# Builds the pinned OpenVINO vehicle-reid-0001 engine on the target Orin.
# It never starts, stops, or changes the live service and has no flight-control,
# actuator, or payload interface.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
ONNX="${VEHICLE_REID_ONNX:-${ROOT}/models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.onnx}"
ENGINE="${VEHICLE_REID_ENGINE:-${ROOT}/models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.b1-b8.fp16.trt86.engine}"
TRTEXEC="${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}"
PROVENANCE="${VEHICLE_REID_ENGINE_PROVENANCE:-${ENGINE}.provenance.json}"
EXPECTED_SHA384="0515ce72f653c39780d5b87dfed7255d396dd2b1e8b6e91fbaacdfad1da189166343157273c02f3b0fede3050ef7abb7"

for path in "${PYTHON}" "${ONNX}" "${TRTEXEC}"; do
    if [[ ! -e "${path}" ]]; then
        printf 'Required vehicle ReID build path is missing: %s\n' "${path}" >&2
        exit 2
    fi
done

actual_sha384="$(sha384sum "${ONNX}" | cut -d' ' -f1)"
if [[ "${actual_sha384}" != "${EXPECTED_SHA384}" ]]; then
    printf 'Vehicle ReID ONNX SHA-384 does not match the pinned OpenVINO artifact.\n' >&2
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
    --minShapes=input:1x3x208x208 \
    --optShapes=input:4x3x208x208 \
    --maxShapes=input:8x3x208x208 \
    --fp16 \
    --workspace=1024 \
    --builderOptimizationLevel=3 \
    --profilingVerbosity=detailed \
    --skipInference \
    2>&1 | tee "${build_log}"

if [[ ! -s "${temporary_engine}" ]]; then
    printf 'TensorRT did not produce a non-empty vehicle ReID engine.\n' >&2
    exit 4
fi

mv "${temporary_engine}" "${ENGINE}"
sha256sum "${ENGINE}" >"${ENGINE}.sha256"
"${PYTHON}" -m multidetect.engine_provenance write \
    --engine "${ENGINE}" \
    --source-model "${ONNX}" \
    --source-hash-algorithm sha384 \
    --expected-source-digest "${EXPECTED_SHA384}" \
    --precision fp16 \
    --min-shapes input:1x3x208x208 \
    --opt-shapes input:4x3x208x208 \
    --max-shapes input:8x3x208x208 \
    --trtexec "${TRTEXEC}" \
    --out "${PROVENANCE}"
printf 'Vehicle ReID TensorRT engine built: %s\n' "${ENGINE}"
printf 'Target runtime provenance written: %s\n' "${PROVENANCE}"
printf 'This artifact provides identity metadata only; flight control remains disabled.\n'

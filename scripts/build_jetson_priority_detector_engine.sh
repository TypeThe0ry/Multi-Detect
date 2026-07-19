#!/usr/bin/env bash
set -euo pipefail

# Builds the VisDrone priority detector on its target Orin. This maintenance
# tool only creates a TensorRT artifact and its integrity metadata.
ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
ONNX="${PRIORITY_OBJECT_ONNX:-${ROOT}/models/visdrone-yolo26n-e30-960/best.onnx}"
ENGINE="${PRIORITY_OBJECT_ENGINE:-${ROOT}/models/visdrone-yolo26n-e30-960/best.b1.fp16.trt86.engine}"
PROVENANCE="${PRIORITY_OBJECT_ENGINE_PROVENANCE:-${ENGINE}.provenance.json}"
MANIFEST="${PRIORITY_OBJECT_MODEL_MANIFEST:-${ENGINE}.manifest.json}"
TRTEXEC="${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}"
TIMING_CACHE="${PRIORITY_OBJECT_TIMING_CACHE:-${ENGINE}.timing.cache}"
EXPECTED_SHA256="${PRIORITY_OBJECT_EXPECTED_SHA256:-}"
MODEL_VERSION="${PRIORITY_OBJECT_MODEL_VERSION:-visdrone-e30-960-20260716}"
CLASS_NAMES="${PRIORITY_OBJECT_CLASS_NAMES:-pedestrian,people,bicycle,car,van,truck,tricycle,awning-tricycle,bus,motor}"

for path in "${PYTHON}" "${ONNX}" "${TRTEXEC}"; do
    if [[ ! -e "${path}" ]]; then
        printf 'Required priority-detector build path is missing: %s\n' "${path}" >&2
        exit 2
    fi
done

if [[ -z "${EXPECTED_SHA256}" ]]; then
    hash_sidecar="${ONNX}.sha256"
    if [[ ! -f "${hash_sidecar}" ]]; then
        printf 'Set PRIORITY_OBJECT_EXPECTED_SHA256 or provide %s\n' "${hash_sidecar}" >&2
        exit 2
    fi
    EXPECTED_SHA256="$(awk 'NR == 1 { print $1 }' "${hash_sidecar}")"
fi
if [[ ! "${EXPECTED_SHA256}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    printf 'Priority-detector expected SHA-256 is malformed.\n' >&2
    exit 2
fi

actual_sha256="$(sha256sum "${ONNX}" | cut -d' ' -f1)"
if [[ "${actual_sha256}" != "${EXPECTED_SHA256}" ]]; then
    printf 'Priority-detector ONNX SHA-256 does not match the pinned artifact.\n' >&2
    exit 2
fi

if pgrep -f 'multidetect live-camera' >/dev/null && [[ "${ALLOW_CONCURRENT_ENGINE_BUILD:-0}" != "1" ]]; then
    printf 'Live recognition is running; refusing a concurrent TensorRT engine build.\n' >&2
    exit 3
fi

mkdir -p "$(dirname "${ENGINE}")"
temporary_engine="${ENGINE}.building"
build_log="${ENGINE}.build.log"
rm -f "${temporary_engine}"

"${TRTEXEC}" \
    --onnx="${ONNX}" \
    --saveEngine="${temporary_engine}" \
    --fp16 \
    --workspace=1024 \
    --builderOptimizationLevel=3 \
    --timingCacheFile="${TIMING_CACHE}" \
    --profilingVerbosity=detailed \
    --skipInference \
    2>&1 | tee "${build_log}"

if [[ ! -s "${temporary_engine}" ]]; then
    printf 'TensorRT did not produce a non-empty priority-detector engine.\n' >&2
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
    --min-shapes images:1x3x960x960 \
    --opt-shapes images:1x3x960x960 \
    --max-shapes images:1x3x960x960 \
    --trtexec "${TRTEXEC}" \
    --out "${PROVENANCE}"

"${PYTHON}" -m multidetect model-manifest-init \
    --model-artifact "${ENGINE}" \
    --out "${MANIFEST}" \
    --model-id yolo26n-visdrone-priority-trt86-raw \
    --model-version "${MODEL_VERSION}" \
    --source-description "TensorRT 8.6 FP16 engine built on the target Orin NX from the trained VisDrone priority detector" \
    --model-role safety_object_evidence \
    --class-names "${CLASS_NAMES}" \
    --input-width 960 \
    --input-height 960 \
    --output-coordinates letterbox_xyxy_px \
    --native-output-format ultralytics_raw_xywh_class_scores \
    --force

printf 'Priority-object TensorRT engine built: %s\n' "${ENGINE}"
printf 'Target runtime provenance written: %s\n' "${PROVENANCE}"
printf 'Hash-bound runtime manifest written: %s\n' "${MANIFEST}"

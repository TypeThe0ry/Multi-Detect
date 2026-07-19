#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIDETECT_ROOT:-/home/jetson/Multi-Detect}"
PYTHON="${MULTIDETECT_PYTHON:-${ROOT}/.venv/bin/python}"
TRTEXEC="${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}"
MODEL_DIR="${SEMANTIC_CONTEXT_MODEL_DIR:-${ROOT}/models/environment/citysemsegformer/deployable_onnx_v1.0}"
ONNX="${SEMANTIC_CONTEXT_ONNX_PATH:-${MODEL_DIR}/citysemsegformer.onnx}"
MANIFEST="${SEMANTIC_CONTEXT_MODEL_MANIFEST:-${MODEL_DIR}/citysemsegformer.manifest.json}"
ENGINE="${SEMANTIC_CONTEXT_ENGINE_PATH:-${MODEL_DIR}/citysemsegformer.b1.fp16.trt86.engine}"
PROVENANCE="${SEMANTIC_CONTEXT_ENGINE_PROVENANCE:-${ENGINE}.provenance.json}"
ENGINE_SHA256_PATH="${SEMANTIC_CONTEXT_ENGINE_SHA256_PATH:-${ENGINE}.sha256}"
EXPECTED_ONNX_SHA256="94ace62e250ed0a3122a46df8573950510b60a90c1b511e53c40dbca2bea21fb"
INPUT_SHAPE="input:1x3x1024x1820"

if pgrep -f 'multidetect live-camera' >/dev/null; then
    printf 'Refusing a concurrent TensorRT engine build while live recognition is running.\n' >&2
    exit 3
fi

for path in "${PYTHON}" "${TRTEXEC}" "${ONNX}" "${MANIFEST}"; do
    if [[ ! -f "${path}" ]]; then
        printf 'Required semantic-context build artifact is missing: %s\n' "${path}" >&2
        exit 2
    fi
done

actual_sha256="$(sha256sum "${ONNX}" | awk '{print $1}')"
if [[ "${actual_sha256}" != "${EXPECTED_ONNX_SHA256}" ]]; then
    printf 'CitySemSegFormer ONNX SHA-256 does not match the NVIDIA NGC artifact.\n' >&2
    exit 2
fi

"${PYTHON}" -c \
    'from pathlib import Path; from multidetect.model_manifest import verify_model_manifest; from multidetect.semantic_environment import CITYSEMSEGFORMER_LABELS; import sys; verify_model_manifest(Path(sys.argv[1]), Path(sys.argv[2]), expected_class_names=CITYSEMSEGFORMER_LABELS, expected_model_role="semantic_scene_context", expected_output_format="categorical_H_W_1")' \
    "${MANIFEST}" "${ONNX}"

mkdir -p "$(dirname "${ENGINE}")"
temporary_engine="${ENGINE}.building"
rm -f "${temporary_engine}"
"${TRTEXEC}" \
    --onnx="${ONNX}" \
    --saveEngine="${temporary_engine}" \
    --fp16 \
    --minShapes="${INPUT_SHAPE}" \
    --optShapes="${INPUT_SHAPE}" \
    --maxShapes="${INPUT_SHAPE}" \
    --skipInference
mv -f "${temporary_engine}" "${ENGINE}"

"${PYTHON}" -m multidetect.engine_provenance write \
    --engine "${ENGINE}" \
    --source-model "${ONNX}" \
    --source-hash-algorithm sha256 \
    --expected-source-digest "${EXPECTED_ONNX_SHA256}" \
    --precision fp16 \
    --min-shapes "${INPUT_SHAPE}" \
    --opt-shapes "${INPUT_SHAPE}" \
    --max-shapes "${INPUT_SHAPE}" \
    --trtexec "${TRTEXEC}" \
    --out "${PROVENANCE}"

(
    cd "$(dirname "${ENGINE}")"
    sha256sum "$(basename "${ENGINE}")" >"$(basename "${ENGINE_SHA256_PATH}")"
)

printf 'Built semantic-context TensorRT engine: %s\n' "${ENGINE}"
printf 'Provenance: %s\n' "${PROVENANCE}"
printf 'Flight control and physical release remain disabled.\n'

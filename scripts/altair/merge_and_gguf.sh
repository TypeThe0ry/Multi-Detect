#!/usr/bin/env bash
# Merge an Altair LoRA into the base model, convert to GGUF and quantize (GPU/CPU heavy:
# run only through the compute thread). Vision tower is frozen, so the existing mmproj stays.
# usage: bash merge_and_gguf.sh ADAPTER_DIR OUT_DIR [QUANT=Q5_K_M]
# env: PY (python with peft/transformers), BASE (local snapshot dir), LLAMA_ROOT
set -euo pipefail
ADAPTER=${1:?adapter dir}; OUT=${2:?output dir}; QUANT=${3:-Q5_K_M}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PY=${PY:-$ROOT/.venv-vlm/bin/python}
BASE=${BASE:-$(ls -d "$ROOT"/artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/*/ | head -1)}
LLAMA_ROOT=${LLAMA_ROOT:-$ROOT/artifacts/cache/llama-cpp}
CONV=$(find "$LLAMA_ROOT" -name convert_hf_to_gguf.py | head -1)
QUANTIZE=$(find "$LLAMA_ROOT" -name llama-quantize -type f | head -1)
[ -n "$CONV" ] && [ -n "$QUANTIZE" ] || { echo "llama.cpp converter/quantize not found under $LLAMA_ROOT" >&2; exit 2; }
mkdir -p "$OUT"
"$PY" - "$BASE" "$ADAPTER" "$OUT/merged-hf" <<'PYEOF'
import sys
from peft import PeftModel
from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
base, adapter, out = sys.argv[1:4]
m = Qwen3VLForConditionalGeneration.from_pretrained(base, local_files_only=True, trust_remote_code=False, torch_dtype="auto")
m = PeftModel.from_pretrained(m, adapter, is_trainable=False).merge_and_unload()
m.save_pretrained(out, safe_serialization=True)
AutoProcessor.from_pretrained(base, local_files_only=True, trust_remote_code=False).save_pretrained(out)
PYEOF
"$PY" "$CONV" "$OUT/merged-hf" --outfile "$OUT/altair-f16.gguf" --outtype f16
"$QUANTIZE" "$OUT/altair-f16.gguf" "$OUT/altair-$QUANT.gguf" "$QUANT"
echo "$OUT/altair-$QUANT.gguf"

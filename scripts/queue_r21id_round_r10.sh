#!/usr/bin/env bash
# r21id-r10: r3 replay plus r9r confusable-pair top-up. Training is opt-in.
set -euo pipefail

R=${R:-$(pwd)}
M=${M:-/data/projects/Multi-Detect-env/Multi-Detect}
V=${V:-$M/.venv-vlm/bin/python}
BUILDER_PY=${BUILDER_PY:-$M/.venv/bin/python}
A=${A:-$M/artifacts}
CFG=$R/configs/training/identity-r10-r21id.json
OUT=${OUT:-$R/.tmp/r21id-r10}
DRY_RUN=${DRY_RUN:-1}
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$R/.tmp CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-0}
cd "$R"

BASE_DATA=$A/r21id-r3/pipeline/r15-sft
BASE_ADAPTER=$A/models/slow-system-lora-v2-r21id-r3/adapter
BASE_MODEL=$A/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203
R9R_CAMPAIGN=$A/r21id-r9rb/episodes
R9R_INDEX=$R/configs/scenarios/generated-r21id-r9r/index.json
R9R_PROPOSALS=$A/r21id-r9rb/gpu-annotate-full/proposals.jsonl
R9R_REBUILT=$A/r21id-r9rb/gpu-annotate-full/sft-rebuilt

run_preflight() {
  "$BUILDER_PY" scripts/check_identity_r10_inputs.py \
    --base-dataset "$BASE_DATA" --index "$R9R_INDEX" \
    --campaign "$R9R_CAMPAIGN" --proposals "$R9R_PROPOSALS"
}

run_preflight

run() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
  if [ "$DRY_RUN" = 0 ]; then "$@"; fi
}

mkdir -p "$OUT"
# Full annotate is pinned before the SUV positive fix; rebuild with this checkout.
run "$BUILDER_PY" scripts/build_slow_system_sft.py \
  --campaign "$R9R_CAMPAIGN" --index "$R9R_INDEX" --output "$R9R_REBUILT" \
  --proposals "$R9R_PROPOSALS" --confusable-negatives --task-text-variants \
  --crop-policy none --min-visible-fraction 0.3 --confirm-min-side-px 8 \
  --balance-identity-views --balance-confusable-negative-views --max-upsample 1 \
  --confusable-negative-view-cap 180

run "$V" scripts/cap_identity_round_additions.py \
  --input "$R9R_REBUILT" --output "$OUT/r15-additions-mix" \
  --base-train "$BASE_DATA/train.jsonl" --addition-fraction 0.2

run "$V" scripts/run_r15_round.py --merge-only --r12-dataset "$BASE_DATA" \
  --additions "$OUT/r15-additions-mix" --merged "$OUT/r15-sft" --r12-replay-fraction 1.0

run "$V" scripts/run_r15_round.py --stage train --out "$OUT" --model "$BASE_MODEL" \
  --r12-adapter "$BASE_ADAPTER" --r12-replay-fraction 1.0 --hot-start \
  --learning-rate 5e-5

if [ "$DRY_RUN" = 0 ]; then
  echo "r21id-r10 training started; run export/eval/report only after adapter completes."
else
  echo "dry-run only; no annotate, build, merge, or training stage was executed"
fi

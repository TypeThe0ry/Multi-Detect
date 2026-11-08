#!/usr/bin/env bash
# r21id-r9t: audited r7+r8 identity top-up over the historical r4/r5 roots.
set -u
R=/data/projects/Multi-Detect-env/mdwt/cx-next-identity-round
M=/data/projects/Multi-Detect-env/Multi-Detect
V=$M/.venv-vlm/bin/python
A=$M/artifacts
KEYS=$R/scripts/data/r21id-r9t-episode-keys.txt
KEY_ROOTS="--episode-key-root $A/r21id-r7a --episode-key-root $A/r21id-r8a"
LS=$A/cache/llama-cpp/b11552/llama-server
MM=$A/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf
BASE=$A/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203
OUT=$A/r21id-r9t/pipeline
DRY_OUT=$R/.tmp/r21id-r9t-dry-run
DRY=${DRY_RUN:-0}
if [ "$DRY" = 1 ]; then
  OUT=$DRY_OUT
fi
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$R/.tmp CUDA_VISIBLE_DEVICES=1 MULTIDETECT_GPU_HEAVY_MIB=20000
cd "$R" || exit 1

# run_r15_round's default holdout-from is the frozen generated-v2 index; it remains enabled.
ARGS="--out $OUT --scenarios configs/scenarios/generated-r21id-r9t --r15-index configs/scenarios/generated-r21id-r9t/index.json --episodes $A/r21id-r4d/episodes --extra-episodes $A/r21id-r4e/episodes --extra-episodes $A/r21id-r4g/episodes --extra-episodes $A/r21id-r5a/episodes --episode-keys-file $KEYS $KEY_ROOTS --source-root $M --data-root $M --low-disk --min-free-gb 12 --r12-dataset $A/r21id-r3/pipeline/r15-sft --r12-replay-fraction 1.0 --confusable-negatives --task-text-variants --task-variant-positive-cap 1.0 --balance-identity-views --balance-confusable-negative-views --max-upsample 1 --model $BASE --r12-adapter $A/models/slow-system-lora-v2-r21id-r3/adapter --hot-start --learning-rate 5e-5 --r12-baseline $A/r21id-r3/pipeline/eval-v3.json --r12-v1-baseline $A/r21id-r3/pipeline/eval-v1.json --baseline-label r21id-r3 --candidate-label r21id-r9t --llama-server $LS --mmproj $MM"

if [ "$DRY" = 1 ]; then
  echo "=== r21idr9t dry-run: audited logical keys and fixed builder counts"
  rm -rf "$DRY_OUT"
  $V scripts/run_r15_round.py --dry-run --stage build_sft $ARGS
  $V scripts/audit_r21id_r9t_data.py --index configs/scenarios/generated-r21id-r9t/index.json --output "$DRY_OUT/count-dataset" --campaign "$A/r21id-r4d/episodes" --campaign "$A/r21id-r4e/episodes" --campaign "$A/r21id-r4g/episodes" --campaign "$A/r21id-r5a/episodes" --episode-keys-file "$KEYS" --episode-key-root "$A/r21id-r7a" --episode-key-root "$A/r21id-r8a"
  echo "=== r21idr9t dry-run complete; no artifacts/ writes and no training"
  exit 0
fi

echo "=== r21idr9t start $(date -Is) head=$(git rev-parse --short HEAD) pid=$$"
for st in annotate build_sft train export eval report; do
  for try in 1 2 3 4; do
    echo "=== r21idr9t stage $st try $try $(date -Is)"
    if [ "$st" = eval ]; then
      flock /data/projects/Multi-Detect-env/runs/eval.lock $V scripts/run_r15_round.py --stage "$st" $ARGS; rc=$?
    else
      $V scripts/run_r15_round.py --stage "$st" $ARGS; rc=$?
    fi
    [ "$rc" = 0 ] && break
    [ "$st" = train ] || break
    sleep 120
  done
  [ "$rc" = 0 ] || exit 1
done

O=$A/evaluation/r21id-r9t-view-eval
mkdir -p "$O"
for m in r21id-r3:$A/models/slow-system-lora-v2-r21id-r3-merged-gguf/slow-system-v2r21idr3-merged-Q5_K_M.gguf r21id-r9t:$A/r21id-r9t/pipeline/merged-gguf/slow-system-Q5_K_M.gguf; do
  name=${m%%:*}; model=${m#*:}
  flock /data/projects/Multi-Detect-env/runs/eval.lock $V scripts/eval_slow_system.py --dataset $A/r21id-r3/pipeline/r15-additions --data-root $M --backend gguf --identity-only --fast-identity --crop-policy candidate_zoom_v2 --gguf-model "$model" --mmproj "$MM" --llama-server "$LS" --port 9947 --output "$O/$name-predictions.json" || exit 1
done
$V scripts/eval_r21id_identity.py --dataset $A/r21id-r3/pipeline/r15-additions/test.jsonl --baseline "$O/r21id-r3-predictions.json" --candidate "$O/r21id-r9t-predictions.json" --output-json "$O/identity-report.json" --output-md "$O/identity-report.md" --baseline-label r21id-r3 --candidate-label r21id-r9t
echo "=== r21idr9t ALL_DONE $(date -Is)"

#!/usr/bin/env bash
# Export an Altair adapter to GGUF, evaluate it like the incumbents, and run the gate.
# GPU job: schedule through the compute thread. Reuses the exporter and evaluators the
# r21..r28 rounds used (export_slow_system_r15.py, run_model_round_eval.py, eval_slow_system.py).
# usage: bash scripts/altair/run_candidate_eval.sh ADAPTER_DIR TAG
# default OUT: $R/artifacts/scratch/altair-eval/TAG; default ID_DATASET: artifacts/datasets/flight-identity-eval-v2-v3prop
# env overrides: R, OUT, R24_EVAL, ID_BASE, ID_DATASET, ID_FLAGS, PORT, DECISION_CONTRACT
set -euo pipefail
ADAPTER=${1:?adapter dir (the one containing adapter_config.json)}; TAG=${2:?tag}
R=${R:-/data/projects/Multi-Detect-env/Multi-Detect}; cd "$R"
PY=$R/.venv-vlm/bin/python; A=$R/artifacts
# run_model_round_eval.py only accepts --out inside the worktree (or its scratch dir).
OUT=${OUT:-$R/artifacts/scratch/altair-eval/$TAG}
R24_EVAL=${R24_EVAL:-$A/evaluation/r24-planner-round/candidate}
ID_BASE=${ID_BASE:-$A/r21id-r3/pipeline/eval-v3.json}
BASE_CACHE=$A/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots
MMPROJ=$A/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf
LLAMA=$A/cache/llama-cpp/b11552/llama-server
PORT=${PORT:-9935}
# scripts/altair may live only in this worktree, not in the main checkout $R.
SR=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH=$R/src:$R PYTHONUTF8=1
mkdir -p "$OUT"
GGUF=$OUT/gguf/slow-system-Q5_K_M.gguf

# Match the incumbent's evaluation conditions: pregate is on iff the r24 run recorded hits.
PREGATE=$("$PY" - "$R24_EVAL/planning-v4.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("summary", d)
print("--plan-pregate" if (s.get("host_pregate") or 0) > 0 else "")
PYEOF
)
CONTRACT=""; [ "${DECISION_CONTRACT:-0}" = 1 ] && CONTRACT="--decision-contract"
echo "planning flags: pregate='${PREGATE}' contract='${CONTRACT}'"

echo "== 1/4 export =="
[ -f "$GGUF" ] || "$PY" scripts/export_slow_system_r15.py --base "$BASE_CACHE" --adapter "$ADAPTER" \
  --output "$OUT/gguf" --quantization Q5_K_M --drop-intermediates

echo "== 2/4 planning eval =="
# shellcheck disable=SC2086
"$PY" scripts/run_model_round_eval.py --model "altair-$TAG" --gguf "$GGUF" --stages planning-v3 planning-v4 \
  --out "$OUT/planning" --data-root "$R" --min-free-disk-gb 8 --min-valid-rate 0 $PREGATE $CONTRACT

echo "== 3/4 identity eval =="
"$PY" - "$ID_BASE" <<'PYEOF'
import json, sys
print("incumbent identity harness:", json.dumps(json.load(open(sys.argv[1])).get("harness"), ensure_ascii=False)[:1500])
PYEOF
ID_DATASET=${ID_DATASET:-$A/datasets/flight-identity-eval-v2-v3prop}  # the 233-row r21id-r3 test set
[ -d "$ID_DATASET" ] || { echo "identity dataset not found: $ID_DATASET (set ID_DATASET)" >&2; exit 2; }
ID_FLAGS=${ID_FLAGS:---identity-only --fast-identity --crop-policy candidate_zoom_v2}
# shellcheck disable=SC2086
"$PY" scripts/eval_slow_system.py --dataset "$ID_DATASET" --data-root "$R" --backend gguf \
  --gguf-model "$GGUF" --mmproj "$MMPROJ" --llama-server "$LLAMA" --port "$PORT" $ID_FLAGS \
  --output "$OUT/identity-eval.json"

echo "== 4/4 gate =="
PYTHONPATH="$SR:$R/src:$R" "$PY" -m scripts.altair.collect_report --planning-summary "$R24_EVAL/planning-v4.json" \
  --identity-eval "$ID_BASE" --output-json "$OUT/baseline-report.json" >/dev/null
PYTHONPATH="$SR:$R/src:$R" "$PY" -m scripts.altair.collect_report --planning-summary "$OUT/planning/planning-v4.json" \
  --identity-eval "$OUT/identity-eval.json" --output-json "$OUT/candidate-report.json" >/dev/null
PYTHONPATH="$SR:$R/src:$R" "$PY" -m scripts.altair.eval_altair --baseline "$OUT/baseline-report.json" \
  --candidate "$OUT/candidate-report.json" --output-json "$OUT/gate.json"
# Row-level comparison is evidence only and must never affect the gate result.
PYTHONPATH="$SR:$R/src:$R" "$PY" -m scripts.altair.paired_report \
  --incumbent "$ID_BASE" --candidate "$OUT/identity-eval.json" \
  --dataset "$ID_DATASET/test.jsonl" --output "$OUT/paired-report.json" \
  || echo "paired report unavailable; gate remains unchanged" >&2
echo "report dir: $OUT"

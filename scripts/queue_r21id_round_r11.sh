#!/usr/bin/env bash
# Higher-resolution r10 ablation. The production model stays r21id-r3.
set -euo pipefail
R=${R:-$(cd "$(dirname "$0")/.." && pwd)}
M=${M:-/data/projects/Multi-Detect-env/Multi-Detect}
V=$M/.venv-vlm/bin/python
A=$M/artifacts
OUT=${OUT:-$A/r21id-r11/pipeline}
DATA=$A/r21id-r10/pipeline/r15-sft
MODEL=$A/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203
MMPROJ=$A/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf
LLAMA=$A/cache/llama-cpp/b11552/llama-server
export CUDA_VISIBLE_DEVICES=1 PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$R/.tmp
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 SLOW_TRAIN_GPU_MEMORY_FRACTION=0.88
mkdir -p "$R/.tmp" "$OUT"
cd "$R"

# This ablation reuses admitted r10 train rows without adding held-out tasks.
"$V" - "$M" "$OUT/input-audit.json" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
dataset = root / "artifacts/r21id-r10/pipeline/r15-sft"
train = [json.loads(line) for line in (dataset / "train.jsonl").open()]
protected = [root / "configs/onboard/plan-eval-tasks-v5.json",
             root / "artifacts/scratch/plannercmp/probe_tasks.json"]
hashes = {}
for path, count in zip(protected, (126, 15)):
    payload = json.loads(path.read_text())
    rows = payload["tasks"] if isinstance(payload, dict) else payload
    assert len(rows) == count, (str(path), len(rows))
    texts = {row.get("text", row.get("task_text")) for row in rows}
    ids = {row.get("id", row.get("sample_id")) for row in rows}
    assert not any(row["task_text"] in texts or row["sample_id"] in ids for row in train)
    hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
for name in ("train", "val", "test"):
    path = dataset / (name + ".jsonl")
    hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
report = {"train_rows": len(train), "heldout_text_or_id_overlap": 0, "sha256": hashes}
destination = pathlib.Path(sys.argv[2])
if destination.exists():
    assert json.loads(destination.read_text()) == report, "input changed since r11 launch"
else:
    destination.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
PY

train_args=(--dataset "$DATA" --model "$MODEL" --output "$OUT/adapter"
  --epochs 1 --accumulation 4 --rank 16 --alpha 32 --learning-rate 5e-5
  --max-side 672 --max-input-tokens 4096 --crop-policy candidate_zoom_v2
  --quantization bf16 --checkpoint-every 25
  --init-adapter "$A/models/slow-system-lora-v2-r21id-r3/adapter")
if [ -f "$OUT/adapter/checkpoint/state.json" ]; then train_args+=(--resume); fi
if [ ! -f "$OUT/train.complete" ]; then
  "$V" scripts/train_slow_system_lora.py "${train_args[@]}"
  "$V" - "$OUT/adapter/training-report.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report["completed"], "training did not finish"
assert not report["skipped_oom_samples"], "OOM skipped samples: candidate requires review"
PY
  touch "$OUT/train.complete"
fi
if [ ! -f "$OUT/export.complete" ]; then
  "$V" scripts/export_slow_system_r15.py --base "$MODEL" --adapter "$OUT/adapter/adapter" \
    --output "$OUT/merged-gguf" --quantization Q5_K_M
  touch "$OUT/export.complete"
fi

# Default-resolution checks remain comparable to the deployed r3 baseline.
for label in v1 v3 four-view r9r-val; do
  case "$label" in
    v1) dataset=$A/datasets/flight-identity-eval-v1 ;;
    v3) dataset=$A/datasets/flight-identity-eval-v2-v3prop ;;
    four-view) dataset=$A/r21id-r3/pipeline/r15-additions ;;
    r9r-val) dataset=$R/.tmp/r11-r9r-val
      mkdir -p "$dataset"
      "$V" - "$A/r21id-r10/data/r15-additions-mix/val.jsonl" "$dataset/test.jsonl" <<'PY'
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).open()]
pathlib.Path(sys.argv[2]).write_text("".join(json.dumps({**r, "split": "test"}, ensure_ascii=False) + "\n" for r in rows))
PY
      ;;
  esac
  if [ ! -f "$OUT/eval-$label.json" ]; then
    flock /data/projects/Multi-Detect-env/runs/eval.lock "$V" scripts/eval_slow_system.py \
      --dataset "$dataset" --data-root "$M" --backend gguf --device cuda:0 \
      --identity-only --fast-identity --crop-policy candidate_zoom_v2 --max-side 448 \
      --gguf-model "$OUT/merged-gguf/slow-system-Q5_K_M.gguf" --mmproj "$MMPROJ" \
      --llama-server "$LLAMA" --port 9951 --output "$OUT/eval-$label.json"
  fi
done
"$V" scripts/run_r15_round.py --stage report --out "$OUT" --model "$MODEL" \
  --r12-baseline "$A/r21id-r3/pipeline/eval-v3.json" \
  --r12-v1-baseline "$A/r21id-r3/pipeline/eval-v1.json" \
  --baseline-label r21id-r3 --candidate-label r21id-r11
"$V" scripts/eval_r21id_identity.py \
  --dataset "$A/r21id-r3/pipeline/r15-additions/test.jsonl" \
  --baseline "$A/r21id-r10/evaluation/r3-four-view/r21id-r3-predictions.json" \
  --candidate "$OUT/eval-four-view.json" \
  --baseline-label r21id-r3 --candidate-label r21id-r11 \
  --output-json "$OUT/four-view-report.json" --output-md "$OUT/four-view-report.md"
"$V" scripts/eval_r21id_identity.py \
  --dataset "$A/r21id-r10/data/r15-additions-mix/val.jsonl" \
  --baseline "$A/r21id-r10/evaluation/r9r-val/r21id-r3-predictions.json" \
  --candidate "$OUT/eval-r9r-val.json" \
  --baseline-label r21id-r3 --candidate-label r21id-r11 \
  --output-json "$OUT/r9r-val-report.json" --output-md "$OUT/r9r-val-report.md"
echo "=== r11 ALL_DONE $(date -Is)"

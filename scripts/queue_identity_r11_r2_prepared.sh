#!/usr/bin/env bash
# Prepared only. Do not register in jobs.d until the r11/hires review is complete.
set -euo pipefail
[ "${START_R11_R2:-0}" = 1 ] || { echo 'prepared only: wait for completed r11 and hires-pair results plus reviewed decision' >&2; exit 2; }
: "${R11_R2_REVIEW:?set the reviewed decision JSON after r11 and hires-pair complete}"
R=$(cd "$(dirname "$0")/.." && pwd)
M=/data/projects/Multi-Detect-env/Multi-Detect
A=$M/artifacts
V=$M/.venv-vlm/bin/python
OUT=$A/r21id-r11-r2/pipeline
DATA=$A/r21id-r11-r2/prepared
export CUDA_VISIBLE_DEVICES=1 PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$R/.tmp
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8
cd "$R"
"$V" - "$DATA/manifest.json" "$R11_R2_REVIEW" <<'PY'
import hashlib,json,pathlib,sys
manifest_path,review_path=map(pathlib.Path,sys.argv[1:])
sha=lambda p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
manifest=json.loads(manifest_path.read_text());review=json.loads(review_path.read_text())
assert review['decision']=='train_r11_r2' and review.get('reason'),'r11/hires evidence needs an explicit reviewed decision'
for key in ('r11_default_comparison','r11_hires_comparison'):
 report=json.loads(pathlib.Path(review[key]).read_text())
 assert report['user_rule_point_estimate_status'] in ('fail','pass_observed_scope'),'prior evaluation processing/evidence is incomplete'
 assert sha(review[key])==review[key+'_sha256'],'reviewed results changed'
assert review['prepared_manifest_sha256']==sha(manifest_path),'reviewed data changed'
for path,digest in {**manifest['input_sha256'],**manifest['output_sha256']}.items():
 assert sha(path)==digest,'prepared input or output changed: '+path
for line in (manifest_path.parent/'additions-index.jsonl').read_text().splitlines():
 row=json.loads(line)
 if row['kind']=='focus_context_crop':
  assert sha(row['source_image'])==row['source_image_sha256']
  assert sha(row['image_path'])==row['image_sha256']
# Training must run in a bounded cgroup; a bare shell is deliberately refused.
entry=next(line.split(':',2)[2] for line in pathlib.Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
limit=(pathlib.Path('/sys/fs/cgroup')/entry.lstrip('/')/'memory.max').read_text().strip()
assert limit!='max' and int(limit)<=68719476736,'launch with systemd MemoryMax=64G'
PY
rg -q '^=== models chain ALL_DONE' /data/projects/Multi-Detect-env/runs/r11_models_chain.out
rg -q '^=== r11 hires pair ALL_DONE' /data/projects/Multi-Detect-env/runs/r11_hires_pair.out
mkdir -p "$R/.tmp" "$OUT"
MODEL=$A/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203
args=()
[ ! -f "$OUT/adapter/checkpoint/state.json" ] || args+=(--resume)
if [ ! -f "$OUT/train.complete" ]; then
 "$V" scripts/train_slow_system_lora.py --dataset "$DATA" --model "$MODEL" --output "$OUT/adapter" \
   --epochs 1 --accumulation 4 --rank 16 --alpha 32 --learning-rate 5e-5 --max-side 448 \
   --max-input-tokens 4096 --crop-policy candidate_zoom_v2 --quantization bf16 --checkpoint-every 25 \
   --init-adapter "$A/models/slow-system-lora-v2-r21id-r3/adapter" "${args[@]}"
 "$V" - "$OUT/adapter/training-report.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]));assert r['completed'] and not r['skipped_oom_samples']
PY
 touch "$OUT/train.complete"
fi
if [ ! -f "$OUT/export.complete" ]; then
 "$V" scripts/export_slow_system_r15.py --base "$MODEL" --adapter "$OUT/adapter/adapter" --output "$OUT/merged-gguf" --quantization Q5_K_M
 touch "$OUT/export.complete"
fi
flock /data/projects/Multi-Detect-env/runs/eval.lock "$V" scripts/evaluate_identity_user_pair.py --execute \
  --root "$M" --baseline "$A/models/slow-system-lora-v2-r21id-r3-merged-gguf/slow-system-v2r21idr3-merged-Q5_K_M.gguf" \
  --candidate "$OUT/merged-gguf/slow-system-Q5_K_M.gguf" --out "$A/r21id-r11-r2/evaluation/matched-448" \
  --baseline-side 448 --candidate-side 448 --mode model_control
echo "=== r11-r2 prepared chain ALL_DONE $(date -Is)"

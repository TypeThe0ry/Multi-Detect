#!/usr/bin/env bash
# Run later, on an idle GPU. No downloads, training or deployment.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="${DATA_ROOT:-}"
if [[ -z "$DATA_ROOT" ]]; then
    if [[ -d "$ROOT/artifacts/cache" ]]; then
        DATA_ROOT="$ROOT"
    else
        common_dir="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
        DATA_ROOT="$(dirname -- "$common_dir")"
    fi
fi
if [[ -z "${PYTHON:-}" ]]; then
    if [[ -f "$DATA_ROOT/.venv-vlm/Scripts/python.exe" ]]; then
        PYTHON="$DATA_ROOT/.venv-vlm/Scripts/python.exe"
    elif [[ -x "$DATA_ROOT/.venv-vlm/bin/python" ]]; then
        PYTHON="$DATA_ROOT/.venv-vlm/bin/python"
    else
        PYTHON=python3
    fi
fi
GGUF="${GGUF:-$DATA_ROOT/artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf/slow-system-v2r12multi-merged-Q5_K_M.gguf}"
MMPROJ="${MMPROJ:-$DATA_ROOT/artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf}"
LLAMA_SERVER="${LLAMA_SERVER:-$DATA_ROOT/artifacts/cache/llama-cpp/b11552/llama-server.exe}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/artifacts/benchmarks/prompt-budget-v2-r12-multi-$(date -u +%Y%m%dT%H%M%SZ)}"
PORT="${PORT:-9957}"
dry_run=false
collect_only=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) dry_run=true; shift ;;
        --collect-only) collect_only=true; shift ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --help|-h)
            printf '%s\n' 'Usage: bash scripts/run_prompt_budget_eval.sh [--dry-run] [--output-dir DIR] [--collect-only]' \
                'Run only after the GPU is free. Overrides: DATA_ROOT, PYTHON, GGUF, MMPROJ, LLAMA_SERVER, OUTPUT_DIR, PORT.'
            exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done
# Every generated report stays inside this checkout, including worktree use.
OUTPUT_DIR="$([[ "$OUTPUT_DIR" = /* || "$OUTPUT_DIR" =~ ^[A-Za-z]: ]] && printf '%s' "$OUTPUT_DIR" || printf '%s/%s' "$ROOT" "$OUTPUT_DIR")"
"$PYTHON" -c 'import pathlib,sys; pathlib.Path(sys.argv[2]).resolve().relative_to(pathlib.Path(sys.argv[1]).resolve())' "$ROOT" "$OUTPUT_DIR"

export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export PYTHONDONTWRITEBYTECODE=1
export TMP="$ROOT/tmp" TEMP="$ROOT/tmp" TMPDIR="$ROOT/tmp"

run() {
    if "$dry_run"; then
        printf '%q ' "$@"
        printf '\n'
    else
        "$@"
    fi
}

if ! "$dry_run"; then
    mkdir -p -- "$OUTPUT_DIR" "$ROOT/tmp"
fi
if ! "$collect_only"; then
    if ! "$dry_run"; then
        for required in "$GGUF" "$MMPROJ" "$LLAMA_SERVER" \
            "$DATA_ROOT/artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl" \
            "$DATA_ROOT/artifacts/datasets/flight-identity-eval-v1/test.jsonl"; do
            [[ -f "$required" ]] || { printf 'Missing local input: %s\n' "$required" >&2; exit 1; }
        done
    fi
    for variant in default focus_only frame_336 crop_160 compact; do
        for dataset in slow-system-sft-v2-r4-prop flight-identity-eval-v1; do
            run "$PYTHON" "$ROOT/scripts/eval_slow_system.py" \
                --dataset "$DATA_ROOT/artifacts/datasets/$dataset" --data-root "$DATA_ROOT" \
                --backend gguf --device cuda:0 --gguf-model "$GGUF" --mmproj "$MMPROJ" \
                --llama-server "$LLAMA_SERVER" --port "$PORT" --context 4096 \
                --crop-policy candidate_zoom_v2 --identity-only --identity-variant "$variant" \
                --no-prompt-cache --max-new-tokens 128 \
                --output "$OUTPUT_DIR/identity--$dataset--$variant.json"
        done
    done
    for variant in default no_examples two_examples compact_rules compact; do
        for dataset in plan-eval-tasks plan-eval-tasks-v2; do
            run "$PYTHON" "$ROOT/scripts/plan_from_task.py" \
                --tasks "$ROOT/configs/onboard/$dataset.json" --gguf "$GGUF" --mmproj "$MMPROJ" \
                --llama-server "$LLAMA_SERVER" --port "$PORT" --device cuda:0 --sorted-keys \
                --prompt-variant "$variant" --no-prompt-cache \
                --output "$OUTPUT_DIR/planning--$dataset--$variant.json"
        done
    done
fi
run "$PYTHON" "$ROOT/scripts/prompt_budget.py" collect --input-dir "$OUTPUT_DIR" \
    --output "$OUTPUT_DIR/summary.md"
printf 'Evaluation table: %s/summary.md\n' "$OUTPUT_DIR"

# Onboard slow-system SFT handoff

The offline builder is `scripts/build_slow_system_sft.py`. It uses the three
2026-10-11 sim-teacher campaigns, projects each `obs_before` and `obs_after`
independently, and writes references to the original RGB files. It does not
copy images. Truth is used to create the assistant label only; the prompt
contains task wording, detector-like normalized boxes, and bounded memory.

## Dataset produced

Output: `artifacts/datasets/slow-system-sft-r1` (1,446,586 bytes). The split
is by generated world: 29 train worlds (including the urban train world), six
validation worlds, and six test worlds. The manifest records SHA-256 hashes for
inputs and JSONL outputs.

| split | total | confirm | approach | reject | search | clarify |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| train | 1,069 | 183 | 217 | 566 | 80 | 23 |
| val | 70 | 12 | 23 | 25 | 7 | 3 |
| test | 104 | 17 | 22 | 58 | 7 | 0 |

The test set therefore has exactly 17 true-target confirmations, 22 remote
true-target approaches, and 58 distractor/background rejections. Across all
splits, 218 rows contain more than one projected candidate. Candidate boxes are
normalized `xyxy` values in `[0, 1]`; `args.track_id` identifies the labelled
candidate. Distractor reasons identify a wrong class or colour.

## CPU checks completed

The builder output was parsed through `parse_onboard_decision`; IDs are unique,
worlds do not cross splits, and no target IDs, truth filenames, or distractor
labels occur in model-facing task/memory text. Chinese and English task
templates are present without replacement characters. The fake evaluator on
the test split produced 104/104 valid JSON decisions, 104/104 correct
decisions, 20/20 confirmation recall and 58/58 distractor rejection.

```text
pytest -q tests/test_slow_system_sft.py tests/test_slow_system_conversion.py
7 passed
```

No CUDA process or download was used for these checks.

## Commands for a free RTX 4060

Run these only after the detector and other GPU jobs have stopped. The trainer
uses `bitsandbytes` 4-bit when it is installed, otherwise BF16; it refuses to
start without BF16-capable CUDA.

```powershell
.venv-vlm\Scripts\python.exe scripts/train_slow_system_lora.py `
  --dataset artifacts/datasets/slow-system-sft-r1 `
  --model artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots `
  --output artifacts/models/slow-system-lora-r1 `
  --epochs 1 --accumulation 4 --quantization auto `
  --max-side 448 --max-input-tokens 2048 --max-new-tokens 128 `
  --seed 20261011

.venv-vlm\Scripts\python.exe scripts/eval_slow_system.py `
  --dataset artifacts/datasets/slow-system-sft-r1 --backend hf `
  --model artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots `
  --adapter artifacts/models/slow-system-lora-r1/adapter --device cuda:0 `
  --output artifacts/benchmarks/slow-system-hf-test.json
```

The expected training peak is about 4.5–6.5 GB with 4-bit loading. BF16 can
approach 7.5–8+ GB and may require reducing image/token budgets. For 1,069
training rows, one epoch is expected to take roughly 10–30 minutes on a free
4060, depending on processor throughput and checkpoint I/O. HF test evaluation
is expected to take roughly 2–5 minutes including model load.

The cached llama.cpp build has no local LoRA or HF-to-GGUF converter. Probe it
without loading weights:

```powershell
.venv-vlm\Scripts\python.exe scripts/convert_slow_system_lora.py `
  --adapter artifacts/models/slow-system-lora-r1/adapter `
  --output-json artifacts/models/slow-system-lora-r1-conversion-plan.json
```

The conversion plan documents the required merge → F16 GGUF → one-time
`Q4_K_M` path. It is intentionally not executed here: a merged HF checkpoint
and GGUF exceed the current 1 GB new-output budget, and no compatible local
converter is present. After conversion, evaluate the merged GGUF with the same
test prompt/schema:

```powershell
.venv-vlm\Scripts\python.exe scripts/eval_slow_system.py `
  --dataset artifacts/datasets/slow-system-sft-r1 --backend gguf `
  --device cuda:0 `
  --gguf-model artifacts/models/slow-system-sft-r1-gguf/slow-system-Q4_K_M.gguf `
  --mmproj artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf `
  --llama-server artifacts/cache/llama-cpp/b11552/llama-server.exe `
  --output artifacts/benchmarks/slow-system-gguf-test.json
```

For a separately converted GGUF LoRA adapter, add
`--adapter <GGUF_LORA_PATH>`; an HF PEFT directory cannot be passed to
`llama-server --lora`. To force a CPU-only llama.cpp smoke, use
`--device cpu`, which maps to `-ngl 0`.

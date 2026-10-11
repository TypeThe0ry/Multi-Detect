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

## Candidate zoom policy (r3)

New datasets default to `candidate_zoom_v1`. The model receives the existing
448 px maximum-side full frame plus a second 224x224 image. The second image is
the selected candidate box expanded to a square of `max(width, height) * 3.0`
with a 64 px minimum in original pixels. It is clamped at image borders and
neutral-gray padded when necessary, then resized with Lanczos interpolation.
Rows without a candidate send only the full frame. The crop is computed from
the original RGB at model-input time; no image copies are written. Set
`--crop-policy none` when reproducing an old single-image run. Manifests without
`crop_policy` are treated as `none`, so r1/r2 datasets remain evaluable.

This addresses the measured perception limit: r1 reached 62% decision
accuracy (48% confirmation recall, 37% precision) and r2 reached 58% (35%
recall, 73% precision) while targets were often only 5-30 px in the full
frame. One extra 224x224 image is estimated at roughly 64-100 additional
Qwen3-VL visual tokens on Orin; this cost is not yet measured on device.

The trainer also accepts `--balance`, which deterministically oversamples the
three decision classes using `--seed` so their optimizer-stream counts stay
within 2:1. The setting and resulting stream size are recorded in
`training-report.json`.

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

## Measured results on the 197-row r2/r3 test split (2026-10-11)

Same 6 held-out box worlds, same rows; HF bf16 on an RTX 4060 shared with a
running Gazebo container, so latency is not an Orin number.

| Adapter | Inputs | Decision acc | Distractor rejection | Confirm recall | Confirm precision | JSON valid | p50 latency |
|---|---|---:|---:|---:|---:|---:|---:|
| LoRA r1 (1 epoch, sft-r1) | full frame 448 | 62.4% | 90.7% | 47.8% | 36.7% | 100% | 5.39 s |
| LoRA r2 (3 epochs, sft-r2) | full frame 448 | 58.4% | 54.6% | 34.8% | 72.7% | 100% | 3.79 s |
| LoRA r3 (2 epochs, `--balance`, sft-r3) | full frame 448 + `candidate_zoom_v1` 224 | **87.3%** | **89.8%** | **69.6%** | **76.2%** | 100% | 7.84 s |

The zoom crop, not more data or epochs, removed the perception bottleneck.
These worlds still use coloured box targets; the realistic-mesh worlds
(`configs/scenarios/generated-v2`) must be collected and re-evaluated before
any claim about general object categories. GGUF conversion and llama.cpp
latency with two images are not yet measured.

## llama.cpp deployment path (2026-10-11)

Conversion uses llama.cpp b11552 `convert_lora_to_gguf.py` / `convert_hf_to_gguf.py`
(downloaded with their `conversion/` and `gguf-py/` trees into
`artifacts/cache/llama-cpp/b11552-src`). Two pipeline bugs had to be fixed
before llama.cpp numbers were meaningful:

1. `eval_slow_system.py` launched `llama-server` with `cwd` set to the binary
   directory, so relative model paths failed silently; paths are now resolved
   and the server stderr is surfaced.
2. Training targets are serialised with `sort_keys=True`
   (`args`, `decision`, `reason`), but the json_schema grammar emitted
   `decision` first. Constrained decoding now uses
   `trained_key_order_schema(DECISION_JSON_SCHEMA)`.

With both fixes, F16 llama.cpp reproduces HF exactly (87.3%), so the
remaining loss is quantisation. Same 197-row test split, LoRA r3, crop
policy `candidate_zoom_v1`, RTX 4060 shared with Gazebo:

| Weights | Size | Decision acc | Distractor rejection | Confirm recall / precision | p50 / p95 |
|---|---:|---:|---:|---:|---:|
| merged Q4_K_M | 1.11 GB | 73.6% | 88.0% | 43.5% / 76.9% | 0.45 / 0.61 s |
| merged Q5_K_M | 1.26 GB | 86.3% | 92.6% | 78.3% / 66.7% | 0.45 / 0.55 s |
| merged Q6_K | 1.42 GB | 83.8% | 92.6% | 65.2% / 62.5% | 0.48 / 0.64 s |
| merged Q8_0 | 1.83 GB | 87.3% | 92.6% | 60.9% / 73.7% | 0.54 / 0.63 s |
| base Q8_0 + LoRA | 1.83 GB | 86.8% | 92.6% | 60.9% / 73.7% | 0.58 / 0.79 s |
| F16 + LoRA | 3.45 GB | 87.3% | 92.6% | 65.2% / 75.0% | 0.92 / 1.90 s |

Q4_K_M is the cliff for this LoRA; Q5_K_M or Q8_0 (merged, no runtime LoRA)
are the Orin candidates (plus the 0.45 GB Q8_0 mmproj). Orin latency is not
measured. Note: `candidate_zoom_v1` leaks the label on multi-candidate rows
(it picks the crop by `assistant_target.args.track_id`); single-candidate rows
are unaffected (r3 87% vs r1 73%). New datasets default to the label-free
`candidate_zoom_v2`, which crops every candidate in listed order, and r3's
multi-candidate numbers must be re-measured with a v2-trained adapter.

## Label-free crops and event focus: LoRA r4 / r5 (2026-10-12)

`candidate_zoom_v2` crops every candidate (listed order, max 4) and never reads
the label. Without the leak, multi-candidate rows became contradictory: one
frame produced several rows with identical inputs and different labels
(approach track-1 / reject track-2). The onboard runtime is event-driven, so the
host always knows which track triggered the call; rows now carry
`focus_track_id` (chosen per candidate by the builder, never from the label) and
the v2 prompt states `Event: decide about focus track_id=...`. The r5 builder
check found 0 rows whose label track differs from the focus and 0 identical
input groups with different labels. Datasets also drop candidates with
`visible_fraction < 0.3` (57 dropped in sft-r4/r5).

194-row test split (same 6 held-out box worlds), HF bf16, 2 epochs, `--balance`:

| Adapter | Inputs | Decision acc | Single-candidate | Multi-candidate | Distractor rejection | Confirm recall / precision |
|---|---|---:|---:|---:|---:|---:|
| r4 (sft-r4) | v2 crops, no focus | 73.2% | 86% | 59% | 64.2% | 43.5% / 55.6% |
| r5 (sft-r5) | v2 crops + event focus | **89.2%** | 85% | 93% | 94.3% | 82.6% / 67.9% |

r5 is the first leak-free number. The largest remaining confusion is
approach_track vs confirm_target (12 of 21 errors), i.e. the distance/size
judgement. Still coloured-box worlds; the generated-v2 realistic-mesh worlds
are the next test.

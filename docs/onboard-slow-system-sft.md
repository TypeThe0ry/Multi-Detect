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

## Realistic-mesh worlds (generated-v2, 2026-10-12)

Test split = 6 held-out gen_v2 worlds (157 rows, Chinese and English task
text, real object categories). Candidate boxes still come from projected
truth (small objects can be a few pixels off; segmentation labels are the
next data revision).

| Adapter | Trained on | Decision acc | Distractor rejection | Confirm recall / precision | JSON valid |
|---|---|---:|---:|---:|---:|
| r5 | coloured-box worlds only | 76.4% | 96.5% | 69.6% / 41.0% | 100% |
| v2-r1 | slow-system-sft-v2-r1 (842 rows) | 86.6% | 95.3% | 60.9% / 70.0% | 97% |

r5 never saw a realistic object yet rejects 96.5% of distractors on these
worlds: category/colour judgement transfers from the pretrained VLM. Its
errors are almost all approach-vs-confirm (distance). Training on v2 data
fixes most of that (23 -> 4 premature confirms) but becomes more cautious
(9 confirms predicted as approach) and 4 ask_clarify rows produced invalid
JSON.

## Identity by the VLM, distance by the host (2026-10-13)

With segmentation-derived candidates (campaign-20261012-v2-r3-seg) the
approach-vs-confirm split stayed the weak point: labels based on true range
(> 45 m) are unobservable (LoRA v2-r2-seg: confirm recall 15%), and a pixel
threshold the model must infer from normalized coordinates is not much better
(v2-r3-seg: 76.1% accuracy, distractor rejection 85.7%). Scoring only
**identity** — approach/confirm = "this is the task's object", reject = "it is
not" — shows the VLM is strong at what it is suited for:

| Adapter | Test set | Identity accuracy | Match recall / precision | Distractor rejection |
|---|---|---:|---:|---:|
| r5 (box worlds only) | sft-v2-r1 | 97.9% | 100% / 95.0% | 96.5% |
| v2-r1 | sft-v2-r1 | 97.2% | 100% / 93.4% | 95.3% |
| v2-r2-seg | sft-v2-r2-seg | 92.8% | 92.7% / 96.2% | 97.0% |
| v2-r3-seg | sft-v2-r3-seg | 92.0% | 100% / 84.6% | 85.7% |

Decision: the VLM decides identity; the host, which knows the candidate's
pixel size, applies `gate_confirm_by_size` (`multidetect.autonomy.onboard_decision`)
to turn an identity match into confirm_target (min side >= 20 px) or
approach_track. Scored against the 20 px labels of sft-v2-r3-seg (138 rows):

| Adapter | Raw decision acc | Host-gated acc | Confirm recall / precision |
|---|---:|---:|---:|
| v2-r2-seg | 75.4% | **91.3%** | 100% / 96.0% |
| v2-r3-seg | 73.9% | 90.6% | 100% / 88.9% |

`eval_slow_system.py` now reports `identity_accuracy`,
`identity_match_recall` and `identity_match_precision`.

Deployment candidate (llama.cpp b11552, merged LoRA v2-r2-seg, sft-v2-r2-seg test,
host size gate scored against the 20 px labels): Q5_K_M 1.26 GB identity 95.2%,
gated decision 92.0%, p50/p95 0.43/0.47 s; Q8_0 1.83 GB identity 92.8%, gated
91.3%, 0.52/0.56 s (RTX 4060; Orin not measured). Q5_K_M + the 0.45 GB mmproj is
the onboard candidate.

## Live onboard proposals, identity-only decoding, counterfactual tasks (2026-10-14)

Training rows so far only used ground-truth objects as candidates, so clutter,
scenery (e.g. a red gas station price sign) and the detector's own box geometry
never appeared. `scripts/cache_onboard_proposals.py` runs the onboard FAST
proposer (YOLOE fine-tune, generic vocabulary, conf 0.05, onboard box filter)
over every campaign frame; `build_slow_system_sft.py --proposals` labels each
live box by the segmentation object it covers (true target / distractor /
scenery / nothing), drops half-covering boxes and distractors the task text also
describes (`multidetect.simulator.task_semantics.task_equivalent`), and keeps
all rare scenery/background negatives.

Onboard identity calls now decode with `identity_decision_schema(focus)`:
decision restricted to approach/confirm/reject and args pinned to the focus
track (keys are emitted in sorted order, so a free `track_id` came out null in
6.6% of constrained calls).

Same test split for all rows below (sft-v2-r4-prop test, 243 rows, identical to
the r5/r6 test files), llama.cpp b11552 Q5_K_M, identity-only pinned decoding,
RTX 4060:

| LoRA | Training data | Identity acc | Match recall / precision | Distractor rejection |
|---|---|---:|---:|---:|
| v2-r3-seg | truth candidates | 85.5% | 100% / 76.1% | 73.1% |
| **v2-r4-prop** | + live proposals | **92.8%** | 100% / 86.4% | **86.6%** |
| v2-r5-cf | + counterfactual tasks, held-out classes unseen | 80.5% | 100% / 70.3% | 63.9% |
| v2-r6-cf-seen | + counterfactual tasks only | 82.4% | 100% / 72.3% | 67.2% |

p50 latency 0.44 s for all. **Counterfactual tasks hurt** (r6 isolates them):
re-asking every visible distractor by name doubles the positives and makes the
model accept similar vehicles (SUV / pickup / hatchback) far more often; errors
concentrate in gen_v2_w0037 and gen_v2_w0035. v2-r4-prop stays the onboard model.

The in-flight stop sign rejection that motivated them is a viewpoint problem,
not a class prior: the vehicle approached the sign from behind, where it is a
grey octagon, and the model rejected it for not being red. That needs a second
look from another side (behaviour), not more labels.

## One model for identity and mission planning (2026-10-15)

Identity fine-tuning cost the planner (v2-r4-prop: 87% valid plans, 57%
correct skill sequences on the 30 hand-written tasks, base model 100% / 70%).
Orin has room for one 2B model, so v2-r7-multi trains both into one LoRA: the
sft-v2-r4-prop identity rows plus 1,200 synthetic text-only task -> MissionPlan
rows (`scripts/build_plan_sft.py`, plan-sft-v1; the evaluation task texts are
excluded). Q5_K_M, llama.cpp b11552, RTX 4060:

| Model | Identity acc | Distractor rejection | Valid plans | Correct skill sequence (strict) | Plan latency p50 |
|---|---:|---:|---:|---:|---:|
| base Qwen3-VL-2B Q4_K_M | - | - | 100% | 70% (20/30) | 1.5 s |
| v2-r4-prop | 92.8% | 86.6% | 87% | 57% (16/30) | 1.9 s |
| **v2-r7-multi** | 91.9% | 84.9% | **100%** | **87% (25/30)** | **0.7 s** |

Identity drops by two of 221 rows; planning no longer needs host repairs. The
remaining plan errors: counting read as patrol, follow read as monitor, a
missing hover or record step. v2-r7-multi (1.26 GB + 0.45 GB mmproj) is the
single onboard candidate; its closed-loop flights are still to be run.

v2-r8-multi (2026-10-15) retrains the same mix with planning data that renames
every place to a random alias and lists the known places with labels in the
prompt, and writes half the Chinese numbers as Chinese numerals (plan-sft-v2).
v2-r7-multi had copied place names from its training data (zone:lot for "the
site") and read "五分钟" as 500 s. Same rows, Q5_K_M, labelled-place prompts:

| Model | Identity acc | Distractor rejection | Valid plans | Correct skill sequence (strict) | Plan p50 |
|---|---:|---:|---:|---:|---:|
| v2-r7-multi | 91.9% | 84.9% | 100% | 80% (23/30) | 1.15 s |
| **v2-r8-multi** | **94.1%** | **89.9%** | 100% | **83% (24/30)** | 0.87 s |

v2-r8-multi compiles "数一数场地里有几个交通锥，然后返航" to count(zone:site,
traffic cone) -> return_home, "监视场地五分钟看有没有背包" to monitor 300 s,
"沿着围栏巡逻两圈" to patrol(route:fence, 2 loops) and an English
find-inspect-land command correctly. It replaces v2-r4-prop as the onboard
model (identity and planning in one 1.26 GB GGUF).

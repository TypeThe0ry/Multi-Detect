# Orin Nano prompt-budget experiments

Prepared on 2026-10-15 for v2-r12-multi. These are opt-in experiments: default
prompts, image rendering and training rows retain existing behavior. The model
was trained on the original prompts. Shorter-prompt accuracy and robustness
remain unmeasured until the evaluation below runs.

No GPU inference, model loading, training, download or deployment was performed
for this change. Offline tokenization and CPU image-processor checks were used.

## Run later on an idle GPU

From this checkout in **Git Bash** (or Bash on the evaluation host):

```bash
bash scripts/run_prompt_budget_eval.sh
```

PowerShell's `bash` resolves to WSL on this computer. From PowerShell, select
the installed Git Bash explicitly:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_prompt_budget_eval.sh
```

The script discovers the main repository's artifacts through Git's common
directory when a linked worktree has no cache. It selects local `.venv-vlm`,
the merged r12 Q5_K_M model, Q8_0 mmproj and b11552 server. The existing CLI
evaluators run sequentially with sorted trained JSON keys, identity-only
constrained decoding and prompt caching disabled. There are **20 runs**: five
identity variants on each dataset, and five planning variants on each task set.

Reports go to a timestamped
`artifacts/benchmarks/prompt-budget-v2-r12-multi-<UTC>/` inside this worktree.
`summary.md` combines accuracy, validity, actual prompt/output tokens and
p50/p95 latency. Planning reports distinguish initial prompt tokens from total
prompt/output tokens across retries. Missing usage stays `n/a`; coverage is
reported. JSON retains per-task skill/language and rejection/recall diagnosis.
Server startup is excluded from latency; server-side image encoding is included.
Client-side PIL/JPEG work is excluded, following the existing evaluator.

Inspect the commands without loading a model:

```bash
bash scripts/run_prompt_budget_eval.sh --dry-run
```

Environment overrides: `DATA_ROOT`, `PYTHON`, `GGUF`, `MMPROJ`, `LLAMA_SERVER`,
`PORT` and `OUTPUT_DIR`. `--output-dir DIR` also selects the report directory,
which must resolve inside this checkout. On Linux/Jetson, set `LLAMA_SERVER` to
the local Linux executable. Nothing is downloaded. Rebuild a completed table:

```bash
bash scripts/run_prompt_budget_eval.sh --collect-only --output-dir artifacts/benchmarks/<run>
```

## Variant flags

| Scope | Flag | Effect |
|---|---|---|
| Planning | `--prompt-variant default` | Original six examples and full rules |
| Planning | `--prompt-variant no_examples` | Remove all examples; retain full rules |
| Planning | `--prompt-variant two_examples` | First two original examples: find and count |
| Planning | `--prompt-variant compact_rules` | Shorter skill/rule wording, same parameter names, all six examples |
| Planning | `--prompt-variant compact` | Compact rules, no examples |
| Identity | `--identity-variant default` | Frame max side 448, up to four 224-square crops |
| Identity | `--identity-variant focus_only` | Frame plus only the focus crop |
| Identity | `--identity-variant frame_336` | Frame max side 336; original crops |
| Identity | `--identity-variant crop_160` | Original frame and selection, 160-square crops |
| Identity | `--identity-variant compact` | Focus crop only, frame 336, crop 160 |

Planning flags work in `plan_from_task.py` and `build_plan_sft.py`;
`plan_prompt` / `compile_mission_plan` accept `prompt_variant=`. Retries keep
the chosen variant. Identity flags work in `eval_slow_system.py` and
`build_slow_system_sft.py`; prompt, PIL, JPEG and chat builders accept
`identity_variant=`. The identity matrix explicitly uses
`--crop-policy candidate_zoom_v2`. Focus selection uses `focus_track_id`,
falling back to the first listed candidate, and never reads `assistant_target`.

For retraining, append the same flag to the existing SFT build command and
choose a new output directory. Planning example:

```bash
python scripts/build_plan_sft.py --version v4 --rows 1500 --val-rows 100 \
  --prompt-variant compact --output artifacts/datasets/plan-sft-v4-compact
```

For identity, add `--identity-variant compact` (or an individual variant) to the
existing proposal-labelled campaign builder command. The builder persists it
in each identity row and manifest; the existing training renderer honors it
automatically. Mixed planning rows keep their stored prompts. Default builds
do not gain variant metadata. Data preparation does not establish model quality.

## Offline accounting method

The local Qwen3-VL-2B-Instruct tokenizer snapshot is
`89644892e4d85e24eaac8bacfd4f463576704203`. Rust `tokenizers` and its local
Jinja chat template provide exact text counts. Incremental prefix attribution
handles BPE boundaries, so parts sum to the total. "System" is instruction text
inside the existing user message, not a new system-role message. Identity refs
include boxes, tracks, crop descriptions and focus instructions. Planning rules
include the skill catalogue/reference syntax; refs are labelled known places.

Every row is counted: r4-prop test **243**, flight identity test **1,900**,
planning v1 **30**, planning v2 **60**. Flight rows log one candidate each, so
focus-only cannot reduce that set's image count. The r4-prop set exercises
multiple crops. Compare accuracy within each set because their rows differ.

The local processor uses 16px patches, 2x2 merging and a 65,536px minimum image
area. Accounting uses the HF evaluator's `max_pixels=448*448`. CPU checks with
the installed `AutoProcessor` confirm: 448x336 becomes 448x320 (140 tokens),
336x252 becomes 320x256 (80), and **224x224 and 160x160 both become 256x256
(64)**. Thus crop_160 loses detail without reducing HF visual tokens. Lowering
the processor floor would be a separate train/inference experiment.

HF visual counts come from verified patch grids. llama.cpp may resize/tokenize
images differently; later measured `usage.prompt_tokens` is authoritative for
that backend. Counts vary with candidate count, task text and labelled refs;
the following means use these exact evaluation sets, so they can differ from
the supplied ~470/~1,270 figures.

Reproduce without network or model weights:

```bash
D:/Projects/Multi-Detect/.venv-vlm/Scripts/python.exe scripts/prompt_budget.py account \
  --data-root D:/Projects/Multi-Detect \
  --output-dir artifacts/benchmarks/prompt-budget-offline
```

Use equivalent local paths on other hosts; `--tokenizer` can select a snapshot.
JSON records tokenizer/template/image-config and dataset/task-file hashes.
The encoder coefficient is an **unmeasured sensitivity scenario**; replace it
using `--encoder-ms-per-megapixel` after calibration. The 1,000ms/MP example
equals 200.7ms for a processed 448x448 image. Fixed per-image, transfer and host
costs are omitted. Identity decode uses tokenized training-answer length as a
proxy; constrained outputs can differ. Planning uses the supplied 90–150 tokens.

<!-- OFFLINE_ACCOUNTING -->

## Offline token tables

Means over every task/test row; counts include the chat template. Image tokens use the local HF resize configuration, not measured GGUF usage.

| Suite / set | Variant | System | Rules | Examples | Refs | Task | Memory/repair | Chat | Images | Prompt total |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| identity/slow-system-sft-v2-r4-prop | default | 7.0 | 48.0 | 0.0 | 104.6 | 10.0 | 11.5 | 13.0 | 236.9 | 431.1 |
| identity/slow-system-sft-v2-r4-prop | focus_only | 7.0 | 48.0 | 0.0 | 99.8 | 10.0 | 11.5 | 11.8 | 198.7 | 386.9 |
| identity/slow-system-sft-v2-r4-prop | frame_336 | 7.0 | 48.0 | 0.0 | 104.6 | 10.0 | 11.5 | 13.0 | 176.9 | 371.1 |
| identity/slow-system-sft-v2-r4-prop | crop_160 | 7.0 | 48.0 | 0.0 | 104.6 | 10.0 | 11.5 | 13.0 | 236.9 | 431.1 |
| identity/slow-system-sft-v2-r4-prop | compact | 7.0 | 48.0 | 0.0 | 99.8 | 10.0 | 11.5 | 11.8 | 138.7 | 326.9 |
| identity/flight-identity-eval-v1 | default | 7.0 | 48.0 | 0.0 | 83.5 | 10.3 | 11.7 | 12.0 | 204.0 | 376.5 |
| identity/flight-identity-eval-v1 | focus_only | 7.0 | 48.0 | 0.0 | 83.5 | 10.3 | 11.7 | 12.0 | 204.0 | 376.5 |
| identity/flight-identity-eval-v1 | frame_336 | 7.0 | 48.0 | 0.0 | 83.5 | 10.3 | 11.7 | 12.0 | 144.0 | 316.5 |
| identity/flight-identity-eval-v1 | crop_160 | 7.0 | 48.0 | 0.0 | 83.5 | 10.3 | 11.7 | 12.0 | 204.0 | 376.5 |
| identity/flight-identity-eval-v1 | compact | 7.0 | 48.0 | 0.0 | 83.5 | 10.3 | 11.7 | 12.0 | 144.0 | 316.5 |
| planning/plan-eval-tasks | default | 17.0 | 419.0 | 819.0 | 52.0 | 15.4 | 0.0 | 8.0 | 0.0 | 1330.4 |
| planning/plan-eval-tasks | no_examples | 17.0 | 419.0 | 0.0 | 52.0 | 15.4 | 0.0 | 8.0 | 0.0 | 511.4 |
| planning/plan-eval-tasks | two_examples | 17.0 | 419.0 | 235.0 | 52.0 | 15.4 | 0.0 | 8.0 | 0.0 | 746.4 |
| planning/plan-eval-tasks | compact_rules | 17.0 | 208.0 | 819.0 | 52.0 | 15.4 | 0.0 | 8.0 | 0.0 | 1119.4 |
| planning/plan-eval-tasks | compact | 17.0 | 208.0 | 0.0 | 52.0 | 15.4 | 0.0 | 8.0 | 0.0 | 300.4 |
| planning/plan-eval-tasks-v2 | default | 17.0 | 419.0 | 819.0 | 121.0 | 19.1 | 0.0 | 8.0 | 0.0 | 1403.1 |
| planning/plan-eval-tasks-v2 | no_examples | 17.0 | 419.0 | 0.0 | 121.0 | 19.1 | 0.0 | 8.0 | 0.0 | 584.1 |
| planning/plan-eval-tasks-v2 | two_examples | 17.0 | 419.0 | 235.0 | 121.0 | 19.1 | 0.0 | 8.0 | 0.0 | 819.1 |
| planning/plan-eval-tasks-v2 | compact_rules | 17.0 | 208.0 | 819.0 | 121.0 | 19.1 | 0.0 | 8.0 | 0.0 | 1192.1 |
| planning/plan-eval-tasks-v2 | compact | 17.0 | 208.0 | 0.0 | 121.0 | 19.1 | 0.0 | 8.0 | 0.0 | 373.1 |

## Orin Nano latency scenarios

Prefill = prompt tokens / 1,400; decode = output tokens / 35. Planning output uses the supplied 90–150-token range; identity output uses the tokenized reference answer mean (actual generations may differ).
Image encoder scenario: 1000 ms per processed MP (unmeasured assumption; use --encoder-ms-per-megapixel to replace it). This linear scenario omits fixed per-image, transfer and host overhead.

| Suite / set | Variant | Prefill s | Decode s | Image MP | Encoder s (scenario) | Total s (scenario) |
|---|---|---:|---:|---:|---:|---:|
| identity/slow-system-sft-v2-r4-prop | default | 0.308 | 1.085 | 0.243 | 0.243 | 1.636 |
| identity/slow-system-sft-v2-r4-prop | focus_only | 0.276 | 1.085 | 0.203 | 0.203 | 1.565 |
| identity/slow-system-sft-v2-r4-prop | frame_336 | 0.265 | 1.085 | 0.181 | 0.181 | 1.532 |
| identity/slow-system-sft-v2-r4-prop | crop_160 | 0.308 | 1.085 | 0.243 | 0.243 | 1.636 |
| identity/slow-system-sft-v2-r4-prop | compact | 0.234 | 1.085 | 0.142 | 0.142 | 1.461 |
| identity/flight-identity-eval-v1 | default | 0.269 | 1.061 | 0.209 | 0.209 | 1.539 |
| identity/flight-identity-eval-v1 | focus_only | 0.269 | 1.061 | 0.209 | 0.209 | 1.539 |
| identity/flight-identity-eval-v1 | frame_336 | 0.226 | 1.061 | 0.147 | 0.147 | 1.435 |
| identity/flight-identity-eval-v1 | crop_160 | 0.269 | 1.061 | 0.209 | 0.209 | 1.539 |
| identity/flight-identity-eval-v1 | compact | 0.226 | 1.061 | 0.147 | 0.147 | 1.435 |
| planning/plan-eval-tasks | default | 0.950 | 2.571–4.286 | 0.000 | 0.000 | 3.522–5.236 |
| planning/plan-eval-tasks | no_examples | 0.365 | 2.571–4.286 | 0.000 | 0.000 | 2.937–4.651 |
| planning/plan-eval-tasks | two_examples | 0.533 | 2.571–4.286 | 0.000 | 0.000 | 3.105–4.819 |
| planning/plan-eval-tasks | compact_rules | 0.800 | 2.571–4.286 | 0.000 | 0.000 | 3.371–5.085 |
| planning/plan-eval-tasks | compact | 0.215 | 2.571–4.286 | 0.000 | 0.000 | 2.786–4.500 |
| planning/plan-eval-tasks-v2 | default | 1.002 | 2.571–4.286 | 0.000 | 0.000 | 3.574–5.288 |
| planning/plan-eval-tasks-v2 | no_examples | 0.417 | 2.571–4.286 | 0.000 | 0.000 | 2.989–4.703 |
| planning/plan-eval-tasks-v2 | two_examples | 0.585 | 2.571–4.286 | 0.000 | 0.000 | 3.156–4.871 |
| planning/plan-eval-tasks-v2 | compact_rules | 0.851 | 2.571–4.286 | 0.000 | 0.000 | 3.423–5.137 |
| planning/plan-eval-tasks-v2 | compact | 0.266 | 2.571–4.286 | 0.000 | 0.000 | 2.838–4.552 |

## Image sizes

| Input W×H | HF W×H | Visual tokens / image |
|---|---|---:|
| 160×160 | 256×256 | 64 |
| 224×224 | 256×256 | 64 |
| 336×252 | 320×256 | 80 |
| 448×336 | 448×320 | 140 |

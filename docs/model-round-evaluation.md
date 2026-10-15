# Sequential model-round evaluation

`scripts/run_model_round_eval.py` replaces manually chained model evaluations.
It runs each requested stage synchronously, holding an exclusive lock for the
whole round. A nonzero child exit, missing/malformed report, incomplete row count,
failed/missing episode, excessive transport errors, or low JSON validity stops
the round and writes `summary.json` and `summary.md`. Invalid stage accuracy is
never copied into the summary. Raw stage reports and append-only logs remain for
diagnosis.

Stages, in the default order:

| Stage | Input |
| --- | --- |
| `offline` | `slow-system-sft-v2-r4-prop/test.jsonl` |
| `flight` | `flight-identity-eval-v1/test.jsonl` |
| `planning` | `plan-eval-tasks.json` |
| `planning-v2` | `plan-eval-tasks-v2.json` |
| `planning-v3` | `plan-eval-tasks-v3.json` |
| `find` | generated-v2 test worlds, lowsearch/fast onboard config |
| `suite-v1` | `mission-suite-v1-r13.json` |
| `suite-v2` | `mission-suite-v2-r13.json` |

Pass a subset with `--stages offline flight planning`, or reorder the list.
Identity evaluation uses candidate zoom v2, pinned identity decoding, and
`--fast-identity`; planning uses sorted keys. All child processes use the current
Python interpreter. No downloads are performed; Hugging Face offline environment
variables are set. `--data-root` locates existing datasets, model artifacts, and
cache in another checkout. All runner outputs, logs, generated configuration,
temporary server logs, and locks stay inside this worktree. Suite indices retain
their existing repository-relative interpretation.

## Evaluate r13 against stored r12 results

Run from `D:\Projects\mdwt\evalrunner`, using a Python environment with the
existing evaluation dependencies, on an idle machine when actual model and
simulator execution is intended:

```powershell
python scripts/run_model_round_eval.py `
  --model v2-r13-multi `
  --gguf D:/Projects/Multi-Detect/artifacts/models/slow-system-lora-v2-r13-multi-merged-gguf/slow-system-v2r13multi-merged-Q5_K_M.gguf `
  --data-root D:/Projects/Multi-Detect `
  --stages offline flight planning planning-v2 planning-v3 find suite-v1 suite-v2 `
  --out artifacts/benchmarks/model-round-v2-r13-multi `
  --baseline configs/onboard/model-round-baseline-r12.json `
  --baseline-root D:/Projects/Multi-Detect `
  --baseline-model v2-r12-multi `
  --episodes 2 --retry-failed-episodes `
  --min-free-disk-gb 30 --disk-check-every 1
```

This command is provided for later use; implementation validation does not run
models, GPU work, Docker, or simulators.

The r12 manifest names existing stored reports. There are no stored r12
planning-v3 or suite-v2 reports in the inspected benchmark directory, so those
comparison cells are unavailable. Existing r12 find/suite reports may be partial
or failed; they are validated and explicitly annotated rather than used as
trustworthy baselines. The existing r12 planning-v2 valid rate is below the
default 99% guard. Baseline problems do not discard a valid new stage; they
produce `baseline_error` and null baseline/delta cells. A previous runner
`summary.json` can also be passed directly as `--baseline`. Relative report paths
resolve under `--baseline-root`, or the baseline JSON's directory if omitted.
Baseline offline/planning comparisons require matching row identities and order.

## Guards and retry behavior

- `--lock-file` defaults to `artifacts/locks/model-evaluation.lock`. Creation is
  atomic and stores PID, host, and model. Evaluation/training wrappers must use
  the **same absolute lock path and exclusive-create protocol**, for example via
  `scripts.model_round_guard.GPULock`. Independent legacy jobs do not acquire
  this lock automatically; existing heavy compute consumers are also checked
  through `nvidia-smi` before the round and every stage. Unknown process memory
  is treated as busy. Missing/broken GPU inspection fails closed.
- `--force` bypasses GPU inspection only. It cannot bypass a held lock, disk
  guard, child failure, or validity failure. Stale locks require inspection and
  manual removal after confirming the owning job is gone.
- `--min-free-disk-gb` defaults to 30 decimal GB. Check before each stage and
  each batch of `--disk-check-every` episodes (default one). A single episode
  can still consume its initial reserve; choose a larger reserve for large
  recordings. Direct campaign/suite invocations expose the same flags but
  default to zero GB for compatibility.
- `--max-transport-rate` defaults to 0.01 and cannot exceed 0.01. Connection
  refusal/reset, URL/HTTP errors, timeouts, server death, and CUDA OOM in error
  fields invalidate a stage when **more than 1%** of rows/calls fail. Closed-loop
  checks inspect nested slow-call evidence in active `steps.jsonl`, excluding
  archived retries; the per-call denominator is not diluted by episode counts.
- `--min-valid-rate` defaults to 0.99. It is independent of transport errors.
  Planning rows that correctly reject an `expect_invalid` task count as valid
  host outcomes. Completed episodes can still fail mission expectations: that
  is a model-quality result, not an infrastructure-invalid result.
- `--retry-failed-episodes` is opt-in on runner, campaign, and suite. The old
  campaign logic counted every `episode.json` as done regardless of status.
  With retry enabled, failed, incomplete, malformed, or missing-metadata episode
  directories move into each world's `retry-history/`; complete recordings with
  step evidence stay untouched. UUID replacements fill only the remaining quota
  under the **same campaign output**. Historical attempts remain available but
  do not duplicate active episodes or enter scoring. Reusing an output directory
  requires the same model, tasks, and configuration; use a new directory when
  changing them.
- Campaign and suite preserve their historical permissive exit status by
  default: collector failures are logged and scoring continues, and suite
  failures only return nonzero for missing rows. The explicit `--strict` flag
  enables the operational failure gate; the round runner passes it to find and
  suite stages automatically.

If a guard fails, inspect `summary.md`, the stage log, and retained episode
evidence; rerun the identical command after fixing the cause. Already complete
campaign/suite episodes are reused; identity/planning reports are regenerated.

## Model-free verification

```powershell
python -m pytest -q -p no:warnings tests/test_run_model_round_eval.py tests/test_onboard_campaign_retry.py tests/test_mission_suite.py
python -m ruff check scripts/run_model_round_eval.py scripts/model_round_guard.py scripts/eval_onboard_loop_campaign.py scripts/run_mission_suite.py tests/test_run_model_round_eval.py tests/test_onboard_campaign_retry.py
```

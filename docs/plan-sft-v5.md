# plan-sft-v5 operator language expansion

V5 is an opt-in data version of `scripts/build_plan_sft.py`. Each split retains
70% v4 backbone candidates and adds 30% new operator-language families. The v4
candidate stream still mixes `mission()` and `mission_v3()` in its original 2:1
ratio, using an independent RNG. Accepted backbone task texts, sample IDs, goals,
step graphs and reports remain unchanged. V5 adds `family` metadata and removes
held-out few-shot examples from prompts. The default CLI version remains v4.

The new templates cover Chinese and English, including explicit Arabic/Chinese/
English number words. The two-target family includes both random distinct targets
and an anchor pair, red car followed by blue backpack. All generated labels pass
`validate_mission_plan`; generated rows have no delivery capability.

| Family | Label behavior | Train | Val |
| --- | --- | ---: | ---: |
| `v4_backbone` | Existing v2/v3 templates from v4 | 1,260 | 105 |
| `conditional_branch` | Search success records; failure goes to a labelled alternate place; both branches terminate | 90 | 8 |
| `two_targets` | Ordered distinct searches, optionally recording each found object; missing the first still tries the second | 90 | 8 |
| `count_stop` | Explicit limit becomes `count.max_count`, followed by return or requested landing | 90 | 8 |
| `alert_action` | Confirm target before hover/photo; optional bounded monitor/patrol first | 90 | 7 |
| `evidence_only` | Current visible object or current view: `record` then terminal, without an extra search | 90 | 7 |
| `colloquial` | 瞅瞅、瞄一眼、转一圈、溜一圈、盯着; look/photo, patrol, monitor, goto/hover and English counterparts | 90 | 7 |
| **Total** | **70% backbone / 30% additions in both splits** | **1,800** | **150** |

For other split sizes, addition counts round 30% to the nearest integer, then
distribute across the six new families with counts differing by at most one.
Rows are shuffled within each split. Train and validation share a normalized-text
uniqueness guard. A bounded retry limit reports template-pool exhaustion instead
of looping forever for an impossible request.

## Contract limits

`monitor` and `patrol` success edges mean observation duration/route completion,
not target detection. The executor records alerts separately and has no
`on_alert` edge. Consequently those new texts explicitly request a search **after**
watching/patrolling completes, then hover/photo only if that search finds the
target. Direct “see X, hover and photograph it” uses an `area_search` success edge.
An absent target skips hover and photography. V5 does not claim to encode an
interrupting action during an active monitor/patrol step.

**Refusal rows: zero.** `plan_from_task.score_task(expect_invalid=True)` accepts
either compilation rejection (`plan is None`) or a sole `return_home` plan with
nonempty host-repair evidence. An ordinary valid single-home response is
explicitly insufficient. `compile_mission_plan` retries invalid output, and
`scripts/onboard_policy.py:build_mission_from_text` creates a safe home fallback
when compilation returns no plan. Its error remains visible in the report.

The MissionPlan contract and decoding grammar have no explicit refusal/null
answer form. An empty-step response could provoke host repair but is an invalid
training label; an unknown reference or ungated delivery is also an invalid
label. Teaching those would contradict the requirement that every label pass
validation. Teaching a valid home plan would fail the refusal metric. V5 therefore
leaves unknown-place and unsupported-delivery cases held out, with no fabricated
refusal representation. Explicit rejection needs a coordinated future change to
the contract, grammar, host handling and scoring before adding such SFT labels.

## Held-out isolation and references

V5 always loads all `configs/onboard/plan-eval-tasks*.json` files. Explicit
`--eval-tasks` files add exclusions rather than disabling the defaults.

`normalize_task` applies Unicode NFKC, case folding, punctuation/whitespace
removal, and Chinese/English number-word conversion. Equality after this
normalization is forbidden against eval and across train/val. This is a lexical
near-duplicate rule, not a semantic similarity or translation detector. Existing
v2/v3/v4 filtering is preserved for byte reproducibility.

The audit found that one legacy default few-shot example is itself an eval task.
V5 removes matching example blocks from each prompt, including short prompt
variants, and records `excluded_prompt_examples` in the manifest. With the default
prompt this is **1**, leaving five examples. The inference prompt implementation
and v4 output are unchanged; v5's filtered training prompt is intentionally
different from the legacy prompt by that held-out example block.

Place aliases must resolve to the labels supplied in the prompt. V5 rejects
legacy alias collisions that map different places onto the same symbol, then
calls `check_plan_refs(..., substitute=False)` as well as plan validation. Only
supplied labelled place refs and contract builtins (`home`, `zone:all`, earlier
finding/track refs in backbone templates) are allowed.

## Reproduction and sample results

Run from the worktree, without inference, downloads or model execution:

```powershell
python scripts/build_plan_sft.py --version v5 --seed 20261015 --rows 1800 --val-rows 150 --output tmp/plan-sft-v5
python -m pytest -q -p no:warnings tests/test_build_plan_sft.py tests/test_build_plan_sft_v5.py tests/test_plan_from_task.py tests/test_plan_eval_tasks_v3.py
python -m ruff check scripts/build_plan_sft.py tests/test_build_plan_sft.py tests/test_build_plan_sft_v5.py
```

`--require-skills` defaults to all twelve ungated skills for v5, as for v4. A
missing training skill aborts before output files are written. Passing it without
values disables that guard for small fixtures. The manifest retains skill,
first-skill and final-skill counts and adds per-split family counts.

Skill counts below count **step occurrences**, including alternative branches;
they are not task counts or just the success path:

| Skill | Train | Val |
| --- | ---: | ---: |
| `area_search` | 1,029 | 77 |
| `count` | 387 | 41 |
| `follow` | 121 | 9 |
| `goto` | 350 | 25 |
| `hover` | 341 | 25 |
| `inspect` | 51 | 4 |
| `land` | 596 | 59 |
| `monitor` | 103 | 13 |
| `patrol` | 174 | 16 |
| `record` | 668 | 53 |
| `return_home` | 1,204 | 91 |
| `survey` | 52 | 5 |
| `deliver` | 0 | 0 |

The sample has 893 Chinese / 907 English training tasks and 87 Chinese / 63
English validation tasks. All 1,950 plans validate; all 1,950 normalized task
keys are unique. There are zero task/prompt-example collisions against the 170
normalized tasks in the three eval files, zero train/val normalized collisions,
and zero unknown place refs. These are data checks, not measured model accuracy.

Generated review artifacts live only under the worktree's `tmp/plan-sft-v5/`:

- `train.jsonl`, `val.jsonl`, `manifest.json`: requested sample and distributions.
- `examples.md`: ten actual rows for each of the six new families, five Chinese
  and five English per family, with labelled refs and complete assistant targets.
- `examples.jsonl`: the same sixty complete original rows, including their prompts.
- `audit.json`: row/ref/leakage checks, language distributions and SHA-256 hashes.

The local export helper is `tmp/report_plan_sft_v5.py`. The tests cover each
family's success and failure paths, host compilation without repairs, all alert
variants, stop-count numbers, current-view recording, reference-label collisions,
prompt variants, default-plus-extra eval exclusion, deterministic generation,
coverage guards and the refusal-scoring boundary. Frozen SHA-256 expectations
for pre-change v4 train/val/manifest enforce byte identity at the same 1,800/150
sample size. No evidence-bound files are modified.

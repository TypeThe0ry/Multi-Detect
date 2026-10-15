# Small-object onboard proposer recall — 2026-10-15

## Decision

Keep `proposer_tiled` **off by default** on Jetson Orin Nano 8GB. The dominant fix in this sample is the onboard **6 px minimum-box filter**, not more detector passes. On tasks explicitly naming these small objects, single-pass with `proposal_min_side_px: 2` recovers 207/225 objects (92.0%) versus 148/225 (65.8%) with the existing filter. Full-frame + four tiles with that same 2 px filter reaches 218/225 (96.9%), at about 5x detector cost. Lowering the filter recovers 59 of the 70 additional objects available from the combined change (84.3% of the gain) without extra detector inference.

Use task-aware lower search altitude/approach to obtain useful image detail, and treat tiny proposals as candidates; retain the existing slow identity, tracking and 20 px confirmation conditions. The 2 px setting is supported and measured offline, but mission success and VLM latency/accuracy have not been measured here. Use tiling as a bounded fallback for unresolved small-object searches rather than on every frame alongside the VLM.

## Definitions and evidence

- Cache: 17,516 rows, 6,462 unique image keys, 11,054 identical repeats, 0 conflicts. All 6,462 images join to recorded observations, RGB, panoptic segmentation and scenario truth.
- Truth uses the same `boxes_from_segmentation` helper as `build_slow_system_sft.py`, with `min_area_px=1, min_box_px=1`. Its normal SFT threshold of 8 px would hide precisely the failures under study. Half-open visible boxes are unions of visible components per object.
- Recall is class-agnostic independent max IoU >=0.3 per visible catalogued target/distractor; >=0.5 is a sensitivity check. One proposal may recall overlapping truth objects. Category labels predicted by YOLOE are ignored.
- Buckets are [0,8), [8,16), [16,32), [32,64), [64,infinity). Exactly 64 px belongs to the last bucket; there are no four-category examples >=64 px.
- Unmatched/false proposal proxies: no IoU >=0.3 to any catalogued object, or to any labelled scene object including scenery. The latter is the main background proxy here. Unlabelled real objects can still count unmatched; these are not verified semantic false positives.
- Altitude means `-pose.local_ned_m[2]`, base height above local origin, not measured terrain AGL, object-relative height or slant range. 149/6462 frames have pose/RGB simulator skew >250 ms; none of the 200 benchmark frames do. Altitude bins are observational and confounded.
- CPU sample: 200 distinct recorded RGB hashes across 31 worlds; deterministic category/size round robin with world balancing, seed 20261015. It contains 450 visible catalogued objects, including 225 from the four categories. This enriched, trajectory-based sample is not a prevalence estimate or an independent real-flight holdout.
- Single-pass CPU results after the default filter reproduce cached sample recall exactly, including every small-object size bucket. The raw model weights, text encoder, sample and runtime versions are bound in `cpu/receipt.json`.

## Cached recall by category and source-image short side

| Category | All sizes | <8 px | 8-16 px | 16-32 px | 32-64 px | >=64 px |
| --- | --- | --- | --- | --- | --- | --- |
| stop_sign | 36/162 (22.2%) | 22/144 (15.3%) | 14/18 (77.8%) | no objects | no objects | no objects |
| traffic_cone | 327/612 (53.4%) | 72/351 (20.5%) | 145/147 (98.6%) | 91/94 (96.8%) | 19/20 (95.0%) | no objects |
| fire_hydrant | 102/422 (24.2%) | 45/365 (12.3%) | 48/48 (100.0%) | 9/9 (100.0%) | no objects | no objects |
| backpack | 188/280 (67.1%) | 53/145 (36.6%) | 96/96 (100.0%) | 39/39 (100.0%) | no objects | no objects |

| Short side | All categories | Four small categories |
| --- | --- | --- |
| <8 | 717/2849 (25.2%) | 192/1005 (19.1%) |
| 8-16 | 1629/1672 (97.4%) | 303/309 (98.1%) |
| 16-32 | 1597/1608 (99.3%) | 139/142 (97.9%) |
| 32-64 | 1040/1058 (98.3%) | 19/20 (95.0%) |
| >=64 | 513/523 (98.1%) | no objects |

Cached **onboard** recall collapses below 8 px: 19.1% versus 98.1% at 8–16 px. That is a system-level threshold, not evidence that YOLOE itself cannot propose sub-8-pixel boxes; the CPU comparison below isolates the filter.

| Short side (px) | Small-category cached recall |
| --- | --- |
| 1 | 0/84 (0.0%) |
| 2 | 0/193 (0.0%) |
| 3 | 6/219 (2.7%) |
| 4 | 16/158 (10.1%) |
| 5 | 24/115 (20.9%) |
| 6 | 62/130 (47.7%) |
| 7 | 84/106 (79.2%) |
| 8 | 73/76 (96.1%) |
| 9 | 71/72 (98.6%) |
| 10 | 47/48 (97.9%) |
| 11 | 48/48 (100.0%) |
| 12 | 20/20 (100.0%) |

## Cached recall by recorded altitude

| Local altitude (m) | All categories | Four small categories |
| --- | --- | --- |
| <8 | 217/287 (75.6%) | 59/85 (69.4%) |
| 8-12 | 1764/2649 (66.6%) | 309/717 (43.1%) |
| 12-16 | 844/1084 (77.9%) | 90/166 (54.2%) |
| 16-20 | 640/866 (73.9%) | 57/118 (48.3%) |
| 20-24 | 815/1126 (72.4%) | 54/128 (42.2%) |
| 24-28 | 1160/1622 (71.5%) | 73/243 (30.0%) |
| 28-32 | 40/60 (66.7%) | 1/9 (11.1%) |
| >=32 | 16/16 (100.0%) | 10/10 (100.0%) |

| Category | <8 m | 8-12 m | 12-16 m | 16-20 m | 20-24 m | 24-28 m | 28-32 m | >=32 m |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| stop_sign | no objects | no objects | 1/4 (25.0%) | 0/34 (0.0%) | 24/80 (30.0%) | 11/44 (25.0%) | no objects | no objects |
| traffic_cone | 33/33 (100.0%) | 190/354 (53.7%) | 30/56 (53.6%) | 20/32 (62.5%) | 8/22 (36.4%) | 46/115 (40.0%) | no objects | no objects |
| fire_hydrant | 4/11 (36.4%) | 55/271 (20.3%) | 18/47 (38.3%) | no objects | 14/18 (77.8%) | 6/70 (8.6%) | no objects | 5/5 (100.0%) |
| backpack | 22/41 (53.7%) | 64/92 (69.6%) | 41/59 (69.5%) | 37/52 (71.2%) | 8/8 (100.0%) | 10/14 (71.4%) | 1/9 (11.1%) | 5/5 (100.0%) |

High-altitude bins have very few small objects and favorable viewing geometry in some trajectories; the >=32 m 10/10 result is not a basis for a high-altitude small-object search policy.

## Paired CPU experiment: 200 frames

Fine-tuned YOLOE-11s, same fixed generic vocabulary, confidence 0.05 and imgsz 1280, PyTorch 2.6.0+cu124 / Ultralytics 8.4.92. `device=cpu`, `CUDA_VISIBLE_DEVICES=-1`, four torch CPU threads and one OpenCV thread. No GPU inference, retraining, downloads, simulator or Docker. Four 712x534 crops cover each 1280x960 frame with >=20% crop-relative overlap and ~1.80x linear magnification; each is resized to the same inference size. Class-agnostic greedy NMS at IoU 0.5 merges restored full-image coordinates. The optional production mode keeps the full-frame pass too (five passes).

Raw = before onboard filtering. Default filter = minimum side 6 px, maximum area 25% of frame, top eight by score. Min2 = same area and candidate limits with `proposal_min_side_px=2`.

| Mode | <8 px | 8-16 px | 16-32 px | 32-64 px | >=64 px | All small |
| --- | --- | --- | --- | --- | --- | --- |
| Single, raw | 75/90 (83.3%) | 72/74 (97.3%) | 44/44 (100.0%) | 16/17 (94.1%) | no objects | 207/225 (92.0%) |
| 4 tiles, raw | 83/90 (92.2%) | 74/74 (100.0%) | 41/44 (93.2%) | 17/17 (100.0%) | no objects | 215/225 (95.6%) |
| Full + 4 tiles, raw | 84/90 (93.3%) | 74/74 (100.0%) | 44/44 (100.0%) | 17/17 (100.0%) | no objects | 219/225 (97.3%) |
| Single, default filter | 17/90 (18.9%) | 71/74 (95.9%) | 44/44 (100.0%) | 16/17 (94.1%) | no objects | 148/225 (65.8%) |
| 4 tiles, default filter | 14/90 (15.6%) | 74/74 (100.0%) | 41/44 (93.2%) | 17/17 (100.0%) | no objects | 146/225 (64.9%) |
| Full + 4 tiles, default filter | 16/90 (17.8%) | 74/74 (100.0%) | 44/44 (100.0%) | 17/17 (100.0%) | no objects | 151/225 (67.1%) |
| Single, min2 filter | 75/90 (83.3%) | 72/74 (97.3%) | 44/44 (100.0%) | 16/17 (94.1%) | no objects | 207/225 (92.0%) |
| 4 tiles, min2 filter | 82/90 (91.1%) | 74/74 (100.0%) | 41/44 (93.2%) | 17/17 (100.0%) | no objects | 214/225 (95.1%) |
| Full + 4 tiles, min2 filter | 83/90 (92.2%) | 74/74 (100.0%) | 44/44 (100.0%) | 17/17 (100.0%) | no objects | 218/225 (96.9%) |

| Mode | Small recall @0.5 | Proposals/frame | Unmatched scene/frame | Unmatched catalogued/frame |
| --- | --- | --- | --- | --- |
| Single, raw | 194/225 (86.2%) | 2.500 | 0.085 | 0.225 |
| 4 tiles, raw | 205/225 (91.1%) | 2.645 | 0.210 | 0.330 |
| Full + 4 tiles, raw | 210/225 (93.3%) | 2.800 | 0.230 | 0.375 |
| Single, default filter | 141/225 (62.7%) | 1.870 | 0.060 | 0.170 |
| 4 tiles, default filter | 142/225 (63.1%) | 1.950 | 0.120 | 0.225 |
| Full + 4 tiles, default filter | 146/225 (64.9%) | 2.030 | 0.135 | 0.250 |
| Single, min2 filter | 194/225 (86.2%) | 2.490 | 0.085 | 0.225 |
| 4 tiles, min2 filter | 205/225 (91.1%) | 2.640 | 0.210 | 0.330 |
| Full + 4 tiles, min2 filter | 210/225 (93.3%) | 2.790 | 0.230 | 0.375 |

| Category | Single default | Single min2 | Hybrid min2 |
| --- | --- | --- | --- |
| stop_sign | 17/35 (48.6%) | 29/35 (82.9%) | 33/35 (94.3%) |
| traffic_cone | 59/81 (72.8%) | 79/81 (97.5%) | 80/81 (98.8%) |
| fire_hydrant | 30/53 (56.6%) | 44/53 (83.0%) | 49/53 (92.5%) |
| backpack | 42/56 (75.0%) | 55/56 (98.2%) | 56/56 (100.0%) |

| Mode | CPU mean (ms) | Median (ms) | p95 (ms) | Mean / single |
| --- | --- | --- | --- | --- |
| single | 668.4 | 572.7 | 1364.8 | 1.00x |
| tiles | 2713.1 | 2317.7 | 5415.5 | 4.06x |
| hybrid | 3381.5 | 2927.6 | 6723.7 | 5.06x |

Times exclude image reads, weight/text embedding initialization and one full-frame + four-tile warmup per invocation. Hybrid time is the sum of its single-pass duration, four-tile duration and hybrid NMS, using those same paired predictions rather than another five passes. The interrupted run resumed at frame 128, with receipt/hash validation and a second warmup. Host CPU load varied, so ratios are more transferable than absolute CPU latency; they do not measure the Jetson or concurrent VLM performance.

At the existing 6 px filter, hybrid increases total small recall only 65.8% → 67.1% and actually lowers sub-8 recall 18.9% → 17.8%: tighter tiled boxes can cross the rejection threshold, and NMS can replace a larger single-pass box. At min2, single-pass sub-8 recall reaches 83.3% and hybrid reaches 92.2%; the additional 11/225 objects cost about four extra detector passes. Single min2 adds 0.62 proposals/frame and 0.025 unmatched-scene proposals/frame over the existing single/default policy. These extra candidates can still increase slow-VLM work.

## Orin Nano 8GB detector cost estimates

The **50–90 ms** single-pass TensorRT FP16 estimate is supplied by the task, not a measurement in this report. Multiply it by serial detector passes; add crop/preprocess/NMS overhead, memory transfers and contention with the slow VLM. No TensorRT throughput or peak-memory claims are inferred from the desktop CPU timings.

| Policy | Passes/frame | Detector estimate | Evidence/limitation |
| --- | --- | --- | --- |
| Single; optional min2 + lower altitude | 1 | 50–90 ms | Min2 recall measured; descent recall not experimentally measured |
| Four tiles only | 4 | 200–360 ms | Measured offline; loses some full-frame objects |
| Full frame + four tiles | 5 | 250–450 ms | Implemented optional proposer_tiled mode |
| Full + lower two existing tiles | 3 | 150–270 ms | Cost/coverage estimate only; partial-ROI mode not implemented or inferred |
| Full + one selected ~half-size ROI | 2 | 100–180 ms | Cost estimate only; crop selection recall unmeasured |
| Hybrid every fifth frame on small-object tasks | 1.8 average on those tasks | 90–162 ms average; 250–450 ms burst | Scheduler alternative, not automatically enabled |

Task gating only: if fraction p of detector frames use hybrid, average cost is `(1 + 4p) * 50–90 ms`; combine task gating and every-k fallback as `(1 + 4p/k) * 50–90 ms`. Scheduling after an unsuccessful single pass is useful only if it is rate limited; otherwise failed searches still consume five passes repeatedly. Serial tile processing avoids batching all five images, but this is not an Orin memory validation.

For ROI selection, 1182/1476 (80.1%) of four-category object centers lie in the lower image half. The bottom two existing overlapping crops start at row 426 and geometrically cover 1425/1476 (96.5%) of centers. This is center coverage, not measured proposal recall: boundary truncation, image attitude and NMS remain relevant. A lower ROI is a credible next experiment, not evidence that it retains that percentage of tiling's gain.

## Camera geometry and task-aware altitude

Recorded logs use 1280x960, fx=fy=539.936368 px, cx=640, cy=480. `camera_projection.py` has 0.18 rad downward mount pitch. `TYPICAL_SIZE_M` is a largest-horizontal-extent prior, not the visible mask's short side. For a level aircraft and a transverse extent L on the central column, with q=(v-cy)/fy and mount depression a:

```text
optical_depth = H / (sin(a) + q*cos(a))
short_side_px ~ fx * L * (sin(a) + q*cos(a)) / H
Hmax = fx * L * (sin(a) + q*cos(a)) / desired_px
```

H is camera height above the object point. At level attitude, convert to local base altitude as `H + z_object_up - 0.12`. The largest-extent projection is an optimistic proxy for the short side; orientation, occlusion and foreshortening can reduce it. Use 8 px as the observed system collapse boundary and 16 px as an initial planning margin.

| Category | Typical L (m) | 8 px Hmax center (m) | 8 px Hmax row 676.5 (m) | 16 px Hmax row 676.5 (m) | Existing policy 16 px budget (m) |
| --- | --- | --- | --- | --- | --- |
| stop_sign | 0.80 | 9.67 | 29.00 | 14.50 | 13.63 |
| traffic_cone | 0.50 | 6.04 | 18.13 | 9.06 | 8.52 |
| fire_hydrant | 0.60 | 7.25 | 21.75 | 10.88 | 10.22 |
| backpack | 0.50 | 6.04 | 18.13 | 9.06 | 8.52 |

The existing `size_aware_search` formula uses the final column (slant-range approximation, about 6% lower than optical-depth geometry at that off-axis row), then clamps to the requested altitude and an 8 m minimum. Nominal 16 px task settings are therefore about **13.6 m stop sign, 8.5 m cone, 10.2 m hydrant and 8.5 m backpack**. These are initial search settings, not guaranteed recall or permission to fly below obstacle clearance. The optical-center and shallow-ray budgets are much lower; keep targets lower in the image and approach rather than relying on one altitude.

Measured asset widths in `configs/sim-assets/fuel-catalog.json` are narrower than some task priors: cone 0.503 m, hydrant 0.424 m, backpack 0.264 m, sign face 0.593 m. At row 676.5 their illustrative 16 px height budgets are 9.12, 7.68, 4.78 and 10.75 m respectively. Thus the existing 8 m floor cannot guarantee 16 px for hydrants or backpacks. A stop sign's 0.023 m plate thickness also makes bearing critical: a face-on approach can be more useful than simply descending. Full derivation and caveats are in `../geometry/geometry-altitudes.md`.

## Configuration and implementation

Existing default behavior remains one pass and a 6 px minimum proposal side. An opt-in small-object task configuration, after checking operating altitude/clearance, is:

```json
{
  "proposer_tiled": false,
  "proposal_min_side_px": 2,
  "size_aware_search": true,
  "min_search_px": 16,
  "min_search_altitude_m": 8
}
```

For a separately scheduled tiling fallback, set `proposer_tiled: true`; optional `proposer_tile_overlap: 0.2` and `proposer_merge_iou: 0.5` control the fixed full-frame + four-crop mode. The boolean is explicit; no automatic task gating or ROI/cadence scheduler is added here. Keep `proposal_min_side_px: 2` if the aim is to retain the measured tiny-box gain.

Files changed for this task:

- `scripts/onboard_policy.py`: default-off hybrid proposer, overlap/merge validation, editable minimum proposal side, CPU device selection for the harness; size-aware altitude now uses the effective config-overridden task text.
- `scripts/analyze_small_object_proposals.py`: cache/segmentation joins, category/size/altitude recall and deterministic enriched manifest.
- `scripts/evaluate_small_object_tiling.py`: resumable CPU-only paired experiment, local text encoder binding, hashes, raw/default/min2 metrics, timing.
- `tests/test_onboard_proposer_tiling.py`: stub detector tests for default one pass, five-pass crop coverage/restoration, class-agnostic NMS, small images, invalid options, config/filter wiring and effective-task altitude.
- `tests/test_small_object_proposal_analysis.py`: tiny panoptic truth, cache dedup/conflicts, joins, IoU and bucket boundaries.
- `tests/test_small_object_tiling_evaluation.py`: benchmark/onboard filter parity and summary alignment/raw/default/min2 distinctions.

The pre-existing `src/multidetect/simulator/__main__.py` diff was left untouched. No protected files changed and no commit was made. All analysis outputs are inside this worktree's `tmp/`.

## Validation and reproduction

48 relevant tests pass. Ruff passes on the six changed source/test files. The separate, artifact-dependent `test_onboard_policy_r37_evidence.py` cannot pass in this worktree because its R37 ONNX artifact is absent; this is unrelated to the proposer tests and was not patched around.

```powershell
python -m pytest -q -p no:warnings --basetemp tmp/pytest-smallobj-final tests/test_onboard_proposer_tiling.py tests/test_small_object_proposal_analysis.py tests/test_small_object_tiling_evaluation.py tests/test_onboard_policy_plan.py tests/test_place_memory_integration.py
python -m ruff check scripts/onboard_policy.py scripts/analyze_small_object_proposals.py scripts/evaluate_small_object_tiling.py tests/test_onboard_proposer_tiling.py tests/test_small_object_proposal_analysis.py tests/test_small_object_tiling_evaluation.py

# Full cache audit; no detector inference
python scripts/analyze_small_object_proposals.py --source-root D:/Projects/Multi-Detect --output tmp/small-object-proposals --sample-size 200 --seed 20261015 --workers 4

# Completed/resumable CPU experiment; only local weights
python scripts/evaluate_small_object_tiling.py --sample tmp/small-object-proposals/sample.jsonl --weights D:/Projects/Multi-Detect/artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt --text-weights D:/Projects/Multi-Detect/weights/mobileclip_blt.ts --output tmp/small-object-proposals/cpu --threads 4

# Rebuild this report/metrics without any inference
python tmp/small-object-proposals/write_report.py
```

Machine-readable evidence: `cached-audit.json`, `frames.jsonl`, `sample.jsonl`, `cpu/predictions.jsonl`, `cpu/metrics.json`, `cpu/receipt.json` and `report-input-hashes.json`. No further model inference is needed to inspect the measurements or regenerate the tables.

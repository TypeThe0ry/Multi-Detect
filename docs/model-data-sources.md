# Fire/smoke model data sources

This inventory is the provenance boundary for model training. A public download is not added to
training until its archive, class mapping, license, duplicate rate and split integrity have been
checked locally.

## User-provided forest-fire dataset

- Location: `森林火灾 检测 7000多   数据集`
- Images: 7,414 (`train=6260`, `val=774`, `test=380`)
- Training labels: 15,080 fire boxes, 7,904 smoke boxes, 692 background images
- Exact duplicate audit: two duplicate hash groups; one source image crosses train/validation
- Source-stem audit: 3,129 repeated source groups, mostly Roboflow augmentation variants
- Rights/provenance: pending user review
- Policy: retain the original test split for historical comparison, but rebuild the next
  source-grouped benchmark without cross-split source variants before any production claim

## Local-camera hard negatives

- Session `20260712-212229`: 297 training negatives and 75 held-out negatives
- Session `20260712-220350-v2`: 43 training negatives from 1,800 no-fire frames
- The second session reproduced three raw flame false positives; maximum confidence was 0.7033
- Labels are intentionally empty because the sessions were explicitly captured as no-fire scenes
- Rights/provenance: locally captured by the project operator
- Policy: temporally adjacent frames remain training evidence only; they are not a deployment-domain
  test set

## Indoor Fire Smoke Dataset (Zenodo v1)

- DOI: <https://doi.org/10.5281/zenodo.15826133>
- License: CC BY 4.0
- Archive: `artifacts/external-data/zenodo-indoor-fire-smoke-v1/Indoor Fire Smoke.zip`
- Published MD5 and verified local MD5: `086fbc3b874139276097f4057bc45a3c`
- Images: 5,000 (`train=3500`, `valid=750`, `test=750`)
- Verified mapping: class `0=fire`, class `1=smoke`
- Background images: zero
- Policy: initially use the original test split as external positive-domain evaluation. Do not add
  all 3,500 positive training images until the negative ratio is corrected with D-Fire and the
  source-stem duplicate audit is complete.

## D-Fire

- Official repository: <https://github.com/gaia-solutions-on-demand/DFireDataset>
- Official collection license: CC0 1.0
- Reported size: more than 21,000 images
- Reported categories: 1,164 fire-only, 5,867 smoke-only, 4,658 fire-and-smoke, 9,838 none
- Format: YOLO normalized bounding boxes, class mapping reported as fire/smoke
- Download source: public Kaggle mirror `shubhamkarande13/d-fire`
- Local archive: `artifacts/external-data/dfire-cc0/d-fire.zip`
- Downloaded bytes: `3049199055`
- ZIP audit: 21,527 images and 21,527 labels; `train=17221`, `test=4306`; no missing
  labels, orphan labels or cross-split source stems
- Verified backgrounds: 9,838, exactly matching the official collection summary
- Verified source mapping from official box totals: class `0=smoke` (11,865 raw boxes), class
  `1=fire` (14,692 raw boxes); this must be swapped for the local `0=fire`, `1=smoke` contract
- Label quality: 397 boxes extend outside the normalized image and require audited clipping or
  quarantine before use
- Status: extraction and audited repair complete. The local contract contains 17,221 training and
  4,306 external-test images; 379 boxes were clipped, 18 unusable boxes were dropped, and the
  2,005 empty-label test images remained external negative evidence.
- Policy: preserve a source-grouped external negative holdout before adding any D-Fire negatives to
  training. The mirror is not trusted as identical to the official collection until local contents
  have been audited.

## Excluded for now

- DFS-FIRE-SMOKE: useful `other` class and 9,462 images, but no clear repository license was found.
- FASDD: not used while the accessible publication record is marked withdrawn and redistribution
  status is unresolved.
- Private/contact-only datasets and untraceable web scraping are excluded.

## Acceptance policy for the next candidate

The next model must pass all of these independent gates:

1. Original 380-image test mAP50 must not regress by more than 0.01 from the current candidate.
2. Both local-camera hard-negative sets must be reported at thresholds 0.10, 0.25, 0.50 and 0.65.
3. A source-grouped D-Fire negative holdout must remain untouched by training.
4. The Zenodo test split must report per-class precision, recall and mAP separately.
5. A fresh live-camera no-fire run must produce no multi-frame-confirmed alert.
6. The model remains quarantined and cannot authorize a payload or establish person clearance.

## Latest candidate: V5 local-calibrated

- Snapshot: `artifacts/training/hardneg-snapshots/v5-local-calibrated`
- Status: quarantined local runtime candidate; production approval remains false.
- Original test: precision `0.859`, recall `0.783`, mAP50 `0.869`, mAP50-95 `0.502`.
- D-Fire external test: mAP50 `0.433`; indoor external test: mAP50 `0.456`.
- D-Fire 2,005-image background holdout: 14 false-positive images at confidence `0.50`,
  5 at `0.65`, and 0 at `0.82` (V1 baseline: 58, 18, and 1).
- Local holdout: 0/75 at every reported threshold. The older 43-image camera set retains one
  `flame` candidate at `0.659`, below the revised display threshold `0.72` and task threshold `0.82`.
- Fresh camera validation: 900 no-fire frames, one raw candidate at `0.112`, zero display or task
  threshold triggers.

## V6 scenario-balanced experiment (not promoted)

- Starting point: V5; one frozen-backbone epoch at learning rate `5e-6`.
- Auditable effective training list: 12,254 entries from 7,280 inputs. The builder repeats dark
  positives and small-object positives at most once, while 1,712 background/hard-negative images
  remain single-weighted.
- At the unchanged runtime thresholds (`flame=0.72`, `smoke=0.60`), local validation recall rose
  from `0.539` to `0.557` and dark-scene recall rose from `0.491` to `0.506`; precision fell from
  `0.963` to `0.960`, with false-positive boxes increasing from 58 to 64.
- Original test mAP50 remained effectively flat (`0.869`), but external D-Fire mAP50 regressed
  from `0.433` to `0.416`. The candidate is therefore retained only as a quarantined experiment;
  V5 remains the runtime snapshot.
- Full decision evidence: `artifacts/evaluation/v6-candidate-decision.json`.

## V7/V8 precision experiments (not promoted)

- V7 started from V5 and used one frozen-backbone epoch over the 37,103-entry multisource list.
  External mAP50 improved (`D-Fire 0.448`, indoor `0.473`), but the isolated 2,005-image D-Fire
  background holdout regressed to 46/12/1 false-positive images at confidence 0.50/0.65/0.82.
- V8 repeated the 10,749 empty-label training entries three times, producing 58,601 effective
  entries with an approximately 55% background ratio. It reduced the same holdout result to
  23/8/1 while preserving local mAP50 (`0.869`), but remained worse than V5's 14/5/0 result.
- At the unchanged class thresholds, V8 produced 65 local-validation false-positive boxes versus
  V5's 58. Both candidates remain quarantined; V5 remains the runtime snapshot.
- Full evidence: `artifacts/evaluation/v7-candidate-decision.json` and
  `artifacts/evaluation/v8-candidate-decision.json`.

## V9/V10 low-rate precision refinements (not promoted)

- V9 started from V8 and ran one additional frozen-backbone epoch over the same 58,601-entry
  background-balanced list at learning rate `1e-6`.
- Original-test precision improved from `0.852` to `0.861`, while recall fell from `0.784` to
  `0.776`; D-Fire and indoor external mAP50 stayed effectively unchanged at `0.446` and `0.469`.
- On the 2,005-image D-Fire background holdout, false-positive images improved from V8's
  23/8/3/1 to 21/6/3/1 at confidence 0.50/0.65/0.72/0.82.
- At the unchanged per-class runtime thresholds, V9 produced 66 local-validation false-positive
  boxes versus V5's 58, for less than one percentage point of recall gain. V9 therefore remains
  quarantined and V5 remains the runtime snapshot.
- Full evidence: `artifacts/evaluation/v9-candidate-decision.json`.
- V10 increased the empty-label repeat from three to five, creating 80,099 effective entries with
  approximately 67% backgrounds. It improved the D-Fire background holdout to 18/4/2/0 at
  confidence 0.50/0.65/0.72/0.82 and moved the local-camera false candidate below 0.65.
- The heavier ratio crossed the useful tradeoff: at runtime thresholds V10 had 63 false-positive
  boxes and recall `0.535`, both worse than V5's 58 and `0.539`. Original mAP50 also fell to
  `0.864` and indoor external mAP50 to `0.466`. Repeating this same background-heavy lineage is
  stopped; the next candidate requires new flight-camera hard negatives and matched positive scenes.
- Full V10 evidence: `artifacts/evaluation/v10-candidate-decision.json`.

## V11 V5-background calibration branch (not promoted)

- V11 restarted from the V5 runtime baseline instead of continuing the degraded V10 lineage. It
  ran one frozen-backbone epoch at learning rate `1e-6` over V8's 58,601-entry balanced list.
- Original-test recall rose from `0.783` to `0.788`; D-Fire and indoor external mAP50 rose from
  `0.433`/`0.456` to `0.444`/`0.471`. Original-test precision fell to `0.845`, and mAP50 was
  effectively flat at `0.868`.
- At unchanged runtime thresholds, recall rose from `0.539` to `0.556`, but false-positive boxes
  increased from 58 to 67. The 2,005-image D-Fire background holdout regressed from V5's
  14/5/0 at confidence 0.50/0.65/0.82 to 28/10/1; the hardest local negative reached `0.687`.
- The ONNX export passed the post-NMS Nx6 interface check and is bound to a quarantined manifest,
  but accuracy promotion failed. V5 remains the runtime snapshot.
- Full V11 evidence: `artifacts/evaluation/v11-candidate-decision.json`.

## Threshold calibration evidence

- `scripts/calibrate_fire_thresholds.py` performs a reproducible IoU-matched threshold sweep and
  reports background, flame-only, smoke-only, combined, dark, normal-light and bright strata.
- On V5 local validation, an F0.5 objective with a recall floor of 0.70 would choose approximately
  `0.44` for both classes. This would improve recall but conflicts with the observed false-positive
  priority, so runtime thresholds remain `flame=0.72`, `smoke=0.60` plus six-frame confirmation.
- These are local validation results, not deployment-domain certification. RTSP flight imagery and
  site-specific hard negatives are still required before changing production thresholds.

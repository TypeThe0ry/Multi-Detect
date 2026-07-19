# Offline AI optimization — 2026-07-16

## Common-object detector calibration

The pinned raw YOLO26n COCO80 ONNX was evaluated on the 128-image COCO128 debug
set with the project detector/post-processing path. The selected tiled discovery
profile keeps the full-frame pass and adds a two-column scan every three frames:

- tile classes: person, bicycle, car, motorcycle, bus, train, truck, boat;
- tile confidence: `0.40`;
- mapped tile box area: at most `0.04` of the source frame;
- overlap: `0.15`;
- same-class suppression IoU: `0.30`;
- priority new-track confidence: `0.25`;
- arbitrary-object new-track confidence: `0.35`.

At confidence `0.25` and IoU `0.50`, priority-class precision/recall changed from
`0.8444 / 0.5758` to `0.8462 / 0.6000` (TP `190 -> 198`, FP `35 -> 36`). All-class
precision/recall changed from `0.7872 / 0.5016` to `0.7943 / 0.5070`. The selected
report is stored under `artifacts/evaluation/coco128-yolo26n-tiled-area004-iou030-t040.json`.

COCO128 is a pipeline/debug set rather than a deployment-domain acceptance set. The
camera-domain review and target-Orin latency measurement remain separate gates.

### Aircraft priority route, 2026-07-18

`airplane` is now a first-class priority class through tile discovery, unified
target tracking and exclusive LCK routing.  It is no longer treated as an arbitrary
object during a lock, so the common COCO detector remains active at full LCK cadence.
The tracker uses an aircraft-specific motion profile and a deterministic
shape/edge/colour appearance descriptor for LOST identity recovery; person and
vehicle embeddings remain separate domains.

The initial `0.40` tile threshold produced one extra COCO128 airplane false positive
(`0.8106`) while the only useful tiled airplane candidate scored `0.8529`.  The
runtime therefore retains the normal `0.40` threshold for people/vehicles but uses a
class-specific tile override of `airplane=0.82`.  The replay report
`artifacts/evaluation/coco128-yolo26n-tiled-priority-aircraft-t082-iou030-t040.json`
keeps airplane at TP `6`, FP `0`, FN `0` (precision/recall `1.000/1.000`) and moves
the selected priority set from full-frame TP/FP/FN `196/35/140` to tiled `204/36/132`.
The extra tile false positive is removed without reducing the measured airplane
recall on this debug set.

The same profile was then checked against the full 548-image VisDrone validation split
after mapping its ten source classes into the runtime person/vehicle families. Precision
and recall changed from `0.8454 / 0.1494` to `0.8554 / 0.2028` (TP `5,792 -> 7,860`,
FP `1,059 -> 1,329`). CPU p50 latency changed from `43.01 ms` to `131.92 ms` on tile
scan frames; the runtime performs that scan once every three frames. The report is
`artifacts/evaluation/visdrone-val-coco-yolo26n-tiled-calibrated.json`.

## Aerial priority-object candidate

The official VisDrone2019-DET train/validation data was downloaded and converted
locally without the optional test-dev split:

- train: 6,471 images / 343,205 retained objects;
- validation: 548 images / 38,759 retained objects;
- local YAML: `C:/Users/TT/Documents/GitHub/datasets/VisDrone-local/visdrone-local.yaml`.

`scripts/train_visdrone_priority_candidate.py` fine-tunes the pinned YOLO26n checkpoint,
validates it, exports an opset-17 raw ONNX without embedded TopK/NMS, verifies the ONNX
graph, hashes both artifacts, and records a JSON summary. A one-epoch 1% smoke run
completed through training, validation, export, ONNX checking, and hashing.

The full 30-epoch 960-pixel run completed with validation precision `0.4648`, recall
`0.3753`, mAP50 `0.3473`, and mAP50-95 `0.2027` across all ten source classes. The
project runtime evaluation at confidence `0.25`, after semantic class mapping, produced
priority-class precision `0.7788` and recall `0.5168` on all 548 validation images.

The raw ONNX SHA-256 is
`28254b7148aae376ef31a1348c053859ef226661ba48234db9612c0a6e0f0f87`. On
2026-07-16 it was transferred to the target Orin NX and converted locally with
TensorRT 8.6.2 FP16. The resulting engine SHA-256 is
`f3b0e9e2a8f9c0fbbb5242c077e72f3f35e54a1ccc7bb7488f5cd787c38be69c`.

The live 720P H.265 RTSP comparison selected a `0.30` priority threshold and disabled
the now-redundant COCO two-column scan in the resident process. Against the otherwise
identical three-model run, steady processing increased from `6.72` to `7.77 FPS` and
inference P95 fell from `168.08` to `113.97 ms`. The 60-frame selected profile had no
camera reconnect, inference, target-pool, short-term tracking, or operator transport
errors; the capture queue high-water mark remained one.

## Frame-cadence optimization on Orin NX

The fire/smoke engine remains active on every frame. COCO80 and VisDrone are scheduled
on separate phases while the unified target pool and short-term tracker continue to run
for every captured frame. Skipped detector frames deliberately emit no copied boxes;
track continuity comes from the tracking layers rather than stale detections.

| Common / priority schedule | Frames | Steady FPS | Inference P50 / P95 |
|---|---:|---:|---:|
| both every frame | 60 | 7.77 | 111.29 / 113.97 ms |
| stride 2, phases 0 / 1 | 120 | 11.50 | 64.42 / 80.10 ms |
| stride 3, phases 0 / 1 | 180 | 13.75 | 56.62 / 80.32 ms |
| stride 4, phases 0 / 2 | 240 | 15.17 | 35.32 / 79.25 ms |
| selected filters + stride 4 | 240 | 15.41 | 33.02 / 76.07 ms |

The selected resident defaults are:

- COCO80: `frame_stride=4`, `frame_phase=0`;
- VisDrone: `frame_stride=4`, `frame_phase=2`;
- VisDrone person threshold: `0.30`;
- VisDrone vehicle threshold: `0.45`;
- mapped vehicle labels: two consecutive specialized-model observations, association
  IoU `0.25`, maximum one missed specialized invocation.

Before the vehicle filters, the indoor 240-frame sample contained 32 `motorcycle` and
4 `car` false candidates from the specialized model. The selected threshold plus
temporal gate reduced those candidates to zero. The final run processed at `15.41 FPS`
with source rate `15.49 FPS`, frame-age P50/P95 `128.78/141.66 ms`, zero camera
reconnects, capture queue high-water mark one, zero target-pool/tracking errors, and
795 V6X messages received with zero transmitted. This is a deployment-domain smoke
sample, not a substitute for the pending person/vehicle/fire field acceptance set.

A subsequent 5-minute resident window exposed additional domain-shift cases that were
not present in the selected 240 frames: 6 `car` candidates along the bottom image edge
and 59 `truck` candidates with normalized box areas from `0.107` to `0.230`. These were
retained as negative evidence instead of fitting a hard geometry cutoff to one indoor
view. The next calibration pass needs real aerial vehicle positives plus indoor/edge
negatives so class thresholds, edge acquisition margin, and maximum new-track box area
can be selected against measured precision/recall.

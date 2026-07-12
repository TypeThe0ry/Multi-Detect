# Upstream fire/smoke baseline audit

## Decision

Use `gengyanlei/fire-smoke-detect-yolov4` only as a pinned, quarantined
perception baseline. Do not merge the upstream repository into the mission
controller and do not run its pickle checkpoint on flight hardware.

Audited source:

- Repository: <https://github.com/gengyanlei/fire-smoke-detect-yolov4>
- Commit: [`98b1fec0f82e09d67ef5fc657a80eaf0b1450360`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/tree/98b1fec0f82e09d67ef5fc657a80eaf0b1450360)
- Audit date: 2026-07-12

The maintainer says the code has stopped updating and recommends using the
dataset to retrain a detector. Core inference files date from 2020 even though a
README-only update was made later.

## Capability fit

| Required capability | Upstream coverage | Integration decision |
| --- | --- | --- |
| RGB fire candidate detection | Partial | YOLOv4 detects one `fire` class; baseline only. |
| RGB fire and smoke detection | Partial | YOLOv5s detects `fire` and `smoke`; baseline/pre-labeler only. |
| Smoldering and burned-area classes | None | Requires new licensed data and retraining. |
| Person, firefighter, vehicle, building, power-line, or tank detection | None in the fire/smoke weights | Use separately governed safety-object models. |
| Instance/semantic segmentation | None | Add an independently validated segmentation component. |
| Thermal/RGB spatial corroboration | None | Add calibrated sensor registration and fusion. |
| Multi-object tracking | None | Consume normalized detections in a separate tracker. |
| Deployment-zone and telemetry rules | None | Keep in the fail-closed safety rules engine. |
| Human authorization | None | Keep in mission control with short-lived challenges. |
| Payload state, interlocks, or release confirmation | None | Keep completely outside perception. |

This is not a full autonomous mission stack. It supplies, at most, untrusted
visual candidates to the rest of Multi-Detect.

## Relevant upstream files

| Path | Finding |
| --- | --- |
| [`readmes/README_ZN.md`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/readmes/README_ZN.md) | Documents the old CUDA/PyTorch environment, dataset links, and academic-use statement. |
| [`yolov5/data/fire_smoke.yaml`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/data/fire_smoke.yaml#L1-L9) | Defines two classes in order: `fire`, `smoke`; training paths are hard-coded. |
| [`yolov5/detect.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/detect.py) | CLI inference, source loading, preprocessing, NMS, coordinate scaling, drawing, and file output. It deletes the configured output directory on startup. |
| [`yolov5/utils/general.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/utils/general.py#L589-L670) | NMS produces `(x1, y1, x2, y2, confidence, class_id)`. |
| [`yolov5/models/experimental.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/models/experimental.py#L132-L145) | Loads the complete pickled model with `torch.load`. |
| [`latest_darknet_API.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/latest_darknet_API.py#L54-L109) | Provides the safer of the two YOLOv4 coordinate conversions. |
| [`yolov4/yolov4_to_onnx/onnx_to_trt7.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov4/yolov4_to_onnx/onnx_to_trt7.py) | Obsolete TensorRT 7 sample with missing imports/definitions; not reusable as-is. |

No published model card, calibration report, mAP table, false-positive rate,
false-negative rate, thermal validation, or deployment-domain acceptance data
was found in the pinned tree.

The complete file tree at the pinned commit was rechecked during integration preparation. It contains historical ONNX conversion scripts, but no committed `.onnx`, TensorRT engine, or directly usable Darknet weight artifact. Therefore the application cannot obtain a safe runtime model merely by checking out that commit.

## Canonical interface mapping

### Legacy YOLOv5

Post-NMS upstream result:

```text
(x1_px, y1_px, x2_px, y2_px, confidence, class_id)
```

Multi-Detect adapter result:

```text
Detection(
    label = {0: "flame", 1: "smoke"}[class_id],
    confidence = confidence,
    bbox = BoundingBox(
        x1 = clamp(x1_px / image_width, 0, 1),
        y1 = clamp(y1_px / image_height, 0, 1),
        x2 = clamp(x2_px / image_width, 0, 1),
        y2 = clamp(y2_px / image_height, 0, 1),
    ),
    sensor = SensorKind.RGB,
    model_version = immutable_artifact_sha256,
)
```

`fire` is the upstream model label; the existing adapter deliberately maps it
to Multi-Detect's canonical `flame` ontology. The source label and class index
remain available as metadata for audit and replay.

Reject zero-area, inverted, non-finite, or out-of-range boxes. Do not infer
authorization, safety clearance, target priority, or release eligibility in
this adapter.

The upstream text output is not an acceptable boundary because it omits
confidence. The upstream rendered image/video output is diagnostic only.

## Architecture boundary

```text
RGB camera
  -> maintained runtime (ONNX/TensorRT)
  -> legacy-compatible decoder and normalization adapter
  -> Detection candidates
  -> tracker
  -> independent thermal and safety-object corroboration
  -> fail-closed safety rules
  -> operator authorization
  -> payload controller with interlocks and confirmation
```

There is intentionally no path from the model adapter directly to a payload
controller.

## Compatibility findings

- Documented baseline: Python 3.6+, Ubuntu 16.04/18.04, CUDA 10.x,
  PyTorch 1.6+, torchvision 0.7+, and OpenCV.
- Current Multi-Detect requires Python 3.11, so importing the legacy package
  into the application environment is unsupported.
- The repository's prebuilt Darknet artifacts have an unverified build chain
  and architecture. They must not be reused on Jetson.
- TensorRT engines are coupled to the target GPU, TensorRT, CUDA, and JetPack
  versions. Build and validate them on the exact target family.
- The old ONNX/TensorRT scripts are historical references, not a maintained
  deployment pipeline.

## Supply-chain finding

The pinned `yolov5/best.pt` is 14,758,954 bytes and has SHA-256:

```text
d1eae6859229ac1f5699c60f9445fa054dafc6a2cc59f00fc30ea6379dc3247e
```

Static inspection shows a PyTorch ZIP containing `archive/data.pkl` with a
complete `models.yolo.Model` object. Because the loader uses `torch.load`, the
checkpoint is executable pickle input. Direct loading in Multi-Detect, normal
CI, a developer session, or flight hardware is prohibited.

Any authorized evaluation must follow the disposable, offline export process
in [`models/README.md`](../models/README.md). The `.pt` is never promoted; only
a hashed, validated graph artifact and its manifest may progress to further
review.

## License and dataset review

- Repository root: MIT license.
- Darknet subtree: separate YOLO public-domain notice.
- YOLOv5 subtree: no separate license, despite clear Ultralytics origin. The
  contemporaneous 2020 Ultralytics YOLOv5 repository used
  [GPL-3.0](https://github.com/ultralytics/yolov5/blob/ea7e78cb1159e6a17821772c85c4c23ccc823b16/LICENSE).
  Treat the code as unresolved GPL-derived material until legal review is
  complete.
- Dataset documentation says
  [“academic exploration only” and cites multiple third-party sources](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/readmes/README_ZN.md#L120-L130).
  Dataset and derived-weight rights are unresolved.

Neither the source code nor the checkpoint is approved for commercial,
operational, or redistributable use based on this audit alone.

## Recommended adoption path

1. Keep this baseline out of the application dependency graph.
2. If approved, use the quarantined checkpoint only to create a reproducible
   accuracy baseline or offline pre-annotations.
3. Acquire or create a licensed dataset with deployment-domain coverage and
   explicit train/validation/test provenance.
4. Retrain a maintained detector with the required class ontology.
5. Export to a non-pickle runtime format and record a complete model manifest.
6. Validate pixel transforms and postprocessing against golden frames.
7. Measure per-domain precision/recall, calibration, latency, thermal
   corroboration, and person-exclusion performance before any field trial.
8. Keep human authorization and all payload safeguards mandatory regardless of
   model results.

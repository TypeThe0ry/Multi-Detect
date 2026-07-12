# Legacy fire/smoke detector reference

This directory records the provenance and integration boundary for the legacy
`gengyanlei/fire-smoke-detect-yolov4` project. No upstream source, executable,
dataset, or model weight is vendored here.

## Pinned upstream

- Repository: <https://github.com/gengyanlei/fire-smoke-detect-yolov4>
- Audited commit: [`98b1fec0f82e09d67ef5fc657a80eaf0b1450360`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/tree/98b1fec0f82e09d67ef5fc657a80eaf0b1450360)
- Default branch at audit time: `master`
- Audit date: 2026-07-12
- Upstream status: the maintainer states that the code has stopped updating and
  recommends retraining from the dataset. See the pinned
  [README](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/README.md#L9-L11).

Always use the full commit ID above in source links, build records, and model
manifests. Do not build from the moving `master` branch.

## What the upstream provides

| Baseline | Classes | Main entry point | Notes |
| --- | --- | --- | --- |
| Darknet YOLOv4 | `fire` | [`yolov4/darknet_API.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov4/darknet_API.py) | 608 x 608 RGB model; the weight is not stored in the repository. |
| Early PyTorch YOLOv5s | `0=fire`, `1=smoke` | [`yolov5/detect.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/detect.py) | The repository contains a legacy `yolov5/best.pt` checkpoint. |

The upstream is an RGB object-detection baseline only. It does not provide
tracking, segmentation, thermal fusion, person-exclusion logic, geofencing,
operator authorization, flight-control integration, payload interlocks, or
release verification.

## Detection interfaces

### YOLOv5

After non-maximum suppression, the in-memory tensor is:

```text
N x 6 = (x1, y1, x2, y2, confidence, class_id)
```

The coordinates are scaled back to the source image by `detect.py`. The
upstream `--save-txt` path writes only `class_id` plus normalized center-format
`x, y, width, height`; it omits confidence. Do not parse those text files as a
runtime interface. Adapt the in-memory result to Multi-Detect's normalized
`XYXY` `Detection` object instead. The project ontology maps upstream `fire`
to canonical `flame`; `smoke` remains `smoke`.

Evidence:

- [`non_max_suppression` return contract](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/utils/general.py#L589-L594)
- [coordinate scaling and text output](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/yolov5/detect.py#L81-L110)

### YOLOv4

The newer wrapper returns detections in this form when `is_show=False`:

```text
[(label, confidence, (center_x, center_y, width, height)), ...]
```

Its coordinates are source-image pixels after scaling and clipping. See
[`latest_darknet_API.py`](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/latest_darknet_API.py#L54-L109).

Do not use the older `yolov4/darknet_API.py` result without correction. It
rescales local variables for drawing but returns the original network-space
detections when `is_show=False`.

## Compatibility boundary

The documented upstream environment is Python 3.6+, Ubuntu 16.04/18.04,
CUDA 10.x, PyTorch 1.6, and torchvision 0.7. The included Darknet executable
and `libdarknet.zip` are prebuilt binaries with an unverified ABI and are not
approved for Jetson or production use.

The supplied TensorRT example targets TensorRT 7, references undefined names,
and uses obsolete builder APIs. It is reference material only. Export and
engine building must use the isolated process in [`models/README.md`](../../models/README.md).

## Checkpoint quarantine

The upstream `yolov5/best.pt` artifact has these audited properties:

- Path: `yolov5/best.pt`
- Size: `14,758,954` bytes
- SHA-256: `d1eae6859229ac1f5699c60f9445fa054dafc6a2cc59f00fc30ea6379dc3247e`
- Format: PyTorch ZIP/pickle containing a complete `models.yolo.Model` object,
  not a tensors-only state dictionary

The upstream loader calls `torch.load(...)["model"]`, which invokes Python
pickle deserialization. Never call `torch.load` on this artifact on developer
workstations, CI runners with secrets, or flight hardware. If evaluation is
authorized, retrieve and deserialize it only inside the disposable,
credential-free export sandbox described in [`models/README.md`](../../models/README.md).

Do not commit the checkpoint or any derived weight to this repository.

## License and data-rights status

The repository root contains an
[MIT license](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/LICENSE),
and its Darknet subtree contains a separate YOLO public-domain notice. The
YOLOv5 subtree does not carry a separate license even though it is derived from
Ultralytics YOLOv5; the corresponding 2020 upstream was
[GPL-3.0](https://github.com/ultralytics/yolov5/blob/ea7e78cb1159e6a17821772c85c4c23ccc823b16/LICENSE).
Treat the YOLOv5 code's redistribution and linking terms as **pending legal
review**, not as cleared by the repository-level MIT file.

The upstream dataset documentation says the data is for
[academic exploration and lists several third-party sources](https://github.com/gengyanlei/fire-smoke-detect-yolov4/blob/98b1fec0f82e09d67ef5fc657a80eaf0b1450360/readmes/README_ZN.md#L120-L130).
Dataset use and any rights associated with derived weights are **pending
provenance and data-rights review**.

Until both reviews are complete, permitted use is limited to offline technical
evaluation, interface prototyping, and comparison against independently
licensed replacement models. No artifact from this baseline is production
approved.

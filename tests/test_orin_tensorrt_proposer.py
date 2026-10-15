from __future__ import annotations

import json

import numpy as np

from scripts.orin_tensorrt_proposer import decode_yoloe_output, letterbox_rgb, unwrap_engine_bytes


def test_unwrap_engine_accepts_ultralytics_header(tmp_path) -> None:
    payload = b"engine-bytes"
    metadata = json.dumps({"args": {"dynamic": False}}).encode()
    path = tmp_path / "model.engine"
    path.write_bytes(len(metadata).to_bytes(4, "little") + metadata + payload)
    assert unwrap_engine_bytes(path) == payload


def test_letterbox_returns_rgb_chw_and_mapping() -> None:
    image = np.zeros((20, 40, 3), dtype=np.uint8)
    tensor, scale, pad = letterbox_rgb(image, 80)
    assert tensor.shape == (1, 3, 80, 80)
    assert tensor.dtype == np.float32
    assert scale == 2.0
    assert pad == (0.0, 20.0)


def test_decode_applies_class_agnostic_nms_and_inverse_letterbox() -> None:
    output = np.zeros((1, 24, 2), dtype=np.float32)
    output[0, :4, 0] = [40, 40, 20, 20]
    output[0, 4, 0] = 0.9
    output[0, :4, 1] = [40, 40, 18, 18]
    output[0, 5, 1] = 0.8
    boxes, scores, classes = decode_yoloe_output(
        output, source_shape=(20, 40), scale=2.0, pad=(0, 20), conf_threshold=0.05
    )
    assert boxes.shape == (1, 4)
    assert np.allclose(boxes[0], [15, 5, 25, 15])
    assert np.allclose(scores, [0.9])
    assert classes.tolist() == [0.0]

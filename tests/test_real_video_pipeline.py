import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "real_video"))

import camera_motion  # noqa: E402
import consensus  # noqa: E402
import domain_gap  # noqa: E402
import extract_frames  # noqa: E402
import fda_augment  # noqa: E402
import pseudo_label  # noqa: E402


def test_fuse_agree_and_single():
    fast = [{"cls": "car", "box": [0.1, 0.1, 0.3, 0.3], "score": 0.6},
            {"cls": "person", "box": [0.6, 0.6, 0.7, 0.8], "score": 0.5}]
    slow = [{"cls": "car", "box": [0.11, 0.1, 0.31, 0.3], "score": 0.8},
            {"cls": "car", "box": [0.8, 0.8, 0.9, 0.9], "score": 0.8}]
    agree, sf, ss = consensus.fuse(fast, slow)
    assert len(agree) == 1 and agree[0]["cls"] == "car" and agree[0]["score"] == 0.6
    assert [d["cls"] for d in sf] == ["person"] and len(ss) == 1


def test_fuse_class_mismatch_not_agree():
    a, sf, ss = consensus.fuse([{"cls": "car", "box": [0, 0, 1, 1], "score": 0.9}],
                               [{"cls": "person", "box": [0, 0, 1, 1], "score": 0.9}])
    assert not a and sf and ss


def test_parse_vlm_boxes_normalizes_and_filters():
    text = 'ok [{"label": "Car", "bbox_2d": [100, 200, 500, 600]}, {"label": "dog", "bbox_2d": [0,0,1,1]}]'
    out = pseudo_label.parse_vlm_boxes(text, {"car": "car"})
    assert len(out) == 1 and out[0]["box"] == [0.1, 0.2, 0.5, 0.6]
    assert pseudo_label.parse_vlm_boxes("nothing", {"car": "car"}) == []


def _frames(n=60, shift=0):
    rng = np.random.default_rng(0)
    base = (rng.random((240, 320, 3)) * 255).astype(np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 1.2)
    for i in range(n):
        m = np.float32([[1, 0, shift * i], [0, 1, 0]])
        yield i, i / 30.0, cv2.warpAffine(base, m, (320, 240), borderMode=cv2.BORDER_REFLECT)


def test_select_frames_drops_blur_and_dups():
    blur = lambda i, t, f: (i, t, cv2.GaussianBlur(f, (0, 0), 15))  # noqa: E731
    assert list(extract_frames.select_frames(blur(*x) for x in _frames())) == []
    kept = list(extract_frames.select_frames(_frames(), every_s=0.5, min_sharp=1.0))
    assert 1 <= len(kept) <= 4


def test_camera_motion_recovers_yaw_sign(tmp_path):
    p = tmp_path / "pan.mp4"
    w = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 240))
    for _, _, f in _frames(60, shift=3):
        w.write(f)
    w.release()
    s = camera_motion.clip_stats(p, hfov_deg=90, max_side=320)
    assert s["usable"] and s["yaw"]["p50"] > 20  # 约 3px/帧 @30fps，向右平移 -> 偏航率为正
    assert abs(s["pitch"]["p50"]) < 5


def test_fda_keeps_shape_and_shifts_color():
    src = np.full((64, 64, 3), 50, np.uint8)
    tgt = np.full((64, 64, 3), 200, np.uint8)
    out = fda_augment.fda(src, tgt, beta=0.1)
    assert out.shape == src.shape and out.mean() > src.mean() + 50


def test_domain_gap_ranks_far_sample_last():
    rng = np.random.default_rng(1)
    sim = [domain_gap.descriptor((rng.random((64, 64, 3)) * 80 + 100).astype(np.uint8)) for _ in range(20)]
    real = [domain_gap.descriptor((rng.random((64, 64, 3)) * 80 + 100).astype(np.uint8)),
            domain_gap.descriptor(np.zeros((64, 64, 3), np.uint8))]
    nn, _ = domain_gap.gap_scores(np.array(sim), np.array(real))
    assert nn[1] > nn[0]

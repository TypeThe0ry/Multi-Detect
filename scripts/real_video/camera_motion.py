#!/usr/bin/env python3
"""从视频估计相机/云台运动统计：俯仰率、偏航率、滚转率、前进/变焦尺度率、高频抖动。CPU。

同一个函数也可以对仿真录像跑，两边分布对比后，用来：
  1) 校准仿真里云台指令/扰动的幅度和频谱；
  2) 找出真实视频里仿真没覆盖到的运动范围（例如快速俯仰追车）。
假设针孔相机、水平视场角 --hfov（缺省 90°）；率值是近似量，用于分布比较，不是标定。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))


def frame_motion(prev_gray, gray, focal_px):
    """返回 (pitch_deg, yaw_deg, roll_deg, log_scale) 两帧间增量，失败返回 None。"""
    pts = cv2.goodFeaturesToTrack(prev_gray, 300, 0.01, 8)
    if pts is None or len(pts) < 20:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, pts, None)
    ok = st.flatten() == 1
    if ok.sum() < 20:
        return None
    m, inl = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if m is None or inl.sum() < 15:
        return None
    s = math.hypot(m[0, 0], m[1, 0])
    roll = math.degrees(math.atan2(m[1, 0], m[0, 0]))
    yaw = math.degrees(math.atan2(m[0, 2], focal_px))
    pitch = math.degrees(math.atan2(m[1, 2], focal_px))
    return pitch, yaw, roll, math.log(max(s, 1e-6))


def clip_stats(path: Path, hfov_deg=90.0, stride=1, max_frames=1800, max_side=480):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    dt = stride / fps
    prev, rows, i = None, [], 0
    while len(rows) < max_frames:
        ok, bgr = cap.read()
        if not ok:
            break
        i += 1
        if i % stride:
            continue
        h, w = bgr.shape[:2]
        k = max_side / max(h, w)
        gray = cv2.cvtColor(cv2.resize(bgr, (int(w * k), int(h * k))), cv2.COLOR_BGR2GRAY)
        focal = 0.5 * gray.shape[1] / math.tan(math.radians(hfov_deg) / 2)
        if prev is not None:
            r = frame_motion(prev, gray, focal)
            if r:
                rows.append(r)
        prev = gray
    cap.release()
    return summarize(np.array(rows, dtype=float), dt)


def summarize(rows: np.ndarray, dt: float) -> dict:
    if len(rows) < 10:
        return {"n": int(len(rows)), "usable": False}
    rate = rows[:, :3] / dt  # deg/s
    out = {"n": int(len(rows)), "usable": True, "dt": dt}
    for k, name in enumerate(("pitch", "yaw", "roll")):
        x = rate[:, k]
        # 高频抖动：一阶差分的标准差，近似机身振动/云台残差
        out[name] = {"p5": float(np.percentile(x, 5)), "p50": float(np.percentile(x, 50)),
                     "p95": float(np.percentile(x, 95)), "std": float(x.std()),
                     "jitter": float(np.diff(x).std())}
    out["scale_rate_per_s"] = {"p5": float(np.percentile(rows[:, 3] / dt, 5)),
                               "p95": float(np.percentile(rows[:, 3] / dt, 95))}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--hfov", type=float, default=90.0)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--out", help="写 JSON；缺省打印")
    args = ap.parse_args()
    res = {v: clip_stats(Path(v), args.hfov, args.stride) for v in args.videos}
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""仿真↔真实的域差测量与「补什么」排序。CPU 版用低成本图像统计描述子；有 GPU 时可换 DINOv2 特征。

描述子：LAB 均值/方差、亮度分位、径向傅里叶幅度谱（细节/锐度）、饱和度。
输出：每个真实片段到仿真分布的距离 -> 距离最小的是「仿真已覆盖」，最大的是「仿真缺口」
（例如逆光、雾霾、低照度、强压缩纹理），指导 rw_* 世界的天气/光照/材质补充。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import data_root, read_jsonl  # noqa: E402


def descriptor(bgr: np.ndarray, size: int = 256, bins: int = 12) -> np.ndarray:
    img = cv2.resize(bgr, (size, size), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    spec = np.abs(np.fft.fftshift(np.fft.fft2(gray - gray.mean())))
    yy, xx = np.indices(spec.shape)
    r = np.hypot(yy - size / 2, xx - size / 2)
    radial = [np.log1p(spec[(r >= a) & (r < b)].mean())
              for a, b in zip(np.linspace(1, size / 2, bins + 1)[:-1], np.linspace(1, size / 2, bins + 1)[1:])]
    pct = np.percentile(gray, [5, 25, 50, 75, 95])
    return np.concatenate([lab.reshape(-1, 3).mean(0), lab.reshape(-1, 3).std(0), pct,
                           [hsv[..., 1].mean(), hsv[..., 1].std()], radial]).astype(np.float32)


def gap_scores(sim: np.ndarray, real: np.ndarray):
    """sim:[N,D] real:[M,D]。用仿真分布标准化后，返回 (每个真实样本到仿真最近邻的距离, 整体 MMD 风格均值差)。"""
    mu, sd = sim.mean(0), sim.std(0) + 1e-6
    s, r = (sim - mu) / sd, (real - mu) / sd
    d = np.sqrt(((r[:, None, :] - s[None, :, :]) ** 2).sum(-1))
    nn = d.min(1)
    mean_shift = float(np.linalg.norm(r.mean(0) - s.mean(0)) / np.sqrt(s.shape[1]))
    return nn, mean_shift


def describe_dir(paths, limit):
    out = []
    for p in paths[:limit]:
        im = cv2.imread(str(p))
        if im is not None:
            out.append(descriptor(im))
    return np.array(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim-dir", required=True, help="仿真 rgb 帧目录（rw_* 采集）")
    ap.add_argument("--source", default="youtube_cc")
    ap.add_argument("--per-set", type=int, default=2000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sim_paths = sorted(Path(args.sim_dir).rglob("*.png")) + sorted(Path(args.sim_dir).rglob("*.jpg"))
    rows = read_jsonl(data_root() / args.source / "frames.jsonl")
    sim = describe_dir(sim_paths, args.per_set)
    real = describe_dir([data_root() / args.source / r["image"] for r in rows], args.per_set)
    nn, shift = gap_scores(sim, real)
    order = np.argsort(nn)
    res = {"sim_n": len(sim), "real_n": len(real), "mean_shift": shift,
           "closest": [rows[i]["image"] for i in order[:50]],
           "farthest_gaps": [rows[i]["image"] for i in order[-50:]]}
    text = json.dumps(res, indent=2, ensure_ascii=False)
    Path(args.out).write_text(text) if args.out else print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

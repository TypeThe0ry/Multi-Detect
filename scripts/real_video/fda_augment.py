"""傅里叶域适配（FDA, Yang & Soatto 2020）：把仿真帧的低频幅度谱换成真实帧的，保留仿真的内容与标注。

无需训练、纯 numpy、不占 GPU；标签位置不变。beta 越大风格越偏真实（建议 0.01~0.1）。
也提供 fda_batch：每张仿真帧随机配一张真实帧，写到新目录，可直接并入训练集。
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np


def _low_freq_swap(src_amp, tgt_amp, beta):
    a = np.fft.fftshift(src_amp, axes=(0, 1))
    b = np.fft.fftshift(tgt_amp, axes=(0, 1))
    h, w = a.shape[:2]
    bh, bw = max(int(h * beta), 1), max(int(w * beta), 1)
    ch, cw = h // 2, w // 2
    a[ch - bh:ch + bh, cw - bw:cw + bw] = b[ch - bh:ch + bh, cw - bw:cw + bw]
    return np.fft.ifftshift(a, axes=(0, 1))


def fda(src: np.ndarray, tgt: np.ndarray, beta: float = 0.05) -> np.ndarray:
    """src/tgt: HxWx3 uint8。tgt 会被缩放到 src 尺寸（最近邻，避免引入 cv2 依赖）。"""
    h, w = src.shape[:2]
    if tgt.shape[:2] != (h, w):
        yi = np.linspace(0, tgt.shape[0] - 1, h).astype(int)
        xi = np.linspace(0, tgt.shape[1] - 1, w).astype(int)
        tgt = tgt[yi][:, xi]
    fs = np.fft.fft2(src.astype(np.float32), axes=(0, 1))
    ft = np.fft.fft2(tgt.astype(np.float32), axes=(0, 1))
    amp = _low_freq_swap(np.abs(fs), np.abs(ft), beta)
    out = np.fft.ifft2(amp * np.exp(1j * np.angle(fs)), axes=(0, 1)).real
    return np.clip(out, 0, 255).astype(np.uint8)


def fda_batch(sim_dir: Path, real_dir: Path, out_dir: Path, beta=0.05, seed=0, limit=0):
    from PIL import Image

    rng = random.Random(seed)
    reals = sorted(p for p in real_dir.rglob("*.jpg"))
    sims = sorted(p for p in sim_dir.rglob("*.png")) + sorted(p for p in sim_dir.rglob("*.jpg"))
    n = 0
    for p in sims[: limit or None]:
        dst = out_dir / p.relative_to(sim_dir).with_suffix(".jpg")
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        s = np.asarray(Image.open(p).convert("RGB"))
        t = np.asarray(Image.open(rng.choice(reals)).convert("RGB"))
        Image.fromarray(fda(s, t, beta)).save(dst, quality=93)
        n += 1
    return n

#!/usr/bin/env python3
"""从视频抽帧并过滤：均匀 + 场景变化触发，丢弃模糊帧，感知哈希去重。CPU 即可。

输出 <out>/<clip>/frame_000123.jpg 与 <out>/frames.jsonl（clip、帧序、时间戳、清晰度、来源 tier/license）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import data_root, read_jsonl, write_jsonl  # noqa: E402


def sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def dhash(gray: np.ndarray, size: int = 8) -> int:
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def select_frames(frames, every_s=1.0, min_sharp=40.0, dup_hamming=4, scene_thresh=0.35):
    """frames: 迭代 (idx, t_sec, bgr)。返回 (idx, t, bgr, sharp)。纯函数，便于测试。"""
    last_t = -1e9
    last_hash = None
    last_hist = None
    for idx, t, bgr in frames:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [32], [0, 256]).flatten()
        hist /= hist.sum() + 1e-9
        scene_cut = last_hist is not None and 0.5 * np.abs(hist - last_hist).sum() > scene_thresh
        due = (t - last_t) >= every_s or scene_cut
        last_hist = hist
        if not due:
            continue
        s = sharpness(gray)
        if s < min_sharp:
            continue
        h = dhash(gray)
        if last_hash is not None and hamming(h, last_hash) <= dup_hamming and not scene_cut:
            continue
        last_t, last_hash = t, h
        yield idx, t, bgr, s


def read_video(path: Path, max_side: int):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    i = 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        h, w = bgr.shape[:2]
        if max(h, w) > max_side:
            k = max_side / max(h, w)
            bgr = cv2.resize(bgr, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        yield i, i / fps, bgr
        i += 1
    cap.release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="youtube_cc", help="data_root 下的子目录，需含 manifest.jsonl 与 videos/")
    ap.add_argument("--every-s", type=float, default=1.0)
    ap.add_argument("--max-side", type=int, default=1280)
    ap.add_argument("--min-sharp", type=float, default=40.0)
    args = ap.parse_args()

    root = data_root() / args.source
    meta = {r["id"]: r for r in read_jsonl(root / "manifest.jsonl")}
    done = {r["clip"] for r in read_jsonl(root / "frames.jsonl")}
    for vid, m in meta.items():
        if vid in done or not (data_root() / m["file"]).exists():
            continue
        rows = []
        for idx, t, bgr, s in select_frames(read_video(data_root() / m["file"], args.max_side),
                                            args.every_s, args.min_sharp):
            rel = f"frames/{vid}/frame_{idx:06d}.jpg"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(root / rel), bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            rows.append({"clip": vid, "frame": idx, "t": round(t, 3), "sharp": round(s, 1),
                         "image": rel, "tier": m.get("tier"), "license": m.get("license")})
        write_jsonl(root / "frames.jsonl", rows)
        print(vid, len(rows), "帧")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Procedural facade textures to true scale: one image spans 12.8 m (4 floors, 4 bays).

Wall materials are CC0 ambientCG photos; windows are drawn here so floors line up with FLOOR_M.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import os
from PIL import Image, ImageDraw, ImageFilter

LIB = Path("/data/datasets/multidetect-sim-assets/library")
OUT = Path(os.environ.get("RW_FACADE_OUT", LIB / "facades"))
PX = 1024
M = PX / 12.8


def wall(query: tuple[str, ...], tint=(1.0, 1.0, 1.0), fallback=(170, 165, 155), tile_m=2.5) -> np.ndarray:
    for q in query:
        for d in sorted((LIB / "ambientcg").glob(f"{q}*")):
            for f in d.glob("*_Color.jpg"):
                im = Image.open(f).convert("RGB").resize((int(tile_m * M), int(tile_m * M)), Image.LANCZOS)
                a = np.asarray(im, np.float32)
                reps = PX // a.shape[0] + 1
                a = np.tile(a, (reps, reps, 1))[:PX, :PX]
                return np.clip(a * np.array(tint), 0, 255)
    rng = np.random.default_rng(1)
    return np.clip(np.array(fallback, np.float32) + rng.normal(0, 5, (PX, PX, 3)), 0, 255)


def window_layer(arr: np.ndarray, bays: int, w_m: float, h_m: float, sill_m: float, frame=(60, 60, 62), seed=0, lit=0.0, blinds=0.3) -> np.ndarray:
    im = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(im)
    rng = np.random.default_rng(seed)
    floor_px = int(3.2 * M)
    bay_px = PX // bays
    for fl in range(4):
        for b in range(bays):
            cx = int((b + 0.5) * bay_px)
            y1 = PX - int((fl * 3.2 + sill_m) * M)
            y0 = y1 - int(h_m * M)
            x0, x1 = cx - int(w_m * M / 2), cx + int(w_m * M / 2)
            d.rectangle([x0 - 4, y0 - 4, x1 + 4, y1 + 4], fill=frame)
            top = np.array([95, 118, 140]) + rng.integers(-12, 12, 3)
            bot = np.array([40, 52, 66]) + rng.integers(-6, 6, 3)
            for y in range(y0, y1):
                t = (y - y0) / max(1, y1 - y0)
                d.line([x0, y, x1, y], fill=tuple((top * (1 - t) + bot * t).astype(int).tolist()))
            if rng.random() < blinds:
                d.rectangle([x0, y0, x1, y0 + int((y1 - y0) * rng.uniform(.2, .6))], fill=(190, 186, 176))
            d.line([cx, y0, cx, y1], fill=frame, width=3)
            d.rectangle([x0 - 6, y1 + 4, x1 + 6, y1 + 9], fill=(145, 142, 135))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(0.5)), np.float32)


def glass(base=(66, 88, 106), mullion=(40, 44, 48), seed=1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    a = np.zeros((PX, PX, 3), np.float32)
    panel_w, panel_h = PX // 8, int(3.2 * M)
    for i in range(8):
        for j in range(5):
            c = np.array(base, np.float32) + rng.normal(0, 1.5, 3) + rng.normal(0, 8)
            a[j * panel_h: (j + 1) * panel_h, i * panel_w: (i + 1) * panel_w] = c
    gy = np.linspace(1.12, 0.86, PX)[:, None, None]
    a *= gy
    for i in range(9):
        a[:, max(0, i * panel_w - 3): i * panel_w + 3] = mullion
    for j in range(5):
        y = PX - j * panel_h
        a[max(0, y - 10): y + 2] = mullion
    return a


def storefront(colors=((60, 62, 66), (205, 200, 190)), seed=0) -> np.ndarray:
    """Shop level spanning one wall image: 12.8 m wide and 4.2 m tall once mapped."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", (PX, PX // 2), colors[0])
    d = ImageDraw.Draw(im)
    h = PX // 2
    bays = 3
    w = PX // bays
    for i in range(bays):
        x0, x1 = i * w + 14, (i + 1) * w - 14
        d.rectangle([x0, int(h * 0.26), x1, int(h * 0.96)], fill=(82, 100, 112))
        for y in range(int(h * 0.26), int(h * 0.96)):
            t = (y - h * 0.26) / (h * 0.7)
            d.line([x0, y, x1, y], fill=tuple(int(c) for c in np.array([150, 172, 188]) * (1 - t) + np.array([48, 54, 60]) * t))
        d.line([(x0 + x1) // 2, int(h * 0.26), (x0 + x1) // 2, int(h * 0.96)], fill=colors[0], width=6)
        tone = int(rng.integers(35, 200))
        sign = (tone, tone - 6, tone - 14)
        d.rectangle([x0, int(h * 0.05), x1, int(h * 0.21)], fill=sign)
        if tone < 110:
            d.rectangle([x0 + 60, int(h * 0.11), x1 - 60, int(h * 0.15)], fill=(226, 224, 216))
    return np.asarray(im.resize((PX, PX)), np.float32)


def corrugated(color, seed=0) -> np.ndarray:
    x = np.arange(PX)
    wave = 0.9 + 0.1 * np.sin(x / PX * 2 * np.pi * 12.8 / 0.2)
    rng = np.random.default_rng(seed)
    a = np.array(color, np.float32)[None, None, :] * wave[None, :, None]
    a = np.tile(a, (PX, 1, 1))
    a += ndi_noise(rng, 12)
    return np.clip(a, 0, 255)


def ndi_noise(rng, amp) -> np.ndarray:
    n = rng.normal(0, 1, (PX // 16, PX // 16))
    im = Image.fromarray(((n + 3) / 6 * 255).clip(0, 255).astype(np.uint8)).resize((PX, PX), Image.BICUBIC)
    return ((np.asarray(im, np.float32) - 127) / 127 * amp)[..., None]


def lap_siding(color, seed=0) -> np.ndarray:
    a = np.tile(np.array(color, np.float32)[None, None, :], (PX, PX, 1))
    course = int(0.2 * M)
    for y in range(0, PX, course):
        a[y: y + 3] *= 0.72
    a += ndi_noise(np.random.default_rng(seed), 8)
    return np.clip(a, 0, 255)


def weathered(arr: np.ndarray, seed: int, contrast: float = 1.0) -> np.ndarray:
    """Add low-frequency staining and restrained exposure so noon facades retain detail."""
    rng = np.random.default_rng(seed)
    n = rng.normal(0, 1, (32, 32)).astype(np.float32)
    n = np.asarray(Image.fromarray(n, mode="F").resize((PX, PX), Image.BICUBIC))
    out = arr * (1.0 + n[..., None] * 0.025)
    out = (out - 128.0) * contrast + 128.0
    return np.clip(out, 0, 248)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items = {
        "glass_a": glass((118, 142, 158), (70, 76, 82), seed=1),
        "glass_b": glass((140, 152, 156), (96, 100, 104), seed=2),
        "storefront_a": storefront(seed=13),
        "storefront_b": storefront(((190, 178, 160), (70, 60, 52)), seed=14),
        "office_a": weathered(window_layer(wall(("Concrete034", "Concrete"), (0.88, 0.87, 0.84)), 6, 1.45, 2.0, 0.8, frame=(45, 46, 48), seed=3, blinds=0.5), 3, .92),
        "office_b": weathered(window_layer(wall(("Concrete034", "Concrete"), (0.72, 0.79, 0.82)), 5, 1.55, 2.0, 0.8, frame=(55, 58, 62), seed=31, blinds=0.35), 31, .9),
        "brick_mid": window_layer(wall(("Bricks0",), (0.95, 0.9, 0.86), (150, 92, 76)), 4, 1.3, 1.9, 0.9, frame=(235, 232, 225), seed=4),
        "brick_mid_b": weathered(window_layer(wall(("Bricks0",), (0.72, 0.62, 0.58), (125, 77, 66)), 5, 1.25, 1.85, 0.9, frame=(218, 216, 210), seed=34), 34, .95),
        "concrete_mid": window_layer(wall(("Concrete03", "Concrete"), (1.05, 1.03, 1.0)), 5, 1.5, 1.8, 0.9, frame=(80, 80, 82), seed=5),
        "stucco_mid": window_layer(wall(("Concrete04", "Concrete"), (1.1, 1.02, 0.9), (205, 190, 165)), 4, 1.2, 1.7, 0.9, frame=(245, 245, 240), seed=6),
        "stucco_mid_b": weathered(window_layer(wall(("Concrete04", "Concrete"), (0.78, 0.86, 0.82), (174, 193, 180)), 4, 1.25, 1.75, 0.9, frame=(238, 239, 233), seed=36), 36, .93),
        "siding_a": window_layer(lap_siding((206, 203, 190), 7), 3, 1.15, 1.35, 0.95, frame=(248, 248, 245), seed=7),
        "siding_b": window_layer(lap_siding((160, 172, 180), 8), 3, 1.15, 1.35, 0.95, frame=(248, 248, 245), seed=8),
        "siding_c": weathered(window_layer(lap_siding((188, 164, 143), 28), 3, 1.12, 1.35, 0.95, frame=(244, 242, 232), seed=28), 28, .94),
        "stucco_a": window_layer(wall(("Concrete04", "Concrete"), (1.15, 1.05, 0.88), (216, 200, 170)), 3, 1.2, 1.3, 0.95, frame=(240, 240, 235), seed=9),
        "brick_low": window_layer(wall(("Bricks0",), (0.92, 0.85, 0.82), (140, 88, 72)), 3, 1.1, 1.35, 0.95, frame=(240, 238, 232), seed=10),
        "brick_low_b": weathered(window_layer(wall(("Bricks0",), (0.68, 0.76, 0.72), (112, 116, 104)), 3, 1.1, 1.35, 0.95, frame=(232, 232, 225), seed=40), 40, .94),
        "metal_a": corrugated((168, 172, 174), 11),
        "metal_b": corrugated((120, 134, 146), 12),
        "metal_c": corrugated((158, 134, 105), 41),
    }
    for name, arr in items.items():
        # Rows in a JPEG run top down; UV v=0 is bottom of the wall, so the image stays upright.
        Image.fromarray(arr.clip(0, 255).astype(np.uint8)).save(OUT / f"{name}.jpg", quality=90)
    print("facades", len(items))


if __name__ == "__main__":
    main()

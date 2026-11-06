#!/usr/bin/env python3
"""Convert CC0 Poly Haven HDRIs into uncompressed DDS cubemaps for Gazebo's <sky><cubemap_uri>."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import cv2
import numpy as np
import requests

ASSET_ROOT = Path("/data/datasets/multidetect-sim-assets")
ROOT = Path(__file__).resolve().parents[2]
RESOLVED = ROOT / "artifacts/cache/gz-fuel/resolved"
FACE = 1024
# weather preset -> preferred category in the library manifest, plus an exposure tweak
# Only "puresky" HDRIs: no ground or scenery, so the horizon beyond the terrain stays believable.
PRESETS = {"noon": ("qwantani_noon_puresky", 1.0), "clear": ("syferfontein_18d_clear_puresky", 1.0), "cloudy": ("kloofendal_48d_partly_cloudy_puresky", 1.0),
           "sunrise": ("kloofendal_misty_morning_puresky", 1.0), "sunset": ("qwantani_sunset_puresky", 1.0), "overcast": ("kloofendal_overcast_puresky", 0.9),
           "dusk": ("qwantani_dusk_2_puresky", 0.8), "night": ("qwantani_night_puresky", 0.06)}


def face_dirs(face: int, n: int) -> np.ndarray:
    a = (np.arange(n) + 0.5) / n * 2 - 1
    u, v = np.meshgrid(a, a)
    one = np.ones_like(u)
    # Cube map face order +X -X +Y -Y +Z -Z, as Direct3D lays them out.
    d = [(one, -v, -u), (-one, -v, u), (u, one, v), (u, -one, -v), (u, -v, one), (-u, -v, -one)][face]
    x, y, z = d
    ln = np.sqrt(x * x + y * y + z * z)
    return np.stack([x / ln, y / ln, z / ln], axis=-1)


def equirect_to_cube(img: np.ndarray, gain: float) -> list[np.ndarray]:
    h, w = img.shape[:2]
    faces = []
    for f in range(6):
        d = face_dirs(f, FACE)
        lon = np.arctan2(d[..., 0], d[..., 2])
        lat = np.arcsin(np.clip(d[..., 1], -1, 1))
        mx = ((lon / (2 * np.pi) + 0.5) * w).astype(np.float32)
        my = ((0.5 - lat / np.pi) * h).astype(np.float32)
        s = cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        # Filmic curve into 8-bit, since Ogre2 here renders without an HDR pipeline.
        s = s * gain
        s = np.clip(s * (2.51 * s + 0.03) / (s * (2.43 * s + 0.59) + 0.14), 0, 1) ** (1 / 2.2)
        rgba = np.dstack([s, np.ones(s.shape[:2])])
        faces.append((rgba * 255).astype(np.uint8))
    return faces


def write_dds(path: Path, faces: list[np.ndarray]) -> None:
    h, w = faces[0].shape[:2]
    DDSD = 0x1 | 0x2 | 0x4 | 0x1000 | 0x8
    caps2 = 0x200 | 0xFC00
    pf = struct.pack("<II4sIIIII", 32, 0x41, b"\0\0\0\0", 32, 0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
    head = struct.pack("<4sIIIIIII", b"DDS ", 124, DDSD, h, w, w * 4, 0, 1) + b"\0" * 44 + pf + struct.pack("<IIIII", 0x1000 | 0x8, caps2, 0, 0, 0)
    with path.open("wb") as f:
        f.write(head)
        for face in faces:
            f.write(face.tobytes())


def fetch_hdri(slug: str) -> Path:
    dest = ASSET_ROOT / "library/polyhaven/hdri" / f"{slug}_4k.hdr"
    if not dest.exists():
        url = requests.get(f"https://api.polyhaven.com/files/{slug}", timeout=60).json()["hdri"]["4k"]["hdr"]["url"]
        data = requests.get(url, timeout=600).content
        dest.write_bytes(data)
        with (ASSET_ROOT / "library/manifest.jsonl").open("a") as f:
            f.write(json.dumps({"kind": "polyhaven_hdri", "slug": slug, "category": "sky_preset", "url": url, "license": "CC0-1.0", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}) + "\n")
    return dest


def main() -> None:
    out = RESOLVED / "rw_sky"
    out.mkdir(parents=True, exist_ok=True)
    (out / "model.config").write_text('<?xml version="1.0"?><model><name>rw_sky</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><description>CC0 Poly Haven sky cubemaps</description></model>\n')
    (out / "model.sdf").write_text('<?xml version="1.0"?><sdf version="1.9"><model name="rw_sky"><static>true</static><link name="l"/></model></sdf>\n')
    used = {}
    for preset, (slug, gain) in PRESETS.items():
        img = cv2.imread(str(fetch_hdri(slug)), cv2.IMREAD_UNCHANGED)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        write_dds(out / f"{preset}.dds", equirect_to_cube(img, gain))
        used[preset] = {"hdri": slug, "license": "CC0-1.0", "source": "https://polyhaven.com/a/" + slug}
        print(preset, slug, flush=True)
    (out / "provenance.json").write_text(json.dumps(used, indent=2))


if __name__ == "__main__":
    main()

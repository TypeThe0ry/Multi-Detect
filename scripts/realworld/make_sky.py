#!/usr/bin/env python3
"""Turn CC0 Poly Haven HDRIs into an unlit sky dome, one texture per weather preset.

Gazebo Harmonic ignores <sky><cubemap_uri> and always draws its built-in sky, so a real dome
mesh is the only way to change the sky per world.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import cv2
import numpy as np
import requests

ASSET_ROOT = Path("/data/datasets/multidetect-sim-assets")
ROOT = Path(__file__).resolve().parents[2]
RESOLVED = Path(os.environ.get("RW_RESOLVED_ROOT", os.environ.get("RW_MODEL_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved")))
DOME_RADIUS_M = 2600.0
# weather preset -> preferred category in the library manifest, plus an exposure tweak
# Only "puresky" HDRIs: no ground or scenery, so the horizon beyond the terrain stays believable.
PRESETS = {"noon": ("qwantani_noon_puresky", 1.0), "clear": ("syferfontein_18d_clear_puresky", 1.0), "cloudy": ("kloofendal_48d_partly_cloudy_puresky", 1.0),
           "sunrise": ("kloofendal_misty_morning_puresky", 1.0), "sunset": ("qwantani_sunset_puresky", 1.0), "overcast": ("kloofendal_overcast_puresky", 0.9),
           "dusk": ("qwantani_dusk_2_puresky", 0.22), "night": ("qwantani_night_puresky", 0.06)}


def write_dome(out: Path) -> None:
    """Inward-facing UV sphere. +Y north, +X east; u wraps with azimuth, v=1 at the zenith."""
    rows, cols = 48, 96
    az = np.linspace(0, 2 * np.pi, cols + 1)
    el = np.linspace(-np.pi / 2, np.pi / 2, rows + 1)
    aa, ee = np.meshgrid(az, el)
    x, y, z = np.cos(ee) * np.sin(aa), np.cos(ee) * np.cos(aa), np.sin(ee)
    v = np.column_stack([x.ravel(), y.ravel(), z.ravel()]) * DOME_RADIUS_M
    vt = np.column_stack([(aa / (2 * np.pi)).ravel(), ((ee + np.pi / 2) / np.pi).ravel()])
    vn = -v / DOME_RADIUS_M
    idx = np.arange((rows + 1) * (cols + 1)).reshape(rows + 1, cols + 1) + 1
    f = np.vstack([np.c_[idx[:-1, :-1].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()],
                   np.c_[idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel()]])
    with (out / "dome.obj").open("w") as fh:
        np.savetxt(fh, v, fmt="v %.1f %.1f %.1f")
        np.savetxt(fh, vt, fmt="vt %.5f %.5f")
        np.savetxt(fh, vn, fmt="vn %.4f %.4f %.4f")
        np.savetxt(fh, np.column_stack([f[:, 0], f[:, 0], f[:, 0], f[:, 1], f[:, 1], f[:, 1], f[:, 2], f[:, 2], f[:, 2]]), fmt="f %d/%d/%d %d/%d/%d %d/%d/%d")


def tonemap(img: np.ndarray, gain: float) -> np.ndarray:
    """ACES curve into 8-bit, with the whole sky scaled so haze near the horizon stays below white."""
    s = np.clip(img * gain, 0, 40)
    s = np.clip(s * (2.51 * s + 0.03) / (s * (2.43 * s + 0.59) + 0.14), 0, 1) ** (1 / 2.2)
    return (s * 255).astype(np.uint8)


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
    write_dome(out)
    for old in out.glob("*.dds"):
        old.unlink()
    used = {}
    for preset, (slug, gain) in PRESETS.items():
        img = cv2.imread(str(fetch_hdri(slug)), cv2.IMREAD_UNCHANGED)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        small = cv2.resize(img, (4096, 2048), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out / f"{preset}.jpg"), cv2.cvtColor(tonemap(small, gain), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
        used[preset] = {"hdri": slug, "license": "CC0-1.0", "source": "https://polyhaven.com/a/" + slug}
        print(preset, slug, flush=True)
    (out / "provenance.json").write_text(json.dumps(used, indent=2))


if __name__ == "__main__":
    main()

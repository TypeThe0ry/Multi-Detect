"""Find a dry, open, flat X1 launch pad near a site origin using its cached 6 km raw (OSM + DEM).

Usage: x1_find_pad.py <site_w6km> [--radius 1800] [--min-water 150]
Reads <asset_root>/<site>/v1/raw/<site>/raw/{osm.json,dem.npy,provenance.json}; prints JSON with ranked candidates.
The pad must also survive the full X1 route preflight after the site is re-fetched around the new origin.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point, Polygon

if __package__:
    from .build_scene import Osm
    from .x1_world_origin import water_distance, water_geometry
else:
    from build_scene import Osm
    from x1_world_origin import water_distance, water_geometry

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "configs/simulation/x1-worlds.json").read_text())


def building_polys(osm):
    out = []
    for way in osm.ways:
        t = way["tags"]
        if way["closed"] and len(way["pts"]) >= 4 and (t.get("building") or t.get("man_made") in {"storage_tank", "crane", "tower"}):
            out.append(Polygon(way["pts"]).buffer(0))
    for row in osm.rel_polys:
        if row["tags"].get("building"):
            out.append(row["poly"])
    return [p for p in out if not p.is_empty]


def candidates(raw, *, radius=1800, min_water=150, min_building=80, step=25, max_slope=5, max_relief=3):
    prov = json.loads((raw / "provenance.json").read_text())
    proj = Transformer.from_crs("EPSG:4326", prov["proj4"], always_xy=True)
    back = Transformer.from_crs(prov["proj4"], "EPSG:4326", always_xy=True)
    osm = Osm(raw / "osm.json", proj)
    water, coasts = water_geometry(osm)
    blds = building_polys(osm)
    tree = STRtree(blds) if blds else None
    dem = np.load(raw / "dem.npy", mmap_mode="r")
    if dem.ndim != 2 or dem.shape[0] != dem.shape[1] or dem.shape[0] < 3 or not np.any(dem):
        raise ValueError(f"{raw}: missing or all-zero DEM cannot establish pad quality")
    n = dem.shape[0]
    res = prov["size_m"] / (n - 1)
    half = prov["size_m"] / 2
    cands = []
    stride = max(1, min(4, int(12 / res)))
    xs = np.arange(-radius, radius + 1, step)
    for x in xs:
        for y in xs:
            if math.hypot(x, y) > radius:
                continue
            p = Point(x, y)
            wd = water_distance(p, water, coasts)
            if wd is not None and wd < min_water:
                continue
            nearby = tree.query(p.buffer(max(300, min_building))) if tree is not None else ()
            bd = float(min((p.distance(blds[i]) for i in nearby), default=999.0))
            if bd < min_building:
                continue
            j, i = int(round((x + half) / res)), int(round((y + half) / res))
            r = int(40 / res)
            if i - r < 0 or j - r < 0 or i + r >= n or j + r >= n:
                continue
            patch = np.asarray(dem[i - r:i + r + 1:stride, j - r:j + r + 1:stride], dtype=np.float32)
            if min(patch.shape) < 3 or not np.isfinite(patch).all() or patch.min() < -500:
                continue
            gy, gx = np.gradient(patch, res * stride)
            slope = np.hypot(gx, gy)
            relief = float(patch.max() - patch.min())
            p90 = float(np.percentile(slope, 90) * 100)
            if p90 > max_slope or relief > max_relief:
                continue
            lon, lat = back.transform(x, y)
            cands.append(dict(x=float(x), y=float(y), offset_m=round(float(math.hypot(x, y)), 1), lat=round(float(lat), 9), lon=round(float(lon), 9),
                              water_m=None if wd is None else round(float(wd), 1), building_m=round(float(bd), 1),
                              slope_p90_pct=round(p90, 2), relief_m=round(relief, 2)))
    return sorted(cands, key=lambda c: (c["offset_m"] - 0.3 * min(c["building_m"], 300) - 0.2 * min(c["water_m"] or 500, 500)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--radius", type=float, default=1800)
    ap.add_argument("--min-water", type=float, default=150)
    ap.add_argument("--min-building", type=float, default=80)
    ap.add_argument("--step", type=float, default=25)
    ap.add_argument("--max-slope", type=float, default=5.0)
    ap.add_argument("--max-relief", type=float, default=3.0)
    args = ap.parse_args()
    raw = Path(CONFIG["asset_root"]) / args.site / "v1/raw" / args.site / "raw"
    cands = candidates(raw, radius=args.radius, min_water=args.min_water, min_building=args.min_building,
                       step=args.step, max_slope=args.max_slope, max_relief=args.max_relief)
    print(json.dumps({"site": args.site, "n_candidates": len(cands), "top": cands[:8]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

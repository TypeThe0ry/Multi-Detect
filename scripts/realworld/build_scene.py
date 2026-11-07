#!/usr/bin/env python3
"""Turn one downloaded site into Gazebo terrain, buildings, roads, vegetation and street objects.

Output model `model://<site>` goes under artifacts/cache/gz-fuel/resolved, which every SITL
container already mounts. Placement of separate objects (trees, cars, signs) goes to
<asset_root>/sites/<site>/scene.json and make_world.py turns that into world files.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
from pathlib import Path

import mapbox_earcut as earcut
import numpy as np
import scipy.ndimage as ndi
import shapely
from PIL import Image, ImageDraw, ImageFilter
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import linemerge, polygonize, split, unary_union
from shapely.strtree import STRtree

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from instancing import Merger  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "configs/sim-assets/realworld-sites.json").read_text())
ASSET_ROOT = Path(CFG["asset_root"])
LIB = ASSET_ROOT / "library"
RESOLVED = Path(os.environ.get("RW_RESOLVED_ROOT", os.environ.get("RW_MODEL_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved")))
SOURCE_ROOT = Path(os.environ.get("RW_SOURCE_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved"))
DEM_RES = 2.0
TILE_M = 1000
PAD_R = 25.0
FLOOR_M = 3.2

ROAD_WIDTH = {"motorway": 15, "trunk": 13, "primary": 12, "secondary": 10.5, "tertiary": 9, "unclassified": 7,
              "residential": 8, "living_street": 6, "service": 4.5, "motorway_link": 7, "trunk_link": 7,
              "primary_link": 6.5, "secondary_link": 6, "tertiary_link": 6, "track": 3, "road": 6}
MARKED = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified"}


def cross2(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]


class Terrain:
    def __init__(self, site: dict, raw: Path) -> None:
        self.half = site["size_m"] / 2
        self.dem = np.load(raw / "dem.npy").astype(np.float64)
        self.dem[~np.isfinite(self.dem) | (self.dem < -500) | (self.dem > 1e6)] = 0.0
        self.elev0 = float(self.dem[self.dem.shape[0] // 2, self.dem.shape[1] // 2])
        self.h = self.dem - self.elev0
        n = self.h.shape[0]
        self.res = 2 * self.half / max(n - 1, 1)
        ax = np.linspace(-self.half, self.half, n)
        xx, yy = np.meshgrid(ax, ax)
        r = np.hypot(xx, yy)
        # Flatten the takeoff pad and blend it back over 25 m.
        w = np.clip((r - PAD_R) / 25.0, 0, 1)
        w = w * w * (3 - 2 * w)
        self.h = self.h * w

    def z(self, x, y):
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        scalar = x.ndim == 0
        c = np.atleast_1d(x + self.half) / self.res
        r = np.atleast_1d(y + self.half) / self.res
        out = ndi.map_coordinates(self.h, [r, c], order=1, mode="nearest")
        return float(out[0]) if scalar else out


def write_obj(path: Path, v: np.ndarray, vt: np.ndarray, vn: np.ndarray, f: np.ndarray) -> None:
    with path.open("w") as out:
        np.savetxt(out, v, fmt="v %.3f %.3f %.3f")
        np.savetxt(out, vt, fmt="vt %.5f %.5f")
        np.savetxt(out, vn, fmt="vn %.3f %.3f %.3f")
        ff = f + 1
        np.savetxt(out, np.column_stack([ff[:, 0], ff[:, 0], ff[:, 0], ff[:, 1], ff[:, 1], ff[:, 1], ff[:, 2], ff[:, 2], ff[:, 2]]),
                   fmt="f %d/%d/%d %d/%d/%d %d/%d/%d")


def face_normals_to_vertex(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    n = np.zeros_like(v)
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    for i in range(3):
        np.add.at(n, f[:, i], fn)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)


class MeshBin:
    """Accumulates triangles with UVs, one bin per material."""

    def __init__(self) -> None:
        self.v: list[np.ndarray] = []
        self.vt: list[np.ndarray] = []
        self.f: list[np.ndarray] = []
        self.count = 0

    def add(self, v: np.ndarray, vt: np.ndarray, f: np.ndarray) -> None:
        self.v.append(v)
        self.vt.append(vt)
        self.f.append(f + self.count)
        self.count += len(v)

    def write(self, path: Path) -> int:
        if not self.count:
            return 0
        v, vt, f = np.vstack(self.v), np.vstack(self.vt), np.vstack(self.f)
        write_obj(path, v, vt, face_normals_to_vertex(v, f), f)
        return len(f)


# ------------------------------------------------------------------ OSM


class Osm:
    def __init__(self, path: Path, proj: Transformer) -> None:
        data = json.loads(path.read_text())
        self.nodes: dict[int, tuple[float, float]] = {}
        self.node_tags: list[dict] = []
        ways: dict[int, dict] = {}
        rels = []
        for e in data["elements"]:
            if e["type"] == "node":
                x, y = proj.transform(e["lon"], e["lat"])
                self.nodes[e["id"]] = (x, y)
                if e.get("tags"):
                    self.node_tags.append({"x": x, "y": y, "tags": e["tags"], "id": e["id"]})
            elif e["type"] == "way":
                ways[e["id"]] = e
            else:
                rels.append(e)
        self.ways = []
        for w in ways.values():
            pts = [self.nodes[n] for n in w.get("nodes", []) if n in self.nodes]
            if len(pts) >= 2 and w.get("tags"):
                self.ways.append({"id": w["id"], "pts": pts, "tags": w["tags"], "closed": w["nodes"][0] == w["nodes"][-1],
                                  "node_ids": w["nodes"]})
        self.rel_polys = []
        for r in rels:
            tags = r.get("tags") or {}
            outers, inners = [], []
            for m in r.get("members", []):
                if m["type"] != "way" or m["ref"] not in ways:
                    continue
                pts = [self.nodes[n] for n in ways[m["ref"]].get("nodes", []) if n in self.nodes]
                if len(pts) >= 2:
                    (inners if m.get("role") == "inner" else outers).append(LineString(pts))
            polys = list(polygonize(unary_union(outers))) if outers else []
            holes = list(polygonize(unary_union(inners))) if inners else []
            for p in polys:
                for h in holes:
                    if p.contains(h.representative_point()):
                        p = p.difference(h)
                self.rel_polys.append({"id": r["id"], "poly": p, "tags": tags})


def apply_3dbag_heights(osm: Osm, raw: Path) -> int:
    path = raw / "3dbag.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text())
    heights = {}
    for feature in data.get("features", []):
        for object_id, obj in feature.get("CityObjects", {}).items():
            attrs = obj.get("attributes", {})
            roof, ground = attrs.get("b3_h_nok"), attrs.get("b3_h_maaiveld")
            if roof is None or ground is None:
                continue
            height = max(1.0, float(roof) - float(ground))
            ident = str(attrs.get("identificatie") or object_id).split(".")[-1]
            heights[ident] = height
            heights["".join(c for c in ident if c.isdigit())] = height

    added = 0
    for building in [*osm.ways, *osm.rel_polys]:
        tags = building["tags"]
        if tags.get("height") or tags.get("building:height"):
            continue
        ref = str(tags.get("ref:bag", ""))
        key = ref.split(".")[-1]
        height = heights.get(key) or heights.get("".join(c for c in key if c.isdigit()))
        if height:
            tags["height"] = f"{height:.2f}"
            added += 1
    return added


def parse_len(value: str | None) -> float | None:
    if not value:
        return None
    s = str(value).strip().lower().replace(",", ".")
    try:
        if s.endswith("ft") or "'" in s:
            return float(s.replace("ft", "").split("'")[0].strip()) * 0.3048
        return float(s.replace("m", "").strip())
    except ValueError:
        return None


# ------------------------------------------------------------------ Buildings


def building_height(tags: dict, kind: str, rng: random.Random, area: float) -> tuple[float, float]:
    h = parse_len(tags.get("height") or tags.get("building:height"))
    lv = parse_len(tags.get("building:levels"))
    if h is None and lv is not None:
        h = lv * FLOOR_M + 1.0
    if h is None:
        b = tags.get("building", tags.get("building:part", "yes"))
        if b in {"house", "detached", "residential", "semidetached_house", "bungalow"}:
            h = rng.uniform(5.5, 8.5)
        elif b in {"garage", "garages", "shed", "carport", "roof"}:
            h = 3.0
        elif b in {"warehouse", "industrial", "hangar"}:
            h = rng.uniform(8, 14)
        elif b in {"silo", "storage_tank"}:
            h = rng.uniform(10, 20)
        elif kind == "urban":
            h = rng.uniform(12, 30)
        elif area < 350:
            h = rng.uniform(5, 8)
        else:
            h = rng.uniform(7, 12)
    mn = parse_len(tags.get("min_height") or tags.get("building:min_height"))
    if mn is None:
        ml = parse_len(tags.get("building:min_level"))
        mn = ml * FLOOR_M if ml else 0.0
    return float(h), float(mn)


def facade_for(tags: dict, h: float, rng: random.Random) -> str:
    b = tags.get("building", tags.get("building:part", "yes"))
    if b in {"warehouse", "industrial", "hangar", "shed", "garage", "garages", "barn", "farm_auxiliary", "silo", "storage_tank"}:
        return rng.choice(["metal_a", "metal_b"])
    if b in {"house", "detached", "residential", "semidetached_house", "bungalow", "cabin", "terrace"} or h < 10:
        return rng.choice(["siding_a", "siding_b", "stucco_a", "brick_low"])
    if h > 28:
        return rng.choice(["glass_a", "glass_b", "office_a"])
    return rng.choice(["office_a", "brick_mid", "concrete_mid", "stucco_mid"])


GABLED = {"house", "detached", "semidetached_house", "bungalow", "cabin", "residential", "terrace", "barn", "farm", "church", "garage", "yes"}


def gable_ridge(poly: Polygon, tags: dict, h: float):
    """A ridge for small, near-rectangular pitched-roof buildings; None means a flat roof."""
    b = tags.get("building", "yes")
    shape = tags.get("roof:shape")
    if shape and shape != "gabled" and shape != "hipped":
        return None
    if b not in GABLED or poly.area > 450 or poly.area < 25 or h > 13 or poly.interiors:
        return None
    if b == "yes" and not shape:
        return None
    rect = poly.minimum_rotated_rectangle
    if poly.area / max(rect.area, 1) < 0.75:
        return None
    c = np.array(rect.exterior.coords)
    e0, e1 = c[1] - c[0], c[2] - c[1]
    long_v, short = (e0, np.linalg.norm(e1)) if np.linalg.norm(e0) > np.linalg.norm(e1) else (e1, np.linalg.norm(e0))
    u = long_v / np.linalg.norm(long_v)
    center = np.array(rect.centroid.coords[0])
    return center, u, short / 2


def triangulate(poly: Polygon) -> tuple[np.ndarray, np.ndarray]:
    rings = [np.array(poly.exterior.coords)[:-1]] + [np.array(r.coords)[:-1] for r in poly.interiors]
    verts = np.vstack(rings)
    ends = np.cumsum([len(r) for r in rings], dtype=np.uint32)
    idx = earcut.triangulate_float64(verts, ends).reshape(-1, 3)
    return verts, idx


def densify_ring_at(ring: LineString, cutter: LineString) -> np.ndarray:
    """Insert ring/ridge crossings so the wall top can follow the gable."""
    pts = list(ring.coords)
    hits = ring.intersection(cutter)
    hit_pts = [hits] if hits.geom_type == "Point" else [g for g in getattr(hits, "geoms", []) if g.geom_type == "Point"]
    out = []
    for a, b in zip(pts, pts[1:]):
        seg = LineString([a, b])
        out.append(a)
        on = sorted((seg.project(p), p.coords[0]) for p in hit_pts if seg.distance(p) < 1e-6)
        for s, q in on:
            if 1e-3 < s < seg.length - 1e-3:
                out.append(q)
    return np.array(out)


def add_building(bins, roof_bin, coll, b, terrain, half: float) -> None:
    poly, tags, h, mn, facade = b["poly"], b["tags"], b["h"], b["min_h"], b["facade"]
    ext = np.array(poly.exterior.coords)
    base = float(terrain.z(ext[:, 0], ext[:, 1]).min()) - 0.3
    bottom = base + mn
    eave = base + h
    rid = gable_ridge(poly, tags, h) if mn == 0 else None
    pitch = 0.45
    top_fn = lambda xy: np.full(len(xy), eave)  # noqa: E731
    roof_polys = [poly]
    rings = [ext[:-1]] + [np.array(r.coords)[:-1] for r in poly.interiors]
    if rid is not None:
        c, u, _ = rid
        n = np.array([-u[1], u[0]])
        wall_top = max(eave - 1.4, base + 2.6)
        top_fn = lambda xy: wall_top + pitch * (2.0 - 0 * xy[:, 0]) * 0 + pitch * np.maximum(0.0, rid[2] - np.abs((xy - c) @ n))  # noqa: E731
        cutter = LineString([c - u * 200, c + u * 200])
        try:
            roof_polys = [g for g in split(poly, cutter).geoms if g.area > 0.1]
            rings = [densify_ring_at(LineString(ext), cutter)]
        except (ValueError, shapely.errors.GEOSException):
            top_fn = lambda xy: np.full(len(xy), eave)  # noqa: E731
    wall = bins[facade]
    front = bins.get(b.get("front")) if b.get("front") else None
    for ring in rings:
        nxt = np.roll(ring, -1, axis=0)
        seg = np.linalg.norm(nxt - ring, axis=1)
        s0 = 0.0
        top0, top1 = top_fn(ring), top_fn(nxt)
        tex = 12.8
        for i in range(len(ring)):
            a, b_, L = ring[i], nxt[i], seg[i]
            if L < 1e-3:
                continue
            ss = s0 / tex
            ee = (s0 + L) / tex
            f = np.array([[0, 1, 2], [0, 2, 3]])
            low = bottom
            if front is not None:
                # A 4.2 m shop level, with its own texture across a whole bay image.
                low = base + 4.2
                vv = np.array([[a[0], a[1], base], [b_[0], b_[1], base], [b_[0], b_[1], low], [a[0], a[1], low]])
                vt = np.array([[ss, 0.0], [ee, 0.0], [ee, 1.0], [ss, 1.0]])
                front.add(vv, vt, f)
                front.add(vv.copy(), vt.copy(), f[:, ::-1])
            vv = np.array([[a[0], a[1], low], [b_[0], b_[1], low], [b_[0], b_[1], top1[i]], [a[0], a[1], top0[i]]])
            vt = np.array([[ss, (low - base) / tex], [ee, (low - base) / tex], [ee, (top1[i] - base) / tex], [ss, (top0[i] - base) / tex]])
            # Footprints may wind either way, so both faces get written.
            wall.add(vv, vt, f)
            wall.add(vv.copy(), vt.copy(), f[:, ::-1])
            s0 += L
    for rp in roof_polys:
        try:
            parts = [rp] if rp.geom_type == "Polygon" else list(rp.geoms)
            for p in parts:
                verts, idx = triangulate(p)
                z = top_fn(verts)
                vt = (verts + half) / (2 * half)
                vv = np.column_stack([verts, z])
                # Force upward faces.
                ok = cross2(verts[idx[:, 1]] - verts[idx[:, 0]], verts[idx[:, 2]] - verts[idx[:, 0]]) < 0
                idx = idx.copy()
                idx[ok] = idx[ok][:, ::-1]
                roof_bin.add(vv, vt, idx.astype(np.int64))
        except (ValueError, IndexError):
            continue
    # Collision prism uses the same footprint with a flat top at the eave.
    try:
        verts, idx = triangulate(poly)
    except ValueError:
        return
    n = len(verts)
    v = np.vstack([np.column_stack([verts, np.full(n, bottom)]), np.column_stack([verts, np.full(n, eave)])])
    walls = []
    off = 0
    for r in [poly.exterior, *poly.interiors]:
        m = len(r.coords) - 1
        for i in range(m):
            j = (i + 1) % m
            walls += [[off + i, off + j, n + off + j], [off + i, n + off + j, n + off + i]]
        off += m
    coll.add(v, np.zeros((len(v), 2)), np.vstack([idx + n, np.array(walls)]).astype(np.int64))


# ------------------------------------------------------------------ Roads


def drape(polys, terrain, offset: float, tex_m: float, out: MeshBin, cell: float = 14.0) -> None:
    """Triangulate polygons, split long edges, and place them on the (smoothed) ground."""
    merged = unary_union(polys)
    if merged.is_empty:
        return
    minx, miny, maxx, maxy = merged.bounds
    grid = []
    cs = 64.0
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            grid.append(box(x, y, x + cs, y + cs))
            y += cs
        x += cs
    tree = STRtree(grid)
    parts = merged.geoms if isinstance(merged, MultiPolygon) else [merged]
    for part in parts:
        for gi in tree.query(part):
            clip = part.intersection(grid[gi])
            if clip.is_empty:
                continue
            for p in clip.geoms if hasattr(clip, "geoms") else [clip]:
                if p.geom_type != "Polygon" or p.area < 0.02:
                    continue
                try:
                    verts, idx = triangulate(p)
                except ValueError:
                    continue
                vv, ff = subdivide(verts, idx.astype(np.int64), cell)
                z = terrain.z(vv[:, 0], vv[:, 1]) + offset
                v3 = np.column_stack([vv, z])
                flip = cross2(vv[ff[:, 1]] - vv[ff[:, 0]], vv[ff[:, 2]] - vv[ff[:, 0]]) < 0
                ff[flip] = ff[flip][:, ::-1]
                out.add(v3, vv / tex_m, ff)


def subdivide(v: np.ndarray, f: np.ndarray, max_len: float) -> tuple[np.ndarray, np.ndarray]:
    for _ in range(8):
        e = np.stack([np.linalg.norm(v[f[:, a]] - v[f[:, b]], axis=1) for a, b in ((0, 1), (1, 2), (2, 0))], axis=1)
        long_tri = e.max(axis=1) > max_len
        if not long_tri.any():
            break
        keep, work = f[~long_tri], f[long_tri]
        a, b, c = work[:, 0], work[:, 1], work[:, 2]
        n = len(v)
        ab, bc, ca = (v[a] + v[b]) / 2, (v[b] + v[c]) / 2, (v[c] + v[a]) / 2
        m = len(work)
        v = np.vstack([v, ab, bc, ca])
        iab, ibc, ica = np.arange(n, n + m), np.arange(n + m, n + 2 * m), np.arange(n + 2 * m, n + 3 * m)
        f = np.vstack([keep, np.c_[a, iab, ica], np.c_[iab, b, ibc], np.c_[ica, ibc, c], np.c_[iab, ibc, ica]])
    return v, f


def dashes(line: LineString, on: float, off: float, width: float) -> list[Polygon]:
    out = []
    s = 2.0
    while s < line.length - 1:
        seg = shapely.ops.substring(line, s, min(s + on, line.length))
        if seg.length > 0.2:
            out.append(seg.buffer(width / 2, cap_style="flat"))
        s += on + off
    return out


# ------------------------------------------------------------------ Vegetation from NDVI


def vegetation_index(rgbn: np.ndarray) -> tuple[np.ndarray, str]:
    r, g, b = (rgbn[..., i].astype(np.float32) for i in range(3))
    fourth = rgbn[..., 3]
    if fourth.size and np.all(fourth == 255):
        # RGB-only orthophotos use VARI; ExG provides a stable fallback when its denominator is small.
        vari = (g - r) / np.maximum(g + r - b, 1.0)
        exg = (2 * g - r - b) / 255.0
        return np.where(np.abs(g + r - b) > 8, vari, exg), "vari"
    nir = fourth.astype(np.float32)
    return (nir - r) / np.maximum(nir + r, 1.0), "ndvi"


def find_trees(mosaic_rgbn: np.ndarray, half: float, rng: np.random.Generator, blocked, max_trees: int) -> list[dict]:
    """Tree crowns from NDVI or a visible-light vegetation index, plus darkness and texture."""
    px = 2 * half / mosaic_rgbn.shape[0]
    index, index_name = vegetation_index(mosaic_rgbn)
    red, green, blue = [mosaic_rgbn[..., i].astype(np.float32) for i in range(3)]
    lum = (red + green + blue) / 3
    win = max(3, int(4.0 / px))
    texture = mosaic_rgbn[..., 3].astype(np.float32) if index_name == "ndvi" else lum
    mean = ndi.uniform_filter(texture, win)
    var = ndi.uniform_filter(texture * texture, win) - mean * mean
    rough = np.sqrt(np.maximum(var, 0))
    mask = (index > (0.12 if index_name == "ndvi" else 0.06)) & (lum < np.percentile(lum, 70)) & (rough > 9)
    mask = ndi.binary_opening(mask, iterations=max(1, int(1.0 / px)))
    if blocked is not None:
        mask &= ~blocked
    dist = ndi.distance_transform_edt(mask) * px
    size = max(3, int(5.0 / px))
    peaks = (ndi.maximum_filter(dist, size=size) == dist) & (dist > 1.2)
    rows, cols = np.nonzero(peaks)
    trees = []
    order = rng.permutation(len(rows))
    for k in order:
        i, j = rows[k], cols[k]
        x, y = -half + (j + 0.5) * px, -half + (i + 0.5) * px
        trees.append({"x": float(x), "y": float(y), "r": float(dist[i, j]), "lum": float(lum[i, j]), index_name: float(index[i, j])})
    # Keep the closest trees first when the budget is short.
    trees.sort(key=lambda t: math.hypot(t["x"], t["y"]) * (0.7 + 0.6 * rng.random()))
    return trees[:max_trees]


# ------------------------------------------------------------------ Main


def rasterize(polys, half: float, res: float) -> np.ndarray:
    n = int(2 * half / res)
    img = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(img)
    for p in polys:
        for g in (p.geoms if hasattr(p, "geoms") else [p]):
            if g.geom_type != "Polygon":
                continue
            d.polygon([((x + half) / res, (y + half) / res) for x, y in g.exterior.coords], fill=255)
    return np.asarray(img) > 0


def pbr_visual(name: str, mesh_uri: str, albedo: str, rough: float = 0.9, metal: float = 0.0, double: bool = False) -> str:
    extra = "<double_sided>true</double_sided>" if double else ""
    return (f'<visual name="{name}"><cast_shadows>{"false" if "ground" in name or "road" in name or "mark" in name else "true"}</cast_shadows><geometry><mesh><uri>{mesh_uri}</uri></mesh></geometry>'
            f"<material><diffuse>1 1 1 1</diffuse><specular>0.05 0.05 0.05 1</specular>{extra}<pbr><metal><albedo_map>{albedo}</albedo_map>"
            f"<roughness>{rough}</roughness><metalness>{metal}</metalness></metal></pbr></material></visual>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--max-trees", type=int, default=2500)
    ap.add_argument("--grid", type=float, default=4.0)
    a = ap.parse_args()
    name = a.site
    site = CFG["sites"][name]
    kind = site["kind"]
    raw = ASSET_ROOT / "sites" / name / "raw"
    half = site["size_m"] / 2
    rng_py = random.Random(hash(name) & 0xffff if False else sum(map(ord, name)))
    rng = np.random.default_rng(sum(map(ord, name)))
    proj = Transformer.from_crs("EPSG:4326", f"+proj=tmerc +lat_0={site['lat']} +lon_0={site['lon']} +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m", always_xy=True)
    terrain = Terrain(site, raw)
    osm = Osm(raw / "osm.json", proj)
    bag_heights = apply_3dbag_heights(osm, raw)
    area = box(-half + 1, -half + 1, half - 1, half - 1)
    out = RESOLVED / name
    shutil.rmtree(out, ignore_errors=True)
    (out / "meshes").mkdir(parents=True)
    (out / "textures").mkdir()
    print(name, "elev0", terrain.elev0, "ways", len(osm.ways), "3dbag heights", bag_heights, flush=True)

    # ---- Buildings
    buildings = []
    all_polys = []
    for w in osm.ways:
        t = w["tags"]
        if ("building" in t and t.get("building") != "no") or "building:part" in t:
            if w["closed"] and len(w["pts"]) >= 4:
                p = Polygon(w["pts"])
                if not p.is_valid:
                    p = p.buffer(0)
                all_polys.append((p, t, w["id"]))
    for r in osm.rel_polys:
        if "building" in r["tags"]:
            all_polys.append((r["poly"] if r["poly"].is_valid else r["poly"].buffer(0), r["tags"], r["id"]))
    parts = [p for p, t, _ in all_polys if "building:part" in t and p.geom_type == "Polygon"]
    part_tree = STRtree(parts) if parts else None
    for p, t, oid in all_polys:
        if p.is_empty or p.area < 8 or not area.contains(p.representative_point()):
            continue
        if "building:part" not in t and part_tree is not None:
            hits = part_tree.query(p, predicate="intersects")
            if sum(parts[i].intersection(p).area for i in hits) > 0.5 * p.area:
                continue
        for g in (p.geoms if hasattr(p, "geoms") else [p]):
            if g.geom_type != "Polygon" or g.area < 8 or g.distance(Point(0, 0)) < PAD_R:
                continue
            g = g.intersection(area)
            if g.geom_type != "Polygon" or g.is_empty:
                continue
            g = g.simplify(0.3)
            rr = random.Random(oid)
            h, mn = building_height(t, kind, rr, g.area)
            fac = facade_for(t, h, rr)
            front = rr.choice(["storefront_a", "storefront_b"]) if h > 9 and mn == 0 and kind in {"urban", "coast"} and not fac.startswith("metal") else None
            buildings.append({"poly": g, "tags": t, "h": h, "min_h": mn, "facade": fac, "front": front, "id": oid})
    print("buildings", len(buildings), "max h", max([b["h"] for b in buildings], default=0), flush=True)

    # ---- Roads
    # Footpaths stay as the aerial shows them; ribbons there read as cartoon strips.
    road_polys: dict[str, list] = {"major": [], "minor": [], "track": [], "rail": []}
    marks_white, marks_yellow, road_lines = [], [], []
    for w in osm.ways:
        t = w["tags"]
        hw = t.get("highway")
        ls = LineString(w["pts"]).intersection(area)
        if ls.is_empty or ls.geom_type != "LineString":
            continue
        if t.get("tunnel") in {"yes", "building_passage"} or t.get("covered") == "yes":
            continue
        if hw in ROAD_WIDTH:
            width = parse_len(t.get("width")) or ROAD_WIDTH[hw]
            lanes = parse_len(t.get("lanes"))
            if lanes and not t.get("width"):
                width = max(width, lanes * 3.4)
            cat = "track" if hw == "track" or t.get("surface") in {"gravel", "dirt", "unpaved", "ground"} else ("major" if hw in {"motorway", "trunk", "primary", "secondary", "motorway_link", "trunk_link", "primary_link"} else "minor")
            road_polys[cat].append(ls.buffer(width / 2, cap_style="flat", join_style="round", quad_segs=3))
            road_lines.append({"line": ls, "class": hw, "width": width, "oneway": t.get("oneway") == "yes", "name": t.get("name", "")})
            if hw in MARKED and ls.length > 25 and t.get("surface") not in {"gravel", "dirt", "unpaved"}:
                if t.get("oneway") == "yes" or (lanes or 2) > 2:
                    marks_white += dashes(ls, 3.0, 6.0, 0.14)
                else:
                    marks_yellow.append(ls.buffer(0.08, cap_style="flat"))
                if width > 6.5:
                    for side in (1, -1):
                        try:
                            off = ls.parallel_offset(width / 2 - 0.35, "left" if side > 0 else "right")
                        except (ValueError, shapely.errors.GEOSException):
                            continue
                        if not off.is_empty:
                            marks_white.append(off.buffer(0.07, cap_style="flat"))
        elif "railway" in t and t["railway"] in {"rail", "light_rail", "tram"}:
            road_polys["rail"].append(ls.buffer(1.8, cap_style="flat", quad_segs=2))
    for r in osm.ways:
        t = r["tags"]
        if t.get("aeroway") in {"runway", "apron", "taxiway"} and r["closed"]:
            p = Polygon(r["pts"]).buffer(0)
            road_polys["major"].append(p.intersection(area))
    # Crosswalks at OSM crossing nodes: stripes across the road direction.
    if road_lines:
        tree = STRtree([r["line"] for r in road_lines])
        for nt in osm.node_tags:
            if nt["tags"].get("highway") != "crossing":
                continue
            pt = Point(nt["x"], nt["y"])
            if not area.contains(pt):
                continue
            j = tree.nearest(pt)
            rl = road_lines[j]
            line = rl["line"]
            if line.distance(pt) > 3:
                continue
            s = line.project(pt)
            p0, p1 = line.interpolate(max(0, s - 1)), line.interpolate(min(line.length, s + 1))
            d = np.array([p1.x - p0.x, p1.y - p0.y])
            if np.linalg.norm(d) < 0.1:
                continue
            d /= np.linalg.norm(d)
            nrm = np.array([-d[1], d[0]])
            c = np.array([pt.x, pt.y])
            for k in np.arange(-rl["width"] / 2 + 0.5, rl["width"] / 2 - 0.2, 1.2):
                q = c + nrm * k
                marks_white.append(LineString([q - d * 1.5, q + d * 1.5]).buffer(0.25, cap_style="flat"))
    print({k: len(v) for k, v in road_polys.items()}, "marks", len(marks_white), len(marks_yellow), flush=True)

    # Terrain visual: settle the ground slightly under roads so ribbons never z-fight.
    all_road = [p for k in road_polys for p in road_polys[k]]
    road_mask = rasterize(all_road, half, terrain.res)
    n = terrain.h.shape[0]
    rm = np.zeros_like(terrain.h)
    rm[: road_mask.shape[0], : road_mask.shape[1]] = road_mask
    smooth = ndi.gaussian_filter(terrain.h, 3.0)
    wgt = ndi.gaussian_filter(rm, 1.0)
    terrain_road = Terrain.__new__(Terrain)
    terrain_road.half, terrain_road.h, terrain_road.res = half, terrain.h * (1 - wgt) + smooth * wgt, terrain.res
    vis = terrain_road.h - 0.18 * np.clip(wgt * 2, 0, 1)
    terrain_vis = Terrain.__new__(Terrain)
    terrain_vis.half, terrain_vis.h, terrain_vis.res = half, vis, terrain.res

    # ---- Ground tiles and mosaic
    nt = int(round(2 * half / TILE_M))
    tiles = {}
    vis_xml, coll_xml = [], []
    # One stretch per site: a per-tile stretch shows up as patches across tile seams.
    sample = np.vstack([np.asarray(Image.open(f).convert("RGB").resize((256, 256))).reshape(-1, 3) for f in sorted(raw.glob("naip_*.png"))]).astype(np.float32)
    stretch = (float(np.percentile(sample, 0.5)), float(np.percentile(sample, 99.7)))
    tile_px = 2048  # per-tile share of the roof mosaic; ground tiles keep full resolution
    mosaic = np.zeros((tile_px * nt, tile_px * nt, 4), np.uint8)
    for j in range(nt):
        for i in range(nt):
            im = Image.open(raw / f"naip_{i}_{j}.png")
            arr = np.asarray(im)
            tiles[(i, j)] = arr
            rgb = arr[..., :3].astype(np.float32)
            # NAIP reads flat and slightly blue; stretch to a daylight look.
            rgb = np.clip((rgb - stretch[0]) / max(stretch[1] - stretch[0], 1), 0, 1) ** 0.95
            rgb = np.clip(rgb * np.array([1.02, 1.0, 0.95]), 0, 1)
            rgb8 = (rgb * 255).astype(np.uint8)
            # Rows in the PNG run north to south; UV v=0 is at the south edge.
            Image.fromarray(rgb8).save(out / "textures" / f"ground_{i}_{j}.jpg", quality=90, subsampling=0)
            small = np.asarray(Image.fromarray(np.dstack([rgb8, arr[..., 3]])).resize((tile_px, tile_px), Image.LANCZOS))
            mosaic[(nt - 1 - j) * tile_px: (nt - j) * tile_px, i * tile_px: (i + 1) * tile_px] = small
            x0, y0 = -half + i * TILE_M, -half + j * TILE_M
            step = a.grid
            k = int(round(TILE_M / step)) + 1
            ax, ay = np.linspace(x0, x0 + TILE_M, k), np.linspace(y0, y0 + TILE_M, k)
            xx, yy = np.meshgrid(ax, ay)
            zz = terrain_vis.z(xx, yy)
            gy, gx = np.gradient(zz, step)
            nrm = np.dstack([-gx, -gy, np.ones_like(zz)])
            nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
            v = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
            vt = np.column_stack([(xx.ravel() - x0) / TILE_M, (yy.ravel() - y0) / TILE_M])
            idx = np.arange(k * k).reshape(k, k)
            f = np.vstack([np.c_[idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel()],
                           np.c_[idx[:-1, :-1].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()]])
            write_obj(out / "meshes" / f"ground_{i}_{j}.obj", v, vt, nrm.reshape(-1, 3), f)
            vis_xml.append(pbr_visual(f"ground_{i}_{j}", f"model://{name}/meshes/ground_{i}_{j}.obj", f"model://{name}/textures/ground_{i}_{j}.jpg", 0.95))
    Image.fromarray(mosaic[..., :3]).save(out / "textures" / "roof_mosaic.jpg", quality=90)
    print("ground done", flush=True)

    # Collision terrain, 8 m grid.
    step = 8.0
    k = int(2 * half / step) + 1
    ax = np.linspace(-half, half, k)
    xx, yy = np.meshgrid(ax, ax)
    zz = terrain.z(xx, yy)
    v = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
    idx = np.arange(k * k).reshape(k, k)
    f = np.vstack([np.c_[idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel()],
                   np.c_[idx[:-1, :-1].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()]])
    write_obj(out / "meshes" / "ground_collision.obj", v, v[:, :2] * 0, np.tile([0, 0, 1.0], (len(v), 1)), f)
    coll_xml.append(f'<collision name="ground"><geometry><mesh><uri>model://{name}/meshes/ground_collision.obj</uri></mesh></geometry></collision>')

    # ---- Road meshes
    road_tex = {"major": ("Asphalt025C", 6.0, 0.9), "minor": ("Asphalt031", 6.0, 0.9), 
                "track": ("Ground037", 5.0, 1.0), "rail": ("Ground037", 3.0, 1.0)}
    for cat, polys in road_polys.items():
        if not polys:
            continue
        b = MeshBin()
        drape(polys, terrain_road, 0.02, road_tex[cat][1], b)
        tris = b.write(out / "meshes" / f"road_{cat}.obj")
        src = next((LIB / "ambientcg" / road_tex[cat][0]).glob("*_Color.jpg"), None) if (LIB / "ambientcg" / road_tex[cat][0]).exists() else None
        tex = f"road_{cat}.jpg"
        if src:
            # The CC0 scans are lighter than sunlit aerial asphalt.
            Image.open(src).convert("RGB").resize((1024, 1024), Image.LANCZOS).point(lambda c: int(c * 0.72)).save(out / "textures" / tex, quality=88)
        else:
            Image.new("RGB", (8, 8), (70, 70, 72)).save(out / "textures" / tex)
        vis_xml.append(pbr_visual(f"road_{cat}", f"model://{name}/meshes/road_{cat}.obj", f"model://{name}/textures/{tex}", road_tex[cat][2]))
        print("road", cat, tris, flush=True)
    for lab, polys, col in (("white", marks_white, (232, 232, 228)), ("yellow", marks_yellow, (222, 178, 60))):
        if polys:
            b = MeshBin()
            drape(polys, terrain_road, 0.05, 4.0, b, cell=30.0)
            b.write(out / "meshes" / f"mark_{lab}.obj")
            Image.new("RGB", (8, 8), col).save(out / "textures" / f"mark_{lab}.png")
            vis_xml.append(pbr_visual(f"mark_{lab}", f"model://{name}/meshes/mark_{lab}.obj", f"model://{name}/textures/mark_{lab}.png", 0.7))

    # ---- Buildings meshes
    bins: dict[str, MeshBin] = {}
    for b in buildings:
        bins.setdefault(b["facade"], MeshBin())
        if b["front"]:
            bins.setdefault(b["front"], MeshBin())
    roof_bin, coll_bin = MeshBin(), MeshBin()
    for b in buildings:
        add_building(bins, roof_bin, coll_bin, b, terrain, half)
    facade_dir = LIB / "facades"
    for fac, bin_ in bins.items():
        tris = bin_.write(out / "meshes" / f"facade_{fac}.obj")
        shutil.copy(facade_dir / f"{fac}.jpg", out / "textures" / f"facade_{fac}.jpg")
        glass = fac.startswith("glass")
        vis_xml.append(pbr_visual(f"facade_{fac}", f"model://{name}/meshes/facade_{fac}.obj", f"model://{name}/textures/facade_{fac}.jpg", 0.45 if glass else 0.85, 0.0))
        print("facade", fac, tris, flush=True)
    if roof_bin.count:
        roof_bin.write(out / "meshes" / "roofs.obj")
        vis_xml.append(pbr_visual("roofs", f"model://{name}/meshes/roofs.obj", f"model://{name}/textures/roof_mosaic.jpg", 0.9))
    if coll_bin.count:
        coll_bin.write(out / "meshes" / "buildings_collision.obj")
        coll_xml.append(f'<collision name="buildings"><geometry><mesh><uri>model://{name}/meshes/buildings_collision.obj</uri></mesh></geometry></collision>')

    # ---- Placements for make_world.py
    bpolys = [b["poly"] for b in buildings]
    blocked = rasterize(bpolys + all_road, half, mosaic_res(mosaic, half)) if bpolys or all_road else None
    if blocked is not None:
        blocked = np.flipud(blocked)
    trees = find_trees(np.flipud(mosaic).copy() if False else np.flipud(mosaic), half, rng, np.flipud(blocked) if blocked is not None else None, a.max_trees)
    print("trees", len(trees), flush=True)
    scene = placements(name, kind, osm, buildings, road_lines, trees, terrain, area, rng_py)
    merger = Merger(name, out)
    for t in scene["trees"]:
        merger.add(t["cat"], t["x"], t["y"], t["z"], t["yaw"], t["s"])
    # Things the AI must tell apart stay separate and labelled; the rest merges.
    keep = sorted([o for o in scene["objects"] if o["role"] in {"parked", "person"} or o["cat"] in {"mailbox", "postbox", "fire_hydrant", "stop_sign"}],
                  key=lambda o: math.hypot(o["x"], o["y"]))[:100]
    keep_ids = {id(o) for o in keep}
    for o in scene["objects"]:
        o["merged"] = id(o) not in keep_ids
        if o["merged"] and (SOURCE_ROOT / o["cat"] / "model.sdf").exists() and o["role"] != "person":
            merger.add(o["cat"], o["x"], o["y"], o["z"], o["yaw"])
    vis_xml += merger.write(write_obj, face_normals_to_vertex)
    if kind == "industrial":
        n_containers = add_containers(name, out, mosaic, half, buildings, road_lines, terrain, rng, vis_xml, coll_xml)
        print("containers", n_containers, flush=True)
    print("merged groups", len(merger.groups), flush=True)
    ground_classes = write_core_ground(name, out, raw, terrain_vis, half, vis_xml, tiles, stretch, osm)
    # ---- Model files
    provenance = json.loads((raw / "provenance.json").read_text()) if (raw / "provenance.json").exists() else {}
    attributions = [source.get("attribution") for source in provenance.get("sources", {}).values()
                   if isinstance(source, dict) and source.get("attribution")]
    description = "; ".join(attributions + ["© OpenStreetMap contributors (ODbL)"])
    (out / "model.config").write_text(
        f'<?xml version="1.0"?><model><name>{name}</name><version>1.0</version><sdf version="1.9">model.sdf</sdf>'
        f"<description>{site['title']}: {description}</description></model>\n")
    (out / "model.sdf").write_text(
        f'<?xml version="1.0"?><sdf version="1.9"><model name="{name}"><static>true</static><link name="terrain">'
        + "".join(coll_xml) + "".join(vis_xml) + "</link></model></sdf>\n")

    provenance = json.loads((raw / "provenance.json").read_text())
    scene.update({"site": name, "title": site["title"], "lat": site["lat"], "lon": site["lon"], "elevation": terrain.elev0, "size_m": site["size_m"], "kind": kind,
                  "takeoff_surface": site.get("takeoff_surface"),
                  "source_resolution": {"imagery_m": provenance["imagery_res_m"], "dem_native_m": provenance["dem_res_m"], "dem_grid_m": provenance.get("source_years", {}).get("raster_grid_res_m")},
                  "stats": {"buildings": len(buildings), "trees": len(trees), "road_km": sum(r["line"].length for r in road_lines) / 1000,
                            "roads": len(road_lines), "ground_class_pixels": ground_classes,
                            "osm_ways": len(osm.ways), "osm_relations": len(osm.rel_polys)},
                  "footprints": [[round(v, 1) for v in b["poly"].bounds] + [round(b["h"], 1)] for b in buildings]})
    (ASSET_ROOT / "sites" / name / "scene.json").write_text(json.dumps(scene, ensure_ascii=False))
    print({k: (len(v) if isinstance(v, list) else v) for k, v in scene.items() if k != "footprints"}, flush=True)


def add_containers(name, out, mosaic, half, buildings, road_lines, terrain, rng, vis_xml, coll_xml, radius=900.0) -> int:
    """Stack 3-D containers where the aerial shows a busy yard, aligned with the main road."""
    longest = max((r for r in road_lines if r["line"].distance(Point(0, 0)) < radius), key=lambda r: r["line"].length)
    c = np.array(longest["line"].coords)
    yaw = math.atan2(c[-1, 1] - c[0, 1], c[-1, 0] - c[0, 0])
    u, w = np.array([math.cos(yaw), math.sin(yaw)]), np.array([-math.sin(yaw), math.cos(yaw)])
    res = 2 * half / mosaic.shape[0]
    lum = mosaic[..., :3].mean(axis=2).astype(np.float32)
    win = max(3, int(6.0 / res))
    mean = ndi.uniform_filter(lum, win)
    rough = np.sqrt(np.maximum(ndi.uniform_filter(lum * lum, win) - mean * mean, 0))
    blocked = unary_union([b["poly"].buffer(3) for b in buildings] + [r["line"].buffer(r["width"] / 2 + 3) for r in road_lines] + [Point(0, 0).buffer(PAD_R + 25)])
    tree = STRtree(list(blocked.geoms) if hasattr(blocked, "geoms") else [blocked])
    colors = [(166, 60, 44), (44, 86, 128), (196, 196, 190), (60, 112, 78), (176, 124, 52), (92, 92, 98)]
    bins = [MeshBin() for _ in colors]
    coll = MeshBin()
    L, W, H = 12.2, 2.44, 2.59
    # Per-face vertices give flat shading; ribs run around the sides, and roofs stay plain.
    quads = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]
    box_f = np.array([[i * 4, i * 4 + 1, i * 4 + 2] for i in range(5)] + [[i * 4, i * 4 + 2, i * 4 + 3] for i in range(5)])
    side_uv = np.array([[0, 0], [2, 0], [2, 1], [0, 1.0]])
    box_uv = np.vstack([side_uv] * 4 + [np.zeros((4, 2))])
    count = 0
    for a in np.arange(-radius, radius, L + 0.6 + 7.4 / 4):
        for b_ in np.arange(-radius, radius, W + 0.25):
            if int((b_ + radius) // ((W + 0.25) * 5)) % 2 == 1 and int((b_ + radius) // (W + 0.25)) % 5 == 4:
                continue
            q = u * a + w * b_
            if math.hypot(*q) > radius:
                continue
            row = int((half - q[1]) / res)
            col = int((q[0] + half) / res)
            if not (0 <= row < rough.shape[0] and 0 <= col < rough.shape[1]) or rough[row, col] < 24:
                continue
            if len(tree.query(Point(q).buffer(L / 2), predicate="intersects")):
                continue
            z0 = float(terrain.z(q[0], q[1])) - 0.05
            for level in range(int(rng.integers(1, 5))):
                ci = int(rng.integers(0, len(colors)))
                pts = np.array([q + s_ * u * L / 2 + t_ * w * W / 2 for s_, t_ in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
                z_lo, z_hi = z0 + level * H, z0 + (level + 1) * H
                v = np.vstack([np.c_[pts, np.full(4, z_lo)], np.c_[pts, np.full(4, z_hi)]])
                v = v[np.array(quads).reshape(-1)]
                bins[ci].add(v, box_uv, box_f)
                coll.add(v, box_uv, box_f)
                count += 1
    for i, (b, col) in enumerate(zip(bins, colors)):
        if b.count:
            b.write(out / "meshes" / f"container_{i}.obj")
            x = np.arange(256)
            ribs = (0.86 + 0.14 * np.sin(x / 256 * 2 * np.pi * 24))[None, :, None]
            Image.fromarray((np.tile(np.array(col, float)[None, None], (64, 256, 1)) * ribs).astype(np.uint8)).save(out / "textures" / f"container_{i}.jpg", quality=90)
            vis_xml.append(pbr_visual(f"container_{i}", f"model://{name}/meshes/container_{i}.obj", f"model://{name}/textures/container_{i}.jpg", 0.7))
    if coll.count:
        coll.write(out / "meshes" / "containers_collision.obj")
        coll_xml.append(f'<collision name="containers"><geometry><mesh><uri>model://{name}/meshes/containers_collision.obj</uri></mesh></geometry></collision>')
    return count


def write_core_ground(name, out, raw, terrain_vis, half, vis_xml, tiles, stretch, osm=None) -> dict[str, int]:
    """Sharp ground around the takeoff point: the aerial photo sets colour, CC0 photos add fine detail."""
    core_half = 300.0
    px = 8192
    res = 2 * core_half / px
    src_res = TILE_M / tiles[(0, 0)].shape[0]
    # Stitch just the part of the tile mosaic the core needs.
    n_src = int(2 * core_half / src_res)
    core_path = raw / "core.png"
    if core_path.exists():
        big = np.asarray(Image.open(core_path).convert("RGBA"), np.float32)
        if big.shape[:2] != (px, px):
            big = np.asarray(Image.fromarray(big.astype(np.uint8)).resize((px, px), Image.LANCZOS), np.float32)
        big[..., :3] = np.asarray(Image.fromarray(big[..., :3].astype(np.uint8)).filter(
            ImageFilter.UnsharpMask(radius=3, percent=45, threshold=1)), np.float32)
    else:
        rgbn = np.zeros((n_src, n_src, 4), np.uint8)
        for (i, j), arr in tiles.items():
            x0, y0 = -half + i * TILE_M, -half + j * TILE_M
            ix0, ix1 = max(x0, -core_half), min(x0 + TILE_M, core_half)
            iy0, iy1 = max(y0, -core_half), min(y0 + TILE_M, core_half)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            c0, c1 = int((ix0 - x0) / src_res), int((ix1 - x0) / src_res)
            r0, r1 = int((y0 + TILE_M - iy1) / src_res), int((y0 + TILE_M - iy0) / src_res)
            d0, d1 = int((ix0 + core_half) / src_res), int((ix1 + core_half) / src_res)
            e0, e1 = int((core_half - iy1) / src_res), int((core_half - iy0) / src_res)
            h, w = min(r1 - r0, e1 - e0), min(c1 - c0, d1 - d0)
            rgbn[e0:e0 + h, d0:d0 + w] = arr[r0:r0 + h, c0:c0 + w]
        big_rgb = np.asarray(Image.fromarray(rgbn[..., :3]).resize((px, px), Image.LANCZOS).filter(
            ImageFilter.UnsharpMask(radius=14, percent=110, threshold=0)), np.float32)
        fourth = np.asarray(Image.fromarray(rgbn[..., 3]).resize((px, px), Image.BILINEAR), np.float32)
        big = np.dstack([big_rgb, fourth])
    rgb = big[..., :3]
    rgb = np.clip((rgb - stretch[0]) / max(stretch[1] - stretch[0], 1), 0, 1) ** 0.95 * 255
    rgb = np.clip(rgb * np.array([1.02, 1.0, 0.95]), 0, 255)
    vegetation, vegetation_name = vegetation_index(big)
    lum = rgb.mean(axis=2)

    def detail(prefix: str, span_m: float) -> np.ndarray:
        dirs = sorted((LIB / "ambientcg").glob(f"{prefix}*"))
        if not dirs:
            return np.ones((px, px), np.float32)
        f = next(dirs[0].glob("*_Color.jpg"))
        k = max(16, int(span_m / res))
        t = np.asarray(Image.open(f).convert("L").resize((k, k), Image.LANCZOS), np.float32)
        t = (t - t.mean()) / max(t.std(), 1e-3)
        reps = px // k + 1
        return np.tile(t, (reps, reps))[:px, :px]

    grass, soil, conc = detail("Grass001", 5.0), detail("Ground037", 6.0), detail("Concrete03", 6.0)
    threshold, span = (0.08, 0.12) if vegetation_name == "ndvi" else (0.04, 0.16)
    g = ndi.gaussian_filter(np.clip((vegetation - threshold) / span, 0, 1), 6)
    s = ndi.gaussian_filter(np.clip((rgb[..., 0] - rgb[..., 2]) / 40.0, 0, 1), 6) * (1 - g)
    c = np.clip(1 - g - s, 0, 1)
    # Sentinel is a low-frequency reference. OSM polygons decide the local class so
    # 10 m pixels do not turn roads, parks and water into the same blurry surface.
    class_masks: dict[str, np.ndarray] = {}
    if osm is not None:
        class_polys: dict[str, list] = {"water": [], "wood": [], "grass": [], "soil": [], "urban": []}
        for w in osm.ways:
            t = w["tags"]
            key = None
            if t.get("natural") == "water" or t.get("waterway"):
                key = "water"
            elif t.get("natural") == "wood" or t.get("landuse") in {"forest", "wood"}:
                key = "wood"
            elif t.get("natural") in {"grassland", "scrub", "heath"} or t.get("landuse") in {"grass", "meadow", "recreation_ground", "village_green"} or t.get("leisure") == "park":
                key = "grass"
            elif t.get("landuse") in {"farmland", "orchard", "vineyard", "allotments", "brownfield", "construction"}:
                key = "soil"
            elif t.get("landuse") in {"commercial", "industrial", "residential", "retail"} or t.get("amenity"):
                key = "urban"
            if key and w["closed"] and len(w["pts"]) >= 4:
                p = Polygon(w["pts"]).buffer(0).intersection(box(-half, -half, half, half))
                if not p.is_empty:
                    class_polys[key].append(p)
        for feature in osm.rel_polys:
            t, p = feature["tags"], feature["poly"]
            if t.get("natural") == "water" or t.get("waterway"):
                key = "water"
            elif t.get("natural") == "wood" or t.get("landuse") in {"forest", "wood"}:
                key = "wood"
            elif t.get("natural") in {"grassland", "scrub", "heath"} or t.get("landuse") in {"grass", "meadow", "recreation_ground", "village_green"} or t.get("leisure") == "park":
                key = "grass"
            elif t.get("landuse") in {"farmland", "orchard", "vineyard", "allotments", "brownfield", "construction"}:
                key = "soil"
            elif t.get("landuse") in {"commercial", "industrial", "residential", "retail"} or t.get("amenity"):
                key = "urban"
            else:
                key = None
            if key:
                p = p.buffer(0).intersection(box(-half, -half, half, half))
                if not p.is_empty:
                    class_polys[key].append(p)
        for key, polys in class_polys.items():
            if polys:
                mask = rasterize(polys, half, res)
                class_masks[key] = ndi.gaussian_filter(mask.astype(np.float32), 1.2)
    class_colors = {
        "water": np.array([72, 128, 158], np.float32),
        "wood": np.array([52, 105, 58], np.float32),
        "grass": np.array([98, 142, 72], np.float32),
        "soil": np.array([142, 119, 83], np.float32),
        "urban": np.array([126, 126, 121], np.float32),
    }
    weights = np.zeros(rgb.shape[:2], np.float32)
    classified = np.zeros_like(rgb)
    for key, mask in class_masks.items():
        m = np.clip(mask, 0, 1) * (1 - weights)
        classified += m[..., None] * class_colors[key]
        weights += m
    classified += (1 - weights)[..., None] * rgb
    d = 1 + g * 0.24 * grass + s * 0.18 * soil + c * 0.07 * conc
    out_rgb = np.clip((classified * d[..., None]) * 0.78 + rgb * 0.22, 0, 255).astype(np.uint8)
    Image.fromarray(out_rgb).save(out / "textures" / "ground_core.jpg", quality=88)
    k = 301
    ax = np.linspace(-core_half, core_half, k)
    xx, yy = np.meshgrid(ax, ax)
    zz = terrain_vis.z(xx, yy) + 0.04
    gy, gx = np.gradient(zz, ax[1] - ax[0])
    nrm = np.dstack([-gx, -gy, np.ones_like(zz)])
    nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    idx = np.arange(k * k).reshape(k, k)
    f = np.vstack([np.c_[idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel()],
                   np.c_[idx[:-1, :-1].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()]])
    v = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
    vt = np.column_stack([(xx.ravel() + core_half) / (2 * core_half), (yy.ravel() + core_half) / (2 * core_half)])
    write_obj(out / "meshes" / "ground_core.obj", v, vt, nrm.reshape(-1, 3), f)
    vis_xml.append(pbr_visual("ground_core", f"model://{name}/meshes/ground_core.obj", f"model://{name}/textures/ground_core.jpg", 0.95))
    return {k: int(np.count_nonzero(v > 0.2)) for k, v in class_masks.items()}


def mosaic_res(mosaic: np.ndarray, half: float) -> float:
    return 2 * half / mosaic.shape[0]


def along(line: LineString, spacing: float, offset: float, rng: random.Random):
    """Positions beside a road edge: (x, y, heading)."""
    s = rng.uniform(0, spacing)
    while s < line.length:
        p = line.interpolate(s)
        q = line.interpolate(min(line.length, s + 1.0))
        d = np.array([q.x - p.x, q.y - p.y])
        if np.linalg.norm(d) > 1e-3:
            d /= np.linalg.norm(d)
            nrm = np.array([-d[1], d[0]])
            yield p.x + nrm[0] * offset, p.y + nrm[1] * offset, math.atan2(d[1], d[0])
        s += spacing * rng.uniform(0.7, 1.3)


def placements(name, kind, osm, buildings, road_lines, trees, terrain, area, rng) -> dict:
    """Decide where discrete objects go; everything is on the real road network or at OSM nodes."""
    bld = [b["poly"] for b in buildings]
    btree = STRtree(bld) if bld else None
    objs = []

    def free(x, y, r=1.5):
        if abs(x) > area.bounds[2] - 5 or abs(y) > area.bounds[3] - 5 or math.hypot(x, y) < PAD_R + 3:
            return False
        return btree is None or not len(btree.query(Point(x, y).buffer(r), predicate="intersects"))

    def add(cat, x, y, yaw, role="scenery"):
        if free(x, y):
            objs.append({"cat": cat, "x": round(float(x), 2), "y": round(float(y), 2), "z": round(float(terrain.z(x, y)), 2), "yaw": round(float(yaw), 3), "role": role})

    tags_map = [("highway", "street_lamp", "lamp_post"), ("highway", "stop", "stop_sign"), ("amenity", "post_box", "postbox"),
                ("emergency", "fire_hydrant", "fire_hydrant"), ("power", "pole", "telephone_pole"), ("power", "tower", "telephone_pole"),
                ("amenity", "waste_basket", "dumpster_small"), ("amenity", "bench", "bench"), ("amenity", "fountain", "fountain")]
    for nt in osm.node_tags:
        for k, v, cat in tags_map:
            if nt["tags"].get(k) == v and area.contains(Point(nt["x"], nt["y"])):
                add(cat, nt["x"], nt["y"], rng.uniform(-3.14, 3.14), "osm")
    for nt in osm.node_tags:
        if nt["tags"].get("natural") == "tree":
            trees.insert(0, {"x": nt["x"], "y": nt["y"], "r": 3.0, "lum": 80, "ndvi": 0.3})

    near = sorted(road_lines, key=lambda r: r["line"].distance(Point(0, 0)))
    density = {"urban": (45, 12), "suburb": (60, 18), "rural": (0, 90), "mountain": (0, 200), "coast": (55, 25), "industrial": (60, 30)}[kind]
    lamp_gap, car_gap = density
    budget = {"lamp": 260, "car": 110, "ped": 40, "misc": 80}
    cars = ["hatchback", "hatchback_red", "hatchback_blue", "suv", "pickup", "prius_hybrid"]
    if kind == "industrial":
        cars += ["bus", "pickup", "pickup"]
    for r in near:
        line, width = r["line"], r["width"]
        if r["class"] in {"motorway", "motorway_link", "trunk"}:
            continue
        if lamp_gap and budget["lamp"] > 0 and r["class"] in MARKED and line.distance(Point(0, 0)) < 700:
            for i, (x, y, h) in enumerate(along(line, lamp_gap, width / 2 + 0.8, rng)):
                if budget["lamp"] <= 0:
                    break
                add("lamp_post", x, y, h + math.pi / 2)
                budget["lamp"] -= 1
        if r["class"] in {"residential", "tertiary", "secondary", "unclassified", "service", "primary"} and width >= 4.5 and line.distance(Point(0, 0)) < 900:
            off = width / 2 - 1.1
            for x, y, h in along(line, car_gap, off, rng):
                if budget["car"] <= 0:
                    break
                if rng.random() < 0.55:
                    add(rng.choice(cars), x, y, h + (0 if rng.random() < .8 else math.pi), "parked")
                    budget["car"] -= 1
            for x, y, h in along(line, 55, -(width / 2 + 1.0), rng):
                if budget["misc"] <= 0:
                    break
                c = rng.random()
                if kind in {"urban", "suburb", "coast"} and c < 0.35:
                    add(rng.choice(["fire_hydrant", "postbox", "electrical_box"]), x, y, h)
                elif c < 0.5:
                    add("stop_sign", x, y, h + math.pi / 2)
                budget["misc"] -= 1
    # Kerbside mailboxes for houses, in the real place: the front of each lot.
    if kind in {"suburb", "rural", "coast"} and road_lines:
        rt = STRtree([r["line"] for r in road_lines])
        mailboxes = 0
        for b in sorted(buildings, key=lambda b: b["poly"].distance(Point(0, 0))):
            if b["tags"].get("building") not in {"house", "detached", "residential", "yes"} or b["poly"].area > 500:
                continue
            c = b["poly"].centroid
            j = rt.nearest(c)
            rl = road_lines[j]
            if rl["line"].distance(c) > 45 or rl["class"] not in {"residential", "unclassified", "tertiary", "service"}:
                continue
            s = rl["line"].project(c)
            p = rl["line"].interpolate(s)
            d = np.array([c.x - p.x, c.y - p.y])
            d /= max(np.linalg.norm(d), 1e-3)
            q = np.array([p.x, p.y]) + d * (rl["width"] / 2 + 0.6)
            add("mailbox", q[0], q[1], math.atan2(d[1], d[0]))
            mailboxes += 1
            if mailboxes >= 120:
                break
    # Pedestrians: on sidewalks near the origin; movers get a route in make_world.py.
    for r in near[:40]:
        if budget["ped"] <= 0 or kind in {"mountain"}:
            break
        for x, y, h in along(r["line"], 35, r["width"] / 2 + 1.2, rng):
            if budget["ped"] <= 0:
                break
            if rng.random() < 0.5:
                add(rng.choice(["standing_person", "casual_female", "male_visitor"]), x, y, rng.uniform(-3, 3), "person")
                budget["ped"] -= 1
    routes = []
    for r in near[:30]:
        if r["line"].length > 120 and r["class"] in MARKED | {"service"}:
            pts = [r["line"].interpolate(s) for s in np.arange(0, r["line"].length, 12.0)]
            routes.append({"class": r["class"], "width": r["width"], "pts": [[round(p.x, 2), round(p.y, 2), round(float(terrain.z(p.x, p.y)), 2)] for p in pts]})
        if len(routes) >= 12:
            break
    # Keep trees off the discrete objects, with species picked by place and colour.
    out_trees = []
    for t in trees:
        if not free(t["x"], t["y"], 0.5):
            continue
        species = "pine_tree" if kind == "mountain" or (kind == "coast" and rng.random() < 0.3) else ("pine_tree" if t["lum"] < 55 and rng.random() < 0.5 else "oak_tree")
        out_trees.append({"cat": species, "x": round(t["x"], 2), "y": round(t["y"], 2), "z": round(float(terrain.z(t["x"], t["y"])), 2),
                          "yaw": round(rng.uniform(-3.14, 3.14), 3), "s": round(min(2.6, max(0.8, t["r"] / 2.6 * rng.uniform(0.85, 1.2))), 2)})
    return {"objects": objs, "trees": out_trees, "routes": routes}


if __name__ == "__main__":
    main()

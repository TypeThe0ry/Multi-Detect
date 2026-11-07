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
from instancing import ASSET_BY_SLUG, Merger, has_local_model  # noqa: E402
from city_models import read_normalized  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "configs/sim-assets/realworld-sites.json").read_text())
ASSET_ROOT = Path(CFG["asset_root"])
SCENE_ROOT = Path(os.environ.get("RW_SCENE_ROOT", ASSET_ROOT))
LIB = ASSET_ROOT / "library"
RESOLVED = Path(os.environ.get(
    "RW_RESOLVED_ROOT",
    os.environ.get("RW_MODEL_ROOT", os.environ.get("REALWORLD_OUTPUT_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved")),
))
DEM_RES = 2.0
TILE_M = 1000
PAD_R = 25.0
FLOOR_M = 3.2


def validate_launch_water(water_polys, clearance_m: float = PAD_R) -> None:
    """Reject water beneath the flattened launch pad instead of hiding it."""
    pad = Point(0, 0).buffer(clearance_m)
    if any(not polygon.is_empty and polygon.intersects(pad) for polygon in water_polys):
        raise ValueError("launch pad intersects mapped water; choose a dry site origin and fetch matching raw data")

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
        return rng.choice(["metal_a", "metal_b", "metal_c"])
    if b in {"house", "detached", "residential", "semidetached_house", "bungalow", "cabin", "terrace"} or h < 10:
        return rng.choice(["siding_a", "siding_b", "stucco_a", "brick_low", "siding_c", "brick_low_b"])
    if h > 28:
        return rng.choice(["glass_a", "glass_b", "office_a", "office_b"])
    return rng.choice(["office_a", "office_b", "brick_mid", "brick_mid_b", "concrete_mid", "stucco_mid", "stucco_mid_b"])


def parse_colour(value: str | None) -> tuple[float, float, float] | None:
    if not value:
        return None
    names = {"white": (1, 1, 1), "black": (.08, .08, .08), "red": (.72, .18, .12), "blue": (.18, .35, .62),
             "green": (.22, .48, .25), "yellow": (.82, .68, .20), "brown": (.45, .27, .16), "gray": (.55, .55, .53), "grey": (.55, .55, .53)}
    s = str(value).strip().lower()
    if s in names:
        return names[s]
    if s.startswith("#") and len(s) in {4, 7}:
        if len(s) == 4:
            s = "#" + "".join(c * 2 for c in s[1:])
        try:
            return tuple(int(s[i:i + 2], 16) / 255 for i in (1, 3, 5))
        except ValueError:
            return None
    return None


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


def add_building(bins, roof_bin, parapet_bin, equipment_bins, coll, b, terrain, half: float) -> None:
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
    # Keep roof equipment small and separate from the parapet material.
    if rid is None and poly.area > 25:
        rr = random.Random(int(b["id"]) ^ 0x5EED)
        ring = np.asarray(poly.exterior.coords)
        for a, bb in zip(ring, np.roll(ring, -1, axis=0)):
            d = bb - a
            L = float(np.linalg.norm(d))
            if L < 1.0:
                continue
            mid = (a + bb) / 2
            yaw = math.atan2(d[1], d[0])
            add_box(parapet_bin, (mid[0], mid[1], eave + 0.20), (L, 0.18, 0.40), yaw)
        minx, miny, maxx, maxy = poly.bounds
        count = min(16, max(1, int(poly.area / 450)))
        rect = poly.minimum_rotated_rectangle
        edge = np.asarray(rect.exterior.coords)[1] - np.asarray(rect.exterior.coords)[0]
        base_yaw = math.atan2(edge[1], edge[0])
        for _ in range(count):
            w, d, hh = rr.uniform(1.0, 3.0), rr.uniform(1.0, 2.6), rr.uniform(1.0, 2.5)
            for _try in range(24):
                x, y = rr.uniform(minx, maxx), rr.uniform(miny, maxy)
                if poly.contains(Point(x, y).buffer(max(w, d) / 2)):
                    color_bin = equipment_bins[rr.randrange(len(equipment_bins))]
                    add_box(color_bin, (x, y, eave + hh / 2 + 0.05), (w, d, hh), base_yaw + rr.uniform(-0.12, 0.12))
                    break
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


def add_city_model(rows: list[dict], out: Path, terrain: Terrain, half: float) -> tuple[int, int, int]:
    """Merge CityGML surfaces by one aerial texture and emit a simple collision mesh."""
    visual, collision = MeshBin(), MeshBin()
    for row in rows:
        ground = np.asarray([p for surface in row.get("surfaces", [])
                             if surface.get("kind") == "GroundSurface"
                             for p in surface.get("vertices", [])], dtype=float)
        # GLO-30 is a surface elevation model and includes urban canopy.
        # Align each building base locally instead of leaving whole blocks
        # below that terrain or floating above it. This remains an approximation.
        ground_shift = 0.0
        if len(ground):
            ground_shift = float(np.median(terrain.z(ground[:, 0], ground[:, 1]) + terrain.elev0 - ground[:, 2]))
        for surface in row.get("surfaces", []):
            rings = []
            for vertices in [surface.get("vertices", []), *surface.get("holes", [])]:
                ring = np.asarray(vertices, dtype=float)
                if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
                    ring = ring[:-1]
                if len(ring) >= 3:
                    rings.append(ring)
            if not rings:
                continue
            pts = np.concatenate(rings)
            pts[:, 2] += ground_shift - terrain.elev0
            # Local aerial mosaic coordinates; both roof and wall surfaces use
            # the same measured image and naturally fall back to OSM facades
            # when no open city model was downloaded.
            uv = np.column_stack([(pts[:, 0] + half) / (2 * half), (pts[:, 1] + half) / (2 * half)])
            # Ear clipping in the surface plane preserves concave roofs and
            # vertical walls; a fan can fill courtyards or cross the footprint.
            _, singular, basis = np.linalg.svd(pts - pts.mean(0), full_matrices=False)
            if len(singular) < 2 or singular[1] < 1e-6:
                continue
            flat = np.ascontiguousarray((pts - pts.mean(0)) @ basis[:2].T)
            ends = np.cumsum([len(ring) for ring in rings], dtype=np.uint32)
            faces = earcut.triangulate_float64(flat, ends).reshape(-1, 3)
            visual.add(pts, uv, faces)
            collision.add(pts, uv, faces)
    if not visual.count:
        return 0, 0, 0
    tris = visual.write(out / "meshes" / "city_buildings.obj")
    collision.write(out / "meshes" / "city_buildings_collision.obj")
    return len(rows), tris, collision.count


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


def drape_features(features, terrain, tex_m: float, out: MeshBin, cell: float = 14.0) -> None:
    """Drape (polygon, extra_z) features while retaining per-feature bridge height."""
    for poly, extra_z in features:
        if poly.is_empty:
            continue
        minx, miny, maxx, maxy = poly.bounds
        for x in np.arange(minx, maxx, 64.0):
            for y in np.arange(miny, maxy, 64.0):
                clip = poly.intersection(box(x, y, x + 64.0, y + 64.0))
                for p in clip.geoms if hasattr(clip, "geoms") else [clip]:
                    if p.geom_type != "Polygon" or p.area < 0.02:
                        continue
                    try:
                        verts, idx = triangulate(p)
                    except ValueError:
                        continue
                    vv, ff = subdivide(verts, idx.astype(np.int64), cell)
                    z = terrain.z(vv[:, 0], vv[:, 1]) + float(extra_z)
                    v3 = np.column_stack([vv, z])
                    flip = cross2(vv[ff[:, 1]] - vv[ff[:, 0]], vv[ff[:, 2]] - vv[ff[:, 0]]) < 0
                    ff[flip] = ff[flip][:, ::-1]
                    out.add(v3, vv / tex_m, ff)


def add_box(bin_: MeshBin, center, size, yaw: float = 0.0, z0: float | None = None) -> None:
    """Add a closed, flat-shaded box to a mesh bin."""
    x, y, z = map(float, center)
    sx, sy, sz = map(float, size)
    z0 = z - sz / 2 if z0 is None else float(z0)
    z1 = z0 + sz
    c, s = math.cos(yaw), math.sin(yaw)
    uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]] * 6, float)
    corners = np.array([[-sx / 2, -sy / 2], [sx / 2, -sy / 2], [sx / 2, sy / 2], [-sx / 2, sy / 2]])
    corners = (np.array([[c, -s], [s, c]]) @ corners.T).T + [x, y]
    low, high = np.c_[corners, np.full(4, z0)], np.c_[corners, np.full(4, z1)]
    faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7), (3, 2, 1, 0)]
    v = np.vstack([low, high])[np.asarray(faces).reshape(-1)]
    f = np.arange(len(v)).reshape(-1, 4)
    tri = np.vstack([f[:, [0, 1, 2]], f[:, [0, 2, 3]]])
    bin_.add(v, uv, tri)


def add_line_prism(bin_: MeshBin, points: list[tuple[float, float, float]], width: float, height: float = 0.04) -> None:
    """Create a thin rectangular prism along a polyline, with small sag at wire midpoints."""
    for a, b in zip(points, points[1:]):
        p0, p1 = np.array(a, float), np.array(b, float)
        d = p1[:2] - p0[:2]
        L = np.linalg.norm(d)
        if L < 0.2:
            continue
        n = np.array([-d[1], d[0]]) / L * width / 2
        q = np.array([[p0[0] + n[0], p0[1] + n[1]], [p1[0] + n[0], p1[1] + n[1]],
                      [p1[0] - n[0], p1[1] - n[1]], [p0[0] - n[0], p0[1] - n[1]]])
        add_box(bin_, (q[:, 0].mean(), q[:, 1].mean(), (p0[2] + p1[2]) / 2), (L, width, height), math.atan2(d[1], d[0]))


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


def pbr_visual(name: str, mesh_uri: str, albedo: str, rough: float = 0.9, metal: float = 0.0, double: bool = False, normal: str | None = None, transparency: float = 0.0) -> str:
    extra = "<double_sided>true</double_sided>" if double else ""
    normal_xml = f"<normal_map>{normal}</normal_map>" if normal else ""
    transparency_xml = f"<transparency>{transparency:.2f}</transparency>" if transparency else ""
    return (f'<visual name="{name}"><cast_shadows>{"false" if "ground" in name or "road" in name or "mark" in name else "true"}</cast_shadows>{transparency_xml}<geometry><mesh><uri>{mesh_uri}</uri></mesh></geometry>'
            f"<material><diffuse>1 1 1 1</diffuse><specular>0.05 0.05 0.05 1</specular>{extra}<pbr><metal><albedo_map>{albedo}</albedo_map>"
            f"{normal_xml}<roughness>{rough}</roughness><metalness>{metal}</metalness></metal></pbr></material></visual>")


def road_texture(src: Path | None, mosaic: np.ndarray, polys, half: float, out: Path, seed: int) -> None:
    """Blend aerial road colour into a scanned asphalt atlas and add scale cues."""
    size = 1024
    base = np.asarray(Image.open(src).convert("RGB").resize((size, size), Image.LANCZOS), np.float32) if src else np.full((size, size, 3), 72, np.float32)
    mask = Image.new("L", (mosaic.shape[1], mosaic.shape[0]), 0)
    d = ImageDraw.Draw(mask)
    res = 2 * half / mosaic.shape[0]
    for p in polys:
        for g in p.geoms if hasattr(p, "geoms") else [p]:
            if g.geom_type == "Polygon":
                d.polygon([((x + half) / res, (y + half) / res) for x, y in g.exterior.coords], fill=255)
    # The atlas is flipped vertically to match OBJ v=0 at the south edge.
    m = np.flipud(np.asarray(mask, np.float32)) > 0
    if m.any():
        aerial = np.median(mosaic[..., :3][m], axis=0)
        target = aerial.astype(np.float32)
        median = np.median(base.reshape(-1, 3), axis=0)
        base *= target / np.maximum(median, 1.0)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, (size // 16, size // 16)).astype(np.float32)
    noise = np.asarray(Image.fromarray(noise, mode="F").resize((size, size), Image.BICUBIC))
    base *= (1.0 + np.clip(noise[..., None] * 0.035, -0.10, 0.10))
    pim = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    pd = ImageDraw.Draw(pim, "RGBA")
    for _ in range(16):
        x, y = int(rng.integers(0, size)), int(rng.integers(0, size))
        w, h = int(rng.integers(25, 170)), int(rng.integers(8, 34))
        pd.rounded_rectangle((x, y, min(size, x + w), min(size, y + h)), radius=4, fill=(35, 38, 40, int(rng.integers(25, 75))))
    for _ in range(22):
        x, y = int(rng.integers(0, size)), int(rng.integers(0, size))
        pts = [(x, y)]
        for _ in range(int(rng.integers(2, 6))):
            x += int(rng.integers(-45, 46)); y += int(rng.integers(10, 80)); pts.append((x, y))
        pd.line(pts, fill=(25, 26, 26, int(rng.integers(70, 155))), width=int(rng.integers(1, 4)))
    pim.save(out, quality=90, subsampling=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--max-trees", type=int, default=2500)
    ap.add_argument("--grid", type=float, default=4.0)
    ap.add_argument("--output-root", type=Path, default=RESOLVED, help="模型输出目录")
    ap.add_argument("--scene-root", type=Path, default=Path(os.environ.get("REALWORLD_SCENE_ROOT", SCENE_ROOT / "sites")), help="scene.json 输出目录")
    ap.add_argument("--raw-root", type=Path, default=ASSET_ROOT / "sites", help="原始数据站点目录")
    a = ap.parse_args()
    name = a.site
    site = CFG["sites"][name]
    kind = site["kind"]
    raw = a.raw_root / name / "raw"
    provenance_path = raw / "provenance.json"
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text())
        if any(abs(float(provenance[key]) - float(site[key])) > 1e-5 for key in ("lat", "lon")):
            raise ValueError(f"raw origin differs from configured site {name}; fetch matching data before building")
    half = site["size_m"] / 2
    rng_py = random.Random(hash(name) & 0xffff if False else sum(map(ord, name)))
    rng = np.random.default_rng(sum(map(ord, name)))
    proj = Transformer.from_crs("EPSG:4326", f"+proj=tmerc +lat_0={site['lat']} +lon_0={site['lon']} +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m", always_xy=True)
    terrain = Terrain(site, raw)
    osm = Osm(raw / "osm.json", proj)
    bag_heights = apply_3dbag_heights(osm, raw)
    area = box(-half + 1, -half + 1, half - 1, half - 1)
    city_rows = read_normalized(raw / "city_model.json") if site.get("provider") == "berlin" else []
    city_rows = [r for r in city_rows if any(area.contains(Point(p[0], p[1])) for surface in r.get("surfaces", []) for p in surface.get("vertices", []))]
    out = a.output_root / name
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
    city_bounds = []
    for row in city_rows:
        pts = [p for surface in row.get("surfaces", []) for p in surface.get("vertices", [])]
        hull = shapely.MultiPoint([(p[0], p[1]) for p in pts]).convex_hull
        if hull.geom_type == "Polygon" and hull.area >= 8 and hull.distance(Point(0, 0)) >= PAD_R:
            city_bounds.append(hull)
        else:
            row["surfaces"] = []
    city_rows = [row for row in city_rows if row["surfaces"]]
    if city_bounds:
        city_cover = unary_union(city_bounds)
        buildings = [b for b in buildings if b["poly"].intersection(city_cover).area < b["poly"].area * 0.5]
    print("buildings", len(buildings), "max h", max([b["h"] for b in buildings], default=0), flush=True)

    # ---- Roads
    # Footpaths stay as the aerial shows them; ribbons there read as cartoon strips.
    road_polys: dict[str, list] = {"major": [], "minor": [], "track": [], "rail": []}
    bridge_features: dict[str, list] = {"major": [], "minor": [], "track": []}
    sidewalk_polys, curb_polys = [], []
    water_polys = []
    utility_lines, fence_lines = [], []
    rail_lines = []
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
            bridge = t.get("bridge") in {"yes", "viaduct", "aqueduct"}
            layer = parse_len(t.get("layer")) or 0.0
            elev = max(3.5, layer * 4.5) if bridge else 0.0
            road_poly = ls.buffer(width / 2, cap_style="flat", join_style="round", quad_segs=3)
            (bridge_features[cat] if bridge else road_polys[cat]).append((road_poly, elev) if bridge else road_poly)
            road_lines.append({"line": ls, "class": hw, "width": width, "oneway": t.get("oneway") == "yes", "name": t.get("name", ""), "bridge": bridge, "elev": elev, "tags": t})
            sw = t.get("sidewalk")
            want_sw = sw in {"both", "left", "right", "separate", "yes"} or (sw is None and kind in {"urban", "suburb", "coast"} and hw in {"residential", "tertiary", "secondary", "primary", "living_street"})
            if want_sw and not bridge:
                for side in (1, -1):
                    if sw in {"left", "right"} and ((sw == "left") != (side > 0)):
                        continue
                    try:
                        off = ls.parallel_offset(width / 2 + 0.75, "left" if side > 0 else "right", join_style=2)
                        if off.geom_type == "LineString":
                            sidewalk_polys.append(off.buffer(1.15, cap_style="flat", join_style="round"))
                            curb_polys.append(off.buffer(0.13, cap_style="flat", join_style="round"))
                    except (ValueError, shapely.errors.GEOSException):
                        pass
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
            rail_lines.append(ls)
        if t.get("power") in {"line", "minor_line"}:
            utility_lines.append((ls, t))
        if t.get("barrier") in {"fence", "wall"}:
            fence_lines.append((ls, t))
        if t.get("natural") == "coastline" or t.get("waterway") == "riverbank":
            if w["closed"]:
                water_polys.append(Polygon(w["pts"]).buffer(0))
    for w in osm.ways:
        t = w["tags"]
        if t.get("natural") == "water" and w["closed"]:
            water_polys.append(Polygon(w["pts"]).buffer(0).intersection(area))
        if t.get("waterway") == "riverbank" and w["closed"]:
            water_polys.append(Polygon(w["pts"]).buffer(0).intersection(area))
    for r in osm.rel_polys:
        if r["tags"].get("natural") == "water" or r["tags"].get("waterway") == "riverbank":
            water_polys.append(r["poly"].intersection(area))
    validate_launch_water(water_polys)
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
    all_road = [p for k in road_polys for p in road_polys[k]] + [p for k in bridge_features for p, _ in bridge_features[k]]
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
    tile_px = 2048  # low-cost atlas used for roof sampling; ground tiles keep local resolution
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
            tile_distance = math.hypot((-half + (i + 0.5) * TILE_M), (-half + (j + 0.5) * TILE_M))
            tile_out_px = 4096 if tile_distance <= TILE_M * 0.85 else 2048
            tile_rgb = rgb8 if tile_out_px == rgb8.shape[0] else np.asarray(Image.fromarray(rgb8).resize((tile_out_px, tile_out_px), Image.LANCZOS))
            Image.fromarray(tile_rgb).save(out / "textures" / f"ground_{i}_{j}.jpg", quality=90, subsampling=0)
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
            # The fine core replaces these triangles. Merely lifting it by
            # 4 cm still intersects the coarser terrain on slopes, producing
            # large rectangular patches from two different image atlases.
            centers = v[f, :2].mean(axis=1)
            f = f[~((np.abs(centers[:, 0]) < 300) & (np.abs(centers[:, 1]) < 300))]
            write_obj(out / "meshes" / f"ground_{i}_{j}.obj", v, vt, nrm.reshape(-1, 3), f)
            vis_xml.append(pbr_visual(f"ground_{i}_{j}", f"model://{name}/meshes/ground_{i}_{j}.obj", f"model://{name}/textures/ground_{i}_{j}.jpg", 0.95))
    roof_atlas = Image.fromarray(mosaic[..., :3]).resize((1024, 1024), Image.LANCZOS)
    roof_atlas.save(out / "textures" / "roof_mosaic.jpg", quality=88)
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
    road_tex = {"major": ("Asphalt025C", 6.0, 0.92), "minor": ("Asphalt031", 5.0, 0.88),
                "track": ("Ground037", 4.0, 0.98), "rail": ("Ground037", 3.0, 0.92)}
    for cat, polys in road_polys.items():
        if not polys:
            continue
        b = MeshBin()
        drape(polys, terrain_road, 0.02, road_tex[cat][1], b)
        tris = b.write(out / "meshes" / f"road_{cat}.obj")
        src = next((LIB / "ambientcg" / road_tex[cat][0]).glob("*_Color.jpg"), None) if (LIB / "ambientcg" / road_tex[cat][0]).exists() else None
        tex = f"road_{cat}.jpg"
        road_texture(src, mosaic, polys, half, out / "textures" / tex, sum(map(ord, cat)) + len(polys))
        vis_xml.append(pbr_visual(f"road_{cat}", f"model://{name}/meshes/road_{cat}.obj", f"model://{name}/textures/{tex}", road_tex[cat][2]))
        print("road", cat, tris, flush=True)
    for cat, features in bridge_features.items():
        if not features:
            continue
        b = MeshBin()
        drape_features(features, terrain_road, road_tex[cat][1], b)
        tris = b.write(out / "meshes" / f"bridge_{cat}.obj")
        tex = f"road_{cat}.jpg"
        vis_xml.append(pbr_visual(f"bridge_{cat}", f"model://{name}/meshes/bridge_{cat}.obj", f"model://{name}/textures/{tex}", 0.82))
        print("bridge", cat, tris, flush=True)
    if sidewalk_polys:
        b = MeshBin(); drape(sidewalk_polys, terrain_road, 0.17, 3.0, b, cell=8.0)
        tris = b.write(out / "meshes" / "sidewalk.obj")
        Image.new("RGB", (128, 128), (150, 153, 148)).save(out / "textures" / "sidewalk.jpg", quality=90)
        vis_xml.append(pbr_visual("sidewalk", f"model://{name}/meshes/sidewalk.obj", f"model://{name}/textures/sidewalk.jpg", 0.86))
        print("sidewalk", tris, flush=True)
    if curb_polys:
        b = MeshBin(); drape(curb_polys, terrain_road, 0.31, 2.0, b, cell=5.0)
        tris = b.write(out / "meshes" / "curb.obj")
        Image.new("RGB", (64, 64), (178, 178, 170)).save(out / "textures" / "curb.jpg", quality=90)
        vis_xml.append(pbr_visual("curb", f"model://{name}/meshes/curb.obj", f"model://{name}/textures/curb.jpg", 0.8))
    # Bridge decks have structural piers and simple safety rails at both edges.
    bridge_bin = MeshBin()
    for cat, features in bridge_features.items():
        for poly, elev in features:
            line = max((r["line"] for r in road_lines if r.get("bridge") and r["line"].buffer(r["width"] / 2).intersects(poly)), key=lambda q: q.length, default=None)
            if line is None:
                continue
            for side in (-1, 1):
                try:
                    edge = line.parallel_offset(line.length and (next(r["width"] for r in road_lines if r.get("bridge") and r["line"] is line) / 2 + 0.35) or 1.0, "left" if side > 0 else "right")
                except (StopIteration, ValueError, shapely.errors.GEOSException):
                    edge = line
                for segment in (edge.geoms if hasattr(edge, "geoms") else [edge]):
                    if segment.geom_type != "LineString" or segment.is_empty:
                        continue
                    pts = [(p[0], p[1], terrain.z(p[0], p[1]) + elev + 1.05) for p in segment.coords]
                    add_line_prism(bridge_bin, pts, 0.12, 1.0)
            npiers = max(1, int(line.length / 28.0))
            for k in range(1, npiers + 1):
                p = line.interpolate(line.length * k / (npiers + 1))
                add_box(bridge_bin, (p.x, p.y, terrain.z(p.x, p.y) + elev / 2), (0.8, 0.8, max(1.0, elev)))
    if bridge_bin.count:
        tris = bridge_bin.write(out / "meshes" / "bridge_details.obj")
        Image.new("RGB", (64, 64), (105, 108, 110)).save(out / "textures" / "bridge_details.jpg", quality=90)
        vis_xml.append(pbr_visual("bridge_details", f"model://{name}/meshes/bridge_details.obj", f"model://{name}/textures/bridge_details.jpg", 0.72, 0.15))
        print("bridge details", tris, flush=True)
    if rail_lines:
        rail_bin = MeshBin()
        for line in rail_lines:
            for side in (-1, 1):
                try:
                    edge = line.parallel_offset(0.72, "left" if side > 0 else "right")
                except (ValueError, shapely.errors.GEOSException):
                    edge = line
                for segment in (edge.geoms if hasattr(edge, "geoms") else [edge]):
                    if segment.geom_type != "LineString" or segment.is_empty:
                        continue
                    pts = [(p[0], p[1], terrain.z(p[0], p[1]) + 0.20) for p in segment.coords]
                    add_line_prism(rail_bin, pts, 0.09, 0.12)
            for s in np.arange(0.0, line.length, 1.8):
                p = line.interpolate(s); q = line.interpolate(min(line.length, s + 0.1))
                add_box(rail_bin, (p.x, p.y, terrain.z(p.x, p.y) + 0.08), (2.0, 0.18, 0.16), math.atan2(q.y - p.y, q.x - p.x))
        tris = rail_bin.write(out / "meshes" / "rail_details.obj")
        Image.new("RGB", (64, 64), (72, 76, 80)).save(out / "textures" / "rail_details.jpg", quality=90)
        vis_xml.append(pbr_visual("rail_details", f"model://{name}/meshes/rail_details.obj", f"model://{name}/textures/rail_details.jpg", 0.42, 0.72))
    if utility_lines:
        power_bin = MeshBin()
        for line, _tags in utility_lines:
            coords = list(line.coords)
            pts = [(x, y, terrain.z(x, y) + 8.5) for x, y in coords]
            for a0, b0 in zip(pts, pts[1:]):
                mid = ((a0[0] + b0[0]) / 2, (a0[1] + b0[1]) / 2, min(a0[2], b0[2]) - 1.0)
                add_line_prism(power_bin, [a0, mid, b0], 0.035, 0.035)
        for ntag in osm.node_tags:
            if ntag["tags"].get("power") in {"pole", "tower"}:
                add_box(power_bin, (ntag["x"], ntag["y"], terrain.z(ntag["x"], ntag["y"]) + 4.25), (0.18, 0.18, 8.5))
        if power_bin.count:
            tris = power_bin.write(out / "meshes" / "power_lines.obj")
            Image.new("RGB", (32, 32), (42, 43, 40)).save(out / "textures" / "power_lines.jpg", quality=90)
            vis_xml.append(pbr_visual("power_lines", f"model://{name}/meshes/power_lines.obj", f"model://{name}/textures/power_lines.jpg", 0.48, 0.7))
            print("power", tris, flush=True)
    if fence_lines:
        fence_bin = MeshBin()
        for line, tags in fence_lines:
            h = 1.8 if tags.get("barrier") == "fence" else 2.2
            line = shapely.segmentize(line, max_segment_length=2.0)
            pts = [(x, y, terrain.z(x, y) + h / 2) for x, y in line.coords]
            add_line_prism(fence_bin, pts, 0.08, h)
        tris = fence_bin.write(out / "meshes" / "fences.obj")
        Image.new("RGB", (32, 32), (105, 98, 83)).save(out / "textures" / "fences.jpg", quality=90)
        vis_xml.append(pbr_visual("fences", f"model://{name}/meshes/fences.obj", f"model://{name}/textures/fences.jpg", 0.82))
    if water_polys:
        wb = MeshBin(); water_polys = [p for p in water_polys if not p.is_empty and p.area > 2]
        drape(water_polys, terrain_road, 0.08, 18.0, wb, cell=12.0)
        tris = wb.write(out / "meshes" / "water.obj")
        water = np.zeros((512, 512, 3), np.uint8); wrng = np.random.default_rng(42)
        water[..., 0], water[..., 1], water[..., 2] = 38, 78, 74
        wave = wrng.normal(0, 1, (64, 64)).astype(np.float32)
        wave = np.asarray(Image.fromarray(wave, mode="F").resize((512, 512), Image.BICUBIC))
        water = np.clip(water.astype(np.float32) + wave[..., None] * np.array([3, 7, 6]), 0, 255).astype(np.uint8)
        Image.fromarray(water).save(out / "textures" / "water.jpg", quality=92)
        gy, gx = np.gradient(wave); nrm = np.dstack([-gx * 0.12, -gy * 0.12, np.ones_like(wave)])
        nrm = np.clip((nrm / np.linalg.norm(nrm, axis=2, keepdims=True) * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)
        Image.fromarray(nrm).save(out / "textures" / "water_normal.png")
        vis_xml.append(pbr_visual("water", f"model://{name}/meshes/water.obj", f"model://{name}/textures/water.jpg", 0.12, 0.05, True, f"model://{name}/textures/water_normal.png", 0.55))
        print("water", tris, flush=True)
    for lab, polys, col in (("white", marks_white, (216, 216, 211)), ("yellow", marks_yellow, (205, 170, 68))):
        if polys:
            b = MeshBin()
            drape(polys, terrain_road, 0.05, 4.0, b, cell=30.0)
            b.write(out / "meshes" / f"mark_{lab}.obj")
            Image.new("RGB", (8, 8), col).save(out / "textures" / f"mark_{lab}.png")
            vis_xml.append(pbr_visual(f"mark_{lab}", f"model://{name}/meshes/mark_{lab}.obj", f"model://{name}/textures/mark_{lab}.png", 0.7))

    # ---- Buildings meshes
    facade_dir = Path(os.environ.get("RW_FACADE_OUT", LIB / "facades"))
    for b in buildings:
        tint = parse_colour(b["tags"].get("building:colour") or b["tags"].get("building:color"))
        if tint is not None:
            base_fac = b["facade"]
            variant = f"{base_fac}_tag_{b['id']}"
            src = facade_dir / f"{base_fac}.jpg"
            if src.exists():
                im = np.asarray(Image.open(src).convert("RGB").resize((768, 768), Image.LANCZOS), np.float32)
                im *= np.array(tint)[None, None, :] * 1.55
                Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).save(out / "textures" / f"facade_{variant}.jpg", quality=90)
                b["facade"] = variant
    bins: dict[str, MeshBin] = {}
    for b in buildings:
        bins.setdefault(b["facade"], MeshBin())
        if b["front"]:
            bins.setdefault(b["front"], MeshBin())
    roof_bin, parapet_bin, equipment_bins, coll_bin = MeshBin(), MeshBin(), [MeshBin() for _ in range(3)], MeshBin()
    for b in buildings:
        add_building(bins, roof_bin, parapet_bin, equipment_bins, coll_bin, b, terrain, half)
    for fac, bin_ in bins.items():
        tris = bin_.write(out / "meshes" / f"facade_{fac}.obj")
        if not tris:
            continue
        src_fac = out / "textures" / f"facade_{fac}.jpg"
        if not src_fac.exists():
            Image.open(facade_dir / f"{fac}.jpg").resize((768, 768), Image.LANCZOS).save(src_fac, quality=90)
        glass = fac.startswith("glass")
        vis_xml.append(pbr_visual(f"facade_{fac}", f"model://{name}/meshes/facade_{fac}.obj", f"model://{name}/textures/facade_{fac}.jpg", 0.45 if glass else 0.85, 0.0))
        print("facade", fac, tris, flush=True)
    if roof_bin.count:
        roof_bin.write(out / "meshes" / "roofs.obj")
        vis_xml.append(pbr_visual("roofs", f"model://{name}/meshes/roofs.obj", f"model://{name}/textures/roof_mosaic.jpg", 0.9))
    detail_specs = [("parapet", parapet_bin, (142, 142, 136))]
    equipment_colors = ((168, 165, 155), (118, 126, 128), (188, 184, 171))
    detail_specs.extend(
        (f"equipment_{i}", bin_, color)
        for i, (bin_, color) in enumerate(zip(equipment_bins, equipment_colors))
    )
    for detail, bin_, color in detail_specs:
        if not bin_.count:
            continue
        tris = bin_.write(out / "meshes" / f"roof_{detail}.obj")
        Image.new("RGB", (64, 64), color).save(out / "textures" / f"roof_{detail}.jpg", quality=90)
        vis_xml.append(pbr_visual(f"roof_{detail}", f"model://{name}/meshes/roof_{detail}.obj", f"model://{name}/textures/roof_{detail}.jpg", 0.78))
        print(f"roof {detail}", tris, flush=True)
    if coll_bin.count:
        coll_bin.write(out / "meshes" / "buildings_collision.obj")
        coll_xml.append(f'<collision name="buildings"><geometry><mesh><uri>model://{name}/meshes/buildings_collision.obj</uri></mesh></geometry></collision>')

    city_stats = (0, 0, 0)
    if city_rows:
        # Orthophoto roofs; walls are explicitly a fallback, not photographed facades.
        Image.fromarray(mosaic[..., :3]).resize((4096, 4096), Image.LANCZOS).save(out / "textures" / "city_texture.jpg", quality=88)
        city_stats = add_city_model(city_rows, out, terrain, half)
        if city_stats[1]:
            vis_xml.append(pbr_visual("city_buildings", f"model://{name}/meshes/city_buildings.obj", f"model://{name}/textures/city_texture.jpg", 0.85, double=True))
            coll_xml.append(f'<collision name="city_buildings"><geometry><mesh><uri>model://{name}/meshes/city_buildings_collision.obj</uri></mesh></geometry></collision>')

    # ---- Placements for make_world.py
    bpolys = [b["poly"] for b in buildings] + city_bounds + water_polys
    blocked = rasterize(bpolys + all_road, half, mosaic_res(mosaic, half)) if bpolys or all_road else None
    if blocked is not None:
        blocked = np.flipud(blocked)
    trees = find_trees(np.flipud(mosaic).copy() if False else np.flipud(mosaic), half, rng, np.flipud(blocked) if blocked is not None else None, a.max_trees)
    # Trees are the primary visual prop; keep detected crowns and budget remaining props afterward.
    print("trees", len(trees), "(4M prop triangle budget)", flush=True)
    scene = placements(name, kind, osm, buildings, road_lines, trees, terrain, area, rng_py, city_bounds + water_polys)
    # Keep the global tree budget across image detections and land-use sampling.
    # The latter adds useful variety but must not turn a 2500-tree scene into a
    # much denser prop field on urban sites.
    scene["trees"] = scene["trees"][:a.max_trees]
    tree_count = len(scene["trees"])
    filtered_objects = []
    for obj in scene["objects"]:
        category = ASSET_BY_SLUG.get(obj["cat"], {}).get("category")
        if category in {"broadleaf_tree", "conifer"}:
            if tree_count >= a.max_trees:
                continue
            tree_count += 1
        filtered_objects.append(obj)
    scene["objects"] = filtered_objects
    merger = Merger(name, out)
    for t in scene["trees"]:
        merger.add(t["cat"], t["x"], t["y"], t["z"], t["yaw"], t["s"])
    # Things the AI must tell apart stay separate and labelled; the rest merges.
    keep = sorted([o for o in scene["objects"] if o["role"] in {"parked", "person"} or o["cat"] in {"mailbox", "postbox", "fire_hydrant", "stop_sign"}],
                  key=lambda o: math.hypot(o["x"], o["y"]))[:100]
    keep_ids = {id(o) for o in keep}
    priority = {"rock": 1, "shrub": 2, "grass": 3}
    merge_objects = sorted(scene["objects"], key=lambda o: (0 if o["role"] == "parked" else priority.get(ASSET_BY_SLUG.get(o["cat"], {}).get("category"), 4)))
    for o in merge_objects:
        o["merged"] = id(o) not in keep_ids
        if o["merged"] and has_local_model(o["cat"]) and o["role"] != "person":
            merger.add(o["cat"], o["x"], o["y"], o["z"], o["yaw"], o.get("s", 1.0))
    vis_xml += merger.write(write_obj, face_normals_to_vertex)
    if kind == "industrial":
        n_containers = add_containers(name, out, mosaic, half, buildings, road_lines, terrain, rng, vis_xml, coll_xml, water_polys=water_polys)
        print("containers", n_containers, flush=True)
    print("merged groups", len(merger.groups), flush=True)
    ground_classes = write_core_ground(name, out, raw, terrain_vis, half, vis_xml, tiles, stretch, osm if site.get("provider") == "global" else None)
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

    texture_decoded = 0
    for tex_path in (out / "textures").iterdir():
        with Image.open(tex_path) as im:
            texture_decoded += im.width * im.height * 4 * 4 // 3
    triangle_total = 0
    for mesh_path in (out / "meshes").glob("*.obj"):
        try:
            triangle_total += sum(1 for line in mesh_path.open() if line.startswith("f "))
        except OSError:
            pass
    texture_decoded_gib = texture_decoded / 2**30
    print(f"texture_decoded_with_mips={texture_decoded_gib:.3f}/2.000 GiB triangles={triangle_total}", flush=True)
    texture_bytes = sum((lambda im: im.width * im.height * 4)(Image.open(p)) for p in (out / "textures").glob("*") if p.is_file())
    prop_texture_bytes = sum((lambda im: im.width * im.height * 4)(Image.open(p)) for p in (out / "textures").glob("inst_*") if p.is_file())
    prop_categories: dict[str, dict[str, int]] = {}
    for slug, stats in merger.stats.items():
        category = ASSET_BY_SLUG.get(slug, {}).get("category", slug)
        row = prop_categories.setdefault(category, {"instances": 0, "triangles": 0})
        row["instances"] += stats["instances"]
        row["triangles"] += stats["triangles"]
    merged_tree_instances = sum(stats["instances"] for slug, stats in merger.stats.items()
                                if ASSET_BY_SLUG.get(slug, {}).get("category") in {"broadleaf_tree", "conifer"})
    scene.update({"raw_dir": str(raw.resolve()), "site": name, "title": site["title"], "lat": site["lat"], "lon": site["lon"], "elevation": terrain.elev0, "size_m": site["size_m"], "kind": kind,
                  "stats": {"ground_class_pixels": ground_classes, "city_model_buildings": city_stats[0], "city_model_triangles": city_stats[1],
                            "city_model_texture_bytes": (out / "textures/city_texture.jpg").stat().st_size if city_stats[1] else 0,
                            "buildings": len(buildings) + len(city_rows), "trees": merged_tree_instances, "tree_candidates": len(scene["trees"]), "road_km": sum(r["line"].length for r in road_lines) / 1000,
                            "water_polygons": len(water_polys), "bridges": sum(len(v) for v in bridge_features.values()),
                            "texture_decoded_gib": round(texture_decoded_gib, 3), "texture_budget_gib": 2.0, "triangles": triangle_total,
                            "mesh_buffer_estimate_bytes": triangle_total * 3 * (32 + 4),
                            "prop_triangles": int(sum(v["triangles"] for v in merger.stats.values())), "prop_instances": int(sum(v["instances"] for v in merger.stats.values())),
                            "prop_by_category": prop_categories, "prop_by_asset": merger.stats,
                            "decoded_texture_bytes": int(texture_bytes), "decoded_prop_texture_bytes": int(prop_texture_bytes)},
                  "water_areas": [shapely.geometry.mapping(p) for p in water_polys if not p.is_empty],
                  "footprints": [[round(v, 1) for v in b["poly"].bounds] + [round(b["h"], 1)] for b in buildings] + [[*map(float, p.bounds), float(row.get("height", 8))] for p, row in zip(city_bounds, city_rows)]})
    scene_dir = a.scene_root / name
    scene_dir.mkdir(parents=True, exist_ok=True)
    (scene_dir / "scene.json").write_text(json.dumps(scene, ensure_ascii=False, indent=2))
    print({k: (len(v) if isinstance(v, list) else v) for k, v in scene.items() if k != "footprints"}, flush=True)
    print("prop categories", json.dumps(merger.stats, ensure_ascii=False, sort_keys=True), flush=True)


def add_containers(name, out, mosaic, half, buildings, road_lines, terrain, rng, vis_xml, coll_xml, radius=900.0, water_polys=()) -> int:
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
    blocked = unary_union([b["poly"].buffer(3) for b in buildings] + [r["line"].buffer(r["width"] / 2 + 3) for r in road_lines] + [Point(0, 0).buffer(PAD_R + 25)] + [p.buffer(10) for p in water_polys])
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
                p = Polygon(w["pts"]).buffer(0).intersection(box(-core_half, -core_half, core_half, core_half))
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
                p = p.buffer(0).intersection(box(-core_half, -core_half, core_half, core_half))
                if not p.is_empty:
                    class_polys[key].append(p)
        for key, polys in class_polys.items():
            if polys:
                mask = rasterize(polys, core_half, res)
                class_masks[key] = ndi.gaussian_filter(np.flipud(mask).astype(np.float32), 1.2)
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
    out_rgb = np.clip(((classified * 0.78 + rgb * 0.22) if osm is not None else rgb) * d[..., None], 0, 255).astype(np.uint8)
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
    return {key: int(np.count_nonzero(mask > 0.5)) for key, mask in class_masks.items()}


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


def placements(name, kind, osm, buildings, road_lines, trees, terrain, area, rng, city_bounds=()) -> dict:
    """Decide where discrete objects go; everything is on the real road network or at OSM nodes."""
    bld = [b["poly"] for b in buildings] + list(city_bounds)
    btree = STRtree(bld) if bld else None
    objs = []

    def free(x, y, r=1.5):
        if abs(x) > area.bounds[2] - 5 or abs(y) > area.bounds[3] - 5 or math.hypot(x, y) < PAD_R + 3:
            return False
        return btree is None or not len(btree.query(Point(x, y).buffer(r), predicate="intersects"))

    def add(cat, x, y, yaw, role="scenery"):
        if free(x, y):
            objs.append({"cat": cat, "x": round(float(x), 2), "y": round(float(y), 2), "z": round(float(terrain.z(x, y)), 2), "yaw": round(float(yaw), 3), "role": role})

    tags_map = [("highway", "street_lamp", "lamp_post"), ("highway", "stop", "stop_sign"), ("highway", "traffic_signals", "stop_sign"),
                ("amenity", "post_box", "postbox"), ("emergency", "fire_hydrant", "fire_hydrant"),
                ("power", "pole", "telephone_pole"), ("power", "tower", "telephone_pole"),
                ("amenity", "waste_basket", "litterbin"), ("amenity", "bench", "airportbench"),
                ("amenity", "bicycle_parking", "airportbench"), ("amenity", "fountain", "fountain"),
                ("man_made", "street_lamp", "lamp_post"), ("man_made", "utility_pole", "telephone_pole"),
                ("man_made", "street_cabinet", "electrical_box"), ("man_made", "wastewater_plant", "electrical_box")]
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
    cars = ["hatchback", "hatchback_red", "hatchback_blue", "suv", "pickup"]
    if kind == "industrial":
        cars += ["bus", "pickup", "truckdelivery", "forklift", "school_bus"]
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
    # Add sampled vegetation inside tagged land-use polygons and roadside hedges.
    tree_species = {
        "urban": ["oak_tree", "tree_small_02", "island_tree_01", "island_tree_02"],
        "suburb": ["oak_tree", "tree_small_02", "island_tree_01", "island_tree_02", "island_tree_03", "pine_tree_01"],
        "rural": ["oak_tree", "tree_small_02", "fir_tree_01", "pine_tree_01", "island_tree_02"],
        "mountain": ["pine_tree", "pine_tree_01", "fir_tree_01", "oak_tree", "tree_small_02"],
        "coast": ["pine_tree", "island_tree_01", "island_tree_02", "island_tree_03", "tree_small_02"],
        "industrial": ["oak_tree", "tree_small_02", "pine_tree_01", "island_tree_02"],
    }[kind]
    tree_species = [s for s in tree_species if has_local_model(s)] or ["oak_tree"]
    shrub_species = [s for s in ("shrub_01", "shrub_02", "shrub_03", "shrub_04", "shrub_sorrel_01", "wild_rooibos_bush") if has_local_model(s)]
    grass_species = [s for s in ("grass_medium_01", "grass_medium_02", "grass_bermuda_01", "weed_plant_02") if has_local_model(s)]
    rock_species = [s for s in ("boulder_01", "namaqualand_boulder_02", "namaqualand_boulder_03", "coast_land_rocks_03", "coast_land_rocks_04", "rock_moss_set_01") if has_local_model(s)]
    for w in osm.ways:
        tags = w["tags"]
        if not w["closed"] or len(w["pts"]) < 4:
            continue
        land = tags.get("natural") in {"wood", "scrub"} or tags.get("landuse") in {"forest", "orchard", "farmland", "meadow", "grass"}
        hedge = tags.get("barrier") == "hedge"
        if not (land or hedge):
            continue
        poly = Polygon(w["pts"]).buffer(0).intersection(area)
        if poly.is_empty:
            continue
        if hedge:
            line = LineString(w["pts"]).intersection(area)
            if line.geom_type == "LineString":
                for x, y, yaw in along(line, 5.0, 0.0, rng):
                    add(rng.choice(shrub_species or ["oak_tree"]), x, y, yaw)
            continue
        wood = tags.get("natural") == "wood" or tags.get("landuse") == "forest"
        orchard = tags.get("landuse") == "orchard"
        spacing = 8.5 if wood else 9.0 if orchard else 18.0
        count = 0 if wood else min(1800, int(poly.area / (spacing * spacing)))
        minx, miny, maxx, maxy = poly.bounds
        tries = 0
        while count and tries < count * 8:
            x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
            tries += 1
            if poly.contains(Point(x, y)) and free(x, y, 1.0):
                add(rng.choice(tree_species), x, y, rng.uniform(-math.pi, math.pi))
                objs[-1]["s"] = round(rng.uniform(0.68, 1.38), 2)
                count -= 1
        if tags.get("natural") in {"scrub"} or tags.get("landuse") in {"farmland", "orchard", "meadow"}:
            for _ in range(min(320, int(poly.area / (28 if tags.get("natural") == "scrub" else 45)))):
                p = poly.representative_point() if poly.area < 10 else Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
                if poly.contains(p) and free(p.x, p.y, 0.5):
                    add(rng.choice(shrub_species or grass_species or tree_species), p.x, p.y, rng.uniform(-math.pi, math.pi))
        if tags.get("landuse") in {"farmland", "meadow", "grass"} and grass_species:
            for _ in range(min(800, int(poly.area / 18))):
                p = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
                if poly.contains(p) and free(p.x, p.y, 0.25):
                    add(rng.choice(grass_species), p.x, p.y, rng.uniform(-math.pi, math.pi))
    # Fences are generated as continuous meshes elsewhere; don't instance fence panels here.
    # Keep trees off discrete objects; species and size vary by climate and image evidence.
    out_trees = []
    for t in trees:
        if not free(t["x"], t["y"], 0.5):
            continue
        conifer_bias = 0.78 if kind == "mountain" else (0.18 if kind in {"coast", "suburb"} else 0.42)
        want_conifer = rng.random() < conifer_bias
        candidates = [s for s in tree_species if (ASSET_BY_SLUG.get(s, {}).get("category") == "conifer") == want_conifer]
        species = rng.choice(candidates or tree_species)
        out_trees.append({"cat": species, "x": round(t["x"], 2), "y": round(t["y"], 2), "z": round(float(terrain.z(t["x"], t["y"])), 2),
                          "yaw": round(rng.uniform(-3.14, 3.14), 3), "s": round(min(1.45, max(0.62, t["r"] / 4.0 * rng.uniform(0.72, 1.32))), 2)})
    # Sparse edge ground cover and alpine rocks, sampled off roads/buildings.
    bounds = area.bounds
    for _ in range(360 if kind in {"rural", "mountain"} else 140):
        x, y = rng.uniform(bounds[0], bounds[2]), rng.uniform(bounds[1], bounds[3])
        if math.hypot(x, y) < PAD_R + 8 or not free(x, y, 0.6):
            continue
        if kind == "mountain" and rock_species and rng.random() < 0.42:
            add(rng.choice(rock_species), x, y, rng.uniform(-math.pi, math.pi))
        elif grass_species and rng.random() < 0.55:
            add(rng.choice(grass_species), x, y, rng.uniform(-math.pi, math.pi))
    return {"objects": objs, "trees": out_trees, "routes": routes}


if __name__ == "__main__":
    main()

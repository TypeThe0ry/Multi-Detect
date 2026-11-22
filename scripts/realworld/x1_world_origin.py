"""Measure launch water clearance against the site's existing OSM cache."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

if __package__:
    from .build_scene import Osm
else:
    from build_scene import Osm

CLEARANCE_M = 40
WATERWAYS = {"river", "canal", "stream", "ditch", "drain"}


def water_geometry(osm):
    def area(tags):
        return (tags.get("natural") in {"water", "coastline"}
                or tags.get("waterway") == "riverbank" or tags.get("landuse") == "reservoir")

    shapes = [row["poly"] for row in osm.rel_polys if area(row["tags"])]
    coasts = []
    for way in osm.ways:
        tags, coords = way["tags"], way["pts"]
        if tags.get("natural") == "coastline":
            coasts.append(LineString(coords))
            shapes.append(coasts[-1])
        elif way["closed"] and len(coords) >= 4 and area(tags):
            shapes.append(Polygon(coords).buffer(0))
        elif (tags.get("waterway") in WATERWAYS and tags.get("tunnel", "no") == "no"
              and tags.get("location") != "underground"):
            shapes.append(LineString(coords))
    return unary_union(shapes), coasts


def water_distance(point, water, coasts):
    if coasts:
        # OSM coastlines are directed with land on the left. Use the nearest
        # segment(s) to reject a point on the seaward side of the coastline.
        nearest, sea = math.inf, False
        for coast in coasts:
            coords = list(coast.coords)
            for (x, y), (nx, ny) in zip(coords, coords[1:]):
                dx, dy = nx - x, ny - y
                if dx == dy == 0:
                    continue
                px, py = point.x - x, point.y - y
                t = max(0, min(1, (px * dx + py * dy) / (dx * dx + dy * dy)))
                d = math.hypot(px - dx * t, py - dy * t)
                right = dx * py - dy * px < 0
                if d < nearest - 1e-6:
                    nearest, sea = d, right
                elif abs(d - nearest) <= 1e-6:
                    sea = sea and right
        if sea:
            return 0.0
    return None if water.is_empty else point.distance(water)


def validate_dem(path):
    """Fail before build_scene can replace missing elevations with its display fallback."""
    import numpy as np

    dem = np.load(path, mmap_mode="r")
    if dem.ndim != 2 or dem.shape[0] != dem.shape[1] or dem.shape[0] < 3:
        raise ValueError(f"{path}: expected a square DEM grid")
    has_nonzero = False
    for start in range(0, len(dem), 256):
        rows = dem[start:start + 256]
        if not np.isfinite(rows).all() or rows.min() < -500 or rows.max() > 1e6:
            raise ValueError(f"{path}: 6 km DEM has uncovered or invalid elevations")
        has_nonzero = has_nonzero or bool(np.any(rows))
    if not has_nonzero:
        raise ValueError(f"{path}: 6 km DEM is all zero")


def audit(raw: Path, base: dict, chosen: dict, extent_m=2000):
    osm_path = raw / "osm.json"
    provenance = json.loads((raw / "provenance.json").read_text())
    if provenance["size_m"] != extent_m:
        raise ValueError(f"{raw}: expected {extent_m} m source coverage")
    proj = Transformer.from_crs("EPSG:4326", provenance["proj4"], always_xy=True)
    osm = Osm(osm_path, proj)
    if not osm.nodes:
        raise ValueError(f"{osm_path}: no OSM nodes in cached extent")
    water, coasts = water_geometry(osm)
    with osm_path.open("rb") as stream:
        osm_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    distances = []
    for entry in (base, chosen):
        point = Point(proj.transform(entry["lon"], entry["lat"]))
        if not all(math.isfinite(v) and abs(v) <= extent_m / 2 - CLEARANCE_M for v in point.coords[0]):
            raise ValueError(f"{raw}: origin's {CLEARANCE_M} m disk escapes cached coverage")
        distances.append(water_distance(point, water, coasts))
    old, new = distances
    changed = any(base[key] != chosen[key] for key in ("lat", "lon"))
    return {
        "raw": str(raw), "extent_m": extent_m,
        "osm_sha256": osm_hash,
        "clearance_m": CLEARANCE_M, "base_water_distance_m": old, "water_distance_m": new,
        "lat": chosen["lat"], "lon": chosen["lon"], "origin_changed": changed,
        "origin_moved_for_pad": bool(chosen.get("origin_moved_for_pad")),
        "passed": (new is None or new >= CLEARANCE_M)
                  and (extent_m != 2000 or not changed or chosen.get("origin_moved_for_pad")
                       or (old is not None and old < CLEARANCE_M)),
    }

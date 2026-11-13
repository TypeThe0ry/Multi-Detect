#!/usr/bin/env python3
"""Small, dependency-light readers for open city model downloads.

The fetcher stores a normalized ``city_model.json`` so the scene builder never
needs to understand a provider-specific archive layout.  CityGML is parsed
with ElementTree; the parser intentionally accepts both EPSG:25833 Berlin
files and the common PLATEAU local-coordinate export.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import MultiPoint, Polygon


def _numbers(text: str) -> list[float]:
    return [float(x) for x in re.split(r"[\s,]+", text.strip()) if x]


def _surface_points(node: ET.Element) -> list[tuple[float, float, float]]:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] in {"posList", "pos"} and child.text:
            a = _numbers(child.text)
            dim = int(child.attrib.get("srsDimension", "3"))
            if dim == 2:
                return [(a[i], a[i + 1], 0.0) for i in range(0, len(a) - 1, 2)]
            return [(a[i], a[i + 1], a[i + 2]) for i in range(0, len(a) - 2, 3)]
    return []


def parse_citygml(path: Path, proj: Transformer, *, area: Polygon | None = None) -> list[dict]:
    """Read building surfaces and normalize them to local ENU coordinates."""
    root = ET.parse(path).getroot()
    rows: list[dict] = []
    for building in root.iter():
        if building.tag.rsplit("}", 1)[-1] not in {"Building", "BuildingPart"}:
            continue
        surfaces: list[dict] = []
        def own_surfaces(node):
            for child in node:
                kind = child.tag.rsplit("}", 1)[-1]
                if kind in {"Building", "BuildingPart"}:
                    continue  # Nested parts get their own row below.
                if kind in {"RoofSurface", "WallSurface", "GroundSurface"}:
                    yield child
                else:
                    yield from own_surfaces(child)

        for node in own_surfaces(building):
            kind = node.tag.rsplit("}", 1)[-1]
            polygons = [p for p in node.iter() if p.tag.rsplit("}", 1)[-1] == "Polygon"]
            for polygon in polygons or [node]:
                rings = []
                boundaries = [p for p in polygon if p.tag.rsplit("}", 1)[-1] in {"exterior", "interior"}]
                for ring in boundaries or [polygon]:
                    pts = _surface_points(ring)
                    if len(pts) < 3:
                        continue
                    xy = [proj.transform(x, y) for x, y, _ in pts]
                    rings.append([[float(x), float(y), float(z)] for (x, y), (_, _, z) in zip(xy, pts)])
                if not rings:
                    continue
                # Vertical walls project to a line, not a valid XY polygon.
                # Polygon.buffer(0) discards them and leaves roofs floating.
                footprint = MultiPoint([(p[0], p[1]) for p in rings[0]]).convex_hull
                if area is not None and not area.intersects(footprint):
                    continue
                surface = {"kind": kind, "vertices": rings[0]}
                if len(rings) > 1:
                    surface["holes"] = rings[1:]
                surfaces.append(surface)
        if not surfaces:
            continue
        allv = np.asarray([p for s in surfaces for p in s["vertices"]], dtype=float)
        rows.append({"id": building.attrib.get("{http://www.opengis.net/gml}id", str(len(rows))),
                     "surfaces": surfaces, "height": float(max(allv[:, 2]) - min(allv[:, 2]))})
    return rows


def write_normalized(path: Path, buildings: list[dict], *, source: str, textured: bool) -> None:
    path.write_text(json.dumps({"schema_version": 1, "source": source, "textured": textured,
                                "buildings": buildings}, ensure_ascii=False), encoding="utf-8")


def read_normalized(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(data.get("buildings", []))


def tile_counts(path: Path) -> tuple[int, int]:
    """Return ``(triangles, texture_bytes)`` for a normalized model."""
    triangles = 0
    for b in read_normalized(path):
        for s in b.get("surfaces", []):
            triangles += max(0, len(s.get("vertices", [])) - 2)
    return triangles, sum(p.stat().st_size for p in path.parent.glob("city_texture*"))


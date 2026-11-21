#!/usr/bin/env python3
"""Build an offline X1 route and publish a verified bundle under ASSET/x1.

Run with a local Python providing NumPy and GDAL Python bindings. The corridor
AGL limits cover cruise and hover return. Vertical launch and recovery use a separate pad check.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

if __package__:
    from .x1_pad import PAD_REJECTED, PadRejected
    from . import x1_scenario_class as policy
else:
    from x1_pad import PAD_REJECTED, PadRejected
    import x1_scenario_class as policy


ROOT = Path(__file__).resolve().parents[2]
AIRFRAME = "delta_tailsitter_gimbal_cam"
RUNNER_PYTHON = Path("/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python")
HALF = 3000
CELL = 8
COUNT = 2 * HALF // CELL
BUFFER = 90
EDGE = 20
RADII = (80, 100, 120)
STEP = 4
ARRIVAL = 15
DEPARTURE = policy.WIDE_DISTANCE_M
MAX_ALTITUDE = 150
CRUISE_SPEED = 15.0
MIN_AGL = 40  # The CPU runner is stricter than the nominal 30 m requirement.
MAX_AGL = 120
CLEARANCE = 10
ALPINE_SITE = policy.ALPINE_SITE
ALPINE_ORIGIN = policy.ALPINE_ORIGIN
QUADRANTS = {"NW": (-1, 1), "NE": (1, 1), "SW": (-1, -1), "SE": (1, -1)}


def mesh_face(line):
    return tuple(int(word.split(b"/", 1)[0]) - 1 for word in line.split()[1:])


def ground_vertices(path):
    """Validate the two ordered triangles per cell emitted by build_scene."""
    vertices = []
    count = 0
    width = COUNT + 1
    with path.open("rb") as stream:
        for line in stream:
            if line.startswith(b"v "):
                vertices.append(line[2:])
            elif line.startswith(b"f "):
                row, col = divmod(count % COUNT**2, COUNT)
                base = row * width + col
                if count < COUNT**2:
                    expected = (base, base + 1, base + width + 1)
                else:
                    expected = (base, base + width + 1, base + width)
                if mesh_face(line) != expected:
                    raise ValueError(f"unexpected ground collision mesh faces: {path}")
                count += 1
    if count != 2 * COUNT**2:
        raise ValueError(f"missing or extra ground collision mesh faces: {path}")
    return np.loadtxt(vertices, ndmin=2)


def paint(grid, lower, upper, heights):
    """Overestimate each face/box by painting its entire horizontal AABB."""
    lower = np.floor((lower + HALF) / CELL).astype(int)
    upper = np.floor((upper + HALF) / CELL).astype(int)
    inside = np.all((lower < COUNT) & (upper >= 0), axis=1)
    lower = np.clip(lower[inside], 0, COUNT - 1)
    upper = np.clip(upper[inside], 0, COUNT - 1)
    heights = heights[inside]
    widths = upper - lower
    for dx, dy in itertools.product(*(range(n + 1) for n in widths.max(axis=0, initial=0))):
        mask = (widths[:, 0] >= dx) & (widths[:, 1] >= dy)
        if mask.any():
            np.maximum.at(grid, (lower[mask, 1] + dy, lower[mask, 0] + dx), heights[mask])


def paint_mesh(grid, path):
    with path.open("rb") as stream:
        vertices = []
        faces = []
        for line in stream:
            if line.startswith(b"v "):
                vertices.append(line[2:])
            elif line.startswith(b"f "):
                face = mesh_face(line)
                if len(face) != 3 or min(face) < 0:
                    raise ValueError(f"expected positive triangle vertex indices in {path}")
                faces.append(face)
    if not faces or not vertices:
        raise ValueError(f"missing mesh triangles or vertices in {path}")
    vertices = np.loadtxt(vertices, ndmin=2)
    if vertices.shape[1] != 3 or not np.isfinite(vertices).all() or np.max(faces) >= len(vertices):
        raise ValueError(f"invalid mesh vertices or face indices in {path}")
    points = vertices[np.asarray(faces)]
    lo, hi = points.min(axis=1), points.max(axis=1)
    paint(grid, lo[:, :2], hi[:, :2], hi[:, 2])
    return len(faces)


def aggregate_dem(path, elevation, collision_path):
    """Bound all raw DEM samples (0.5 m) and all corners of the 8 m collision mesh."""
    dem = np.load(path, mmap_mode="r")
    vertices = ground_vertices(collision_path)
    axis = np.linspace(-HALF, HALF, COUNT + 1)
    if (vertices.shape != ((COUNT + 1)**2, 3) or not np.isfinite(vertices).all() or
            not np.allclose(vertices[:, 0].reshape(COUNT + 1, -1), axis) or
            not np.allclose(vertices[:, 1].reshape(-1, COUNT + 1), axis[:, None])):
        raise ValueError("unexpected ground collision mesh layout")
    if dem.ndim != 2 or dem.shape[0] != dem.shape[1] or len(dem) <= COUNT or (len(dem) - 1) % COUNT:
        raise ValueError("raw DEM grid does not align with the collision mesh")
    scale = (len(dem) - 1) // COUNT
    minima, maxima = (np.empty((COUNT, COUNT)) for _ in range(2))
    ground = vertices[:, 2].reshape(COUNT + 1, COUNT + 1)
    for i in range(COUNT):
        rows = dem[i * scale:(i + 1) * scale + 1]
        if not np.isfinite(rows).all() or rows.min() < -500 or rows.max() > 1e6:
            raise ValueError("raw DEM has invalid values")
        core = rows[:, :-1].reshape(scale + 1, COUNT, scale)
        # Include east edge of every cell. North edge is included in rows.
        east_edge = rows[:, scale::scale]
        minima[i] = np.minimum(core.min(axis=(0, 2)), east_edge.min(axis=0)) - elevation
        maxima[i] = np.maximum(core.max(axis=(0, 2)), east_edge.max(axis=0)) - elevation
        for j in (i, i + 1):
            np.minimum(minima[i], np.minimum(ground[j, :-1], ground[j, 1:]), out=minima[i])
            np.maximum(maxima[i], np.maximum(ground[j, :-1], ground[j, 1:]), out=maxima[i])
    return minima, maxima, ground


def water_mask(scene, raw):
    """Rasterize scene polygons plus raw OSM water areas and waterway axes."""
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    osm = json.loads((raw / "osm.json").read_text())["elements"]
    source, target = osr.SpatialReference(), osr.SpatialReference()
    source.ImportFromEPSG(4326)
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target.ImportFromProj4(read_json(raw / "provenance.json")["proj4"])
    transformer = osr.CoordinateTransformation(source, target)
    east, north, *_ = transformer.TransformPoint(scene["lon"], scene["lat"])
    if not math.hypot(east, north) <= 1.0:
        raise ValueError("raw OSM projection does not match the scene centre")
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in osm if e["type"] == "node"}
    ways = {e["id"]: e for e in osm if e["type"] == "way"}
    vector = ogr.GetDriverByName("MEM").CreateDataSource("")
    layer = vector.CreateLayer("water")

    def add(geometry):
        if geometry is not None and not geometry.IsEmpty():
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(geometry)
            if layer.CreateFeature(feature):
                raise ValueError("water feature could not be recorded")

    def way_line(way, ring=False):
        coords = [nodes[n] for n in way.get("nodes", ()) if n in nodes]
        line = ogr.Geometry(ogr.wkbLinearRing if ring else ogr.wkbLineString)
        for lon, lat, *_ in transformer.TransformPoints(coords) if coords else ():
            line.AddPoint_2D(lon, lat)
        return line

    def area(tags):
        return tags.get("natural") in {"water", "coastline"} or tags.get("waterway") == "riverbank"

    for entry in scene["water_areas"]:
        add(ogr.CreateGeometryFromJson(json.dumps(entry)))
    for way in ways.values():
        tags, refs = way.get("tags", {}), way.get("nodes", [])
        if len(refs) >= 4 and refs[0] == refs[-1] and area(tags) and all(n in nodes for n in refs):
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(way_line(way, ring=True))
            add(polygon.MakeValid())
        elif tags.get("waterway") in {"river", "canal", "stream", "ditch", "drain"}:
            if tags.get("tunnel") != "yes" and tags.get("location") != "underground":
                add(way_line(way))
    # Multipolygon relations are already assembled and clipped by the scene builder.
    raster = gdal.GetDriverByName("MEM").Create("", COUNT, COUNT, 1, gdal.GDT_Byte)
    raster.SetGeoTransform((-HALF, CELL, 0, HALF, 0, -CELL))
    if gdal.RasterizeLayer(raster, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"]):
        raise ValueError("water rasterization failed")
    return raster.ReadAsArray()[::-1].astype(bool), layer.GetFeatureCount()


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def local_file(root, relative):
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"asset is outside its expected root: {relative}")
    return path


def vector(element, count):
    values = np.array([float(value) for value in (element.text or "").split()])
    if len(values) != count or not np.isfinite(values).all():
        raise ValueError(f"invalid {element.tag} vector")
    return values


def load_layers(asset, world_file):
    root = ET.parse(world_file).getroot()
    worlds = root.findall("world")
    if root.tag != "sdf" or len(worlds) != 1 or worlds[0].get("name") != "default":
        raise ValueError("expected a single default SDF world")
    world = worlds[0]
    if any(world.findall(f".//{tag}") for tag in ("include", "plugin", "frame", "joint", "wind")):
        raise ValueError("preflight requires a flattened calm world without plugins or custom frames")
    scene_id = world_file.stem.split("__", 1)[0]
    inputs = {}

    def record(relative):
        path = local_file(asset, relative)
        key = str(relative)
        if key not in inputs:
            inputs[key] = file_sha256(path)
        return path

    scene = read_json(record(f"scenes/{scene_id}/scene.json"))
    raw = f"raw/{scene_id}/raw"
    dem_path = record(f"{raw}/dem.npy")
    record(f"{raw}/osm.json")
    record(f"{raw}/provenance.json")
    if scene["site"] != scene_id or scene["size_m"] != 2 * HALF:
        raise ValueError("expected matching 6 km scenario source assets")
    origin = world.find("spherical_coordinates")
    if (origin is None or origin.findtext("surface_model") != "EARTH_WGS84"
            or origin.findtext("world_frame_orientation") != "ENU"
            or float(origin.findtext("heading_deg", "0")) != 0):
        raise ValueError("expected a WGS84 ENU world")
    for tag, key, tolerance in (("latitude_deg", "lat", 1e-8), ("longitude_deg", "lon", 1e-8),
                                ("elevation", "elevation", 0.01)):
        if not math.isclose(float(origin.findtext(tag, "nan")), scene[key], rel_tol=0, abs_tol=tolerance):
            raise ValueError(f"world and scene disagree on {tag}")

    def model_path(uri):
        if not uri.startswith("model://") or len(Path(uri[8:]).parts) < 2:
            raise ValueError(f"unsupported SDF asset URI: {uri}")
        return local_file(asset / "models", uri[8:])

    # Check visible resources as well, including textures and actor animation files.
    for element in world.iter():
        text = (element.text or "").strip()
        if text.startswith("model://") or element.tag in {"uri", "filename"}:
            model_path(text)

    terrain = world.find("model[@name='rw_terrain']")
    if terrain is None:
        raise ValueError("missing SDF terrain model")

    def mesh(element):
        mesh_node = element.find("geometry/mesh")
        if mesh_node is None:
            raise ValueError("expected an SDF mesh geometry")
        uri = mesh_node.findtext("uri", "")
        if not uri.startswith(f"model://{scene_id}/") or [e.tag for e in mesh_node] != ["uri"]:
            raise ValueError(f"unsupported mesh geometry for {uri}")
        return record(Path("models") / uri[8:])

    def translation(element):
        pose = element.find("pose")
        if pose is None:
            return np.zeros(3)
        values = vector(pose, 6)
        if pose.attrib or any(values[3:]):
            raise ValueError("unsupported SDF rotation or pose frame for audited geometry")
        return values[:3]

    ground = terrain.find("link/collision[@name='ground']")
    ground_link = next((link for link in terrain.findall("link") if ground in list(link)), None)
    if (ground is None or terrain.findtext("static") not in {"true", "1"}
            or any(np.any(translation(e)) for e in (terrain, ground_link, ground))):
        raise ValueError("ground mesh must be in the world frame")
    low, high, pad_ground = aggregate_dem(dem_path, scene["elevation"], mesh(ground))
    collision = np.full(low.shape, -np.inf)
    water_surface = np.full_like(collision, -np.inf)
    trees = np.full_like(collision, -np.inf)
    counts = dict(boxes=0, building_triangles=0, canopy_triangles=0, water_triangles=0)
    handled = {ground}
    pad = None
    for model in world.findall("model"):
        if model.find("placement_frame") is not None or model.get("placement_frame"):
            raise ValueError("unsupported placed model frame")
        for link in model.findall("link"):
            for element in link.findall("collision"):
                if element is ground:
                    continue
                handled.add(element)
                if model.findtext("static") not in {"true", "1"}:
                    raise ValueError(f"collision in a non-static model: {model.get('name')}")
                centre = translation(model) + translation(link) + translation(element)
                geometry = element.find("geometry")
                box = element.find("geometry/box/size")
                if box is not None and len(geometry) == 1:
                    half = vector(box, 3) / 2
                    if not (half > 0).all():
                        raise ValueError("SDF box has nonpositive size")
                    if model.get("name") == "rw_pad":
                        if (pad is not None or not np.allclose(centre[:2], 0, atol=0.01)
                                or np.min(half[:2]) < 5 or abs(centre[2] + half[2]) > 0.01):
                            raise ValueError("expected a level central takeoff pad at ENU z=0")
                        pad = element
                    else:
                        paint(collision, (centre - half)[None, :2], (centre + half)[None, :2],
                              np.array([centre[2] + half[2]]))
                        counts["boxes"] += 1
                elif model is terrain and not centre.any() and len(geometry) == 1 and geometry[0].tag == "mesh":
                    counts["building_triangles"] += paint_mesh(collision, mesh(element))
                else:
                    raise ValueError(f"unhandled collision in {model.get('name')}")
    if set(world.findall(".//collision")) != handled or pad is None:
        raise ValueError("world lacks the central takeoff pad or contains unsupported nested collisions")

    species = tuple({f"inst_{tree['cat']}_" for tree in scene["trees"]})
    for link in terrain.findall("link"):
        for visual in link.findall("visual"):
            name = Path(visual.findtext("geometry/mesh/uri", "")).name
            if name == "water.obj" or name.startswith(species):
                if any(np.any(translation(e)) for e in (terrain, link, visual)):
                    raise ValueError(f"unexpected visual pose for {name}")
                if name == "water.obj":
                    counts["water_triangles"] += paint_mesh(water_surface, mesh(visual))
                else:
                    counts["canopy_triangles"] += paint_mesh(trees, mesh(visual))
    if (scene["stats"]["trees"] and not counts["canopy_triangles"]
            or scene["stats"]["buildings"] and not counts["building_triangles"]
            or scene["water_areas"] and not counts["water_triangles"]):
        raise ValueError(f"missing geometry reported by the scene: {counts}")
    water, counts["water_features"] = water_mask(scene, dem_path.parent)
    water |= np.isfinite(water_surface)
    validate_pad(pad_ground, collision, trees, water)
    return scene, dem_path, dict(ground_low=low, ground_high=high, buildings_and_boxes=collision,
                                canopy=trees, water_surface=water_surface, water=water), counts, inputs


def straight(start, end):
    count = max(1, math.ceil(np.linalg.norm(end - start) / STEP))
    return np.linspace(start, end, count + 1)


def rounded_path(points, radius):
    """Yield sampled line / tangent arc subpaths with waypoint altitude indices."""
    directions = np.diff(points, axis=0)
    lengths = np.linalg.norm(directions, axis=1)
    if np.any(lengths < 1):
        raise ValueError("coincident route points")
    directions /= lengths[:, None]
    entries, exits = points.copy(), points.copy()
    arcs = {}
    for i in range(1, len(points) - 1):
        before, after = directions[i - 1], directions[i]
        angle = math.atan2(before[0] * after[1] - before[1] * after[0], before @ after)
        if abs(angle) < 1e-7:
            continue
        if abs(angle) >= math.pi - 1e-7:
            raise ValueError("180-degree pivot requires intermediate approach and exit legs")
        lead = radius * math.tan(abs(angle) / 2)
        entries[i], exits[i] = points[i] - lead * before, points[i] + lead * after
        centre = entries[i] + math.copysign(radius, angle) * np.array([-before[1], before[0]])
        start = math.atan2(*(entries[i] - centre)[::-1])
        angles = np.linspace(start, start + angle, math.ceil(radius * abs(angle) / STEP) + 1)
        arcs[i] = centre + radius * np.column_stack((np.cos(angles), np.sin(angles)))
    for i, direction in enumerate(directions):
        if (entries[i + 1] - exits[i]) @ direction < -1e-6:
            raise ValueError(f"{radius} m turns overlap on segment {i + 1}")
        yield straight(exits[i], entries[i + 1]), (i, i + 1)
        if i + 1 in arcs:
            yield arcs[i + 1], (i, i + 1, i + 2)


def corridor_cells(path, radius):
    """Cover a sampled buffer including full cells and the distance between samples."""
    reach = radius + CELL / math.sqrt(2) + STEP / 2
    spread = math.ceil(reach / CELL)
    offsets = np.arange(-spread, spread + 1)
    x = np.floor((path[:, 0] + HALF) / CELL).astype(int)[:, None, None] + offsets[None, :, None]
    y = np.floor((path[:, 1] + HALF) / CELL).astype(int)[:, None, None] + offsets[None, None, :]
    mask = ((x + 0.5) * CELL - HALF - path[:, 0, None, None])**2 + (
        (y + 0.5) * CELL - HALF - path[:, 1, None, None])**2 <= reach**2
    shape = mask.shape
    return (np.clip(np.broadcast_to(y, shape)[mask], 0, COUNT - 1),
            np.clip(np.broadcast_to(x, shape)[mask], 0, COUNT - 1))


def validate_pad(ground, collision, trees, water):
    origin = np.zeros((1, 2))
    home = corridor_cells(origin, 5)
    y, x = home
    # Only this vertical check uses the flattened physical ground. The route
    # envelope still includes both the raw elevations and collision geometry.
    corners = np.concatenate([ground[y + j, x + i] for j in (0, 1) for i in (0, 1)])
    if (max(collision[home].max(), trees[home].max()) >= 0
            or corners.max() > 0.40 or corners.min() < -3):
        raise PadRejected("central vertical launch/recovery volume is obstructed or pad terrain is inconsistent")
    if water[corridor_cells(origin, 40)].any():
        raise PadRejected("water at home in the route geometry")


def bounds(path, layers, radius):
    cells = corridor_cells(path, radius)
    surfaces = {key: float(layers[key][cells].max()) for key in
                ("ground_high", "buildings_and_boxes", "canopy", "water_surface")}
    return (float(layers["ground_low"][cells].min()), surfaces,
            bool(layers["water"][cells].any()))


def violation_lengths(routes, layers, heights, radius_slack):
    """Measure flagged subsegment lengths, not entire failing waypoint legs."""
    top = np.maximum.reduce([layers[k] for k in ("ground_high", "buildings_and_boxes", "canopy", "water_surface")])
    result = {}
    for radius, chunks in routes.items():
        reach = BUFFER + (radius_slack if radius else 0) + CELL / math.sqrt(2) + STEP / 2
        offsets = np.arange(-math.ceil(reach / CELL), math.ceil(reach / CELL) + 1)
        totals = np.zeros(4)
        for path, indices in chunks:
            segments = np.linalg.norm(np.diff(path, axis=0), axis=1)
            z = heights[list(indices)]
            midpoints = (path[:-1] + path[1:]) / 2
            for start in range(0, len(midpoints), 128):
                xy = midpoints[start:start + 128, None, None, :]
                ix = np.floor((xy + HALF) / CELL).astype(int)
                x = ix[..., 0] + offsets[None, :, None]
                y = ix[..., 1] + offsets[None, None, :]
                mask = ((x + 0.5) * CELL - HALF - xy[..., 0])**2 + (
                    (y + 0.5) * CELL - HALF - xy[..., 1])**2 <= reach**2
                cells = np.clip(y, 0, COUNT - 1), np.clip(x, 0, COUNT - 1)
                weights = segments[start:start + 128]
                ground = layers["ground_high"][cells]
                for i, bad in enumerate((ground > z.min() - MIN_AGL, ground > z.min() - 30,
                                         layers["ground_low"][cells] < z.max() - MAX_AGL,
                                         top[cells] > z.min() - CLEARANCE)):
                    totals[i] += weights[(mask & bad).any(axis=(1, 2))].sum()
        result[str(radius)] = dict(zip(("agl_below_m", "agl_below_30m_m", "agl_above_m", "clearance_below_m"),
                                       totals.tolist()))
    return result


def terrain_altitudes(lower, upper, lengths, rates):
    """Find a rate-limited altitude chain or return an auditable rejected chain.

    Start from a terrain-following preference within the vertical corridor.
    Two sweeps compute the least majorant under the asymmetric slope limits.
    If that exceeds a ceiling, retry from the actual floors: any feasible
    solution must dominate the least majorant of those floors.
    """
    travel = np.maximum(0, lengths - 2 * ARRIVAL) / CRUISE_SPEED
    travel[-1] = 0  # Tie back-transition and hover-return altitudes.
    climb, descent = rates

    def propagate(profile):
        for i, dt in enumerate(travel):
            profile[i + 1] = max(profile[i + 1], profile[i] - descent * dt)
        for i in range(len(travel) - 1, -1, -1):
            profile[i] = max(profile[i], profile[i + 1] - climb * travel[i])
        return profile

    profile = propagate(np.maximum(lower, np.minimum(lower + 8, (lower + upper) / 2)))
    if np.any(profile > upper):
        profile = propagate(lower.copy())
    return profile


def evaluate(points, layers, heights=None, *, terrain_rates=None, diagnostics=False,
             threshold_m=DEPARTURE, agl_band=(MIN_AGL, MAX_AGL), transition_min_agl=None,
             bounded_pass=False):
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 4 or not np.isfinite(points).all():
        raise ValueError("expected finite ENU route coordinates")
    if not np.array_equal(points[0], points[-1]) or np.linalg.norm(points[0]) > 1e-8:
        raise ValueError("route must start and return at the centre home")
    # Also test the commanded chords, in case the real fly-through guidance cuts less.
    routes = {0: [(straight(a, b), (i, i + 1)) for i, (a, b) in enumerate(zip(points, points[1:]))]}
    for radius in RADII:
        routes[radius] = list(rounded_path(points, radius))
    directions = np.diff(points, axis=0)
    lengths = np.linalg.norm(directions, axis=1)
    directions /= lengths[:, None]
    max_angle = np.arccos(np.clip(np.sum(directions[:-1] * directions[1:], axis=1), -1, 1)).max()
    # Within a radius interval, the tangent points and every fixed-angle point
    # on an arc move by at most delta_radius * tan(turn_angle / 2).
    radius_slack = max(np.diff(RADII)) / 2 * math.tan(max_angle / 2)
    edge = min(HALF - np.abs(path).max() - BUFFER - (radius_slack if radius else 0) - STEP / 2
               for radius, chunks in routes.items() for path, _ in chunks)
    continuous_max = {str(radius): max(float(np.linalg.norm(path, axis=1).max()) for path, _ in chunks)
                      for radius, chunks in routes.items()}
    min_agl, max_agl_limit = agl_band
    lower = np.full(len(points), 5.0 if terrain_rates is not None else 0.0)
    upper = np.full(len(points), MAX_ALTITUDE, dtype=float)
    stats = []
    for radius, chunks in routes.items():
        for path, indices in chunks:
            low, surfaces, over_water = bounds(path, layers, BUFFER + (radius_slack if radius else 0))
            floor = max(surfaces["ground_high"] + (min_agl if terrain_rates is not None else 30),
                        max(surfaces.values()) + CLEARANCE)
            # The forward transition and first target need a larger reserve for sink.
            if transition_min_agl is not None and indices[0] == 0:
                for i in indices:
                    lower[i] = max(lower[i], surfaces["ground_high"] + transition_min_agl)
            for i in indices:
                lower[i] = max(lower[i], floor)
                upper[i] = min(upper[i], low + max_agl_limit)
            stats.append((indices, low, surfaces, over_water,
                          bool(layers["water"][corridor_cells(path, 0)].any())))
    # Hover-return uses the final FW altitude, and transition begins 40–90 m above home.
    lower[-2:] = max(lower[-2:])
    upper[-2:] = min(upper[-2:])
    lower[0], upper[0] = max(lower[0], 40), min(upper[0], 90)
    if transition_min_agl is not None:
        lower[:2] = max(74.0, *lower[:2])
        upper[:2] = min(upper[:2])
    if heights is None and terrain_rates is not None:
        heights = terrain_altitudes(lower, upper, lengths, terrain_rates)
    elif heights is None:
        # Select smooth plateaus where possible, keeping spare margin above and below.
        heights = np.minimum(np.maximum(85.0, np.ceil(lower + 5)), np.floor(upper - 5))
        heights[-1] = heights[-2]
    heights = np.asarray(heights)
    if heights.shape != (len(points),) or not np.isfinite(heights).all():
        raise ValueError("expected finite altitudes for each route point")
    min_crest = {key: min((min(heights[list(ix)]) - surfaces[key]) for ix, _, surfaces, _, _ in stats)
                 for key in stats[0][2]}
    max_agl = max(max(heights[list(ix)]) - low for ix, low, _, _, _ in stats)
    far = float(np.linalg.norm(points, axis=1).max())
    arrival_extent = policy.fw_capture_witness_m(policy.FLIGHT_MARGIN_REVISION) if bounded_pass else ARRIVAL
    distance_witness = min(far - arrival_extent, min(continuous_max.values()) - radius_slack)
    max_capture_offset = max(
        np.linalg.norm(wp - np.concatenate([path for path, _ in chunks]), axis=1).min()
        for radius, chunks in routes.items() if radius for wp in points[1:-1]
    ) + radius_slack
    radius100 = routes[100]
    path_length = sum(np.linalg.norm(np.diff(path, axis=0), axis=1).sum() for path, _ in radius100)
    time = (path_length - np.linalg.norm(points[-2])) / CRUISE_SPEED + np.linalg.norm(points[-2]) / 8
    failures = []
    for reason, bad in (
        ("departure", distance_witness < threshold_m + 5),
        ("FW handoff buffer", bounded_pass and BUFFER < policy.FW_PASS_BUFFER_M), ("boundary", edge < EDGE),
        ("fly-through capture", max_capture_offset > ARRIVAL),
        ("ceiling/transition", heights[0] < 40 or heights[0] > 90 or heights.max() > MAX_ALTITUDE),
        ("AGL", min_crest["ground_high"] < min_agl - 1e-9 or max_agl > max_agl_limit + 1e-9),
        ("clearance", min(min_crest.values()) < CLEARANCE),
        ("water at home", layers["water"][corridor_cells(points[:1], 40)].any()),
    ):
        if bad:
            failures.append(reason)
    extra = {}
    if terrain_rates is not None:
        travel = np.maximum(0, lengths[:-1] - 2 * ARRIVAL) / CRUISE_SPEED
        change = np.diff(heights)[:-1]
        # Equivalent rate over the conservative interval between capture disks.
        rates = np.divide(change, travel, out=np.zeros_like(change), where=travel > 0)
        climb, descent = float(max(0, rates.max())), float(max(0, -rates.min()))
        if (np.any((travel == 0) & (change != 0)) or climb > terrain_rates[0] + 1e-9
                or descent > terrain_rates[1] + 1e-9):
            failures.append("vertical rate")
        if heights.min() < 5 or heights[-1] != heights[-2]:
            failures.append("relative altitude/hover return")
        if not 250 <= np.linalg.norm(points[-2]) <= 650:
            failures.append("back-transition distance")
        extra.update(maximum_commanded_climb_mps=climb, maximum_commanded_descent_mps=descent,
                     vertical_rate_limits_mps=list(terrain_rates))
    if transition_min_agl is not None:
        first_agl = min(min(heights[list(ix)]) - surfaces["ground_high"]
                        for ix, _, surfaces, _, _ in stats if ix[0] == 0)
        if first_agl < transition_min_agl or heights[0] != heights[1]:
            failures.append("forward transition altitude reserve")
        extra.update(transition_minimum_agl_m=float(first_agl), planned_agl_limits_m=list(agl_band))
    if diagnostics:
        extra["violations_by_turn_radius_m"] = violation_lengths(routes, layers, heights, radius_slack)
    return {"passed": not failures, "failures": failures, "buffer_m": BUFFER,
            "radii_m": RADII, "capture_radius_m": ARRIVAL, "planned_max_distance_m": far, "max_departure_witness_m": distance_witness,
            "rounded_max_distance_m": continuous_max, "minimum_boundary_margin_m": float(edge),
            "maximum_modeled_capture_offset_m": float(max_capture_offset),
            "intermediate_radius_allowance_m": float(radius_slack),
            "minimum_vertical_clearance_m": float(min(min_crest.values())),
            "layer_clearance_m": {k: float(v) if math.isfinite(v) else None for k, v in min_crest.items()},
            "agl_m": [float(min_crest["ground_high"]), float(max_agl)],
            "route_length_m": float(path_length), "estimated_cruise_and_hover_return_s": float(time),
            "water_in_buffer": any(s[3] for s in stats), "water_near_centerline": any(s[4] for s in stats),
            "altitude_bounds_by_waypoint_m": np.column_stack((lower, upper)).round(2).tolist(),
            "waypoints_enu_m": np.column_stack((points, heights)).round(6).tolist(), **extra}


def corner_route(signs, direction, offset=0):
    """Tangent waypoint chain for a 180 degree U turn at the far corner."""
    diagonal = np.asarray(signs) / math.sqrt(2)
    lateral = direction * np.array([-diagonal[1], diagonal[0]])
    centre = diagonal * ((2879 - 120) * math.sqrt(2))
    # Six 30-degree corners. The intersections lie on the outer tangents.
    arc = [centre + (120 / math.cos(math.pi / 12)) * (
        diagonal * math.sin(angle) - lateral * math.cos(angle))
        for angle in np.linspace(math.pi / 12, 11 * math.pi / 12, 6)]
    out = [diagonal * along + lateral * (-120 + offset * weight)
           for along, weight in ((550, 0.5), (1300, 1), (2100, 1), (2900, 0.5), (3500, 0))]
    back = [diagonal * along + lateral * (120 + offset * weight)
            for along, weight in ((3500, 0), (2900, 0.5), (2100, 1), (1300, 1), (550, 0.5))]
    return np.array([(0, 0), *out, *arc, *back, (0, 0)], dtype=float)


def amsterdam_route(signs, direction):
    """Start turning earlier and spread the U-turn over 15-degree course changes."""
    diagonal = np.asarray(signs) / math.sqrt(2)
    lateral = direction * np.array([-diagonal[1], diagonal[0]])
    radius = 120.1
    half_turn = math.pi / 24
    angles = np.linspace(half_turn, math.pi - half_turn, 12)
    turn = radius / math.cos(half_turn) * (np.sin(angles)[:, None] * diagonal
                                          - np.cos(angles)[:, None] * lateral)
    nose = turn[len(turn) // 2]
    # Reserve the full arrival-plane offset and at least 5 m past the gate.
    centre = math.sqrt(4032.0**2 - (nose @ lateral)**2) - nose @ diagonal
    along = (550, 1300, 2100, 2900, centre - 240)
    out = [distance * diagonal - radius * lateral for distance in along]
    back = [distance * diagonal + radius * lateral for distance in reversed(along)]
    return np.array([(0, 0), *out, *(centre * diagonal + turn), *back, (0, 0)])


def departure_certificate(layers):
    """Necessary conditions only, independent of the route search.

    For any path point inside a cell, the terrain crest in every inner-disk
    cell is within BUFFER (allow half a cell diagonal for each endpoint).
    Omit the upper AGL, path connectivity, turn and capture constraints here.
    Thus excluded cells cannot contain even a single admissible route point.
    """
    reach = BUFFER - CELL * math.sqrt(2)
    crest = layers["ground_high"]
    pad = math.floor(reach / CELL)
    ground = np.pad(crest, pad, constant_values=-np.inf)
    high = crest.copy()
    for dy in range(-pad, pad + 1):
        span = math.floor(math.sqrt(reach**2 - (CELL * dy)**2) / CELL)
        for dx in range(-span, span + 1):
            np.maximum(high, ground[pad + dy:pad + dy + COUNT, pad + dx:pad + dx + COUNT], out=high)
    axis = (np.arange(COUNT) + 0.5) * CELL - HALF
    # Upper bound for the distance of any point in a cell; the inner cell
    # nearest the edge can straddle the 110 m boundary exclusion line.
    outer = np.minimum(np.abs(axis) + CELL / 2, HALF - BUFFER - EDGE)
    eligible = (np.abs(axis) - CELL / 2) <= HALF - BUFFER - EDGE
    in_world = eligible[:, None] & eligible
    distance = np.hypot(outer[:, None], outer)
    max_distance = {str(agl): float(np.max(distance, where=in_world & (high + agl <= MAX_ALTITUDE), initial=0))
                    for agl in (30, MIN_AGL)}
    far = in_world & (distance >= DEPARTURE)
    best_crest = float(np.min(high[far]))
    return {"maximum_possible_distance_m": max_distance,
            "best_possible_minimum_agl_at_gate_m": MAX_ALTITUDE - best_crest,
            "best_possible_minimum_clearance_at_gate_m": MAX_ALTITUDE - best_crest}


def valley_route(centreline, turn_radius, direction, *, turn_segments=6):
    """Offset a valley axis to two passes and join them on tangent intersections."""
    tangents = np.diff(centreline, axis=0)
    tangents /= np.linalg.norm(tangents, axis=1)[:, None]
    tangents = np.concatenate((tangents[:1], tangents[:-1] + tangents[1:], tangents[-1:]))
    tangents /= np.linalg.norm(tangents, axis=1)[:, None]
    lateral = direction * np.column_stack((-tangents[:, 1], tangents[:, 0]))
    offsets = turn_radius * lateral
    half_turn = math.pi / (2 * turn_segments)
    angles = np.linspace(half_turn, math.pi - half_turn, turn_segments)
    arc = centreline[-1] + turn_radius / math.cos(half_turn) * (
        np.sin(angles)[:, None] * tangents[-1] - np.cos(angles)[:, None] * lateral[-1])
    # The first back-pass waypoint is inside the required back-transition ring.
    out = centreline - offsets
    back = centreline + offsets
    out[0] = 0
    back[0] = 0

    def densify(pass_points):
        return np.concatenate([np.linspace(a, b, max(1, math.ceil(math.dist(a, b) / 140)), endpoint=False)
                               for a, b in zip(pass_points, pass_points[1:])])

    # Do not densify the hover return or create a short leg beyond the tangent point.
    return np.concatenate((densify(out), arc, densify(back[1:][::-1])[1:], back[1::-1]))


def valley_candidates(*, bounded_pass=False):
    """Survey the NW valley from the stated pad, varying width and turnaround."""
    # ENU survey axis through the low valley in this particular 6 km DEM.
    axis = np.array([(0, 0), (-440, 175), (-1000, 490), (-1670, 860), (-2140, 1260),
                     (-2380, 1470), (-2650, 1670), (-2750, 1810)], dtype=float)
    for terminal in range(len(axis), 3, -1):
        fractions = (1.0, 0.5) if terminal > 4 else (1.0,)
        for fraction, radius, shift, direction in itertools.product(fractions, (130, 140), (0, -70, 70, -10), (1, -1)):
            centreline = axis[:terminal].copy()
            centreline[-1] = centreline[-2] + fraction * (centreline[-1] - centreline[-2])
            centreline[2:, 1] += shift
            name = f"valley/{terminal}/{fraction:g}/{radius}m/{shift:+d}m/{direction:+d}"
            if bounded_pass:
                yield name + "/8-turn", valley_route(centreline, radius, direction, turn_segments=8)
            else:
                yield name, valley_route(centreline, radius, direction)


def is_alpine(scene):
    return (scene.get("site") == ALPINE_SITE
            and math.dist((scene["lat"], scene["lon"]), ALPINE_ORIGIN) <= 1e-8)


def corner_candidates():
    for offset, (name, signs), direction in itertools.product((0, 220, -220), QUADRANTS.items(), (1, -1)):
        yield f"{name}/{direction:+d}/{offset:+d}m", corner_route(signs, direction, offset)


def survey_valley(layers, *, threshold_m=DEPARTURE, candidate=None, flight_margin=False):
    profile = read_json(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    rates = tuple(min(2.5, profile[f"max_{direction}_rate_mps"]) for direction in ("climb", "descent"))
    options = {}
    if flight_margin:
        rates = (min(rates[0], 2.0), min(rates[1], 1.5))
        options = {"agl_band": policy.ALPINE_PLANNED_AGL_M,
                   "transition_min_agl": policy.ALPINE_TRANSITION_MIN_AGL_M, "bounded_pass": True}
    for name, points in valley_candidates(bounded_pass=flight_margin):
        if candidate is not None and name != candidate:
            continue
        try:
            yield {**evaluate(points, layers, terrain_rates=rates, threshold_m=threshold_m, **options), "candidate": name}
        except ValueError as exc:
            yield {"candidate": name, "passed": False, "failures": [str(exc)]}


def route_diagnostics(asset, world):
    """Survey old and valley paths without staging or publishing a mission."""
    scene, _, layers, counts, inputs = load_layers(asset, world)
    old = []
    for name, points in corner_candidates():
        m = evaluate(points, layers, diagnostics=True)
        old.append({"candidate": name, **m, "maximum_agl_deficit_m": max(0, MIN_AGL - m["agl_m"][0]),
                    "maximum_agl_deficit_below_30m_m": max(0, 30 - m["agl_m"][0]),
                    "maximum_agl_excess_m": max(0, m["agl_m"][1] - MAX_AGL),
                    "maximum_clearance_deficit_m": max(0, CLEARANCE - m["minimum_vertical_clearance_m"])})
    result = {"schema_version": 1, "world_sha256": file_sha256(world), "geometry_inputs": inputs,
              "origin": [scene["lat"], scene["lon"], scene["elevation"]], "geometry_counts": counts,
              "legacy_thresholds_m": {"agl_min": MIN_AGL, "agl_max": MAX_AGL, "clearance_min": CLEARANCE},
              "legacy_candidates": old}
    if is_alpine(scene):
        valley = list(survey_valley(layers))
        best = max((m for m in valley if m["failures"] in ([], ["departure"])),
                   key=lambda m: (m["max_departure_witness_m"], m["minimum_vertical_clearance_m"]), default=None)
        result.update(departure_certificate(layers), valley_candidates=valley,
                      best_valley_without_distance_gate=best)
    return result


TEMPLATE = "rw_farm_iowa_w6km__clear_noon"
TEMPLATE_DIR = ROOT / "configs/scenarios/realworld" / AIRFRAME


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def canonical_hash(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def include_runtime_dem(layers, dem):
    """Union the runtime bilinear DEM and fine terrain bounds in each audit cell."""
    spacing = dem["east_spacing_m"]
    grid = np.asarray(dem["elevation_m"]) - dem["origin"]["elevation_m"]
    left = np.arange(COUNT) * CELL
    right = left + CELL
    # A bilinear patch has extrema at its corners. Include every coarse grid
    # knot within an audit cell as well as the audit cell's own edges.
    samples = [left, np.maximum(left, right - right % spacing), right]
    coords = [(np.minimum(x // spacing, len(grid) - 2), x / spacing) for x in samples]
    low = layers["ground_low"].copy()
    high = layers["ground_high"].copy()
    for y, yp in coords:
        v = (yp - y)[:, None]
        y = y[:, None]
        for x, xp in coords:
            u = xp - x
            z = ((1 - v) * ((1 - u) * grid[y, x] + u * grid[y, x + 1])
                 + v * ((1 - u) * grid[y + 1, x] + u * grid[y + 1, x + 1]))
            np.minimum(low, z, out=low)
            np.maximum(high, z, out=high)
    return {**layers, "ground_low": low, "ground_high": high}


def write_scenario(directory, world_id, scene, dem_path):
    manifest = {"schema_version": 1, "scenario_version": 1, "scenario_id": world_id, "channels": {}}
    for key in ("runtime_input", "truth", "expectations"):
        body = read_json(TEMPLATE_DIR / f"{TEMPLATE}.{key}.json")
        body["scenario_id"] = world_id
        if key == "runtime_input":
            body["vehicle"]["spawn_id"] = f"{world_id}_vtol"
            body["world"].update(world_asset=f"builtin://gazebo/{world_id}",
                                 map_asset=f"builtin://maps/{world_id}.geojson",
                                 dem_asset=f"builtin://terrain/{world_id}.dem.json")
            for sensor in body["sensors"]:
                sensor["stream_uri"] = sensor["stream_uri"].replace(f"gazebo://{TEMPLATE}/", f"gazebo://{world_id}/")
        path = directory / f"{world_id}.{key}.json"
        write_json(path, body)
        manifest["channels"][key] = {"path": path.name, "sha256": canonical_hash(body)}
    scenario = directory / f"{world_id}.scenario.json"
    write_json(scenario, manifest)

    lat, lon = scene["lat"], scene["lon"]
    dem = np.load(dem_path, mmap_mode="r")
    spacing = 50
    idx = np.linspace(0, len(dem) - 1, int(2 * HALF / spacing) + 1).round().astype(int)
    grid = dem[np.ix_(idx, idx)]
    write_json(directory / f"{world_id}.dem.json", {
        "schema_version": 1, "scenario_id": world_id, "coordinate_reference": "WGS84", "vertical_datum": "MSL",
        "origin": {"latitude_deg": lat, "longitude_deg": lon, "elevation_m": round(scene["elevation"], 2)},
        "north_origin_m": -HALF, "east_origin_m": -HALF, "north_spacing_m": spacing, "east_spacing_m": spacing,
        "vertical_sigma_m": 1.0, "slope_sigma_m_per_m": 0.02, "elevation_m": grid.astype(float).round(2).tolist(),
    })
    dlat = math.degrees(HALF / 6_378_137.0)
    dlon = dlat / math.cos(math.radians(lat))
    ring = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]
    write_json(directory / f"{world_id}.geojson", {
        "type": "FeatureCollection", "schema_version": 1, "scenario_id": world_id, "coordinate_reference": "WGS84",
        "features": [{"type": "Feature", "id": "operating-area",
                      "properties": {"role": "operating_area", "surface": "real_terrain",
                                     "clearance_radius_m": HALF, "extent_m": 2 * HALF},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}}],
    })
    return scenario


def write_route(scenario, world_id, scene, metrics, inputs, hashes, *, route_policy=None):
    body = read_json(TEMPLATE_DIR / f"{TEMPLATE}.long_range.json")
    xy = np.asarray(metrics["waypoints_enu_m"])
    origin = {"latitude_deg": scene["lat"], "longitude_deg": scene["lon"]}

    def point(east, north, up):
        return {"latitude_deg": scene["lat"] + math.degrees(north / 6_378_137.0),
                "longitude_deg": scene["lon"] + math.degrees(east / (6_378_137.0 * math.cos(math.radians(scene["lat"])))),
                "altitude_m": up, "altitude_reference": "relative_home"}

    waypoints = [point(*row) for row in xy[1:-1]]
    landing = point(0, 0, 0)
    body.update(scenario_id=world_id, profile=world_id, output_stem=f"{world_id}__long-range__rtf90")
    if route_policy is not None:
        body.update(route_policy, minimum_max_distance_m=route_policy["threshold_m"])
    runtime = body["mission_compile_runtime"]
    runtime["home_position"] = origin
    runtime["safety_policy"]["geofence_center"] = origin
    runtime["mission_geometry"].update(waypoints=waypoints, landing=landing,
                                        takeoff_altitude_m=float(xy[0, 2]), acceptance_radius_m=ARRIVAL)
    task = body["task"]
    task["task_id"] = f"{world_id}-long-range"
    task["geometry"].update(centerline=waypoints, length_m=float(np.linalg.norm(np.diff(xy[1:-1, :2], axis=0), axis=1).sum()))
    body["context"].update(home_position=origin, aircraft_position=origin)
    body["route_preflight"] = {"schema_version": 1, "fly_through_radius_m": ARRIVAL,
                               "turn_radius_m": RADII[1], "buffer_m": BUFFER, "assets": hashes,
                               "geometry_inputs": inputs, "metrics": metrics}
    path = scenario.with_name(f"{world_id}.long_range.json")
    write_json(path, body)

    manifest = read_json(scenario)
    runtime_path = scenario.parent / manifest["channels"]["runtime_input"]["path"]
    runtime_doc = read_json(runtime_path)
    runtime_doc["vehicle"]["spawn_pose"]["yaw_rad"] = math.atan2(xy[1, 1], xy[1, 0])
    write_json(runtime_path, runtime_doc)
    manifest["channels"]["runtime_input"]["sha256"] = canonical_hash(runtime_doc)
    write_json(scenario, manifest)
    return path


def cpu_plan(scenario, staged_world, model_root):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MULTIDETECT_")}
    env.update(MULTIDETECT_RTF_WORLD_ROOT=str(staged_world.parent),
               MULTIDETECT_RTF_MAP_ROOT=str(scenario.parent), MULTIDETECT_RTF_TERRAIN_ROOT=str(scenario.parent),
               MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=str(model_root), MULTIDETECT_SITL_SLOTS="8")
    process = subprocess.run([str(RUNNER_PYTHON), "-B", str(ROOT / "scripts/realworld/long_range_flight.py"),
                              "5", str(scenario), "--airframe", AIRFRAME, "--gimbal-bridge-stabilize", "--plan-only"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    if process.returncode:
        raise ValueError(f"CPU plan failed: {process.stderr.strip() or process.stdout.strip()}")
    plan = json.loads(process.stdout)
    if (plan.get("mode") != "cpu_plan_only" or plan.get("flight_started") is not False
            or plan.get("airframe") != AIRFRAME or plan.get("fw_guidance") != "offboard_path"
            or plan.get("profile") != staged_world.stem or not plan.get("compiled_mission")
            or plan.get("collector_plan", {}).get("docker_command") is not None):
        raise ValueError("CPU runner did not confirm a compiled offline X1 fly-through plan")
    return plan


def publish(asset, scenario, sidecar, receipt):
    destination = asset / "x1"
    with tempfile.TemporaryDirectory(prefix=".x1-preflight-", dir=asset) as temporary:
        staged = Path(temporary)
        manifest = read_json(scenario)
        world_id = manifest["scenario_id"]
        paths = [scenario, sidecar, scenario.parent / f"{world_id}.geojson",
                 scenario.parent / f"{world_id}.dem.json",
                 *(scenario.parent / item["path"] for item in manifest["channels"].values())]
        for path in paths:
            shutil.copyfile(path, staged / path.name)
        write_json(staged / "preflight.json", receipt)
        for path in staged.iterdir():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if destination.exists() or destination.is_symlink():
            def unchanged(path):
                existing = destination / path.name
                if not existing.is_file() or existing.is_symlink():
                    return False
                if file_sha256(existing) == file_sha256(path):
                    return True
                # Reproducing a legacy 4 km bundle must not replace its receipt or change its identity.
                return (path.name == "preflight.json" and receipt["scenario_class"] == policy.WIDE_AREA
                        and read_json(existing) == {key: value for key, value in receipt.items()
                                                    if key not in {"scenario_class", "threshold_m"}})

            if (destination.is_symlink() or not destination.is_dir()
                    or not all(unchanged(path) for path in staged.iterdir())):
                raise ValueError(f"existing bundle differs: {destination}. Use a new asset version or move the old bundle aside.")
        else:
            staged.rename(destination)
            fd = os.open(asset, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return destination / "preflight.json"


def preflight(asset, world, *, bundle_root=None):
    asset = asset.resolve(strict=True)
    if bundle_root is not None:
        bundle_root = bundle_root.resolve(strict=True)
    world = world.absolute()
    world_id = world.stem
    if world.suffix != ".sdf" or not re.fullmatch(r"rw_[a-z0-9_]+_w6km__[a-z0-9_]+", world_id):
        raise ValueError("expected an rw_*_w6km__<variant>.sdf filename")
    scenario_class = policy.configured_class(world_id)
    threshold = policy.distance_gate(world_id, scenario_class)
    route_policy = {"scenario_class": scenario_class, "threshold_m": threshold}
    scratch = ROOT / ".tmp/x1-worlds"
    if not scratch.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("CPU plan staging must be inside the worktree")
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preflight-", dir=scratch) as temporary:
        stage = Path(temporary)
        staged_world = stage / world.name
        shutil.copyfile(world, staged_world)
        world_hash = file_sha256(staged_world)
        scene, dem_path, layers, counts, inputs = load_layers(asset, staged_world)
        alpine = is_alpine(scene)
        mountain_short = scenario_class == policy.MOUNTAIN_SHORT
        if mountain_short and not alpine:
            raise ValueError("mountain_short requires the surveyed Grindelwald origin")
        if alpine:
            certificate = departure_certificate(layers)
            limit = certificate["maximum_possible_distance_m"][str(MIN_AGL)]
            if limit < threshold:
                raise ValueError(f"no route passed geometry and CPU preflight: {MIN_AGL} m AGL and "
                                 f"{MAX_ALTITUDE} m relative ceiling bound departure to {limit:.1f} m "
                                 f"(< {threshold} m); use --diagnostics for the valley survey")
            if mountain_short:
                route_policy["wide_area_unreachable"] = {
                    **certificate, "threshold_m": DEPARTURE, "maximum_relative_home_altitude_m": MAX_ALTITUDE,
                    "buffer_m": BUFFER, "boundary_margin_m": EDGE,
                    "origin_wgs84": [scene["lat"], scene["lon"]],
                }
                policy.verify_route_policy(route_policy, world_id, scenario_class)
        scenario = write_scenario(stage, world_id, scene, dem_path)
        hashes = {"world_sha256": world_hash, "map_sha256": file_sha256(stage / f"{world_id}.geojson"),
                  "dem_sha256": file_sha256(stage / f"{world_id}.dem.json")}
        failures = []
        if mountain_short:
            runtime_dem = read_json(stage / f"{world_id}.dem.json")
            candidates = survey_valley(include_runtime_dem(layers, runtime_dem), threshold_m=threshold,
                                       candidate=policy.ALPINE_CANDIDATE, flight_margin=True)
        elif world_id == policy.AMSTERDAM_WORLD:
            points = amsterdam_route(QUADRANTS["NW"], 1)
            mission_radius = read_json(TEMPLATE_DIR / f"{TEMPLATE}.long_range.json")[
                "mission_compile_runtime"]["safety_policy"]["maximum_mission_radius_m"]
            metrics = evaluate(points, include_runtime_dem(layers, read_json(stage / f"{world_id}.dem.json")),
                               bounded_pass=True)
            runout = policy.fw_runout(points[:-1, ::-1], mission_radius, HALF,
                                      revision=policy.FLIGHT_MARGIN_REVISION)
            if not policy.has_runout_margin(runout):
                metrics["failures"].append("mission radius or world boundary runout")
                metrics["passed"] = False
            candidates = [{**metrics, "candidate": "NW/+1/bounded-pass", "fw_runout": runout}]
        else:
            candidates = (survey_valley(layers, threshold_m=threshold) if alpine else
                          ({**evaluate(points, layers), "candidate": name} for name, points in corner_candidates()))
        for metrics in candidates:
            candidate = metrics["candidate"]
            if not metrics["passed"]:
                failures.append(f"{candidate}: {', '.join(metrics['failures'])}")
                continue
            if mountain_short or world_id == policy.AMSTERDAM_WORLD:
                metrics["flight_margin_revision"] = policy.FLIGHT_MARGIN_REVISION
            metrics["geometry_counts"] = counts
            sidecar = write_route(scenario, world_id, scene, metrics, inputs, hashes,
                                  route_policy=route_policy if mountain_short else None)
            try:
                cpu_plan(scenario, staged_world, asset / "models")
            except ValueError as exc:
                failures.append(f"{candidate}: {exc}")
                continue
            if (file_sha256(world) != world_hash
                    or any(file_sha256(local_file(asset, path)) != digest for path, digest in inputs.items())):
                raise ValueError("world or geometric inputs changed during preflight")
            receipt = {"scenario": scenario.name, **route_policy, **hashes, "scenario_sha256": file_sha256(scenario),
                       "sidecar_sha256": file_sha256(sidecar), "passed": True}
            # Lock the asset directory inode so concurrent runs cannot publish different bundles.
            destination_root = bundle_root if bundle_root is not None else asset
            fd = os.open(destination_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return publish(destination_root, scenario, sidecar, receipt)
            finally:
                os.close(fd)
        raise ValueError("no route passed geometry and CPU preflight:\n" + "\n".join(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", required=True, type=Path, help="versioned asset directory with models, raw and scenes")
    parser.add_argument("--world", required=True, type=Path, help="flattened, local 6 km world SDF")
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--bundle-root", type=Path,
                         help="write x1/ to an existing staging directory instead of the asset")
    outputs.add_argument("--diagnostics", type=Path, metavar="JSON",
                        help="write a geometry-only survey, without staging or publishing a route")
    args = parser.parse_args(argv)
    try:
        if args.diagnostics is None:
            print(preflight(args.asset, args.world, bundle_root=args.bundle_root))
        else:
            report = route_diagnostics(args.asset.resolve(strict=True), args.world.absolute())
            with args.diagnostics.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n")
            print(args.diagnostics)
    except PadRejected as exc:
        parser.exit(PAD_REJECTED, f"X1 preflight rejected launch pad: {exc}\n")
    except (OSError, ValueError, TypeError, KeyError, ImportError, ET.ParseError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"X1 preflight failed: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

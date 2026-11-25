#!/usr/bin/env python3
"""Compare the deployed Iowa DEM, 3DEP export, and pad collision geometry offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from multidetect.terrain_dem import load_local_dem_grid
from scripts.realworld import x1_world_route as route

SITE = "rw_farm_iowa_w6km"
WORLD = f"{SITE}__clear_noon"
ROLES = {
    "raw_dem", "raw_provenance", "runtime_dem", "scenario", "runtime_input", "long_range",
    "mission_package", "scene", "world", "ground_collision", "building_collision",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_inputs(manifest):
    if manifest["schema_version"] != 1 or set(manifest["inputs"]) != ROLES:
        raise ValueError("expected the complete schema-v1 Iowa input manifest")
    paths, digests = {}, {}
    for role, entry in sorted(manifest["inputs"].items()):
        path = Path(entry["path"])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"expected an absolute local file for {role}")
        digest = sha256(path)
        if digest != entry["sha256"]:
            raise ValueError(f"digest mismatch for {role}")
        paths[role], digests[role] = path, digest
    return paths, digests


def bilinear(grid, rows, cols):
    """Sample same-shaped or broadcastable fractional indices on a closed grid."""
    rows, cols = np.asarray(rows), np.asarray(cols)
    if (not np.isfinite(rows).all() or not np.isfinite(cols).all()
            or (rows < 0).any() or (rows > grid.shape[0] - 1).any()
            or (cols < 0).any() or (cols > grid.shape[1] - 1).any()):
        raise ValueError("sample outside grid coverage")
    row = np.minimum(rows.astype(int), grid.shape[0] - 2)
    col = np.minimum(cols.astype(int), grid.shape[1] - 2)
    n, e = rows - row, cols - col
    z00 = grid[row, col].astype(float)
    z10 = grid[row + 1, col].astype(float)
    z01 = grid[row, col + 1].astype(float)
    z11 = grid[row + 1, col + 1].astype(float)
    return z00 + (z10 - z00) * n + (z01 - z00) * e + (z11 - z10 - z01 + z00) * n * e


def lattice_stats(raw, coarse, raw_axis, coarse_step, box):
    """Measure runtime minus raw on every export node in an inclusive rectangle."""
    n0, n1, e0, e1 = box
    if not (raw_axis[0] <= n0 <= n1 <= raw_axis[-1] and raw_axis[0] <= e0 <= e1 <= raw_axis[-1]):
        raise ValueError("diagnostic rectangle is outside the raw DEM")
    rows = np.flatnonzero((n0 <= raw_axis) & (raw_axis <= n1))
    cols = np.flatnonzero((e0 <= raw_axis) & (raw_axis <= e1))
    if not rows.size or not cols.size:
        raise ValueError("diagnostic rectangle has no raw DEM nodes")
    idx = (raw_axis - raw_axis[0]) / coarse_step
    low, high, max_abs, sum_sq, total = math.inf, -math.inf, -1.0, 0.0, 0.0
    worst = None
    for offset in range(0, len(rows), 128):
        part = rows[offset:offset + 128]
        delta = bilinear(coarse, idx[part, None], idx[None, cols]) - raw[np.ix_(part, cols)]
        low, high = min(low, float(delta.min())), max(high, float(delta.max()))
        total += float(delta.sum())
        sum_sq += float(np.square(delta).sum())
        index = np.unravel_index(np.abs(delta).argmax(), delta.shape)
        value = float(delta[index])
        if abs(value) > max_abs:
            max_abs = abs(value)
            worst = [float(raw_axis[part[index[0]]]), float(raw_axis[cols[index[1]]])]
    count = int(len(rows) * len(cols))
    return {"count": count, "box_ne_m": list(box), "min_m": low, "max_m": high,
            "mean_m": total / count, "rmse_m": math.sqrt(sum_sq / count),
            "max_abs_m": max_abs, "max_abs_at_ne_m": worst}


def closed_cells(axis, radius):
    return np.flatnonzero((axis[:-1] <= radius) & (axis[1:] >= -radius))


def height_range(values):
    low, high = float(values.min()), float(values.max())
    return {"min_m": low, "max_m": high, "spread_m": high - low}


def clipped_ground_range(ground, axis, cells, radius):
    """Evaluate each clipped mesh triangle at all vertices of its intersection."""
    values = []
    for row in cells:
        nlo, nhi = max(-radius, axis[row]), min(radius, axis[row + 1])
        dn = axis[row + 1] - axis[row]
        for col in cells:
            elo, ehi = max(-radius, axis[col]), min(radius, axis[col + 1])
            de = axis[col + 1] - axis[col]
            north = [(n - axis[row]) / dn for n in (nlo, nhi)]
            east = [(e - axis[col]) / de for e in (elo, ehi)]
            z00, z01 = ground[row, col:col + 2]
            z10, z11 = ground[row + 1, col:col + 2]
            for n in north:
                for e in east:
                    if n <= e:  # South/east triangle of the production OBJ.
                        values.append(z00 + e * (z01 - z00) + n * (z11 - z01))
                    else:
                        values.append(z00 + n * (z10 - z00) + e * (z11 - z10))
            for f in (max(north[0], east[0]), min(north[1], east[1])):
                if north[0] <= f <= north[1] and east[0] <= f <= east[1]:
                    values.append(z00 + f * (z11 - z00))
    return height_range(np.asarray(values))


def pad_neighborhood(raw, raw_axis, coarse, coarse_axis, ground, ground_axis, buildings, radius):
    """Use an enclosing square and full intersecting collision cells."""
    def breakpoints(axis):
        return np.r_[-radius, axis[(axis > -radius) & (axis < radius)], radius]

    raw_coords = (breakpoints(raw_axis) - raw_axis[0]) / (raw_axis[1] - raw_axis[0])
    coarse_coords = (breakpoints(coarse_axis) - coarse_axis[0]) / (coarse_axis[1] - coarse_axis[0])
    cells = closed_cells(ground_axis, radius)
    corners = np.unique(np.r_[cells, cells + 1])
    collision = buildings[np.ix_(cells, cells)]
    building_cells = np.isfinite(collision)
    return {
        "square_half_width_m": radius,
        "raw_bilinear_export_m": height_range(bilinear(raw, raw_coords[:, None], raw_coords[None, :])),
        "runtime_bilinear_msl_m": height_range(bilinear(coarse, coarse_coords[:, None], coarse_coords[None, :])),
        "ground_mesh_full_cell_vertices_enu_up_m": height_range(ground[np.ix_(corners, corners)]),
        "ground_mesh_clipped_square_enu_up_m": clipped_ground_range(ground, ground_axis, cells, radius),
        "building_aabb_cells_with_hits": int(building_cells.sum()),
        "building_aabb_cells_examined": int(collision.size),
        "building_aabb_top_enu_up_m": float(collision[building_cells].max()) if building_cells.any() else None,
    }


def world_geometry(paths, scene, dem):
    root = ET.parse(paths["world"]).getroot()
    if len(root.findall("world")) != 1:
        raise ValueError("expected one SDF world")
    world = root.find("world")
    if any(world.findall(f".//{tag}") for tag in ("heightmap", "include", "frame", "joint", "plugin")):
        raise ValueError("expected the flattened Iowa mesh world without heightmaps, includes or custom frames")
    origin = world.find("spherical_coordinates")
    expected = {"latitude_deg": dem.origin_latitude_deg, "longitude_deg": dem.origin_longitude_deg,
                "elevation": dem.origin_elevation_m}
    if (origin is None or origin.findtext("surface_model") != "EARTH_WGS84"
            or origin.findtext("world_frame_orientation") != "ENU"
            or float(origin.findtext("heading_deg", "0")) != 0
            or any(float(origin.findtext(k, "nan")) != v for k, v in expected.items())
            or (scene["lat"], scene["lon"], round(scene["elevation"], 2)) != tuple(expected.values())):
        raise ValueError("world, scene and DEM origins disagree")
    found = set()
    for model in world.findall("model"):
        for link in model.findall("link"):
            for collision in link.findall("collision"):
                key = (model.get("name"), collision.get("name"))
                if (model.findtext("static") not in {"true", "1"} or model.get("placement_frame")
                        or model.find("placement_frame") is not None):
                    raise ValueError("unsupported non-static or placed collision model")
                for node in (model, link, collision):
                    pose = node.find("pose")
                    offset_up = -0.5 if node is model and key == ("rw_pad", "c") else 0.0
                    actual = [0.0] * 6 if pose is None else list(map(float, pose.text.split()))
                    if (pose is not None and pose.attrib) or actual != [0, 0, offset_up, 0, 0, 0]:
                        raise ValueError(f"unexpected pose for collision {key}")
                geometry = collision.find("geometry")
                if key == ("rw_pad", "c"):
                    if len(geometry) != 1 or list(map(float, geometry.findtext("box/size").split())) != [40, 40, 1]:
                        raise ValueError("unexpected central pad geometry")
                elif key in {("rw_terrain", "ground"), ("rw_terrain", "buildings")}:
                    name = key[1]
                    role = "ground_collision" if name == "ground" else "building_collision"
                    uri = geometry.findtext("mesh/uri")
                    relative = f"rw_farm_iowa/meshes/{name}_collision.obj"
                    if ([node.tag for node in geometry] != ["mesh"]
                            or [node.tag for node in geometry[0]] != ["uri"] or uri != f"model://{relative}"
                            or paths[role].parts[-3:] != Path(relative).parts):
                        raise ValueError(f"unexpected mesh geometry or asset path for collision {key}")
                else:
                    raise ValueError(f"unsupported collision {key}")
                if key in found:
                    raise ValueError(f"duplicate collision {key}")
                found.add(key)
    if len(found) != 3 or len(world.findall(".//collision")) != len(found):
        raise ValueError("expected all three deployed collisions to be accounted for")
    if paths["ground_collision"].parent != paths["building_collision"].parent:
        raise ValueError("expected collisions from the same deployed model")
    vertices = route.ground_vertices(paths["ground_collision"])
    width = route.COUNT + 1
    axis = np.linspace(-route.HALF, route.HALF, width)
    if vertices.shape != (width**2, 3) or not np.isfinite(vertices).all():
        raise ValueError("invalid ground collision mesh vertices")
    mesh = vertices.reshape(width, width, 3)
    if not ((mesh[:, :, 0] == axis).all() and (mesh[:, :, 1] == axis[:, None]).all()):
        raise ValueError("invalid ground collision mesh axes")
    buildings = np.full((route.COUNT, route.COUNT), -np.inf)
    face_count = route.paint_mesh(buildings, paths["building_collision"])
    return axis, mesh[:, :, 2], buildings, face_count, expected


def diagnose(manifest):
    paths, digests = verified_inputs(manifest)
    dem = load_local_dem_grid(paths["runtime_dem"])
    if dem.source_sha256 != digests["runtime_dem"]:
        raise ValueError("runtime DEM changed during verification")
    provenance, scene, scenario, channel, sidecar, mission = (
        read_json(paths[key]) for key in ("raw_provenance", "scene", "scenario", "runtime_input", "long_range", "mission_package")
    )
    if not all(w == WORLD for w in (dem.scenario_id, scenario["scenario_id"], channel["scenario_id"],
                                    sidecar["scenario_id"], mission["x1_planning"]["world"], paths["world"].stem)):
        raise ValueError("scenario, sidecars, mission, DEM and world must belong to Clear Noon Iowa")
    channel_ref = scenario["channels"]["runtime_input"]
    if (channel_ref["path"] != paths["runtime_input"].name or channel_ref["sha256"] != route.canonical_hash(channel)
            or channel["world"]["dem_asset"] != f"builtin://terrain/{paths['runtime_dem'].name}"
            or channel["world"]["world_asset"] != f"builtin://gazebo/{WORLD}"
            or mission["x1_live_evidence"]["dem_sha256"] != digests["runtime_dem"]):
        raise ValueError("runtime input or mission DEM binding mismatch")
    spawn = channel["vehicle"]["spawn_pose"]
    if (mission["home"] != [0.0, 0.0] or [spawn[k] for k in ("y_north_m", "x_east_m")] != mission["home"]
            or scene["site"] != SITE or scene["size_m"] != 6000 or provenance["size_m"] != scene["size_m"]
            or (provenance["lat"], provenance["lon"]) != (scene["lat"], scene["lon"])):
        raise ValueError("only the pinned centred Iowa terrain layout is supported")
    raw_entries = [row for row in provenance["downloads"] if row["path"] == "dem.npy"]
    if (len(raw_entries) != 1 or raw_entries[0]["sha256"] != digests["raw_dem"]
            or raw_entries[0]["bytes"] != paths["raw_dem"].stat().st_size):
        raise ValueError("raw DEM does not match the download provenance")
    raw = np.load(paths["raw_dem"], mmap_mode="r", allow_pickle=False)
    raw_step = provenance["dem_res_m"]
    expected_proj4 = (f"+proj=tmerc +lat_0={scene['lat']} +lon_0={scene['lon']} "
                      "+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m")
    if provenance["proj4"] != expected_proj4 or provenance["sources"]["dem"]["name"] != "USGS 3DEP":
        raise ValueError("unexpected Iowa export source or horizontal projection")
    if (raw_step != 2 or raw.shape != (3001, 3001) or raw.dtype != np.float32 or not np.isfinite(raw).all()
            or raw.min() < -500 or raw.max() >= 1e6 or raw[1500, 1500] != scene["elevation"]):
        raise ValueError("raw DEM dimensions, samples or origin do not match the production export")
    if (dem.north_origin_m != -3000 or dem.east_origin_m != -3000
            or dem.north_spacing_m != 50 or dem.east_spacing_m != 50
            or dem.north_max_m != 3000 or dem.east_max_m != 3000):
        raise ValueError("unexpected runtime DEM layout")
    raw_axis = np.linspace(-3000, 3000, raw.shape[0])
    coarse_axis = np.linspace(-3000, 3000, dem.row_count)
    coarse = np.array(dem.elevations_m)
    raw_at_nodes = raw[::25, ::25].astype(float)
    if not np.array_equal(np.round(raw_at_nodes, 2), coarse):
        raise ValueError("runtime DEM is not the centimetre-rounded subset of this raw export")

    def at_point(name, north, east):
        export = float(bilinear(raw, (north + 3000) / raw_step, (east + 3000) / raw_step))
        runtime = dem.sample_local(north, east).elevation_m
        return {"point": name, "north_m": north, "east_m": east, "raw_export_m": export,
                "runtime_msl_m": runtime, "runtime_minus_raw_m": runtime - export}

    runtime = sidecar["mission_compile_runtime"]
    geometry = runtime["mission_geometry"]
    points = [("sidecar_home", runtime["home_position"])] + [
        (f"sidecar_waypoint_{index}", point) for index, point in enumerate(geometry["waypoints"], 1)
    ] + [("sidecar_landing", geometry["landing"])]
    points = [(name, *dem.local_from_wgs84(p["latitude_deg"], p["longitude_deg"])) for name, p in points]
    if points[0][1:] != tuple(mission["home"]) or points[-1][1:] != tuple(mission["home"]):
        raise ValueError("legacy sidecar home or landing is not the mission home")
    points += [(f"mission_place:{name}", *ne) for name, ne in sorted(mission["places"].items())]
    corridor = mission["x1_planning"]["corridor"]
    box = tuple(corridor[key] for key in ("north_min_m", "north_max_m", "east_min_m", "east_max_m"))
    axis, ground, buildings, face_count, world_origin = world_geometry(paths, scene, dem)
    point_residuals = [at_point(*point) for point in points]
    worst_sidecar = max(point_residuals[:len(geometry["waypoints"]) + 2], key=lambda p: abs(p["runtime_minus_raw_m"]))
    neighborhoods = [pad_neighborhood(raw, raw_axis, coarse, coarse_axis, ground, axis, buildings, radius)
                     for radius in (5, 20, 40)]
    pad_ground = neighborhoods[1]["ground_mesh_clipped_square_enu_up_m"]
    pad_surface = height_range(np.array([max(0, pad_ground["min_m"]), max(0, pad_ground["max_m"])]))
    return {
        "schema_version": 1, "kind": "iowa_site_terrain_diagnostic", "world": WORLD,
        "eligible_for_live_evidence_bounds": False, "supported_bounds": {},
        "inputs": manifest["inputs"],
        "datum": {"runtime_label": "MSL", "runtime_vertical_sigma_m": dem.vertical_sigma_m,
                  "export_source": provenance["sources"]["dem"], "export_fetched_utc": provenance["fetched_utc"],
                  "export_horizontal_proj4": provenance["proj4"], "source_vertical_crs_verified": False,
                  "world_origin": world_origin, "physical_ground": "triangulated ENU collision mesh"},
        "residuals": {
            "sampling_frame": "numeric local N/E alignment; sidecar WGS84 converted by runtime DEM",
            "runtime_nodes": {"count": int(coarse.size), "rounding_reproduced_at_every_node": True,
                              "max_abs_m": float(np.abs(coarse - raw_at_nodes).max())},
            "raw_lattice_full_site": lattice_stats(raw, coarse, raw_axis, 50, (-3000, 3000, -3000, 3000)),
            "raw_lattice_mission_corridor": lattice_stats(raw, coarse, raw_axis, 50, box),
            "sidecar_max_abs_m": abs(worst_sidecar["runtime_minus_raw_m"]),
            "sidecar_max_abs_point": worst_sidecar["point"],
            "sidecar_and_mission_points": point_residuals,
        },
        "pad_diagnostics": {
            "pad_box_horizontal_size_m": [40, 40], "pad_box_top_enu_up_m": 0.0,
            "pad_box_inscribed_radius_m": 20.0, "building_collision_triangles": face_count,
            "all_world_collision_geometries_accounted_for": True,
            "pad_box_and_terrain_top_enu_up_m": pad_surface,
            "neighborhoods": neighborhoods,
        },
        "missing_conditions": [
            "A traceable source vertical CRS and conversion to the runtime/telemetry datum with bounded error.",
            "A source and processing absolute terrain error bound on the required sweep, including registration.",
            "A task-specific grounded footprint and approach envelope with conservative obstacle coverage and allowances.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True, help="schema-v1 JSON manifest of local paths and SHA-256 pins")
    args = parser.parse_args()
    try:
        result = diagnose(read_json(args.inputs))
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except (KeyError, OSError, ValueError, TypeError, AttributeError, ET.ParseError) as exc:
        parser.exit(2, f"Iowa site diagnostic rejected inputs: {exc}\n")


if __name__ == "__main__":
    main()

"""Export occupancy for the audited clear-noon Iowa X1 SIM world."""

from __future__ import annotations

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

SITE = "rw_farm_iowa_w6km"
SCENARIO = f"{SITE}__clear_noon"
WORLD_SHA = "4e09aa4febff6b78568201a94bc4552ac5bbc6420f2c128d0784e1845bb9999c"
SCENE_SHA = "2fdb173e77e99744922de99f8d906351f291454951fa6a728a749b991eab952e"
GROUND_SHA = "bf2cfeac293f34b4773846b255295d19a7c4b35b7041da244d2cb9e46c5ad1d7"
WORLD_MODEL = "rw_farm_iowa"
# Extra room beyond outward centimeter rounding of the deployed vertices.
PADDING_M = 0.01
EXTENT_M = 3000.0
PAD_AUDIT_RADII_M = (5, 20, 40)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _asset(root: Path, relative: Path) -> Path:
    path = (root / relative).resolve(strict=True)
    if relative.is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"asset is outside its model root: {relative}")
    return path


def _triangles(path: Path) -> tuple[np.ndarray, np.ndarray]:
    vertices, faces = [], []
    with path.open("rb") as stream:
        for line in stream:
            fields = line.split()
            if not fields or fields[0].startswith(b"#"):
                continue
            if fields[0] == b"v" and len(fields) == 4:
                vertices.append([float(value) for value in fields[1:]])
            elif fields[0] == b"f" and len(fields) == 4:
                faces.append([int(value.split(b"/", 1)[0]) - 1 for value in fields[1:]])
            elif fields[0] not in {b"vt", b"vn", b"o", b"g", b"s", b"usemtl", b"mtllib"}:
                raise ValueError(f"unsupported OBJ record in {path}: {fields[0]!r}")
    xyz, indices = np.asarray(vertices), np.asarray(faces)
    if (
        not vertices
        or not faces
        or not np.isfinite(xyz).all()
        or indices.min() < 0
        or indices.max() >= len(xyz)
    ):
        raise ValueError(f"invalid triangulated OBJ: {path}")
    return xyz, indices


def _components(vertices: np.ndarray, faces: np.ndarray) -> list[np.ndarray]:
    """Find occupied components by shared OBJ vertex indices."""
    parents = list(range(len(vertices)))

    def find(index):
        while index != parents[index]:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for a, b, c in faces.tolist():
        root = find(a)
        parents[find(b)] = root
        parents[find(c)] = root
    groups = {}
    for index in np.unique(faces).tolist():
        groups.setdefault(find(index), []).append(index)
    return [vertices[indices] for indices in groups.values()]


def _box(lo, hi, datum_m):
    # OBJ uses E,N,Z. The live-clearance interface uses N,N,E,E,MSL,MSL.
    lows = (lo[1], lo[0], lo[2] + datum_m)
    highs = (hi[1], hi[0], hi[2] + datum_m)
    return [
        bound
        for low, high in zip(lows, highs, strict=True)
        for bound in (
            math.floor((low - PADDING_M) * 100) / 100,
            math.ceil((high + PADDING_M) * 100) / 100,
        )
    ]


def _canopy_groups(vertices, faces):
    points = vertices[np.unique(faces)]
    radius = max(PAD_AUDIT_RADII_M)
    if np.any(points.min(axis=0)[:2] > radius) or np.any(points.max(axis=0)[:2] < -radius):
        return [points]
    # An instanced OBJ can span several trees. Separate triangles by the
    # farthest separating side of the pad so batching across sides does not
    # create false obstructions over home. A triangle stays entirely in one
    # group, including when it crosses an axis.
    triangles = vertices[faces]
    lo, hi = triangles.min(axis=1), triangles.max(axis=1)
    distances = np.column_stack((-hi[:, 0], lo[:, 0], -hi[:, 1], lo[:, 1]))
    sides = distances.argmax(axis=1)
    sides[distances.max(axis=1) <= 0] = 4
    return [triangles[sides == side].reshape(-1, 3) for side in np.unique(sides)]


def _mesh_boxes(path, layer, datum_m):
    vertices, faces = _triangles(path)
    if layer in {"buildings", "water"}:
        groups = _components(vertices, faces)
    else:
        groups = _canopy_groups(vertices, faces)
    return [_box(group.min(axis=0), group.max(axis=0), datum_m) for group in groups], len(faces)


def _matches_footprint(footprint, boxes):
    west, south, east, north, height = footprint
    if not all(math.isfinite(value) for value in footprint) or not (
        west < east and south < north and height > 0
    ):
        raise ValueError("invalid source building footprint")
    # Footprint coordinates are rounded to one decimal place. Check that a
    # supported building component agrees in XY, independently of box padding.
    return any(
        all(abs(a - b) <= 0.08 for a, b in zip((south, north, west, east), box[:4], strict=True))
        for box in boxes
    )


def _demo_dem_domain(dem, origin):
    size = dem["elevation_m"]
    if (
        dem["scenario_id"] != SCENARIO
        or dem["vertical_datum"] != "MSL"
        or dem["origin"] != origin
        or len(size) < 2
        or len(size[0]) < 2
        or any(len(row) != len(size[0]) for row in size)
    ):
        raise ValueError("DEM does not match Iowa world frame")
    for axis, count in (("north", len(size)), ("east", len(size[0]))):
        start, spacing = dem[f"{axis}_origin_m"], dem[f"{axis}_spacing_m"]
        if start != -EXTENT_M or spacing <= 0 or start + spacing * (count - 1) != EXTENT_M:
            raise ValueError(f"DEM does not cover the full Iowa {axis} domain")


def inventory(world_path: Path, dem_path: Path, variant: Path, models: Path) -> dict:
    """Verify the source/deployment correspondence and bound the modeled layers."""
    world_sha, dem_sha = sha256(world_path), sha256(dem_path)
    scene_path = variant / "scenes" / SITE / "scene.json"
    scene_sha = sha256(scene_path)
    if (world_sha, scene_sha) != (WORLD_SHA, SCENE_SHA):
        raise ValueError("Iowa clear-noon world or scene differs from the audited bytes")
    scene = json.loads(scene_path.read_bytes())
    world = ET.parse(world_path).getroot().find("world")
    datum_m = float(world.findtext("spherical_coordinates/elevation"))
    origin = {
        "latitude_deg": float(world.findtext("spherical_coordinates/latitude_deg")),
        "longitude_deg": float(world.findtext("spherical_coordinates/longitude_deg")),
        "elevation_m": datum_m,
    }
    if (
        scene["site"] != SITE
        or scene["size_m"] != 2 * EXTENT_M
        or scene["lat"] != origin["latitude_deg"]
        or scene["lon"] != origin["longitude_deg"]
        or not math.isclose(scene["elevation"], datum_m, rel_tol=0, abs_tol=PADDING_M)
        or world.findtext("spherical_coordinates/world_frame_orientation") != "ENU"
    ):
        raise ValueError("Iowa coordinates or extent differ from scene source")
    _demo_dem_domain(json.loads(dem_path.read_bytes()), origin)
    terrain = world.find("model[@name='rw_terrain']/link[@name='terrain']")
    pad = world.find("model[@name='rw_pad']/link/collision")
    colliders = {node.get("name"): node for node in terrain.findall("collision")}
    if (
        set(colliders) != {"ground", "buildings"}
        or set(world.findall(".//collision")) != {*colliders.values(), pad}
        or any(world.findall(f".//{tag}") for tag in ("include", "plugin", "joint", "frame"))
        or any(actor.findall("link") for actor in world.findall("actor"))
    ):
        raise ValueError("unexpected geometry in audited Iowa world")
    inputs = {}

    def matching_mesh(node):
        mesh = node.find("geometry/mesh")
        if (
            mesh is None
            or [child.tag for child in mesh] != ["uri"]
            or node.find("pose") is not None
        ):
            raise ValueError("expected untransformed Iowa geometry")
        uri = mesh.findtext("uri")
        prefix = f"model://{WORLD_MODEL}/"
        if not uri.startswith(prefix) or not uri.endswith(".obj"):
            raise ValueError(f"unexpected Iowa geometry URI: {uri}")
        relative = Path(uri.removeprefix("model://"))
        source_relative = Path("models", SITE, uri.removeprefix(prefix))
        deployed = _asset(models, relative)
        digest = sha256(deployed)
        if digest != sha256(_asset(variant, source_relative)):
            raise ValueError(f"Iowa variant and deployment mesh differ: {uri}")
        inputs[relative.as_posix()] = digest
        return deployed, digest

    _, ground_sha = matching_mesh(colliders["ground"])
    if ground_sha != GROUND_SHA:
        raise ValueError("Iowa DEM coverage domain lacks the audited ground mesh")
    tree_prefixes = tuple(sorted({f"inst_{row['cat']}_" for row in scene["trees"]}))
    layers = {"buildings": [colliders["buildings"]], "canopy": [], "water": []}
    for visual in terrain.findall("visual"):
        name = Path(visual.findtext("geometry/mesh/uri", "")).name
        if name.startswith(tree_prefixes):
            layers["canopy"].append(visual)
        elif name == "water.obj":
            layers["water"].append(visual)
    if any(not nodes for nodes in layers.values()):
        raise ValueError("Iowa is missing a modeled obstacle layer")
    occupied, counts = {}, {}
    for layer, nodes in layers.items():
        boxes, triangles = [], 0
        for node in nodes:
            path, _ = matching_mesh(node)
            ranges, face_count = _mesh_boxes(path, layer, datum_m)
            boxes.extend(ranges)
            triangles += face_count
        occupied[layer] = boxes
        counts[layer] = {"meshes": len(nodes), "triangles": triangles, "boxes": len(boxes)}
    footprints, trees = scene["footprints"], scene["trees"]
    if len(footprints) != scene["stats"]["buildings"] or len(trees) != scene["stats"]["trees"]:
        raise ValueError("Iowa inventory disagrees with the source counts")
    matched = sum(_matches_footprint(row, occupied["buildings"]) for row in footprints)
    anchored = sum(
        any(
            box[0] <= tree["y"] <= box[1] and box[2] <= tree["x"] <= box[3]
            for box in occupied["canopy"]
        )
        for tree in trees
    )
    if matched != len(footprints) or anchored != len(trees):
        raise ValueError("deployment does not account for all Iowa footprints and tree anchors")
    obstacles = sorted(box for boxes in occupied.values() for box in boxes)
    if not 0 < len(obstacles) <= 4096:
        raise ValueError(f"occupancy needs {len(obstacles)} boxes; live evidence allows up to 4096")
    altitude_min, altitude_max = round(datum_m - 200, 2), round(datum_m + 1000, 2)
    if any(box[4] <= altitude_min or box[5] >= altitude_max for box in obstacles):
        raise ValueError("modeled obstacle escapes the chosen vertical coverage slab")
    approach = []
    for radius in PAD_AUDIT_RADII_M:
        column = [-radius, radius, -radius, radius, round(datum_m - PADDING_M, 2), altitude_max]
        hits = sum(
            all(
                a <= d and c <= b
                for a, b, c, d in zip(column[::2], column[1::2], box[::2], box[1::2], strict=True)
            )
            for box in obstacles
        )
        approach.append({"column_ne_msl_m": column, "occupied_box_intersections": hits})
    return {
        "world_sha256": world_sha,
        "dem_sha256": dem_sha,
        "scene_sha256": scene_sha,
        "scene_path": str(scene_path.resolve()),
        "models_root": str(models.resolve()),
        "mesh_sha256": inputs,
        "counts": counts,
        "footprints": len(footprints),
        "footprints_matching_component": matched,
        "trees": len(trees),
        "scene_tree_centres_inside_canopy_xy": anchored,
        "render_only_actors": len(world.findall("actor")),
        "padding_horizontal_m": PADDING_M,
        "padding_vertical_m": PADDING_M,
        "coordinate_frame": (
            "north/east and SDF z + 318.23 m in the runtime DEM's nominal MSL frame"
        ),
        "coverage_scope": (
            "SIM building collisions, canopy and water meshes. Terrain and pad are separate."
        ),
        "pad_approach_diagnostic": approach,
        "gaps": [
            "Physical survey and off-scene terrain",
            "Collision-free animation and decorative visuals",
            "Terrain, landing surface and injected vehicle dynamics require their own evidence",
        ],
        "obstacle_coverage": [-2999.99, 2999.99, -2999.99, 2999.99, altitude_min, altitude_max],
        "obstacles": obstacles,
    }

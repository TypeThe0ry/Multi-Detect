#!/usr/bin/env python3
"""Compose world SDF files from a built site: sky, sun, weather, trees, street objects, targets."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import sys
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, shape

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402

DATA_ROOT = Path("/data/datasets/multidetect-sim-assets")
SCENE_ROOT = Path(os.environ.get("RW_SCENE_ROOT", DATA_ROOT))
RESOLVED = Path(os.environ.get("RW_RESOLVED_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved"))
CATALOG = {a["slug"]: a for a in json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text())["assets"]}
OUT = Path(os.environ.get("RW_WORLD_OUT", ROOT / "simulation/gazebo/worlds"))


def local_visual_asset(slug: str) -> str:
    """Repair internal mesh texture paths in a private rw_ copy of a Fuel asset."""
    name = f"rw_prop_{slug}"
    destination = RESOLVED / name
    marker = destination / ".texture-paths-v2"
    if marker.exists():
        return name
    source = RESOLVED / slug
    shutil.copytree(source, destination, dirs_exist_ok=True)
    for path in destination.rglob("*"):
        if path.suffix.lower() not in {".mtl", ".dae"}:
            continue
        def repair(reference: str) -> str:
            text = reference.strip().replace("\\", "/")
            if text.startswith("model://"):
                candidate = RESOLVED / text.removeprefix("model://")
            else:
                candidate = path.parent / text
            if not candidate.exists():
                candidate = next(destination.rglob(Path(text).name), None)
            if candidate is None:
                # Some Fuel variants omit a shared wheel texture.
                candidate = next(source.parent.glob(f"*/materials/textures/{Path(text).name}"), None)
                if candidate is not None:
                    target = destination / "materials/textures" / candidate.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidate, target)
                    candidate = target
            if candidate is None:
                raise FileNotFoundError(f"{slug}: missing mesh texture {text}")
            return os.path.relpath(candidate, path.parent)

        if path.suffix.lower() == ".dae":
            tree = ET.parse(path)
            if tree.getroot().tag.startswith("{"):
                # Gazebo's ColladaLoader requires an unprefixed COLLADA root.
                ET.register_namespace("", tree.getroot().tag[1:].split("}", 1)[0])
            for element in tree.getroot().iter():
                if element.tag.rsplit("}", 1)[-1] == "init_from" and element.text:
                    # Surface init_from elements refer to image IDs, not files.
                    if Path(element.text).suffix.lower() in {".png", ".jpg", ".jpeg", ".tga", ".dds"}:
                        element.text = repair(element.text)
            tree.write(path, encoding="utf-8", xml_declaration=True)
        else:
            path.write_text(re.sub(r"(?m)^(\s*map_\w+\s+)([^\r\n]+)$",
                                   lambda match: match[1] + repair(match[2]), path.read_text()))
    marker.touch()
    return name

# Sun direction points from the sun; ambient models sky light since Ogre2 has no GI here.
GUST_PERIOD_S = 18.0
WEATHER = {
    "partly_cloudy": {"sky": "cloudy", "ambient": (0.42, 0.43, 0.46), "bg": (0.62, 0.74, 0.92), "sun": (-0.30, 0.35, -0.88), "i": 0.76, "dif": (1, 0.97, 0.92), "shadows": True},
    "clear_noon": {"sky": "noon", "ambient": (0.48, 0.50, 0.53), "bg": (0.62, 0.74, 0.92), "sun": (0.35, -0.25, -0.90), "i": 0.82, "dif": (1, 0.98, 0.93), "shadows": True},
    "morning": {"sky": "sunrise", "ambient": (0.40, 0.39, 0.42), "bg": (0.82, 0.76, 0.68), "sun": (-0.75, -0.20, -0.42), "i": 0.76, "dif": (1, 0.82, 0.62), "shadows": True},
    "evening": {"sky": "sunset", "ambient": (0.36, 0.33, 0.36), "bg": (0.80, 0.60, 0.48), "sun": (0.80, 0.15, -0.28), "i": 0.68, "dif": (1, 0.62, 0.40), "shadows": True},
    "overcast": {"sky": "overcast", "ambient": (0.52, 0.53, 0.56), "bg": (0.72, 0.74, 0.77), "sun": (0.2, 0.15, -0.95), "i": 0.24, "dif": (0.9, 0.92, 0.95), "shadows": False},
    "rain": {"sky": "overcast", "ambient": (0.43, 0.45, 0.49), "bg": (0.48, 0.51, 0.55), "sun": (0.2, 0.15, -0.95), "i": 0.16, "dif": (0.8, 0.84, 0.9), "shadows": False, "rain": True, "wind": (5.0, 2.0)},
    "dusk": {"sky": "dusk", "ambient": (0.17, 0.19, 0.27), "bg": (0.20, 0.24, 0.36), "sun": (0.85, 0.1, -0.12), "i": 0.25, "dif": (0.9, 0.6, 0.5), "shadows": True},
    "night": {"sky": "night", "ambient": (0.06, 0.07, 0.11), "bg": (0.015, 0.02, 0.04), "sun": (0.3, 0.2, -0.9), "i": 0.04, "dif": (0.6, 0.7, 1), "shadows": False, "lamps": True},
    "windy": {"sky": "clear", "ambient": (0.44, 0.46, 0.49), "bg": (0.62, 0.74, 0.92), "sun": (0.35, -0.25, -0.90), "i": 0.86, "dif": (1, 0.98, 0.93), "shadows": True, "wind": (9.0, 3.0)},
}


def visuals_of(slug: str) -> tuple[list[ET.Element], tuple[float, float, float]]:
    """Visual elements of a Fuel model, with URIs pinned to the local cache and any mesh scale applied."""
    asset_slug = local_visual_asset(slug)
    root = ET.fromstring((RESOLVED / asset_slug / "model.sdf").read_text(encoding="utf-8-sig").lstrip())
    model = root.find("model")
    out = []
    if model is None:
        actor = root.find("actor")
        filename = actor.findtext("skin/filename", "")
        v = ET.fromstring(f'<visual name="v"><geometry><mesh><uri>model://{slug}/{filename}</uri></mesh></geometry></visual>')
        return [ET.fromstring(ET.tostring(v, encoding="unicode").replace(f"model://{slug}/", f"model://{asset_slug}/"))], (0, 0, 0)
    for i, v in enumerate(model.iter("visual")):
        v = ET.fromstring(ET.tostring(v, encoding="unicode"))
        v.set("name", f"v{i}")
        for uri in v.iter("uri"):
            text = uri.text or ""
            if "/files/" in text:
                uri.text = f"model://{slug}/" + text.split("/files/", 1)[1]
            elif not text.startswith("model://"):
                uri.text = f"model://{slug}/" + text
            else:
                # Fuel models name themselves; the local directory is the slug.
                rest = text.split("//", 1)[1].split("/", 1)
                uri.text = f"model://{slug}/" + (rest[1] if len(rest) > 1 else "")
        for tag in ("albedo_map", "normal_map", "roughness_map", "metalness_map"):
            for t in v.iter(tag):
                text = t.text or ""
                if "/files/" in text:
                    t.text = f"model://{slug}/" + text.split("/files/", 1)[1]
                elif text.startswith("model://"):
                    rest = text.split("//", 1)[1].split("/", 1)
                    t.text = f"model://{slug}/" + (rest[1] if len(rest) > 1 else "")
                elif not text.startswith("http"):
                    t.text = f"model://{slug}/" + text
        out.append(ET.fromstring(ET.tostring(v, encoding="unicode").replace(f"model://{slug}/", f"model://{asset_slug}/")))
    a = CATALOG.get(slug)
    return out, tuple(a["bbox_min_m"]) if a else (0, 0, 0)


_cache: dict[str, str] = {}


def inst_visuals(slug: str, scale: float = 1.0) -> str:
    """Serialized visuals for one instance. Cached per slug to keep generation fast."""
    if slug not in _cache:
        vs, bb = visuals_of(slug)
        for v in vs:
            cs = v.find("cast_shadows")
            if cs is None:
                cs = ET.Element("cast_shadows")
                v.insert(0, cs)
            cs.text = "true"
        _cache[slug] = "".join(ET.tostring(v, encoding="unicode") for v in vs)
    xml = _cache[slug]
    if scale != 1.0:
        vs = ET.fromstring(f"<a>{xml}</a>")
        for s in vs.iter("scale"):
            vals = [float(c) * scale for c in s.text.split()]
            s.text = " ".join(f"{c:.3f}" for c in vals)
        for mesh in vs.iter("mesh"):
            if mesh.find("scale") is None:
                ET.SubElement(mesh, "scale").text = f"{scale:.3f} {scale:.3f} {scale:.3f}"
        for v in vs.iter("visual"):
            p = v.find("pose")
            if p is not None and p.text:
                nums = [float(c) for c in p.text.split()]
                nums[:3] = [c * scale for c in nums[:3]]
                p.text = " ".join(map(str, nums))
        xml = "".join(ET.tostring(v, encoding="unicode") for v in vs)
    return xml


def static_visual_model(name: str, slug: str, x, y, z, yaw, scale=1.0) -> str:
    a = CATALOG.get(slug)
    lift = -(a["bbox_min_m"][2]) * scale if a else 0.0
    return (f'<model name="{name}"><static>true</static><pose>{x:.2f} {y:.2f} {z + lift:.2f} 0 0 {yaw:.3f}</pose>'
            f'<link name="l">{inst_visuals(slug, scale)}</link></model>')


def labelled_model(name: str, slug: str, x, y, z, yaw, label: int, emit_label: bool = False) -> str:
    a = CATALOG[slug]
    lo, hi = a["bbox_min_m"], a["bbox_max_m"]
    size = [h - l for l, h in zip(lo, hi)]
    # Same shape as world_gen.inline_asset: box bounds at bbox centre so depth teachers and the obstacle loader work.
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    c, s = math.cos(yaw), math.sin(yaw)
    wx, wy = x + c * cx - s * cy, y + s * cx + c * cy
    return (f'<model name="{name}"><static>true</static><pose>{wx:.3f} {wy:.3f} {z + size[2] / 2:.3f} 0 0 {yaw:.4f}</pose>'
            f'<link name="bounds"><collision name="bounds_collision"><geometry><box><size>{size[0]:.3f} {size[1]:.3f} {size[2]:.3f}</size></box></geometry></collision></link>'
            + (f'<plugin filename="gz-sim-label-system" name="gz::sim::systems::Label"><label>{label}</label></plugin>' if emit_label else "") +
            f'<model name="mesh"><pose>{-cx:.3f} {-cy:.3f} {-lo[2] - size[2] / 2:.3f} 0 0 0</pose><static>true</static><link name="link">{inst_visuals(slug)}</link></model></model>')


def _ensure_emissive_map(path: Path, seed: int) -> Path | None:
    """Build a sparse warm window emission map beside a facade albedo texture."""
    out = path.with_name(path.stem + "_emissive_v2.png")
    if out.exists():
        return out
    try:
        src = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    except (OSError, ValueError):
        return None
    rng = np.random.default_rng(seed)
    # Facade window glass is generally darker/bluer than wall pixels. Keep most bays dark.
    # Generated glazing is blue; dark brick and grey concrete must not glow.
    mask = ((src[..., 2] - src[..., 0] > 8)
            & (src[..., 2] - src[..., 1] > 3)
            & (src.mean(axis=2) < 150))
    coarse = rng.random((max(1, src.shape[0] // 80), max(1, src.shape[1] // 80))) > 0.48
    coarse = np.asarray(Image.fromarray((coarse * 255).astype(np.uint8)).resize((src.shape[1], src.shape[0]), Image.NEAREST)) > 0
    rgb = np.zeros_like(src)
    rgb[..., 0] = np.where(mask & coarse, 0.24, 0.0)
    rgb[..., 1] = np.where(mask & coarse, 0.15, 0.0)
    rgb[..., 2] = np.where(mask & coarse, 0.06, 0.0)
    try:
        Image.fromarray(np.clip(rgb * 255, 0, 255).astype(np.uint8), "RGB").save(out)
    except OSError:
        return None
    return out


def night_terrain_materials(xml: str, site: str) -> str:
    """Keep facade materials unlit at night; Ogre2's emissive-map path overexposes walls."""
    return xml


def _actor(name: str, skin: str, anim: str, kind: str, pts: list[tuple], speed: float, side: float, turn_s: float,
           height: float = 0.0, length: float = 260.0, delay: float = 0.0, scale: float = 1.0,
           loop_route: bool = False, pause_s: float = 0.0) -> str:
    """Create a timed actor route with optional looping and initial phase delay."""
    if len(pts) < 2:
        return ""
    xy = [(p[0], p[1], p[2]) for p in pts]
    s = 0.0
    keep = [xy[0]]
    for a, b in zip(xy, xy[1:]):
        s += math.hypot(b[0] - a[0], b[1] - a[1])
        if s > length:
            break
        keep.append(b)
    if len(keep) < 2:
        return ""
    way, t = [], 0.0

    def leg(seq, t):
        for i, (x, y, z) in enumerate(seq):
            nx, ny = seq[min(i + 1, len(seq) - 1)], seq[max(i - 1, 0)]
            yaw = math.atan2(nx[1] - ny[1], nx[0] - ny[0])
            ox, oy = -math.sin(yaw) * side, math.cos(yaw) * side
            if i:
                px, py = seq[i - 1][0], seq[i - 1][1]
                t += math.hypot(x - px, y - py) / speed
            way.append(f"<waypoint><time>{t:.2f}</time><pose>{x + ox:.2f} {y + oy:.2f} {z + height:.2f} 0 0 {yaw:.3f}</pose></waypoint>")
        return t

    t = leg(keep, t)
    if loop_route:
        first, last = keep[0], keep[-1]
        if math.hypot(last[0] - first[0], last[1] - first[1]) > 1:
            t += math.hypot(last[0] - first[0], last[1] - first[1]) / speed
            yaw = math.atan2(first[1] - last[1], first[0] - last[0])
            way.append(f"<waypoint><time>{t:.2f}</time><pose>{first[0]:.2f} {first[1]:.2f} {first[2] + height:.2f} 0 0 {yaw:.3f}</pose></waypoint>")
    else:
        t = leg(keep[::-1], t + turn_s)
    if pause_s:
        way.append(f"<waypoint><time>{t + pause_s:.2f}</time><pose>{keep[0][0]:.2f} {keep[0][1]:.2f} {keep[0][2] + height:.2f} 0 0 0</pose></waypoint>")
    return (f'<actor name="{name}"><pose>0 0 0 0 0 0</pose><skin><filename>{skin}</filename><scale>{scale:.3f}</scale></skin>'
            f'<animation name="{kind}"><filename>{anim}</filename><interpolate_x>{str(kind == "walk").lower()}</interpolate_x></animation>'
            f'<script><loop>true</loop><auto_start>true</auto_start><delay_start>{delay:.2f}</delay_start><trajectory id="0" type="{kind}">{"".join(way)}</trajectory></script></actor>')


def movers(routes: list[dict], rng: random.Random, kind: str, count_scale: float = 1.0) -> str:
    out = []
    if count_scale <= 0:
        return ""
    common = ["hatchback", "hatchback_blue", "suv", "pickup", "prius_hybrid"]
    vehicle_sets = {
        "urban": common + ["bus", "ambulance", "fire_truck"],
        "suburb": common + ["bus", "fire_truck"],
        "coast": common + ["bus", "pickup"],
        "industrial": common + ["bus", "ambulance", "fire_truck"],
        "rural": common + ["pickup", "bus"],
        "mountain": common + ["pickup", "bus"],
    }
    # Stitch OSM ways at nearby endpoints so actors continue through junctions instead of
    # reversing at every individual way. The greedy chain is deliberately short to avoid
    # expensive graph bookkeeping in large scenes.
    source = [r for r in routes if r["width"] >= 5.5]
    used: set[int] = set()
    road = []
    for seed_i, seed_r in enumerate(source):
        if seed_i in used:
            continue
        chain = list(seed_r["pts"])
        used.add(seed_i)
        for _ in range(3):
            tail = chain[-1]
            best = None
            for j, cand in enumerate(source):
                if j in used:
                    continue
                for rev, endpoint in ((False, cand["pts"][0]), (True, cand["pts"][-1])):
                    d = math.hypot(endpoint[0] - tail[0], endpoint[1] - tail[1])
                    if d <= 0.5 and (best is None or d < best[0]):
                        best = (d, j, rev, cand)
            if best is None:
                break
            _, j, rev, cand = best
            segment = list(reversed(cand["pts"])) if rev else list(cand["pts"])
            chain.extend(segment[1:])
            used.add(j)
        road.append({"class": seed_r["class"], "width": seed_r["width"], "pts": chain})
    road.sort(key=lambda r: (r["class"] not in {"primary", "secondary", "tertiary"}, -r["width"]))
    density = {"urban": 6, "coast": 5, "industrial": 6, "suburb": 5, "rural": 3, "mountain": 2}.get(kind, 2)
    counts = {"urban": 48, "coast": 42, "industrial": 48, "suburb": 32, "rural": 18, "mountain": 14}
    target = min(80, max(density, round(min(counts.get(kind, 24), len(road) * density) * count_scale)))
    vehicles = vehicle_sets.get(kind, common)
    vehicle_i = 0
    for ri, r in enumerate(road):
        n = min(density, max(0, target - vehicle_i))
        if n == 0:
            break
        pts = r["pts"]
        road_class = r["class"]
        speed = {"primary": 13.0, "secondary": 11.0, "tertiary": 8.0, "residential": 6.0, "unclassified": 6.0, "service": 4.0}.get(road_class, 7.0)
        for j in range(n):
            slug = vehicles[(vehicle_i + rng.randrange(len(vehicles))) % len(vehicles)]
            # Delays keep following vehicles separated at the route start.
            # Open roads must return along the road, never close with a cross-block chord.
            route_pts = pts if j % 2 == 0 else list(reversed(pts))
            lane = (r["width"] / 4) * (1 if j % 2 == 0 else -1)
            gap = 3.0 + j * (max(24.0, len(pts) * 12 / n) / speed)
            mesh = f"model://rw_movers/meshes/{slug}.dae"
            out.append(_actor(f"rw_car_{vehicle_i:02d}", mesh, mesh, "drive", route_pts, speed, lane, 0.0,
                              length=sum(math.dist(a[:2], b[:2]) for a, b in zip(route_pts, route_pts[1:])) + 1, delay=gap, loop_route=False))
            vehicle_i += 1
    if kind != "mountain":
        desired = min(60, max(1, round({"urban": 40, "coast": 36, "industrial": 28, "suburb": 24, "rural": 14}.get(kind, 24) * count_scale)))
        walk_routes = [r for r in routes if r["width"] >= 3.0]
        for i in range(desired):
            if not walk_routes:
                break
            r = walk_routes[i % len(walk_routes)]
            pts = r["pts"]
            shift = (i * max(1, len(pts) // max(1, desired // len(walk_routes)))) % max(1, len(pts) - 2)
            walk = "model://rw_actor_walk/meshes/walk.dae"
            out.append(_actor(f"rw_walker_{i:02d}", walk, walk, "walk", pts[shift:], rng.uniform(1.0, 1.5),
                              r["width"] / 2 + 1.6, 1.0, height=1.0, length=72.0,
                              delay=rng.uniform(0, 30), scale=rng.uniform(0.88, 1.12), pause_s=rng.uniform(1.0, 4.0)))
    return "".join(out)


def obstacle_boxes(scene: dict, site: str, radius: float = 350.0, limit: int = 80) -> str:
    """Nearby buildings as obstacle_* boxes, the form the takeoff-altitude and teacher code reads.

    The merged building mesh keeps exact collision; these only add coarse bounds at low entity cost.
    """
    dem = np.load(Path(scene.get("raw_dir", DATA_ROOT / "sites" / site / "raw")) / "dem.npy")
    half = scene["size_m"] / 2
    n = dem.shape[0]
    near = sorted(scene["footprints"], key=lambda f: math.hypot((f[0] + f[2]) / 2, (f[1] + f[3]) / 2))
    out = []
    for i, (x0, y0, x1, y1, h) in enumerate(near[:limit]):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if math.hypot(cx, cy) > radius:
            break
        dimensions = (x1 - x0, y1 - y0, h)
        if not all(math.isfinite(value) for value in (*dimensions, cx, cy)):
            raise ValueError(f"{site}: non-finite auxiliary building bounds {i}")
        # These are auxiliary AABBs, not the source building collision mesh.
        # OSM includes zero-height footprints; DART rejects a zero-volume box.
        # Check the serialized precision too, without inventing building height.
        if any(round(value, 2) <= 0 for value in dimensions):
            warnings.warn(
                f"{site}: skipped non-volumetric auxiliary building bounds {i}: {dimensions}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        # An AABB can cover the clear courtyard of an L-shaped building.
        # Exact building collisions remain in the merged terrain; never seal
        # the launch pad with these auxiliary teacher bounds.
        if x0 < 28 and x1 > -28 and y0 < 28 and y1 > -28:
            continue
        spacing = scene["size_m"] / max(n - 1, 1)
        col = min(n - 1, max(0, round((cx + half) / spacing)))
        row = min(n - 1, max(0, round((cy + half) / spacing)))
        base = float(dem[row, col]) - scene["elevation"]
        out.append(f'<model name="obstacle_{i:02d}"><static>true</static><pose>{cx:.2f} {cy:.2f} {base + h / 2:.2f} 0 0 0</pose><link name="bounds">'
                   f'<collision name="bounds_collision"><geometry><box><size>{x1 - x0:.2f} {y1 - y0:.2f} {h:.2f}</size></box></geometry></collision></link></model>')
    return "".join(out)


def rain_emitter() -> str:
    return ('<model name="rw_rain"><static>true</static><pose>0 0 0 0 0 0</pose><link name="l"><visual name="streaks"><cast_shadows>false</cast_shadows>'
            '<transparency>0.35</transparency><geometry><mesh><uri>model://rw_rain/meshes/streaks.obj</uri></mesh></geometry>'
            '<material><ambient>0.8 0.84 0.9 1</ambient><diffuse>0.8 0.84 0.9 1</diffuse><specular>0 0 0 1</specular><double_sided>true</double_sided></material></visual></link></model>')


def street_lights(objects: list[dict], limit=24) -> str:
    near = sorted([o for o in objects if o["cat"] == "lamp_post"], key=lambda o: math.hypot(o["x"], o["y"]))[:limit]
    return "".join(
        f'<light name="rw_lamp_{i}" type="point"><pose>{o["x"]} {o["y"]} {o["z"] + 7.5} 0 0 0</pose><diffuse>1 0.88 0.68 1</diffuse><specular>0.2 0.2 0.15 1</specular>'
        f'<attenuation><range>28</range><constant>0.2</constant><linear>0.04</linear><quadratic>0.012</quadratic></attenuation><cast_shadows>false</cast_shadows><intensity>0.9</intensity></light>'
        for i, o in enumerate(near))


def street_light_heads(objects: list[dict], limit=24) -> str:
    near = sorted([o for o in objects if o["cat"] == "lamp_post"], key=lambda o: math.hypot(o["x"], o["y"]))[:limit]
    return "".join(
        f'<model name="rw_lamp_head_{i}"><static>true</static><pose>{o["x"]:.2f} {o["y"]:.2f} {o["z"] + 7.5:.2f} 0 0 0</pose><link name="l"><visual name="v"><geometry><sphere><radius>0.18</radius></sphere></geometry><material><emissive>1 0.68 0.25 1</emissive><diffuse>1 0.68 0.25 1</diffuse><lighting>false</lighting></material></visual></link></model>'
        for i, o in enumerate(near))


def takeoff_lights() -> str:
    return "".join(
        f'<light name="rw_pad_light_{i}" type="point"><pose>{x} {y} 12 0 0 0</pose><diffuse>0.72 0.82 1 1</diffuse><specular>0.15 0.18 0.22 1</specular>'
        '<attenuation><range>55</range><constant>0.25</constant><linear>0.025</linear><quadratic>0.004</quadratic></attenuation>'
        '<cast_shadows>false</cast_shadows><intensity>0.45</intensity></light>'
        for i, (x, y) in enumerate(((-34, -34), (34, -34), (-34, 34), (34, 34))))


def boat_actor(scene: dict) -> str:
    if scene["kind"] not in {"coast", "industrial"}:
        return ""
    waters = [shape(geometry).buffer(-8) for geometry in scene.get("water_areas", [])]
    for water in sorted(waters, key=lambda poly: poly.area, reverse=True):
        if water.is_empty:
            continue
        anchor = water.representative_point()
        for angle in (0, math.pi / 2, math.pi / 4, -math.pi / 4):
            dx, dy = math.cos(angle) * 250, math.sin(angle) * 250
            route = LineString([(anchor.x - dx, anchor.y - dy), (anchor.x + dx, anchor.y + dy)]).intersection(water)
            segments = list(route.geoms) if hasattr(route, "geoms") else [route]
            line = max((part for part in segments if part.geom_type == "LineString"), key=lambda part: part.length, default=None)
            if line is None or line.length < 80:
                continue
            dem = np.load(Path(scene.get("raw_dir", DATA_ROOT / "sites" / scene["site"] / "raw")) / "dem.npy")
            from scipy.ndimage import map_coordinates
            xy = np.asarray(line.coords)
            half = scene["size_m"] / 2
            spacing = scene["size_m"] / (dem.shape[0] - 1)
            z = map_coordinates(dem, [(xy[:, 1] + half) / spacing, (xy[:, 0] + half) / spacing], order=1, mode="nearest") - scene["elevation"] + 0.25
            pts = [(*point, float(height)) for point, height in zip(xy, z)]
            mesh = "model://rw_movers/meshes/rc_boat.dae"
            return _actor("rw_boat_00", mesh, mesh, "drive", pts, 4, 0, 2, length=line.length + 1, delay=2, scale=1.25)
    return ""


def build(site: str, weather: str, targets: list[dict], max_trees: int | None, seed: int, name_suffix: str = "", mover_scale: float = 1.0, out_dir: Path | None = None) -> Path:
    scene = json.loads((SCENE_ROOT / "sites" / site / "scene.json").read_text())
    w = WEATHER[weather]
    wname = f"{site}__{weather}{name_suffix}"
    amb = " ".join(f"{v:.3f}" for v in w["ambient"])
    bg = " ".join(f"{v:.3f}" for v in w["bg"])
    sd = w["sun"]
    provenance = RESOLVED / "rw_sky" / "provenance.json"
    try:
        bright = json.loads(provenance.read_text()).get(w["sky"], {}).get("brightest_direction")
        if bright and math.hypot(bright[0], bright[1]) > 1e-4:
            horiz = np.array([bright[0], bright[1]], float)
            horiz /= np.linalg.norm(horiz)
            horizontal_mag = math.hypot(w["sun"][0], w["sun"][1])
            sd = (float(horiz[0] * horizontal_mag), float(horiz[1] * horizontal_mag), w["sun"][2])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    dif = " ".join(map(str, w["dif"]))
    terrain_xml = ET.tostring(ET.fromstring((RESOLVED / site / "model.sdf").read_text()).find("model"), encoding="unicode").replace(f'<model name="{site}">', '<model name="rw_terrain">', 1)
    if weather == "night":
        terrain_xml = night_terrain_materials(terrain_xml, site)
    parts = [
        f'<?xml version="1.0"?><sdf version="1.9"><world name="default"><physics name="px4_ode" type="ode"><max_step_size>0.004</max_step_size><real_time_factor>1.0</real_time_factor></physics>',
        '<gravity>0 0 -9.80665</gravity><magnetic_field>6.0e-6 2.3e-5 -4.2e-5</magnetic_field><atmosphere type="adiabatic"/>',
        f'<spherical_coordinates><surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation><latitude_deg>{scene["lat"]}</latitude_deg><longitude_deg>{scene["lon"]}</longitude_deg><elevation>{scene["elevation"]:.2f}</elevation></spherical_coordinates>',
        f'<scene><grid>false</grid><ambient>{amb} 1</ambient><background>{bg} 1</background><shadows>{str(w["shadows"]).lower()}</shadows>',
        "__SKY__</scene>",
        f'<light name="sun" type="directional"><pose>0 0 800 0 0 0</pose><direction>{sd[0]} {sd[1]} {sd[2]}</direction><cast_shadows>{str(w["shadows"]).lower()}</cast_shadows><intensity>{w["i"]}</intensity><diffuse>{dif} 1</diffuse><specular>0.2 0.2 0.2 1</specular></light>',
        # Inlined rather than <include>: the stack plan only admits repository-owned includes.
        terrain_xml,
    ]
    parts[4] = parts[4].replace("__SKY__", "")
    # A real box under the pad: the coarse terrain mesh gives jittery contact at rest.
    parts.append('<model name="rw_pad"><static>true</static><pose>0 0 -0.5 0 0 0</pose><link name="l"><collision name="c"><geometry><box><size>40 40 1</size></box></geometry></collision></link></model>')
    sky_tex = f"model://rw_sky/{w['sky']}.jpg"
    parts.append(f'<model name="rw_sky_dome"><static>true</static><link name="l"><visual name="dome"><cast_shadows>false</cast_shadows><geometry><mesh><uri>model://rw_sky/dome.obj</uri></mesh></geometry>'
                 f'<material><lighting>false</lighting><ambient>0 0 0 1</ambient><diffuse>0 0 0 1</diffuse><specular>0 0 0 1</specular><emissive>1 1 1 1</emissive><double_sided>true</double_sided>'
                 f'<pbr><metal><emissive_map>{sky_tex}</emissive_map><roughness>1</roughness><metalness>0</metalness></metal></pbr></material></visual></link></model>')
    rng = random.Random(seed)
    label = 100
    # Trees and most props already live in the merged terrain model.
    for i, o in enumerate(scene["objects"]):
        if o.get("merged") or o["cat"] not in CATALOG or not (RESOLVED / o["cat"] / "model.sdf").exists():
            continue
        parts.append(labelled_model(f"rw_obj_{i:04d}", o["cat"], o["x"], o["y"], o["z"], o["yaw"], label))
        o["label"] = label
        label += 1
    parts.append(obstacle_boxes(scene, site))
    parts.append(movers(scene["routes"], random.Random(seed + 1), scene["kind"], mover_scale))
    parts.append(boat_actor(scene))
    for k, t in enumerate(targets):
        parts.append(labelled_model(t.get("name", f"rw_target_{k:02d}"), t["cat"], t["x"], t["y"], t["z"], t["yaw"], t.get("label", 10 + k), emit_label=True))
    if w.get("rain"):
        parts.append(rain_emitter())
    if w.get("lamps"):
        parts.append(street_lights(scene["objects"]))
        parts.append(street_light_heads(scene["objects"]))
        parts.append(takeoff_lights())
    if "wind" in w:
        spd, gust = w["wind"]
        ang = rng.uniform(0, 6.283)
        # No world <plugin> here: it would replace Gazebo's default systems and hang the world.
        # The container start-up patch adds WindEffects when it sees this comment.
        parts.insert(1, f"<!-- multidetect-wind-gust amplitude_percent={min(1.0, gust / spd):.4g} period={GUST_PERIOD_S:.6g} -->")
        parts.append(f'<wind><linear_velocity>{spd * math.cos(ang):.2f} {spd * math.sin(ang):.2f} 0</linear_velocity></wind>')
    parts.append("</world></sdf>\n")
    destination = out_dir or OUT
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{wname}.sdf"
    path.write_text("".join(parts))
    return path


def write_scenario(world: Path, site: str, airframe: str, seed: int = 91000, truth_extra: dict | None = None) -> Path:
    """Scenario bundle with a real DEM, so geolocation and terrain checks see the true ground."""
    wid = world.stem
    scene = json.loads((SCENE_ROOT / "sites" / site / "scene.json").read_text())
    sim = Path(os.environ.get("RW_SIM_OUT", ROOT / "simulation"))
    half = scene["size_m"] / 2
    raw = Path(scene.get("raw_dir", DATA_ROOT / "sites" / site / "raw"))
    dem = np.load(raw / "dem.npy")
    n = dem.shape[0]
    spacing = 50.0
    k = int(2 * half / spacing) + 1
    idx = np.linspace(0, n - 1, k).round().astype(int)
    grid = dem[np.ix_(idx, idx)]
    lat0, lon0 = scene["lat"], scene["lon"]
    dlat, dlon = half / 111320.0, half / (111320.0 * math.cos(math.radians(lat0)))
    (sim / "terrain").mkdir(exist_ok=True)
    (sim / "maps").mkdir(exist_ok=True)
    (sim / "terrain" / f"{wid}.dem.json").write_text(json.dumps({
        "schema_version": 1, "scenario_id": wid, "coordinate_reference": "WGS84", "vertical_datum": "MSL",
        "origin": {"latitude_deg": lat0, "longitude_deg": lon0, "elevation_m": round(scene["elevation"], 2)},
        "north_origin_m": -half, "east_origin_m": -half, "north_spacing_m": spacing, "east_spacing_m": spacing,
        "vertical_sigma_m": 1.0, "slope_sigma_m_per_m": 0.02,
        "elevation_m": [[round(float(v), 2) for v in row] for row in grid]}) + "\n")
    ring = [[lon0 - dlon, lat0 - dlat], [lon0 + dlon, lat0 - dlat], [lon0 + dlon, lat0 + dlat], [lon0 - dlon, lat0 + dlat], [lon0 - dlon, lat0 - dlat]]
    (sim / "maps" / f"{wid}.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "schema_version": 1, "scenario_id": wid, "coordinate_reference": "WGS84",
        "features": [{"type": "Feature", "id": "operating-area", "properties": {"role": "operating_area", "surface": "real_terrain", "clearance_radius_m": half},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}}]}) + "\n")
    # The world id is fixed, so other airframes get a parallel folder with the same scenario id.
    out = ROOT / "configs/scenarios/realworld" / ("" if airframe == "x500_mono_cam" else airframe)
    out.mkdir(parents=True, exist_ok=True)
    # Sensor contract comes from the mainline multirotor scenario; only world bindings change.
    template = {"x500_mono_cam": "urban_inspection_multirotor_001", "x500_gimbal": "urban_inspection_multirotor_001", "standard_vtol_mono_cam": "river_rescue_vtol_001", "rc_cessna_mono_cam": "coastal_patrol_fixed_wing_001",
                "tiltrotor_mono_cam": "airframes/tiltrotor_mono_cam_001", "quadtailsitter_mono_cam": "airframes/quadtailsitter_mono_cam_001",
                "advanced_plane_mono_cam": "airframes/advanced_plane_mono_cam_001"}[airframe]
    runtime = json.loads((ROOT / f"configs/scenarios/{template}.runtime.json").read_text())
    runtime.update({"scenario_id": wid, "seed": seed})
    runtime["world"].update({"world_asset": f"builtin://gazebo/{wid}", "map_asset": f"builtin://maps/{wid}.geojson", "dem_asset": f"builtin://terrain/{wid}.dem.json",
                             "wind_north_mps": 0.0, "wind_east_mps": 0.0, "gust_mps": 0.0})
    runtime["vehicle"]["model"] = airframe
    runtime["vehicle"].pop("spawn_pose", None)
    for sensor in runtime["sensors"]:
        sensor["stream_uri"] = sensor["stream_uri"].replace(Path(template).name, wid)
    truth = {"schema_version": 1, "scenario_id": wid, "scenario_version": 1, "targets": []}
    truth.update(truth_extra or {})
    expect = {"schema_version": 1, "scenario_id": wid, "scenario_version": 1, "allowed_terminal_states": ["completed", "safe_rtl", "safe_land"],
              "maximum_geofence_violations": 0, "maximum_false_geolocations": 0, "maximum_unauthorized_commands": 0}
    chans = {}
    for key, name, body in (("runtime_input", "runtime", runtime), ("truth", "truth", truth), ("expectations", "expectations", expect)):
        path = out / f"{wid}.{name}.json"
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n")
        chans[key] = {"path": path.name, "sha256": canonical_sha256(body)}
    scen = out / f"{wid}.scenario.json"
    scen.write_text(json.dumps({"schema_version": 1, "scenario_id": wid, "scenario_version": 1, "channels": chans}, indent=1) + "\n")
    return scen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--weather", nargs="*", default=list(WEATHER))
    ap.add_argument("--max-trees", type=int)
    ap.add_argument("--scenario-airframe", default="x500_mono_cam")
    ap.add_argument("--mover-scale", type=float, default=1.0, help="动态车辆/行人数相对默认值的倍数，用于 RTF/SDF 预算测量")
    ap.add_argument("--world-out-dir", type=Path)
    ap.add_argument("--skip-scenarios", action="store_true")
    ap.add_argument("--name-suffix", default="", help="附加到世界文件名，用于并列预算样本")
    a = ap.parse_args()
    for wx in a.weather:
        p = build(a.site, wx, [], a.max_trees, 0, name_suffix=a.name_suffix, mover_scale=max(0.0, a.mover_scale), out_dir=a.world_out_dir)
        if not a.skip_scenarios:
            write_scenario(p, a.site, a.scenario_airframe)
        print(p, p.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()

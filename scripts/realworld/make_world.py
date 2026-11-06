#!/usr/bin/env python3
"""Compose world SDF files from a built site: sky, sun, weather, trees, street objects, targets."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402

DATA_ROOT = Path("/data/datasets/multidetect-sim-assets")
SCENE_ROOT = Path(os.environ.get("RW_SCENE_ROOT", DATA_ROOT))
RESOLVED = Path(os.environ.get("RW_RESOLVED_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved"))
CATALOG = {a["slug"]: a for a in json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text())["assets"]}
OUT = Path(os.environ.get("RW_WORLD_OUT", ROOT / "simulation/gazebo/worlds"))

# Sun direction points from the sun; ambient models sky light since Ogre2 has no GI here.
GUST_PERIOD_S = 18.0
WEATHER = {
    "partly_cloudy": {"sky": "cloudy", "ambient": (0.34, 0.35, 0.38), "bg": (0.62, 0.74, 0.92), "sun": (-0.30, 0.35, -0.88), "i": 0.62, "dif": (1, 0.97, 0.92), "shadows": True},
    "clear_noon": {"sky": "noon", "ambient": (0.30, 0.32, 0.35), "bg": (0.62, 0.74, 0.92), "sun": (0.35, -0.25, -0.90), "i": 0.68, "dif": (1, 0.98, 0.93), "shadows": True},
    "morning": {"sky": "sunrise", "ambient": (0.34, 0.33, 0.36), "bg": (0.82, 0.76, 0.68), "sun": (-0.75, -0.20, -0.42), "i": 0.68, "dif": (1, 0.82, 0.62), "shadows": True},
    "evening": {"sky": "sunset", "ambient": (0.30, 0.27, 0.30), "bg": (0.80, 0.60, 0.48), "sun": (0.80, 0.15, -0.28), "i": 0.60, "dif": (1, 0.62, 0.40), "shadows": True},
    "overcast": {"sky": "overcast", "ambient": (0.48, 0.49, 0.52), "bg": (0.72, 0.74, 0.77), "sun": (0.2, 0.15, -0.95), "i": 0.20, "dif": (0.9, 0.92, 0.95), "shadows": False},
    "rain": {"sky": "overcast", "ambient": (0.38, 0.40, 0.44), "bg": (0.48, 0.51, 0.55), "sun": (0.2, 0.15, -0.95), "i": 0.12, "dif": (0.8, 0.84, 0.9), "shadows": False, "rain": True, "wind": (5.0, 2.0)},
    "dusk": {"sky": "dusk", "ambient": (0.17, 0.19, 0.27), "bg": (0.20, 0.24, 0.36), "sun": (0.85, 0.1, -0.12), "i": 0.25, "dif": (0.9, 0.6, 0.5), "shadows": True},
    "night": {"sky": "night", "ambient": (0.06, 0.07, 0.11), "bg": (0.015, 0.02, 0.04), "sun": (0.3, 0.2, -0.9), "i": 0.04, "dif": (0.6, 0.7, 1), "shadows": False, "lamps": True},
    "windy": {"sky": "clear", "ambient": (0.38, 0.40, 0.43), "bg": (0.62, 0.74, 0.92), "sun": (0.35, -0.25, -0.90), "i": 0.74, "dif": (1, 0.98, 0.93), "shadows": True, "wind": (9.0, 3.0)},
}


def visuals_of(slug: str) -> tuple[list[ET.Element], tuple[float, float, float]]:
    """Visual elements of a Fuel model, with URIs pinned to the local cache and any mesh scale applied."""
    root = ET.fromstring((RESOLVED / slug / "model.sdf").read_text(encoding="utf-8-sig").lstrip())
    model = root.find("model")
    out = []
    if model is None:
        actor = root.find("actor")
        filename = actor.findtext("skin/filename", "")
        v = ET.fromstring(f'<visual name="v"><geometry><mesh><uri>model://{slug}/{filename}</uri></mesh></geometry></visual>')
        return [v], (0, 0, 0)
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
        out.append(v)
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


def labelled_model(name: str, slug: str, x, y, z, yaw, label: int) -> str:
    a = CATALOG[slug]
    lo, hi = a["bbox_min_m"], a["bbox_max_m"]
    size = [h - l for l, h in zip(lo, hi)]
    # Same shape as world_gen.inline_asset: box bounds at bbox centre so depth teachers and the obstacle loader work.
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    c, s = math.cos(yaw), math.sin(yaw)
    wx, wy = x + c * cx - s * cy, y + s * cx + c * cy
    pl = f'<plugin filename="gz-sim-label-system" name="gz::sim::systems::Label"><label>{label}</label></plugin>'
    return (f'<model name="{name}"><static>true</static><pose>{wx:.3f} {wy:.3f} {z + size[2] / 2:.3f} 0 0 {yaw:.4f}</pose>'
            f'<link name="bounds"><collision name="bounds_collision"><geometry><box><size>{size[0]:.3f} {size[1]:.3f} {size[2]:.3f}</size></box></geometry></collision></link>{pl}'
            f'<model name="mesh"><pose>{-cx:.3f} {-cy:.3f} {-lo[2] - size[2] / 2:.3f} 0 0 0</pose><static>true</static><link name="link">{inst_visuals(slug)}</link>{pl}</model></model>')


def _actor(name: str, skin: str, anim: str, kind: str, pts: list[tuple], speed: float, side: float, turn_s: float, height: float = 0.0, length: float = 260.0) -> str:
    """Out-and-back trip along a road line, each way on its own side, as a looping actor script."""
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
            ox, oy = -math.sin(yaw) * -side, math.cos(yaw) * -side
            if i:
                px, py = seq[i - 1][0], seq[i - 1][1]
                t += math.hypot(x - px, y - py) / speed
            way.append(f"<waypoint><time>{t:.2f}</time><pose>{x + ox:.2f} {y + oy:.2f} {z + height:.2f} 0 0 {yaw:.3f}</pose></waypoint>")
        return t

    t = leg(keep, t)
    t = leg(keep[::-1], t + turn_s)
    return (f'<actor name="{name}"><pose>0 0 0 0 0 0</pose><skin><filename>{skin}</filename><scale>1</scale></skin>'
            f'<animation name="{kind}"><filename>{anim}</filename><interpolate_x>{str(kind == "walk").lower()}</interpolate_x></animation>'
            f'<script><loop>true</loop><auto_start>true</auto_start><trajectory id="0" type="{kind}">{"".join(way)}</trajectory></script></actor>')


def movers(routes: list[dict], rng: random.Random, kind: str) -> str:
    out = []
    vehicles = ["hatchback", "hatchback_blue", "suv", "pickup", "prius_hybrid"]
    if kind == "industrial":
        vehicles += ["bus", "pickup"]
    if kind == "urban":
        vehicles += ["bus", "ambulance"]
    road = [r for r in routes if r["width"] >= 6]
    for i, r in enumerate(road[:6]):
        slug = rng.choice(vehicles)
        out.append(_actor(f"rw_car_{i:02d}", f"model://rw_movers/meshes/{slug}.dae", f"model://rw_movers/meshes/{slug}.dae", "drive", r["pts"], rng.uniform(5.0, 10.0), r["width"] / 4, 2.0))
    if kind != "mountain":
        for i, r in enumerate((routes * 2)[:8]):
            walk = "model://rw_actor_walk/meshes/walk.dae"
            shift = rng.randrange(max(1, len(r["pts"]) - 4))
            out.append(_actor(f"rw_walker_{i:02d}", walk, walk, "walk", r["pts"][shift:], rng.uniform(1.0, 1.6), r["width"] / 2 + 1.5 + rng.uniform(0, 1), 1.0, height=1.0, length=90.0))
    return "".join(out)


def obstacle_boxes(scene: dict, site: str, radius: float = 350.0, limit: int = 80) -> str:
    """Nearby buildings as obstacle_* boxes, the form the takeoff-altitude and teacher code reads.

    The merged building mesh keeps exact collision; these only add coarse bounds at low entity cost.
    """
    dem = np.load(DATA_ROOT / "sites" / site / "raw" / "dem.npy")
    half = scene["size_m"] / 2
    n = dem.shape[0]
    near = sorted(scene["footprints"], key=lambda f: math.hypot((f[0] + f[2]) / 2, (f[1] + f[3]) / 2))
    out = []
    for i, (x0, y0, x1, y1, h) in enumerate(near[:limit]):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if math.hypot(cx, cy) > radius:
            break
        col = min(n - 1, max(0, int((cx + half) / 2.0)))
        row = min(n - 1, max(0, int((cy + half) / 2.0)))
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


def build(site: str, weather: str, targets: list[dict], max_trees: int | None, seed: int, name_suffix: str = "") -> Path:
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
    parts = [
        f'<?xml version="1.0"?><sdf version="1.9"><world name="default"><physics name="px4_ode" type="ode"><max_step_size>0.004</max_step_size><real_time_factor>1.0</real_time_factor></physics>',
        '<gravity>0 0 -9.80665</gravity><magnetic_field>6.0e-6 2.3e-5 -4.2e-5</magnetic_field><atmosphere type="adiabatic"/>',
        f'<spherical_coordinates><surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation><latitude_deg>{scene["lat"]}</latitude_deg><longitude_deg>{scene["lon"]}</longitude_deg><elevation>{scene["elevation"]:.2f}</elevation></spherical_coordinates>',
        f'<scene><grid>false</grid><ambient>{amb} 1</ambient><background>{bg} 1</background><shadows>{str(w["shadows"]).lower()}</shadows>',
        "__SKY__</scene>",
        f'<light name="sun" type="directional"><pose>0 0 800 0 0 0</pose><direction>{sd[0]} {sd[1]} {sd[2]}</direction><cast_shadows>{str(w["shadows"]).lower()}</cast_shadows><intensity>{w["i"]}</intensity><diffuse>{dif} 1</diffuse><specular>0.2 0.2 0.2 1</specular></light>',
        # Inlined rather than <include>: the stack plan only admits repository-owned includes.
        ET.tostring(ET.fromstring((RESOLVED / site / "model.sdf").read_text()).find("model"), encoding="unicode").replace(f'<model name="{site}">', '<model name="rw_terrain">', 1),
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
    parts.append(movers(scene["routes"], random.Random(seed + 1), scene["kind"]))
    for k, t in enumerate(targets):
        parts.append(labelled_model(f"rw_target_{k:02d}", t["cat"], t["x"], t["y"], t["z"], t["yaw"], t.get("label", 10 + k)))
    if w.get("rain"):
        parts.append(rain_emitter())
    if w.get("lamps"):
        parts.append(street_lights(scene["objects"]))
    if "wind" in w:
        spd, gust = w["wind"]
        ang = rng.uniform(0, 6.283)
        # No world <plugin> here: it would replace Gazebo's default systems and hang the world.
        # The container start-up patch adds WindEffects when it sees this comment.
        parts.insert(1, f"<!-- multidetect-wind-gust amplitude_percent={min(1.0, gust / spd):.4g} period={GUST_PERIOD_S:.6g} -->")
        parts.append(f'<wind><linear_velocity>{spd * math.cos(ang):.2f} {spd * math.sin(ang):.2f} 0</linear_velocity></wind>')
    parts.append("</world></sdf>\n")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{wname}.sdf"
    path.write_text("".join(parts))
    return path


def write_scenario(world: Path, site: str, airframe: str, seed: int = 91000) -> Path:
    """Scenario bundle with a real DEM, so geolocation and terrain checks see the true ground."""
    wid = world.stem
    scene = json.loads((SCENE_ROOT / "sites" / site / "scene.json").read_text())
    sim = Path(os.environ.get("RW_SIM_OUT", ROOT / "simulation"))
    half = scene["size_m"] / 2
    raw = DATA_ROOT / "sites" / site / "raw"
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
    template = {"x500_mono_cam": "urban_inspection_multirotor_001", "standard_vtol_mono_cam": "river_rescue_vtol_001", "rc_cessna_mono_cam": "coastal_patrol_fixed_wing_001",
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
    a = ap.parse_args()
    for wx in a.weather:
        p = build(a.site, wx, [], a.max_trees, 0)
        write_scenario(p, a.site, a.scenario_airframe)
        print(p, p.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()

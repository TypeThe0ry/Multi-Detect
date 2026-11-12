#!/usr/bin/env python3
"""Fire-hydrant cluster count worlds on top of the real-place (rw_*) sites.

Each site gets one open spot near the launch pad and one world per neighbour spacing
(equilateral triangle, all three pairwise gaps equal), so worlds differ only in spacing.
Writes the world, an x500 scenario bundle (other airframes are derived at run time by
``--airframe``), and ``configs/scenarios/realworld-clusters/index.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point, box, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_world as mw  # noqa: E402

ROOT = mw.ROOT
SPACINGS_M = (3.0, 6.0, 10.0)
SITES = ("rw_city_tokyo", "rw_downtown_sf", "rw_city_zurich", "rw_suburb_davis")
SLUG = "fire_hydrant"
OUT_DIR = ROOT / "configs/scenarios/realworld-clusters"
SOFT_PROPS = {"grass_medium_01", "grass_medium_02", "grass_bermuda_01", "weed_plant_02"}


def triangle(cx: float, cy: float, side: float, angle: float) -> list[tuple[float, float]]:
    radius = side / math.sqrt(3)
    return [(cx + radius * math.cos(angle + k * 2 * math.pi / 3), cy + radius * math.sin(angle + k * 2 * math.pi / 3)) for k in range(3)]


class Ground:
    def __init__(self, scene: dict):
        self.dem = np.load(Path(scene["raw_dir"]) / "dem.npy")
        self.half = scene["size_m"] / 2
        self.elevation = scene["elevation"]

    def z(self, x: float, y: float) -> float:
        n = self.dem.shape[0]
        c = (x + self.half) / (2 * self.half) * (n - 1)
        r = (y + self.half) / (2 * self.half) * (n - 1)
        r0, c0 = int(math.floor(r)), int(math.floor(c))
        r1, c1 = min(r0 + 1, n - 1), min(c0 + 1, n - 1)
        fr, fc = r - r0, c - c0
        d = self.dem
        v = d[r0, c0] * (1 - fr) * (1 - fc) + d[r0, c1] * (1 - fr) * fc + d[r1, c0] * fr * (1 - fc) + d[r1, c1] * fr * fc
        return float(v) - self.elevation


def pick_center(scene: dict, ground: Ground, angle: float) -> tuple[float, float, float]:
    """Nearest open, flat, dry spot 30-70 m from the pad that fits every spacing's triangle."""
    footprints = [box(f[0], f[1], f[2], f[3]) for f in scene["footprints"] if math.hypot((f[0] + f[2]) / 2, (f[1] + f[3]) / 2) < 200]
    water = [shape(w) for w in scene.get("water_areas", [])]
    roads = [(LineString(r["pts"]), r["width"]) for r in scene["routes"] if len(r["pts"]) >= 2]
    props = [(o["x"], o["y"], 3.0 if o["cat"] in ("broadleaf_tree",) else 2.0) for o in scene["objects"] if o["cat"] not in SOFT_PROPS]
    trees = [(t["x"], t["y"]) for t in scene["trees"] if math.hypot(t["x"], t["y"]) < 200]
    best = None
    for radius in range(30, 71, 4):
        for k in range(24):
            a = math.radians(15 * k)
            cx, cy = radius * math.cos(a), radius * math.sin(a)
            members = [p for side in SPACINGS_M for p in triangle(cx, cy, side, angle)] + [(cx, cy)]
            ok = True
            for x, y in members:
                point = Point(x, y)
                if any(f.distance(point) < 5.0 for f in footprints) or any(w.distance(point) < 3.0 for w in water):
                    ok = False
                elif any(line.distance(point) < width / 2 + 1.5 for line, width in roads):
                    ok = False
                elif any(math.hypot(px - x, py - y) < m for px, py, m in props):
                    ok = False
                elif any(math.hypot(tx - x, ty - y) < 5.0 for tx, ty in trees):
                    ok = False
                if not ok:
                    break
            if not ok:
                continue
            zs = [ground.z(x, y) for x, y in members]
            spread = max(zs) - min(zs)
            if spread > 0.8:
                continue
            score = radius + 20 * spread
            if best is None or score < best[0]:
                best = (score, cx, cy, spread)
        if best and radius >= 38:
            break
    if best is None:
        raise RuntimeError(f"no open flat spot for a hydrant cluster in {scene['site']}")
    return best[1], best[2], best[3]


def spec(world_id: str, scene: dict, ground: Ground, members: list[tuple[float, float]], seed: int) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    size = mw.CATALOG[SLUG]["measured_size_m"]
    half_height = size[2] / 2
    lat0, lon0 = scene["lat"], scene["lon"]
    placements, targets, labels = [], [], {}
    for k, (x, y) in enumerate(members):
        name = f"target_true_{world_id}" if k == 0 else f"count_member_0000_{k:02d}"
        yaw = rng.uniform(-math.pi, math.pi)
        z = ground.z(x, y) - 0.03  # seat the base slightly into the coarse terrain mesh
        label = 10 + k
        placements.append({"name": name, "cat": SLUG, "x": x, "y": y, "z": z, "yaw": yaw, "label": label})
        labels[str(label)] = {"asset_slug": SLUG, "role": "target" if k == 0 else "count_member", "target_id": name}
        targets.append({
            "asset_slug": SLUG, "behavior": "stationary", "bottom_offset_m": round(-half_height, 6),
            "category_words": ["fire hydrant", "hydrant"], "class": SLUG, "color": None, "count_member": True,
            "label": "fire hydrant hydrant", "measured_size_m": size, "size_m": size,
            "pose": {"x_east_m": round(x, 3), "y_north_m": round(y, 3), "yaw_rad": round(yaw, 6), "z_up_m": round(z + half_height, 3)},
            "position": {"altitude_m": round(z + half_height, 3), "altitude_reference": "world",
                         "latitude_deg": round(lat0 + y / 111320.0, 8),
                         "longitude_deg": round(lon0 + x / (111320.0 * math.cos(math.radians(lat0))), 8)},
            "target_id": name, "true_target": k == 0,
        })
    return placements, {"asset_mode": "fuel_v1", "segmentation_labels": labels, "targets": targets}


def build_site(site: str, weather: str, seed: int, out_dir: Path | None) -> list[dict]:
    scene = json.loads((mw.SCENE_ROOT / "sites" / site / "scene.json").read_text())
    ground = Ground(scene)
    rng = random.Random(f"{site}:{seed}")
    angle = rng.uniform(0, 2 * math.pi)
    cx, cy, spread = pick_center(scene, ground, angle)
    entries = []
    for side in SPACINGS_M:
        suffix = f"_clu{side:g}m"
        world_id = f"{site}__{weather}{suffix}"
        members = triangle(cx, cy, side, angle)
        placements, truth = spec(world_id, scene, ground, members, seed)
        path = mw.build(site, weather, placements, None, seed, name_suffix=suffix, out_dir=out_dir)
        mw.write_scenario(path, site, "x500_mono_cam", truth_extra=truth)
        entries.append({
            "asset_mode": "fuel_v1", "expected_count": 3, "split": "test", "seed": seed, "task_kind": "count",
            "task_text": "数一数有几个消防栓并报告数量和位置",
            "task_text_en": "Count the fire hydrant objects and report their positions.",
            "find_task_text": "找到消防栓并记录位置", "find_task_text_en": "Find the fire hydrant and record its position.",
            "target_combo": {"category": SLUG, "color": None, "slug": SLUG},
            "true_target_id": f"target_true_{world_id}", "site": site, "cluster_spacing_m": side,
            "cluster_center_m": [round(cx, 2), round(cy, 2)], "cluster_distance_from_home_m": round(math.hypot(cx, cy), 1),
            "cluster_ground_spread_m": round(spread, 2),
            "world_file": f"{world_id}.sdf", "world_id": world_id,
            "files": {"scenario": f"{world_id}.scenario.json"},
        })
        print(path, f"d={side:g} m centre=({cx:.1f},{cy:.1f})", flush=True)
    return entries


VTOL_AIRFRAMES = ("delta_tailsitter", "delta_tailsitter_gimbal")


def derive_airframe_index(worlds: list[dict], airframe: str) -> Path:
    """Scenario bundles whose runtime.vehicle.model matches a VTOL airframe (the collector checks that)."""
    sys.path.insert(0, str(ROOT / "src"))
    from multidetect.simulator.world_gen import derive_vtol_scenario

    out = OUT_DIR / airframe
    out.mkdir(parents=True, exist_ok=True)
    for w in worlds:
        derive_vtol_scenario(OUT_DIR / w["files"]["scenario"], out, airframe=airframe)
    path = out / "index.json"
    path.write_text(json.dumps({
        "schema_version": 1, "generator": "scripts.realworld.hydrant_clusters", "count": len(worlds), "airframe": airframe,
        "worlds": worlds}, ensure_ascii=False, indent=2) + "\n")
    return path


def write_suite(worlds: list[dict], airframe: str) -> Path:
    """Mission suite (same defaults as the cities suite) with one count mission per cluster world."""
    base = json.loads((ROOT / "configs/onboard/suite-cities-r21idr3-r24plan.json").read_text())
    missions = []
    for w in worlds:
        weather = w["world_id"].split("__")[1].rsplit("_clu", 1)[0]
        tag = "" if weather == "clear_noon" else f"-{weather}"
        mission = {
            "id": f"rwclu-{w['site'].removeprefix('rw_')}{tag}-d{int(w['cluster_spacing_m']):02d}",
            "task_text": "Count the fire hydrants in the site and report their positions.",
            "index": ("configs/scenarios/realworld-clusters/" + (f"{airframe}/" if airframe in VTOL_AIRFRAMES else "") + "index.json"),
            "world": w["world_id"],
            "refs": {"zone:site": [0, 0, 55]}, "ref_labels": {"zone:site": "the site"},
            "expect": {"skills": ["count", "return_home"], "min_count_exact": 1},
            "steps": 150, "max_episode_s": 2400, "airframe": airframe,
        }
        missions.append(mission)
    path = ROOT / f"configs/onboard/suite-rw-hydrant-clusters-{airframe.removesuffix('_mono_cam')}.json"
    path.write_text(json.dumps({"defaults": base["defaults"], "missions": missions}, indent=1) + "\n")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sites", nargs="*", default=list(SITES))
    ap.add_argument("--weather", nargs="+", default=["clear_noon"])
    ap.add_argument("--skip-existing", action="store_true", help="leave worlds already in the index untouched (safe while flights use them)")
    ap.add_argument("--seed", type=int, default=20261112)
    ap.add_argument("--world-out-dir", type=Path)
    a = ap.parse_args()
    index_path = OUT_DIR / "index.json"
    known = {w["world_id"]: w for w in json.loads(index_path.read_text())["worlds"]} if index_path.is_file() else {}
    for site in a.sites:
        for weather in a.weather:
            if a.skip_existing and all(f"{site}__{weather}_clu{d:g}m" in known for d in SPACINGS_M):
                continue
            try:
                for entry in build_site(site, weather, a.seed, a.world_out_dir):
                    known[entry["world_id"]] = entry
            except Exception as exc:  # one bad site must not stop the rest
                print(f"SKIP {site} {weather}: {exc}", flush=True)
    worlds = list(known.values())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Scenario bundles live in configs/scenarios/realworld; the index sits beside copies of them.
    scen_dir = ROOT / "configs/scenarios/realworld"
    for w in worlds:
        for p in scen_dir.glob(f"{w['world_id']}.*.json"):
            (OUT_DIR / p.name).write_text(p.read_text())
            p.unlink()

    index_path.write_text(json.dumps({
        "schema_version": 1, "generator": "scripts.realworld.hydrant_clusters", "count": len(worlds),
        "note": "Three fire hydrants in an equilateral triangle (3/6/10 m sides) on real-place terrain; count task.",
        "worlds": worlds}, ensure_ascii=False, indent=2) + "\n")
    print(index_path, len(worlds), "worlds")
    for airframe in VTOL_AIRFRAMES:
        print(derive_airframe_index(worlds, airframe))
    for airframe in ("x500_mono_cam", *VTOL_AIRFRAMES):
        print(write_suite(worlds, airframe))


if __name__ == "__main__":
    main()

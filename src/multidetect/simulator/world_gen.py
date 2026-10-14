# ruff: noqa
"""Deterministic procedural Gazebo worlds for privileged sim-teacher data.

The generator emits self-contained Gazebo Harmonic SDF files and the three
scenario channels used by the simulator.  A generated runtime uses a
``builtin://gazebo/<world-id>`` asset; :mod:`multidetect.simulator.stack`
resolves that asset from the repository world directory and the Docker
launcher mounts that directory at ``/opt/px4-gazebo/share/gz/worlds``.
PX4/Gazebo normally runs at real time (``real_time_factor=1``).  Passing a
larger ``--real-time-factor`` writes that value to the SDF physics block; the
container launch path itself does not impose a speed cap, although sensor and
MAVLink consumers may become the limiting factor.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multidetect.simulator.task_semantics import (
    fuel_count_texts,
    fuel_task_texts,
    task_equivalent,
)

from multidetect.autonomy.serialization import canonical_sha256

SCHEMA_VERSION = 1
ORIGIN_LAT = 1.352100
ORIGIN_LON = 103.819800
WORLD_RADIUS_M = 150.0
def _cn(*codes: int) -> str:
    return "".join(chr(code) for code in codes)
TASK_TEMPLATES = (
    _cn(0x627E, 0x5230) + "{color}" + _cn(0x8272) + "{kind}" + _cn(0x5E76, 0x8BB0, 0x5F55, 0x4F4D, 0x7F6E),
    "\u641c\u7d22\u573a\u666f\u4e2d\u7684{color}\u8272{kind}\uff0c\u62a5\u544a\u5750\u6807",
    "\u8bf7\u5b9a\u4f4d{color}\u8272{kind}\u5e76\u8bb0\u5f55\u5b83\u7684\u4f4d\u7f6e",
    "\u5728\u641c\u7d22\u533a\u57df\u627e\u5230{color}\u8272{kind}",
    "\u8bc6\u522b{color}\u8272{kind}\u540e\u56de\u62a5\u4f4d\u7f6e",
    "\u5bfb\u627e\u76ee\u6807{color}\u8272{kind}\u5e76\u5b8c\u6210\u5b9a\u4f4d",
    "\u5de1\u89c6\u533a\u57df\uff0c\u627e\u5230{color}\u8272{kind}\u5e76\u8bb0\u5f55\u5750\u6807",
    "\u8bf7\u641c\u7d22\u5e76\u6807\u8bb0{color}\u8272{kind}",
    "\u53d1\u73b0{color}\u8272{kind}\u540e\u62a5\u544a\u5176\u4f4d\u7f6e",
    "\u6267\u884c\u641c\u7d22\u4efb\u52a1\uff1a\u5b9a\u4f4d{color}\u8272{kind}",
    "\u5728\u672c\u4e16\u754c\u4e2d\u627e\u5230{color}\u8272{kind}\u7684\u4f4d\u7f6e",
    "\u9501\u5b9a{color}\u8272{kind}\u5e76\u8bb0\u5f55\u5730\u7406\u4f4d\u7f6e",
)
COLORS = ((_cn(0x7EA2), "red", (0.85, 0.05, 0.04)), (_cn(0x84DD), "blue", (0.04, 0.20, 0.90)),
          ("\u7eff", "green", (0.05, 0.75, 0.10)), ("\u9ec4", "yellow", (0.90, 0.72, 0.03)))
KINDS = (("\u8f66\u8f86", "vehicle"), ("\u63a2\u6d4b\u8f66", "rover"), ("\u8d27\u8fd0\u8f66", "cargo_cart"))


@dataclass(frozen=True)
class _Object:
    object_id: str
    x: float
    y: float
    z: float
    yaw: float
    radius: float
    label: str
    color_name: str
    color_rgb: tuple[float, float, float]
    kind: str
    true_target: bool = False


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write(path: Path, value: Any) -> str:
    raw = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _lat_lon(x_east: float, y_north: float) -> tuple[float, float]:
    return (ORIGIN_LAT + y_north / 111_132.0, ORIGIN_LON + x_east / (111_320.0 * math.cos(math.radians(ORIGIN_LAT))))


def _split(index: int, count: int, ratios: tuple[float, float, float]) -> str:
    train_n = int(math.floor(count * ratios[0]))
    val_n = int(math.floor(count * ratios[1]))
    if index < train_n:
        return "train"
    if index < train_n + val_n:
        return "val"
    return "test"


def _material(rgb: tuple[float, float, float]) -> str:
    r, g, b = rgb
    return f"<material><ambient>{r:.3f} {g:.3f} {b:.3f} 1</ambient><diffuse>{r:.3f} {g:.3f} {b:.3f} 1</diffuse><specular>0.08 0.08 0.08 1</specular></material>"


def _object_sdf(obj: _Object) -> str:
    r, g, b = obj.color_rgb
    # A box is deliberately used for every target/distractor: it is portable
    # across Gazebo Harmonic and gives the depth teacher a collision surface.
    sx, sy, sz = (3.0, 1.8, 1.5) if obj.kind == "vehicle" else (3.8, 1.6, 1.7)
    return f'''    <model name="{obj.object_id}">
      <static>true</static><pose>{obj.x:.3f} {obj.y:.3f} {obj.z:.3f} 0 0 {obj.yaw:.6f}</pose>
      <link name="body">
        <collision name="collision"><geometry><box><size>{sx:.2f} {sy:.2f} {sz:.2f}</size></box></geometry></collision>
        <visual name="visual"><geometry><box><size>{sx:.2f} {sy:.2f} {sz:.2f}</size></box></geometry>{_material((r,g,b))}</visual>
      </link>
    </model>'''


def _obstacle_sdf(name: str, x: float, y: float, z: float, sx: float, sy: float, sz: float, rgb: tuple[float, float, float], yaw: float) -> str:
    return f'''    <model name="{name}"><static>true</static><pose>{x:.3f} {y:.3f} {z:.3f} 0 0 {yaw:.6f}</pose>
      <link name="obstacle_link"><collision name="collision"><geometry><box><size>{sx:.3f} {sy:.3f} {sz:.3f}</size></box></geometry></collision>
      <visual name="visual"><geometry><box><size>{sx:.3f} {sy:.3f} {sz:.3f}</size></box></geometry>{_material(rgb)}</visual></link>
    </model>'''


def _world_sdf(world_id: str, objects: list[_Object], obstacles: list[tuple[Any, ...]], rng: random.Random, real_time_factor: float) -> str:
    ambient = rng.uniform(0.35, 0.60)
    bg = rng.uniform(0.65, 0.88)
    intensity = rng.uniform(0.75, 1.35)
    az = rng.uniform(-math.pi, math.pi)
    el = rng.uniform(0.55, 1.15)
    direction = (math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), -math.sin(el))
    bits = [f'''<?xml version="1.0" encoding="UTF-8"?>
<sdf version="1.9"><world name="default">
  <physics name="px4_ode" type="ode"><max_step_size>0.004</max_step_size><real_time_factor>{real_time_factor:.6f}</real_time_factor><real_time_update_rate>250</real_time_update_rate></physics>
  <gravity>0 0 -9.80665</gravity><magnetic_field>6e-06 2.3e-05 -4.2e-05</magnetic_field><atmosphere type="adiabatic"/>
  <scene><grid>false</grid><ambient>{ambient:.3f} {ambient:.3f} {ambient:.3f} 1</ambient><background>{bg:.3f} {min(bg+0.06,1):.3f} {min(bg+0.12,1):.3f} 1</background><shadows>true</shadows></scene>
  <spherical_coordinates><surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation><latitude_deg>{ORIGIN_LAT:.6f}</latitude_deg><longitude_deg>{ORIGIN_LON:.6f}</longitude_deg><elevation>18</elevation></spherical_coordinates>
  <light name="sun" type="directional"><pose>0 0 500 0 0 0</pose><cast_shadows>true</cast_shadows><intensity>{intensity:.3f}</intensity><direction>{direction[0]:.4f} {direction[1]:.4f} {direction[2]:.4f}</direction><diffuse>1 1 1 1</diffuse><specular>0.2 0.2 0.2 1</specular></light>
  <model name="ground_plane"><static>true</static><link name="ground_link"><collision name="ground_collision"><geometry><plane><normal>0 0 1</normal><size>340 340</size></plane></geometry></collision><visual name="ground_visual"><geometry><plane><normal>0 0 1</normal><size>340 340</size></plane></geometry><material><ambient>0.25 0.38 0.22 1</ambient><diffuse>0.34 0.50 0.28 1</diffuse></material></visual></link></model>
  <model name="multirotor_home_pad"><static>true</static><link name="pad_link"><collision name="pad_collision"><pose>0 0 0.05 0 0 0</pose><geometry><cylinder><radius>5</radius><length>0.1</length></cylinder></geometry></collision><visual name="pad_visual"><pose>0 0 0.05 0 0 0</pose><geometry><cylinder><radius>5</radius><length>0.1</length></cylinder></geometry><material><ambient>0.15 0.16 0.18 1</ambient><diffuse>0.25 0.27 0.30 1</diffuse></material></visual></link></model>''']
    bits.extend(_obstacle_sdf(*item) for item in obstacles)
    bits.extend(_object_sdf(obj) for obj in objects)
    bits.append("</world></sdf>\n")
    return "\n".join(bits)


def _map_terrain_dirs(worlds_dir: Path) -> tuple[Path, Path]:
    """Return the map/DEM directories that ``builtin://maps|terrain`` resolve to."""
    repository_root = worlds_dir.resolve().parents[2]
    maps_dir = repository_root / "maps"
    terrain_dir = repository_root / "terrain"
    # For the normal repository path, worlds_dir is .../simulation/gazebo/worlds
    # and the assets live beside gazebo under simulation/.
    if worlds_dir.resolve().name == "worlds" and worlds_dir.resolve().parent.name == "gazebo":
        maps_dir = worlds_dir.resolve().parents[1] / "maps"
        terrain_dir = worlds_dir.resolve().parents[1] / "terrain"
    return maps_dir, terrain_dir


def _write_map_and_terrain(maps_dir: Path, terrain_dir: Path, world_id: str) -> None:
    maps_dir.mkdir(parents=True, exist_ok=True)
    terrain_dir.mkdir(parents=True, exist_ok=True)
    _write(maps_dir / f"{world_id}.geojson", {"schema_version": 1, "scenario_id": world_id, "coordinate_reference": "WGS84", "type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[ORIGIN_LON - 0.002, ORIGIN_LAT - 0.002], [ORIGIN_LON + 0.002, ORIGIN_LAT - 0.002], [ORIGIN_LON + 0.002, ORIGIN_LAT + 0.002], [ORIGIN_LON - 0.002, ORIGIN_LAT + 0.002], [ORIGIN_LON - 0.002, ORIGIN_LAT - 0.002]]]}}]})
    _write(terrain_dir / f"{world_id}.dem.json", {"schema_version": 1, "scenario_id": world_id, "coordinate_reference": "WGS84", "vertical_datum": "MSL", "origin": {"latitude_deg": ORIGIN_LAT, "longitude_deg": ORIGIN_LON, "elevation_m": 18.0}, "north_origin_m": -200.0, "east_origin_m": -200.0, "north_spacing_m": 100.0, "east_spacing_m": 100.0, "elevation_m": [[18.0] * 5 for _ in range(5)], "vertical_sigma_m": 0.4, "slope_sigma_m_per_m": 0.002})


def generate_worlds(count: int, seed: int, split_ratios: tuple[float, float, float] = (0.7, 0.15, 0.15), out_worlds: str | Path = "simulation/gazebo/worlds", out_scenarios: str | Path = "configs/scenarios/generated", real_time_factor: float = 1.0) -> dict[str, Any]:
    """Generate ``count`` worlds and return the deterministic index document."""
    if count < 1 or seed < 0 or real_time_factor <= 0:
        raise ValueError("count/seed must be non-negative (count > 0), and real_time_factor > 0")
    if len(split_ratios) != 3 or any(r < 0 for r in split_ratios) or not math.isclose(sum(split_ratios), 1.0, abs_tol=1e-9):
        raise ValueError("split_ratios must be three non-negative values summing to 1")
    worlds_dir, scenarios_dir = Path(out_worlds), Path(out_scenarios)
    maps_dir, terrain_dir = _map_terrain_dirs(worlds_dir)
    rng = random.Random(seed)
    entries: list[dict[str, Any]] = []
    for index in range(count):
        world_id = f"gen_w{index:04d}"
        split = _split(index, count, split_ratios)
        objects: list[_Object] = []
        obstacles: list[tuple[Any, ...]] = []
        occupied: list[tuple[float, float, float]] = [(0.0, 0.0, 8.0)]
        def point(radius: float) -> tuple[float, float]:
            for _ in range(1000):
                angle, distance = rng.uniform(-math.pi, math.pi), rng.uniform(22.0, WORLD_RADIUS_M - radius)
                x, y = math.cos(angle) * distance, math.sin(angle) * distance
                if all(math.hypot(x - ox, y - oy) >= radius + oradius + 3.0 for ox, oy, oradius in occupied):
                    occupied.append((x, y, radius))
                    return x, y
            raise RuntimeError("unable to place non-overlapping generated object")
        # Keep a clear +/-X takeoff corridor and populate the remainder.
        for obstacle_index in range(rng.randint(4, 8)):
            sx, sy, sz = rng.uniform(6, 16), rng.uniform(6, 16), rng.uniform(4, 18)
            radius = math.hypot(sx, sy) / 2 + 2.0
            x, y = point(radius)
            obstacles.append((f"obstacle_{obstacle_index:02d}", x, y, sz / 2, sx, sy, sz, (rng.uniform(.25,.65), rng.uniform(.25,.65), rng.uniform(.25,.65)), rng.uniform(-math.pi, math.pi)))
        target_color_cn, target_color, target_rgb = COLORS[index % len(COLORS)]
        target_kind_cn, target_kind = KINDS[index % len(KINDS)]
        x, y = point(4.0)
        objects.append(_Object(f"target_true_{index:04d}", x, y, 0.75, rng.uniform(-math.pi, math.pi), 4.0, f"{target_color}_{target_kind}", target_color_cn, target_rgb, target_kind, True))
        for distractor_index in range(rng.randint(0, 4)):
            color_cn, color, rgb = COLORS[(index + distractor_index + 1) % len(COLORS)]
            kind_cn, kind = KINDS[(index + distractor_index + 1) % len(KINDS)]
            dx, dy = point(4.0)
            objects.append(_Object(f"distractor_{index:04d}_{distractor_index:02d}", dx, dy, 0.75, rng.uniform(-math.pi, math.pi), 4.0, f"{color}_{kind}", color_cn, rgb, kind))
        sdf = _world_sdf(world_id, objects, obstacles, rng, real_time_factor)
        world_path = worlds_dir / f"{world_id}.sdf"
        worlds_dir.mkdir(parents=True, exist_ok=True)
        world_path.write_text(sdf, encoding="utf-8", newline="\n")
        targets = []
        for obj in objects:
            lat, lon = _lat_lon(obj.x, obj.y)
            targets.append({"target_id": obj.object_id, "label": obj.label, "class": obj.kind, "color": obj.color_name, "pose": {"x_east_m": round(obj.x, 3), "y_north_m": round(obj.y, 3), "z_up_m": round(obj.z, 3), "yaw_rad": round(obj.yaw, 6)}, "position": {"latitude_deg": round(lat, 8), "longitude_deg": round(lon, 8), "altitude_m": obj.z, "altitude_reference": "world"}, "behavior": "stationary"})
        scenario = {"schema_version": 1, "scenario_id": world_id, "scenario_version": 1, "channels": {}}
        runtime = {"schema_version": 1, "scenario_id": world_id, "scenario_version": 1, "seed": seed + index, "world": {"world_asset": f"builtin://gazebo/{world_id}", "map_asset": f"builtin://maps/{world_id}.geojson", "dem_asset": f"builtin://terrain/{world_id}.dem.json", "wind_north_mps": round(rng.uniform(-2, 2), 3), "wind_east_mps": round(rng.uniform(-2, 2), 3), "gust_mps": round(rng.uniform(0, 2), 3)}, "vehicle": {"model": "px4_multirotor_standard", "airframe_profile_id": "px4_multirotor_gazebo_v1", "spawn_id": f"{world_id}_home", "spawn_pose": {"x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.20, "roll_rad": 0.0, "pitch_rad": 0.0, "yaw_rad": 0.0}}, "sensors": [{"sensor_id": "front-camera", "kind": "rgb_camera", "stream_uri": f"gazebo://{world_id}/front-camera", "calibration_id": "gazebo-multirotor-front-camera-v1"}, {"sensor_id": "downward-depth", "kind": "depth_camera", "stream_uri": f"gazebo://{world_id}/downward-depth", "calibration_id": "gazebo-multirotor-depth-v1"}, {"sensor_id": "px4-telemetry", "kind": "mavlink_telemetry", "stream_uri": "udp://127.0.0.1:14541"}], "faults": []}
        truth = {"schema_version": 1, "scenario_id": world_id, "scenario_version": 1, "targets": targets}
        expectations = {"schema_version": 1, "scenario_id": world_id, "scenario_version": 1, "allowed_terminal_states": ["completed", "safe_rtl"], "maximum_geofence_violations": 0, "maximum_false_geolocations": 0, "maximum_unauthorized_commands": 0}
        runtime_path, truth_path, expectations_path = (scenarios_dir / f"{world_id}.{suffix}.json" for suffix in ("runtime", "truth", "expectations"))
        runtime_hash, truth_hash, expectations_hash = (_write(path, value) for path, value in ((runtime_path, runtime), (truth_path, truth), (expectations_path, expectations)))
        # Channel references are verified with canonical JSON hashes by
        # multidetect.simulation.index.load_verified_channel; index.json keeps raw file hashes.
        scenario["channels"] = {"runtime_input": {"path": runtime_path.name, "sha256": canonical_sha256(runtime)}, "truth": {"path": truth_path.name, "sha256": canonical_sha256(truth)}, "expectations": {"path": expectations_path.name, "sha256": canonical_sha256(expectations)}}
        scenario_path = scenarios_dir / f"{world_id}.scenario.json"
        scenario_hash = _write(scenario_path, scenario)
        entries.append({"world_id": world_id, "split": split, "seed": seed + index, "task_text": TASK_TEMPLATES[index % len(TASK_TEMPLATES)].format(color=target_color_cn, kind=target_kind_cn), "world_file": world_path.name, "files": {"world": {"path": world_path.name, "sha256": hashlib.sha256(world_path.read_bytes()).hexdigest()}, "scenario": {"path": scenario_path.name, "sha256": scenario_hash}, "runtime": {"path": runtime_path.name, "sha256": runtime_hash}, "truth": {"path": truth_path.name, "sha256": truth_hash}, "expectations": {"path": expectations_path.name, "sha256": expectations_hash}}, "true_target_id": objects[0].object_id, "target_pose": {"x_east_m": objects[0].x, "y_north_m": objects[0].y, "z_up_m": objects[0].z, "yaw_rad": objects[0].yaw}})
        _write_map_and_terrain(maps_dir, terrain_dir, world_id)
    index_doc = {"schema_version": 1, "generator": "multidetect.simulator.world_gen", "seed": seed, "count": count, "split_ratios": list(split_ratios), "real_time_factor": real_time_factor, "worlds": entries}
    _write(scenarios_dir / "index.json", index_doc)
    return index_doc


def generate_fuel_worlds(count: int = 40, seed: int = 20261011,
                         split_ratios: tuple[float, float, float] = (0.7, 0.15, 0.15),
                         out_worlds: str | Path = "simulation/gazebo/worlds",
                         out_scenarios: str | Path = "configs/scenarios/generated-v2",
                         real_time_factor: float = 1.0,
                         catalog_path: str | Path = "configs/sim-assets/fuel-catalog.json",
                         resolved_root: str | Path = "artifacts/cache/gz-fuel/resolved",
                         segmentation_labels: bool = False,
                         unambiguous_tasks: bool = False,
                         world_prefix: str = "gen_v2_w",
                         count_copies: int | Sequence[int] = 1,
                         count_slugs: tuple[str, ...] | None = None,
                         spread_m: tuple[float, float] = (24.0, 120.0),
                         confusers: Mapping[str, Sequence[str]] | None = None,
                         confuser_copies: int = 1) -> dict[str, Any]:
    """Generate realistic offline Fuel model worlds while preserving box mode.

    ``unambiguous_tasks`` keeps out distractors that the generated task text would
    also describe (e.g. a second person for "find the person"); it is off by
    default so existing world sets regenerate byte-identically.

    ``count_copies`` > 1 builds counting worlds: the target asset (cycled from
    ``count_slugs``) is placed that many times, every instance is marked
    ``count_member`` in truth, distractors the task would also describe are left
    out, and the index carries a counting task plus the per-object find task.
    A sequence of copy counts cycles per world to mix find and count tasks;
    ``count_slugs`` selects targets for both kinds of task.

    ``confusers`` forces semantically distinct distractor assets alongside each
    target, with ``confuser_copies`` instances per slug and ``confuser: true``
    in truth. Invalid or task-equivalent pairs fail before any files are written.
    """
    copy_counts = (count_copies,) if isinstance(count_copies, int) else tuple(count_copies)
    if not copy_counts or any(type(n) is not int or n < 1 for n in copy_counts):
        raise ValueError("count_copies must contain positive integer copy counts")
    if type(confuser_copies) is not int or confuser_copies not in (1, 2):
        raise ValueError("confuser_copies must be 1 or 2")
    if count < 1 or seed < 0 or real_time_factor <= 0:
        raise ValueError("count > 0, seed >= 0 and real_time_factor > 0 are required")
    if len(split_ratios) != 3 or any(r < 0 for r in split_ratios) or not math.isclose(sum(split_ratios), 1.0, abs_tol=1e-9):
        raise ValueError("split_ratios must be three non-negative values summing to 1")
    if len(spread_m) != 2 or not 0 <= spread_m[0] < spread_m[1] <= WORLD_RADIUS_M:
        raise ValueError("spread_m must satisfy 0 <= min < max <= WORLD_RADIUS_M")
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    assets = list(catalog.get("assets", []))
    eligible = [a for a in assets if "target" in a.get("role_eligibility", [])]
    scenery = [a for a in assets if "scenery-obstacle" in a.get("role_eligibility", [])]
    asset_by_slug = {a["slug"]: a for a in assets}
    if not eligible or not scenery:
        raise ValueError("fuel catalog has no eligible assets")
    for slug in count_slugs or ():
        if slug not in asset_by_slug or "target" not in asset_by_slug[slug].get("role_eligibility", []):
            raise ValueError(f"ineligible target slug: {slug}")
    confuser_assets: dict[str, list[dict[str, Any]]] = {}
    for target_slug, slugs in (confusers or {}).items():
        if target_slug not in asset_by_slug or "target" not in asset_by_slug[target_slug].get("role_eligibility", []):
            raise ValueError(f"ineligible confuser target slug: {target_slug}")
        if isinstance(slugs, str):
            raise ValueError("confuser slugs must be a sequence of slugs, not a string")
        confuser_assets[target_slug] = []
        for slug in dict.fromkeys(slugs):
            if slug not in asset_by_slug or "distractor" not in asset_by_slug[slug].get("role_eligibility", []):
                raise ValueError(f"ineligible confuser slug: {slug}")
            asset = asset_by_slug[slug]
            if task_equivalent(asset_by_slug[target_slug], asset):
                raise ValueError(f"confuser {slug} also matches task for {target_slug}")
            confuser_assets[target_slug].append(asset)
    rng = random.Random(seed); worlds_dir, scenarios_dir = Path(out_worlds), Path(out_scenarios)
    worlds_dir.mkdir(parents=True, exist_ok=True); scenarios_dir.mkdir(parents=True, exist_ok=True)
    entries = []; target_combos = []
    train_target_slugs = [a["slug"] for a in eligible[: max(1, len(eligible) - 6)]]
    test_pool = [a for a in eligible if a["slug"] not in train_target_slugs] or eligible
    def inline_asset(slug: str, object_id: str, x: float, y: float, yaw: float, bottom_z: float,
                     label_id: int | None = None) -> str:
        source = Path(resolved_root) / slug / "model.sdf"
        root = ET.fromstring(source.read_text(encoding="utf-8-sig").lstrip())
        model = root.find("model")
        if model is None and root.find("actor") is not None:
            actor = root.find("actor")
            filename = actor.findtext("skin/filename", "")
            size = asset_by_slug[slug]["measured_size_m"]
            model = ET.fromstring(
                f'<model name="{object_id}"><static>true</static><link name="link">'
                f'<collision name="collision"><geometry><box><size>{size[0]} {size[1]} {size[2]}</size></box></geometry></collision>'
                f'<visual name="visual"><geometry><mesh><uri>model://{slug}/{filename}</uri></mesh></geometry></visual>'
                f'</link></model>'
            )
        if model is None:
            raise ValueError(f"asset {slug} has no model or actor element")
        if model.findall(".//include"):
            raise ValueError(f"asset {slug} contains nested include")
        model = ET.fromstring(ET.tostring(model, encoding="unicode"))
        model.set("name", "mesh")
        # The outer model is placed at the measured bbox centre (truth x/y/z),
        # and its first collision is that box so load_sdf_obstacles and the
        # depth teacher see real footprints; the mesh is nested with the
        # inverse offset so its bottom rests on the ground.
        lo, hi = asset_by_slug[slug]["bbox_min_m"], asset_by_slug[slug]["bbox_max_m"]
        size = [h - lo_ for lo_, h in zip(lo, hi)]
        cx, cy = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
        pose = model.find("pose")
        if pose is None:
            pose = ET.Element("pose")
            model.insert(0, pose)
        pose.text = f"{-cx:.6f} {-cy:.6f} {-lo[2] - size[2] / 2.0:.6f} 0 0 0"
        if model.find("static") is None:
            model.insert(1, ET.fromstring("<static>true</static>"))
        outer = ET.fromstring(
            f'<model name="{object_id}"><static>true</static>'
            f'<pose>{x:.3f} {y:.3f} {size[2] / 2.0:.6f} 0 0 {yaw:.6f}</pose>'
            f'<link name="bounds"><collision name="bounds_collision"><geometry><box>'
            f'<size>{size[0]:.4f} {size[1]:.4f} {size[2]:.4f}</size></box></geometry></collision></link>'
            f'</model>'
        )
        if label_id is not None:
            if not 1 <= label_id <= 254:
                raise ValueError("segmentation label ids must be between 1 and 254")
            # Gazebo Harmonic's Label system annotates rendered segmentation
            # images with this stable per-model integer.  Keep the plugin out
            # of the default path so existing worlds remain byte-identical.
            label_plugin = ET.fromstring(
                f'<plugin filename="gz-sim-label-system" name="gz::sim::systems::Label">'
                f'<label>{label_id}</label></plugin>'
            )
            outer.append(label_plugin)
            # Explicitly label every nested model too: Fuel meshes can have
            # another model level, and the outer bounds link has no visual.
            for mesh_model in model.iter("model"):
                mesh_model.append(ET.fromstring(ET.tostring(label_plugin, encoding="unicode")))
        outer.append(model)
        for uri in model.iter("uri"):
            value = (uri.text or "").strip()
            if value.startswith("model://"):
                relative = value.split("/", 3)[-1] if value.count("/") >= 3 else ""
                uri.text = f"model://{slug}/{relative}" if relative else f"model://{slug}"
            elif value and not value.startswith(("file://", "http://", "https://")):
                uri.text = f"model://{slug}/{value.lstrip('./')}"
            elif value.startswith("http://") or value.startswith("https://"):
                raise ValueError(f"asset {slug} contains network URI")
        return ET.tostring(outer, encoding="unicode")
    def pose_point(used: list[tuple[float, float, float]], radius: float) -> tuple[float, float]:
        for _ in range(1000):
            angle, distance = rng.uniform(-math.pi, math.pi), rng.uniform(*spread_m)
            x, y = math.cos(angle) * distance, math.sin(angle) * distance
            if all(math.hypot(x - ox, y - oy) > radius + rr + 4 for ox, oy, rr in used):
                used.append((x, y, radius)); return x, y
        raise RuntimeError("unable to place fuel asset")
    for i in range(count):
        copies = copy_counts[i % len(copy_counts)]
        counting = copies > 1
        world_id = f"{world_prefix}{i:04d}"; split = _split(i, count, split_ratios); used=[(0,0,8)]
        train_pool = [a for a in eligible if a["slug"] in train_target_slugs] or eligible
        pool = test_pool if split == "test" else train_pool
        target = pool[(i * 7 + (11 if split == "test" else 0)) % len(pool)]
        if count_slugs:
            target = asset_by_slug[count_slugs[i % len(count_slugs)]]
        target_combos.append({"category": target["category"], "color": target.get("color"), "slug": target["slug"], "split": split})
        objects=[]
        tx,ty=pose_point(used, max(target["measured_size_m"][:2])/2)
        objects.append(("target_true_"+world_id, target, tx, ty, rng.uniform(-math.pi, math.pi), True))
        count_ids = {"target_true_"+world_id}
        for k in range(1, copies):
            cx, cy = pose_point(used, max(target["measured_size_m"][:2])/2)
            copy_id = f"count_member_{i:04d}_{k:02d}"
            count_ids.add(copy_id)
            objects.append((copy_id, target, cx, cy, rng.uniform(-math.pi, math.pi), False))
        confuser_ids: set[str] = set()
        for j, asset in enumerate(confuser_assets.get(target["slug"], [])):
            for k in range(confuser_copies):
                x, y = pose_point(used, max(asset["measured_size_m"][:2])/2)
                oid = f"confuser_{i:04d}_{j:02d}_{k:02d}"
                confuser_ids.add(oid)
                objects.append((oid, asset, x, y, rng.uniform(-math.pi, math.pi), False))
        same_category = [a for a in eligible if a["slug"] != target["slug"] and a["category"] == target["category"]]
        distractor_pool = same_category + [a for a in eligible if a["slug"] != target["slug"] and a not in same_category]
        if unambiguous_tasks or counting:
            distractor_pool = [a for a in distractor_pool if not task_equivalent(target, a)]
        for j in range(rng.randint(2,5)):
            asset = distractor_pool[(i+j*3)%len(distractor_pool)]; x,y=pose_point(used,max(asset["measured_size_m"][:2])/2)
            objects.append((f"distractor_{i:04d}_{j:02d}",asset,x,y,rng.uniform(-math.pi,math.pi),False))
        models=[]
        segmentation_map: dict[str, dict[str, Any]] = {}
        next_label = 1
        for j,asset in enumerate(rng.sample(scenery, min(len(scenery), rng.randint(3,6)))):
            x,y=pose_point(used,max(asset["measured_size_m"][:2])/2+2)
            obstacle_id = f"obstacle_{j:02d}"
            label_id = next_label if segmentation_labels else None
            if segmentation_labels:
                segmentation_map[str(label_id)] = {"target_id": obstacle_id, "asset_slug": asset["slug"], "role": "obstacle"}
                next_label += 1
            models.append(inline_asset(asset["slug"], obstacle_id, x, y, rng.uniform(-math.pi,math.pi), asset["mesh_origin_offset_m"]["z_bottom"], label_id))
        for oid,asset,x,y,yaw,is_target in objects:
            z=asset["mesh_origin_offset_m"]["z_bottom"]
            label_id = next_label if segmentation_labels else None
            if segmentation_labels:
                segmentation_map[str(label_id)] = {
                    "target_id": oid,
                    "asset_slug": asset["slug"],
                    "role": "target" if is_target else (
                        "count_member" if counting and oid in count_ids else
                        "confuser" if oid in confuser_ids else "distractor"
                    ),
                }
                next_label += 1
            models.append(inline_asset(asset["slug"], oid, x, y, yaw, z, label_id))
        sdf=f'''<?xml version="1.0"?><sdf version="1.9"><world name="default"><physics name="px4_ode" type="ode"><max_step_size>0.004</max_step_size><real_time_factor>{real_time_factor:.6f}</real_time_factor></physics><gravity>0 0 -9.80665</gravity><scene><ambient>0.45 0.45 0.45 1</ambient><background>0.75 0.80 0.88 1</background><shadows>true</shadows></scene><light name="sun" type="directional"><pose>0 0 500 0 0 0</pose><direction>-0.4 0.3 -0.8</direction><cast_shadows>true</cast_shadows></light><model name="ground_plane"><static>true</static><link name="ground_link"><collision name="ground_collision"><geometry><plane><normal>0 0 1</normal><size>340 340</size></plane></geometry></collision><visual name="ground_visual"><geometry><plane><normal>0 0 1</normal><size>340 340</size></plane></geometry><material><diffuse>0.34 0.50 0.28 1</diffuse></material></visual></link></model>\n{chr(10).join(models)}</world></sdf>\n'''
        world_path=worlds_dir/f"{world_id}.sdf"
        sdf=sdf.replace("<gravity>0 0 -9.80665</gravity>", f"<gravity>0 0 -9.80665</gravity><spherical_coordinates><surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation><latitude_deg>{ORIGIN_LAT}</latitude_deg><longitude_deg>{ORIGIN_LON}</longitude_deg><elevation>18</elevation></spherical_coordinates>")
        world_path.write_text(sdf,encoding="utf-8",newline="\n")
        _write_map_and_terrain(*_map_terrain_dirs(worlds_dir), world_id)
        target_rows=[]
        for oid,a,x,y,yaw,is_target in objects:
            height = float(a["measured_size_m"][2]); center_height = height / 2.0
            lat,lon=_lat_lon(x,y); target_rows.append({"target_id":oid,"label":(" ".join(a["semantic_words"][:2])),"class":a["category"],"category_words":a["semantic_words"],"asset_slug":a["slug"],"color":a.get("color"),"pose":{"x_east_m":round(x,3),"y_north_m":round(y,3),"z_up_m":round(center_height,3),"yaw_rad":round(yaw,6)},"position":{"latitude_deg":round(lat,8),"longitude_deg":round(lon,8),"altitude_m":round(center_height,3),"altitude_reference":"world"},"size_m":a["measured_size_m"],"measured_size_m":a["measured_size_m"],"bottom_offset_m":round(-center_height,6),"behavior":"stationary","true_target":is_target, **({"count_member": oid in count_ids} if counting else {})})
        runtime={"schema_version":1,"scenario_id":world_id,"scenario_version":1,"seed":seed+i,"world":{"world_asset":f"builtin://gazebo/{world_id}","map_asset":f"builtin://maps/{world_id}.geojson","dem_asset":f"builtin://terrain/{world_id}.dem.json","wind_north_mps":0.0,"wind_east_mps":0.0,"gust_mps":0.0},"vehicle":{"model":"px4_multirotor_standard","airframe_profile_id":"px4_multirotor_gazebo_v1","spawn_id":f"{world_id}_home","spawn_pose":{"x_east_m":0.0,"y_north_m":0.0,"z_up_m":0.2,"roll_rad":0.0,"pitch_rad":0.0,"yaw_rad":0.0}},"sensors":[{"sensor_id":"front-camera","kind":"rgb_camera","stream_uri":f"gazebo://{world_id}/front-camera","calibration_id":"gazebo-multirotor-front-camera-v1"},{"sensor_id":"downward-depth","kind":"depth_camera","stream_uri":f"gazebo://{world_id}/downward-depth","calibration_id":"gazebo-multirotor-depth-v1"},{"sensor_id":"px4-telemetry","kind":"mavlink_telemetry","stream_uri":"udp://127.0.0.1:14541"}],"faults":[]}
        for row in target_rows:
            if row["target_id"] in confuser_ids:
                row["confuser"] = True
        truth={"schema_version":1,"scenario_id":world_id,"scenario_version":1,"asset_mode":"fuel_v1","targets":target_rows}
        if segmentation_labels:
            truth["segmentation_labels"] = segmentation_map
        expectations={"schema_version":1,"scenario_id":world_id,"scenario_version":1,"allowed_terminal_states":["completed","safe_rtl"],"maximum_geofence_violations":0,"maximum_false_geolocations":0,"maximum_unauthorized_commands":0}
        for suffix,data in (("runtime_input",runtime),("truth",truth),("expectations",expectations)):
            _write(scenarios_dir/f"{world_id}.{suffix}.json",data)
        scenario={"schema_version":1,"scenario_id":world_id,"scenario_version":1,"channels":{k:{"path":f"{world_id}.{k}.json","sha256":canonical_sha256(v)} for k,v in (("runtime_input",runtime),("truth",truth),("expectations",expectations))}}
        _write(scenarios_dir/f"{world_id}.scenario.json",scenario)
        task_text, task_text_en = fuel_task_texts(target)
        counting_fields = {}
        if counting:
            counting_fields = {"task_kind": "count", "expected_count": copies,
                               "find_task_text": task_text, "find_task_text_en": task_text_en}
            task_text, task_text_en = fuel_count_texts(target)
        elif confusers is not None:
            counting_fields = {"task_kind": "find", "expected_count": 1,
                               "find_task_text": task_text, "find_task_text_en": task_text_en}
        entries.append({"world_id":world_id,"split":split,"seed":seed+i,"task_text":task_text,"task_text_en":task_text_en,"asset_mode":"fuel_v1","target_combo":{"slug":target["slug"],"category":target["category"],"color":target.get("color")},"world_file":world_path.name,"true_target_id":objects[0][0], **counting_fields})
    doc={"schema_version":1,"generator":"multidetect.simulator.world_gen","asset_mode":"fuel_v1","seed":seed,"count":count,"split_ratios":list(split_ratios),"held_out_target_combos":[x for x in target_combos if x["split"]=="test"],"worlds":entries}; _write(scenarios_dir/"index.json",doc); return doc


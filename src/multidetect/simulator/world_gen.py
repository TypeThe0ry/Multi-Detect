# ruff: noqa: E501, B023
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def generate_worlds(count: int, seed: int, split_ratios: tuple[float, float, float] = (0.7, 0.15, 0.15), out_worlds: str | Path = "simulation/gazebo/worlds", out_scenarios: str | Path = "configs/scenarios/generated", real_time_factor: float = 1.0) -> dict[str, Any]:
    """Generate ``count`` worlds and return the deterministic index document."""
    if count < 1 or seed < 0 or real_time_factor <= 0:
        raise ValueError("count/seed must be non-negative (count > 0), and real_time_factor > 0")
    if len(split_ratios) != 3 or any(r < 0 for r in split_ratios) or not math.isclose(sum(split_ratios), 1.0, abs_tol=1e-9):
        raise ValueError("split_ratios must be three non-negative values summing to 1")
    worlds_dir, scenarios_dir = Path(out_worlds), Path(out_scenarios)
    repository_root = worlds_dir.resolve().parents[2]
    maps_dir = repository_root / "maps"
    terrain_dir = repository_root / "terrain"
    # For the normal repository path, worlds_dir is .../simulation/gazebo/worlds
    # and the assets live beside gazebo under simulation/.
    if worlds_dir.resolve().name == "worlds" and worlds_dir.resolve().parent.name == "gazebo":
        maps_dir = worlds_dir.resolve().parents[1] / "maps"
        terrain_dir = worlds_dir.resolve().parents[1] / "terrain"
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
        scenario["channels"] = {"runtime_input": {"path": runtime_path.name, "sha256": runtime_hash}, "truth": {"path": truth_path.name, "sha256": truth_hash}, "expectations": {"path": expectations_path.name, "sha256": expectations_hash}}
        scenario_path = scenarios_dir / f"{world_id}.scenario.json"
        scenario_hash = _write(scenario_path, scenario)
        entries.append({"world_id": world_id, "split": split, "seed": seed + index, "task_text": TASK_TEMPLATES[index % len(TASK_TEMPLATES)].format(color=target_color_cn, kind=target_kind_cn), "world_file": world_path.name, "files": {"world": {"path": world_path.name, "sha256": hashlib.sha256(world_path.read_bytes()).hexdigest()}, "scenario": {"path": scenario_path.name, "sha256": scenario_hash}, "runtime": {"path": runtime_path.name, "sha256": runtime_hash}, "truth": {"path": truth_path.name, "sha256": truth_hash}, "expectations": {"path": expectations_path.name, "sha256": expectations_hash}}, "true_target_id": objects[0].object_id, "target_pose": {"x_east_m": objects[0].x, "y_north_m": objects[0].y, "z_up_m": objects[0].z, "yaw_rad": objects[0].yaw}})
        maps_dir.mkdir(parents=True, exist_ok=True)
        terrain_dir.mkdir(parents=True, exist_ok=True)
        _write(maps_dir / f"{world_id}.geojson", {"schema_version": 1, "scenario_id": world_id, "coordinate_reference": "WGS84", "type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[ORIGIN_LON - 0.002, ORIGIN_LAT - 0.002], [ORIGIN_LON + 0.002, ORIGIN_LAT - 0.002], [ORIGIN_LON + 0.002, ORIGIN_LAT + 0.002], [ORIGIN_LON - 0.002, ORIGIN_LAT + 0.002], [ORIGIN_LON - 0.002, ORIGIN_LAT - 0.002]]]}}]})
        _write(terrain_dir / f"{world_id}.dem.json", {"schema_version": 1, "scenario_id": world_id, "coordinate_reference": "WGS84", "vertical_datum": "MSL", "origin": {"latitude_deg": ORIGIN_LAT, "longitude_deg": ORIGIN_LON, "elevation_m": 18.0}, "north_origin_m": -200.0, "east_origin_m": -200.0, "north_spacing_m": 100.0, "east_spacing_m": 100.0, "elevation_m": [[18.0] * 5 for _ in range(5)], "vertical_sigma_m": 0.4, "slope_sigma_m_per_m": 0.002})
    index_doc = {"schema_version": 1, "generator": "multidetect.simulator.world_gen", "seed": seed, "count": count, "split_ratios": list(split_ratios), "real_time_factor": real_time_factor, "worlds": entries}
    _write(scenarios_dir / "index.json", index_doc)
    return index_doc


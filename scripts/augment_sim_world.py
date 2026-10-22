#!/usr/bin/env python3
# ruff: noqa: E501
"""为 Gazebo 世界生成可复现的风、光照、噪声、演员和地形变体。

变体只写入 ``simulation/gazebo/worlds/variants``，不会改写输入世界。
风只写 world ``<wind>`` 和一条阵风注释；世界里一旦声明任何 ``<plugin>``，
Gazebo 就不再加载默认系统、世界永远无法就绪。WindEffects 由容器启动补丁
（``run_simulator_docker._ENABLE_WIND_EFFECTS``）按需加入 server config。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ROOT / "simulation/gazebo/worlds/variants"


def _child(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    node = parent.find(tag)
    if node is None:
        node = ET.SubElement(parent, tag)
    if text is not None:
        node.text = text
    return node


def _suffix(args: argparse.Namespace) -> str:
    bits = []
    if args.wind_mps is not None:
        bits.append(f"wind{args.wind_mps:g}")
    if args.gust_mps is not None:
        bits.append(f"gust{args.gust_mps:g}p{args.gust_period:g}")
    if args.camera_noise is not None:
        bits.append(f"noise{args.camera_noise:g}")
    if args.time_of_day:
        bits.append(args.time_of_day)
    if args.fog:
        bits.append("fog")
    if args.actors:
        bits.append(f"actors{args.actors}")
    if args.terrain:
        bits.append(args.terrain)
    return "__" + "_".join(bits or ["base"])


def _add_wind(world: ET.Element, speed: float, gust: float | None, period: float) -> None:
    # Only <wind> goes into the world: any world <plugin> replaces Gazebo's default
    # systems and the world never becomes ready.  The container start-up patch
    # (run_simulator_docker._ENABLE_WIND_EFFECTS) adds WindEffects to the server
    # config and reads the gust amplitude/period from this comment.
    for plugin in [p for p in world.findall("plugin") if "WindEffects" in (p.get("name") or "")]:
        world.remove(plugin)
    for comment in [
        c for c in world if c.tag is ET.Comment and "multidetect-wind-gust" in (c.text or "")
    ]:
        world.remove(comment)
    wind = _child(world, "wind")
    _child(wind, "linear_velocity", f"{speed:.6g} 0 0")
    if gust is not None and speed > 0:
        amplitude = min(1.0, gust / speed)
        world.insert(
            0,
            ET.Comment(
                f" multidetect-wind-gust amplitude_percent={amplitude:.4g} period={period:.6g} "
            ),
        )


def _add_camera_noise(world: ET.Element, stddev: float) -> int:
    count = 0
    for sensor in world.iter("sensor"):
        if sensor.get("type") not in {"camera", "rgbd_camera", "depth_camera", "segmentation"}:
            continue
        noise = sensor.find(".//noise")
        if noise is None:
            camera = sensor.find("camera")
            if camera is None:
                continue
            noise = ET.SubElement(camera, "noise")
        _child(noise, "type", "gaussian")
        _child(noise, "mean", "0")
        _child(noise, "stddev", f"{stddev:.6g}")
        count += 1
    return count


def _add_actor(world: ET.Element, index: int) -> None:
    # A generated local mesh is deliberate: no Fuel/model:// dependency at sim time.
    actor = ET.SubElement(world, "actor", {"name": f"variant_actor_{index:02d}"})
    ET.SubElement(actor, "pose").text = f"{index * 4 - 2:.3f} {index * 2:.3f} 0 0 0 0"
    skin = ET.SubElement(actor, "skin")
    ET.SubElement(skin, "filename").text = "file://actor_box.dae"
    ET.SubElement(skin, "scale").text = "1"
    script = ET.SubElement(actor, "script")
    ET.SubElement(script, "loop").text = "true"
    ET.SubElement(script, "auto_start").text = "true"
    trajectory = ET.SubElement(script, "trajectory", {"id": "0", "type": "walk"})
    for time, x in ((0, index * 4 - 2), (20, index * 4 + 8)):
        waypoint = ET.SubElement(trajectory, "waypoint")
        ET.SubElement(waypoint, "time").text = str(time)
        ET.SubElement(waypoint, "pose").text = f"{x:.3f} {index * 2:.3f} 0 0 0 0"


def _write_actor_mesh(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.parent.joinpath("actor_box.dae").write_text(
        "<?xml version='1.0'?><COLLADA xmlns='http://www.collada.org/2005/11/COLLADASchema' version='1.4.1'>"
        "<asset><up_axis>Z_UP</up_axis></asset><library_geometries><geometry id='box'><mesh>"
        "<source id='p'><float_array id='pa' count='24'>-0.2 -0.2 0 0.2 -0.2 0 0.2 0.2 0 -0.2 0.2 0 -0.2 -0.2 1.6 0.2 -0.2 1.6 0.2 0.2 1.6 -0.2 0.2 1.6</float_array>"
        "<technique_common><accessor source='#pa' count='8' stride='3'><param name='X' type='float'/><param name='Y' type='float'/><param name='Z' type='float'/></accessor></technique_common></source>"
        "<vertices id='v'><input semantic='POSITION' source='#p'/></vertices><polylist count='6'><input semantic='VERTEX' source='#v' offset='0'/><vcount>4 4 4 4 4 4</vcount><p>0 1 2 3 4 7 6 5 0 4 5 1 1 5 6 2 2 6 7 3 4 0 3 7</p></polylist>"
        "</mesh></geometry></library_geometries><library_visual_scenes><visual_scene id='Scene'><node id='box'><instance_geometry url='#box'/></node></visual_scene></library_visual_scenes><scene><instance_visual_scene url='#Scene'/></scene></COLLADA>\n",
        encoding="ascii",
    )


def _add_terrain(world: ET.Element, kind: str, output: Path) -> None:
    model = ET.SubElement(world, "model", {"name": "variant_terrain"})
    ET.SubElement(model, "static").text = "true"
    link = ET.SubElement(model, "link", {"name": "terrain_link"})
    collision = ET.SubElement(link, "collision", {"name": "terrain_collision"})
    geometry = ET.SubElement(collision, "geometry")
    if kind == "dem":
        hm = ET.SubElement(geometry, "heightmap")
        ET.SubElement(hm, "uri").text = "file://terrain.pgm"
        ET.SubElement(hm, "size").text = "120 120 8"
        ET.SubElement(hm, "pos").text = "0 0 0"
    else:
        box = ET.SubElement(geometry, "box")
        ET.SubElement(box, "size").text = "120 120 2"
        ET.SubElement(collision, "pose").text = "0 0 1 0 0 0"
    visual = ET.SubElement(link, "visual", {"name": "terrain_visual"})
    vgeom = ET.SubElement(visual, "geometry")
    if kind == "dem":
        hm = ET.SubElement(vgeom, "heightmap")
        ET.SubElement(hm, "uri").text = "file://terrain.pgm"
        ET.SubElement(hm, "size").text = "120 120 8"
    else:
        box = ET.SubElement(vgeom, "box")
        ET.SubElement(box, "size").text = "120 120 2"
        ET.SubElement(visual, "pose").text = "0 0 1 0 0 0"
    if kind == "dem":
        # A tiny deterministic local DEM; callers can replace it with their own PGM.
        pgm = output.parent / "terrain.pgm"
        pgm.write_text("P2\n3 3\n255\n0 64 128\n64 128 192\n128 192 255\n", encoding="ascii")


def transform(source: Path, output: Path, args: argparse.Namespace) -> dict[str, object]:
    root = ET.parse(source).getroot()
    world = root.find("world")
    if world is None:
        raise ValueError("SDF has no world element")
    if args.wind_mps is not None:
        _add_wind(world, args.wind_mps, args.gust_mps, args.gust_period)
    noise_sensors = (
        _add_camera_noise(world, args.camera_noise) if args.camera_noise is not None else 0
    )
    if args.time_of_day:
        light = world.find("light")
        if light is not None:
            direction = {
                "dawn": "-0.15 0.65 -0.75",
                "noon": "-0.4 0.3 -0.8",
                "dusk": "0.25 -0.65 -0.7",
            }[args.time_of_day]
            _child(light, "direction", direction)
            _child(
                light, "intensity", {"dawn": "0.55", "noon": "1.0", "dusk": "0.3"}[args.time_of_day]
            )
        scene = _child(world, "scene")
        _child(
            scene,
            "ambient",
            {"dawn": "0.22 0.25 0.32 1", "noon": "0.45 0.45 0.45 1", "dusk": "0.12 0.14 0.2 1"}[
                args.time_of_day
            ],
        )
    # Harmonic Ogre2 fog is renderer/config dependent; intentionally skipped.
    if args.actors:
        _write_actor_mesh(output)
    for i in range(args.actors):
        _add_actor(world, i)
    if args.terrain:
        _add_terrain(world, args.terrain, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return {
        "camera_sensors_noised": noise_sensors,
        "fog_note": "Ogre2 support depends on pinned renderer" if args.fog else None,
    }


def register_variant(index: Path, source_name: str, variant_name: str) -> None:
    data = json.loads(index.read_text(encoding="utf-8"))
    worlds = data.setdefault("worlds", [])
    source = next((item for item in worlds if item.get("world_file") == source_name), None)
    source_dir = index.parent
    target_dir = index.parent
    if source is None:
        fallback = ROOT / "configs/scenarios/generated-count-site/index.json"
        if fallback.is_file():
            base = json.loads(fallback.read_text(encoding="utf-8"))
            source = next(
                (item for item in base.get("worlds", []) if item.get("world_file") == source_name),
                None,
            )
            source_dir = fallback.parent
    if source is None:
        raise ValueError(f"source world not found in {index}: {source_name}")
    entry = dict(source)
    entry["world_file"] = variant_name
    entry["world_id"] = Path(variant_name).stem
    entry["variant_of"] = source_name
    variant_stem = Path(variant_name).stem
    source_stem = Path(source_name).stem
    for suffix in (".scenario.json", ".runtime_input.json", ".truth.json", ".expectations.json"):
        source_file = source_dir / f"{source_stem}{suffix}"
        target_file = target_dir / f"{variant_stem}{suffix}"
        if source_file.is_file():
            payload = json.loads(source_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["scenario_id"] = variant_stem
                if suffix == ".scenario.json":
                    payload["channels"] = {
                        name: {"path": f"{variant_stem}.{name}.json"}
                        for name in ("expectations", "runtime_input", "truth")
                    }
                if suffix == ".runtime_input.json":
                    payload.setdefault("world", {})["world_asset"] = (
                        f"builtin://gazebo/{variant_stem}"
                    )
                    payload["world"]["map_asset"] = f"builtin://maps/{variant_stem}.geojson"
                    payload["world"]["dem_asset"] = f"builtin://terrain/{variant_stem}.dem.json"
                target_file.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            else:
                shutil.copyfile(source_file, target_file)
    for asset_kind, suffix in (("maps", ".geojson"), ("terrain", ".dem.json")):
        original = ROOT / "simulation" / asset_kind / f"{source_stem}{suffix}"
        target = ROOT / "simulation" / asset_kind / f"{variant_stem}{suffix}"
        if original.is_file():
            asset = json.loads(original.read_text(encoding="utf-8"))
            asset["scenario_id"] = variant_stem
            target.write_text(
                json.dumps(asset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    entry["files"] = {
        "scenario": {"path": f"{variant_stem}.scenario.json"},
        "runtime_input": {"path": f"{variant_stem}.runtime_input.json"},
        "truth": {"path": f"{variant_stem}.truth.json"},
        "expectations": {"path": f"{variant_stem}.expectations.json"},
        "world": {"path": f"{variant_name}"},
    }
    scenario_file = target_dir / f"{variant_stem}.scenario.json"
    if scenario_file.is_file():
        payload = json.loads(scenario_file.read_text(encoding="utf-8"))
        payload["channels"] = {
            name: {
                "path": f"{variant_stem}.{name}.json",
                "sha256": hashlib.sha256(
                    json.dumps(
                        json.loads(
                            (target_dir / f"{variant_stem}.{name}.json").read_text(encoding="utf-8")
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for name in ("expectations", "runtime_input", "truth")
        }
        scenario_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    worlds.append(entry)
    data["count"] = len(worlds)
    index.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--wind-mps", type=float)
    parser.add_argument("--gust-mps", type=float)
    parser.add_argument("--gust-period", type=float, default=5.0)
    parser.add_argument("--camera-noise", nargs="?", const=0.01, type=float)
    parser.add_argument("--time-of-day", choices=("dawn", "noon", "dusk"))
    parser.add_argument("--fog", action="store_true")
    parser.add_argument("--actors", type=int, default=0)
    parser.add_argument("--terrain", choices=("dem", "procedural"))
    parser.add_argument("--out-dir", type=Path, default=VARIANTS)
    parser.add_argument("--register-index", type=Path)
    parser.add_argument(
        "--px4-world",
        metavar="NAME",
        help="copy an offline PX4 world named NAME from --px4-world-root after checking local assets",
    )
    parser.add_argument("--px4-world-root", type=Path)
    args = parser.parse_args()
    if args.px4_world:
        if args.px4_world_root is None:
            parser.error("--px4-world requires --px4-world-root")
        source = args.px4_world_root / f"{args.px4_world}.sdf"
        if not source.is_file():
            parser.error(f"offline PX4 world not found: {source}")
        text = source.read_text(encoding="utf-8", errors="replace")
        external = sorted(set(re.findall(r"model://([A-Za-z0-9_.-]+)", text)))
        missing = [name for name in external if not (args.out_dir / name).is_dir()]
        if missing:
            parser.error(
                "offline PX4 world references assets not present in output cache: "
                + ", ".join(missing)
            )
        output = args.out_dir / source.name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(json.dumps({"output": str(output), "px4_world": args.px4_world, "assets": external}))
        return 0
    if args.wind_mps is None and args.gust_mps is not None:
        parser.error("--gust-mps requires --wind-mps")
    if args.gust_period <= 0 or args.actors < 0:
        parser.error("gust period and actors must be non-negative (period > 0)")
    source = args.source.resolve()
    output = args.out_dir.resolve() / f"{source.stem}{_suffix(args)}.sdf"
    report = transform(source, output, args)
    if args.register_index:
        register_variant(args.register_index.resolve(), source.name, output.name)
    print(json.dumps({"output": str(output), **report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

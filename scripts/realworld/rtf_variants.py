#!/usr/bin/env python3
"""生成真实世界 RTF 实验变体；默认世界和机型文件保持不变。"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
WORLD_ROOT = ROOT / "simulation/gazebo/worlds/variants"
SCENARIO_ROOT = ROOT / "configs/scenarios/realworld"
MODEL_ROOT = ROOT / "simulation/gazebo/models"
MONO_TEMPLATE = ROOT / "simulation/gazebo/model-overlays/mono_cam_noise"

VARIANTS = {
    "V0": {"description": "基线；保持默认物理、阴影和相机", "shadows": None, "camera_hz": 30, "width": 1280, "height": 960, "camera_overlay": False},
    "V1": {"description": "相机降到 10 Hz、640x480", "shadows": None, "camera_hz": 10, "width": 640, "height": 480, "camera_overlay": True},
    "V2": {"description": "关闭场景与太阳阴影", "shadows": False, "camera_hz": 30, "width": 1280, "height": 960, "camera_overlay": True},
    "V4": {"description": "V1 + V2 + 0.008 s 物理步长", "shadows": False, "camera_hz": 10, "width": 640, "height": 480, "max_step": 0.008, "update_rate": 125, "camera_overlay": True},
    "V5": {"description": "budget_6km 视觉 LOD；8 m 碰撞地形保持不变", "terrain_budget_profile": "budget_6km", "camera_overlay": False},
    "V6": {"description": "保持 1280x960 与标定，只将 RGB/深度相机降至 10 Hz", "camera_hz": 10, "width": 1280, "height": 960, "camera_overlay": True, "preserve_camera_geometry": True},
    "V6_5": {"description": "V6 子变体：保持 1280x960 与标定，只将 RGB/深度相机降至 5 Hz", "camera_hz": 5, "width": 1280, "height": 960, "camera_overlay": True, "preserve_camera_geometry": True},
    "V7": {"description": "关闭合并建筑碰撞，保留完整 8 m 地形碰撞", "remove_building_collision": True, "camera_overlay": False},
    "V8": {"description": "V6 + V7：1280x960 RGB/深度相机 10 Hz，并关闭合并建筑碰撞", "compose": ("V6", "V7"), "camera_overlay": True},
}


def _set_text(root: ET.Element, path: str, value: object) -> None:
    node = root.find(path)
    if node is None:
        parent_path, _, name = path.rpartition("/")
        parent = root.find(parent_path) if parent_path else root
        if parent is None:
            raise ValueError(f"SDF path missing: {path}")
        node = ET.SubElement(parent, name)
    node.text = str(value)


def _rewrite_world(source: Path, destination: Path, *, shadows: bool | None, max_step: float | None,
                   update_rate: float | None, remove_building_collision: bool = False) -> None:
    root = ET.fromstring(source.read_text(encoding="utf-8"))
    if max_step is not None:
        _set_text(root, "world/physics/max_step_size", f"{max_step:g}")
    if update_rate is not None:
        _set_text(root, "world/physics/real_time_update_rate", f"{update_rate:g}")
    if shadows is not None:
        _set_text(root, "world/scene/shadows", str(shadows).lower())
        _set_text(root, "world/light[@name='sun']/cast_shadows", str(shadows).lower())
    if remove_building_collision:
        link = root.find("world/model[@name='rw_terrain']/link")
        collision = link.find("collision[@name='buildings']") if link is not None else None
        if collision is None:
            raise ValueError("V7 requires the rw_terrain buildings collision element")
        link.remove(collision)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    destination.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")


def _rewrite_model(source: Path, destination: Path, *, width: int, height: int, hz: float,
                   visualize: bool = False, preserve_camera_geometry: bool = False) -> None:
    root = ET.fromstring(source.read_text(encoding="utf-8"))
    for sensor in root.iter("sensor"):
        if sensor.get("type") not in {"camera", "depth_camera"}:
            continue
        if not preserve_camera_geometry:
            _set_text(sensor, "camera/image/width", width)
            _set_text(sensor, "camera/image/height", height)
        _set_text(sensor, "update_rate", f"{hz:g}")
        if not preserve_camera_geometry:
            _set_text(sensor, "visualize", str(visualize).lower())
            for noise in list(sensor.iter("noise")):
                parent = next((p for p in root.iter() if noise in list(p)), None)
                if parent is not None:
                    parent.remove(noise)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    destination.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")


def generate(source_world: Path, out_dir: Path, *, variant: str, airframe: str, scenario: Path | None = None) -> dict:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant}; choose from {', '.join(VARIANTS)}")
    settings = VARIANTS[variant]
    if "compose" in settings:
        composed = {}
        for component in settings["compose"]:
            composed.update({key: value for key, value in VARIANTS[component].items() if key != "description"})
        settings = {**composed, **{key: value for key, value in settings.items() if key != "compose"}}
    if scenario is not None and not scenario.is_file():
        raise FileNotFoundError(scenario)
    source_stem = source_world.stem
    stem = source_stem.removesuffix("__clear_noon") + f"__rtf_{variant.lower()}__clear_noon"
    world = out_dir / "worlds" / f"{stem}.sdf"
    _rewrite_world(source_world, world, shadows=settings.get("shadows"), max_step=settings.get("max_step"),
                   update_rate=settings.get("update_rate"),
                   remove_building_collision=settings.get("remove_building_collision", False))
    models = out_dir / "models"
    camera_overlay = settings["camera_overlay"]
    if camera_overlay:
        mono = models / "mono_cam"
        mono.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MONO_TEMPLATE / "model.config", mono / "model.config")
        _rewrite_model(MONO_TEMPLATE / "model.sdf", mono / "model.sdf", width=settings["width"], height=settings["height"],
                       hz=settings["camera_hz"], preserve_camera_geometry=settings.get("preserve_camera_geometry", False))
        wrapper_source = MODEL_ROOT / airframe
        wrapper = models / airframe
        wrapper.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wrapper_source / "model.config", wrapper / "model.config")
        _rewrite_model(wrapper_source / "model.sdf", wrapper / "model.sdf", width=settings["width"], height=settings["height"],
                       hz=settings["camera_hz"], preserve_camera_geometry=settings.get("preserve_camera_geometry", False))
    result = {"schema_version": 1, "variant": variant, "description": settings["description"], "world": str(world),
              "overlay_root": str(models) if camera_overlay else None,
              "camera": {"width": settings.get("width", 1280), "height": settings.get("height", 960),
                         "update_rate_hz": settings.get("camera_hz", 30)},
              "source_world": str(source_world)}
    if "terrain_budget_profile" in settings:
        result["terrain"] = {"budget_profile": settings["terrain_budget_profile"], "visual_grid_m": 8,
                             "near_field_core_half_extent_m": 300, "near_field_core_grid_m": 2,
                             "collision_grid_m": 8,
                             "collision_preserved": True, "requires_budget_scene_build": True}
    if settings.get("remove_building_collision"):
        result["collision_policy"] = {"removed": ["rw_terrain/buildings"],
                                      "preserved": ["rw_terrain/ground"], "near_field_terrain_precision_changed": False}
    if scenario is not None:
        body = json.loads(scenario.read_text(encoding="utf-8"))
        body["scenario_id"] = stem
        scenario_out = out_dir / "scenario" / f"{stem}.scenario.json"
        scenario_out.parent.mkdir(parents=True, exist_ok=True)
        source_stem = scenario.name.removesuffix(".scenario.json")
        runtime = json.loads((scenario.parent / f"{source_stem}.runtime.json").read_text(encoding="utf-8"))
        truth = json.loads((scenario.parent / f"{source_stem}.truth.json").read_text(encoding="utf-8"))
        expectations = json.loads((scenario.parent / f"{source_stem}.expectations.json").read_text(encoding="utf-8"))
        runtime["scenario_id"] = truth["scenario_id"] = expectations["scenario_id"] = stem
        runtime["world"]["world_asset"] = f"builtin://gazebo/{stem}"
        for key, source_dir, output_dir, suffix in (
            ("map_asset", ROOT / "simulation/maps", out_dir / "assets/maps", ".geojson"),
            ("dem_asset", ROOT / "simulation/terrain", out_dir / "assets/terrain", ".dem.json"),
        ):
            old_name = runtime["world"][key].split("/")[-1]
            source_asset = source_dir / old_name
            if source_asset.is_file():
                target_asset = output_dir / f"{stem}{suffix}"
                target_asset.parent.mkdir(parents=True, exist_ok=True)
                target_asset.write_text(source_asset.read_text(encoding="utf-8"), encoding="utf-8")
                asset_body = json.loads(target_asset.read_text(encoding="utf-8"))
                if isinstance(asset_body, dict) and "scenario_id" in asset_body:
                    asset_body["scenario_id"] = stem
                    target_asset.write_text(json.dumps(asset_body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                runtime["world"][key] = runtime["world"][key].rsplit("/", 1)[0] + "/" + target_asset.name
        runtime_path = scenario_out.with_name(f"{stem}.runtime_input.json")
        truth_path = scenario_out.with_name(f"{stem}.truth.json")
        expectations_path = scenario_out.with_name(f"{stem}.expectations.json")
        runtime_path.write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        truth_path.write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        expectations_path.write_text(json.dumps(expectations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        body["channels"] = {
            "runtime_input": {"path": runtime_path.name, "sha256": canonical_sha256(runtime)},
            "truth": {"path": truth_path.name, "sha256": canonical_sha256(truth)},
            "expectations": {"path": expectations_path.name, "sha256": canonical_sha256(expectations)},
        }
        scenario_out.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
        source_sidecar = scenario.parent / f"{source_stem}.long_range.json"
        if source_sidecar.is_file():
            sidecar = json.loads(source_sidecar.read_text(encoding="utf-8"))
            sidecar["scenario_id"] = stem
            sidecar_path = scenario_out.with_name(f"{stem}.long_range.json")
            sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["long_range_sidecar"] = str(sidecar_path)
        result["scenario"] = str(scenario_out)
    (out_dir / "variant.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def build_v5_budget_scene(out_dir: Path, *, raw_root: Path, python: Path, airframe: str,
                          model_source: Path | None = None) -> dict:
    """Build the lower-detail V5 model without touching the shared resolved cache."""
    models = out_dir / "models"
    build_name = "rw_farm_iowa_rtf_v5_build"
    generated = models / build_name
    target = models / "rw_farm_iowa"
    if generated.exists():
        raise FileExistsError(f"refusing to overwrite V5 model output under {models}")
    models.mkdir(parents=True, exist_ok=True)
    scenes = out_dir / "scenes"
    log_path = out_dir / "build-scene.log"
    if model_source is not None:
        model_source = model_source.resolve()
        scene_path = model_source.parent.parent / "scenes" / model_source.name / "scene.json"
        if not (model_source / "model.sdf").is_file() or not (model_source / "model.config").is_file():
            raise FileNotFoundError(f"V5 budget model is incomplete: {model_source}")
        scene = json.loads(scene_path.read_text(encoding="utf-8"))
        profile = scene.get("range_profile", {})
        if (scene.get("source_site") != "rw_farm_iowa" or float(scene.get("size_m", 0)) != 6000.0
                or profile.get("name") != "budget_6km" or float(profile.get("visual_grid_m", 0)) != 8.0):
            raise ValueError(f"V5 source is not a validated 6 km budget_6km model: {scene_path}")
        model_root = ET.parse(model_source / "model.sdf").getroot()
        if model_root.find("model/link/collision[@name='ground']") is None:
            raise ValueError("V5 budget model is missing its ground collision")
        provenance = {"source_model": str(model_source), "scene": str(scene_path), "verified_profile": profile}
        provenance_path = out_dir / "budget-source.json"
        if target.exists():
            previous = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.is_file() else {}
            if previous.get("source_model") != str(model_source):
                raise FileExistsError(f"refusing to overwrite V5 model output under {models}")
        else:
            shutil.copytree(model_source, target)
        provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        if target.exists():
            raise FileExistsError(f"refusing to overwrite V5 model output under {models}")
        command = [str(python), str(ROOT / "scripts/realworld/build_scene.py"), "rw_farm_iowa",
                   "--raw-root", str(raw_root), "--output-root", str(models), "--scene-root", str(scenes),
                   "--extent-m", "6000", "--budget-profile", "budget_6km", "--output-name", build_name]
        with log_path.open("w", encoding="utf-8") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        generated.rename(target)
    for path in target.rglob("*"):
        if path.is_file() and path.suffix in {".sdf", ".config"}:
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace(build_name, "rw_farm_iowa"), encoding="utf-8")
    wrapper_source = MODEL_ROOT / airframe
    wrapper_target = models / airframe
    if wrapper_target.exists():
        for filename in ("model.config", "model.sdf"):
            if (wrapper_target / filename).read_bytes() != (wrapper_source / filename).read_bytes():
                raise FileExistsError(f"refusing to replace non-identical V5 airframe overlay: {wrapper_target}")
    else:
        shutil.copytree(wrapper_source, wrapper_target)
    return {"overlay_root": str(models), "model_path": str(target),
            "airframe_overlay": str(wrapper_target),
            "builder_log": str(log_path) if model_source is None else None,
            "source_model": str(model_source.resolve()) if model_source is not None else None,
            "budget_profile": "budget_6km", "visual_grid_m": 8, "near_field_core_half_extent_m": 300,
            "near_field_core_grid_m": 2,
            "collision_grid_m": 8, "collision_preserved": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-world", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", choices=tuple(VARIANTS), required=True)
    parser.add_argument("--airframe", default="rc_cessna_mono_cam")
    parser.add_argument("--scenario", type=Path, help="源 scenario.json；生成独立 runtime/truth channel")
    parser.add_argument("--raw-root", type=Path, help="V5 build_scene 原始站点数据根目录")
    parser.add_argument("--builder-python", type=Path, default=Path(sys.prefix) / "bin/python",
                        help="build_scene 使用的 Python（默认当前虚拟环境）")
    parser.add_argument("--budget-model-source", type=Path,
                        help="可选的已构建 budget_6km 模型目录；会校验 6 km profile 后复制到 --out")
    args = parser.parse_args(argv)
    result = generate(args.source_world.resolve(), args.out.resolve(), variant=args.variant, airframe=args.airframe,
                      scenario=args.scenario.resolve() if args.scenario else None)
    if args.variant == "V5":
        config = json.loads((ROOT / "configs/sim-assets/realworld-sites.json").read_text(encoding="utf-8"))
        raw_root = args.raw_root.resolve() if args.raw_root else Path(config["asset_root"]) / "sites"
        result.update(build_v5_budget_scene(args.out.resolve(), raw_root=raw_root,
                                            python=args.builder_python, airframe=args.airframe,
                                            model_source=args.budget_model_source))
        result["terrain"]["requires_budget_scene_build"] = False
        (args.out.resolve() / "variant.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

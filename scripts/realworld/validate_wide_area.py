#!/usr/bin/env python3
"""CPU-only audit and command generator for a wide realworld scene."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from scripts.realworld.range_profile import make_profile
except ModuleNotFoundError:  # direct execution from scripts/realworld
    from range_profile import make_profile


def audit(scene_path: Path, world_path: Path | None = None, resolved_root: Path | None = None) -> dict:
    scene = json.loads(scene_path.read_text())
    profile = make_profile(float(scene["size_m"]), name=scene.get("range_profile", {}).get("name", "default") if isinstance(scene.get("range_profile", {}), dict) else scene["range_profile"])
    stats = scene.get("stats", {})
    result = {
        "scene": str(scene_path),
        "extent_m": profile.extent_m,
        "near_radius_m": profile.near_radius_m,
        "mid_radius_m": profile.mid_radius_m,
        "models": int(stats.get("prop_instances", 0)),
        "triangles": int(stats.get("triangles", 0)),
        "prop_triangles": int(stats.get("prop_triangles", 0)),
        "texture_decoded_gib": stats.get("texture_decoded_gib"),
        "budget_ok": int(stats.get("prop_triangles", 0)) <= profile.max_prop_triangles,
        "mesh_buffer_estimate_bytes": stats.get("mesh_buffer_estimate_bytes"),
    }
    texture_bytes = stats.get("decoded_texture_bytes")
    mesh_bytes = stats.get("mesh_buffer_estimate_bytes")
    if texture_bytes is not None and mesh_bytes is not None:
        # This excludes sky, cameras, shadows and driver allocations.
        estimate = int(texture_bytes) * 4 / 3 + int(mesh_bytes)
        result["render_memory_lower_bound_gib"] = round(estimate / 2**30, 3)
        result["render_memory_lower_bound_exceeds_2gib"] = estimate > 2 * 2**30
    if world_path:
        text = world_path.read_text()
        result["world_has_plugin"] = bool(re.search(r"<plugin(?:\s|/?>)", text))
        result["world_has_include"] = bool(re.search(r"<include(?:\s|/?>)", text))
        result["world_has_network_uri"] = bool(re.search(r"(?:https?|ftp)://", text))
        result["world_static_ok"] = not any(result[k] for k in ("world_has_plugin", "world_has_include", "world_has_network_uri"))
        world = ET.fromstring(text)
        result["world_model_count"] = len(world.findall(".//model"))
        result["world_actor_count"] = len(world.findall(".//actor"))
        if resolved_root is not None:
            root = resolved_root.resolve()
            references = sorted({node.text.strip() for node in world.iter()
                                 if node.text and node.text.strip().startswith("model://")})
            missing = []
            for uri in references:
                path = (root / uri.removeprefix("model://")).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    missing.append(uri)
            result.update(resolved_root=str(root), model_resource_count=len(references),
                          missing_model_resources=missing, model_resources_ok=not missing)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scene", type=Path)
    ap.add_argument("--world", type=Path)
    ap.add_argument("--resolved-root", type=Path, help="检查 world 中 model:// 文件引用的独立模型根")
    ap.add_argument("--print-command", action="store_true")
    a = ap.parse_args()
    result = audit(a.scene, a.world, a.resolved_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if a.print_command:
        print("export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240 MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1")
        if a.resolved_root:
            print("export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=" + shlex.quote(str(a.resolved_root.resolve())))
        if a.world:
            print(shlex.join(["/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python", "scripts/realworld/leased_flight.py",
                              "5", "standard_vtol_mono_cam", a.world.stem, f".tmp/flights/{a.world.stem}-vtol"]))
    return 0 if all(result.get(key, True) for key in ("budget_ok", "world_static_ok", "model_resources_ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())

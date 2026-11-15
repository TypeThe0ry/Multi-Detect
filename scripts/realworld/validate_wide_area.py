#!/usr/bin/env python3
"""CPU-only audit and command generator for a wide realworld scene."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    from scripts.realworld.range_profile import make_profile
except ModuleNotFoundError:  # direct execution from scripts/realworld
    from range_profile import make_profile


def audit(scene_path: Path, world_path: Path | None = None) -> dict:
    scene = json.loads(scene_path.read_text())
    profile = make_profile(float(scene["size_m"]))
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
    }
    if world_path:
        text = world_path.read_text()
        result["world_has_plugin"] = bool(re.search(r"<plugin(?:\s|>)", text))
        result["world_has_include"] = bool(re.search(r"<include(?:\s|>)", text))
        result["world_has_network_uri"] = bool(re.search(r"(?:https?|ftp)://", text))
        result["world_static_ok"] = not any(result[k] for k in ("world_has_plugin", "world_has_include", "world_has_network_uri"))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scene", type=Path)
    ap.add_argument("--world", type=Path)
    ap.add_argument("--print-command", action="store_true")
    a = ap.parse_args()
    print(json.dumps(audit(a.scene, a.world), ensure_ascii=False, indent=2))
    if a.print_command:
        print("export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240")
        print("/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 5 standard_vtol_mono_cam <world-stem> <out-dir>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

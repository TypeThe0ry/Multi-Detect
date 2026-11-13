#!/usr/bin/env python3
"""Build the three requested city worlds and register their scenario index."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SITES = ("rw_shanghai_lujiazui", "rw_shanghai_century_park", "rw_singapore_marina_bay")
WEATHER = "clear_noon"


def check_origin(site: str, configured: dict, actual: dict) -> None:
    for key in ("lat", "lon", "size_m"):
        if abs(float(configured[key]) - float(actual[key])) > 1e-7:
            raise ValueError(f"{site}: {key} differs from configuration; use a separate matching cache")


def write_index() -> Path:
    directory = ROOT / "configs/scenarios/realworld"
    worlds = []
    for site in SITES:
        world = f"{site}__{WEATHER}"
        manifest = directory / f"{world}.scenario.json"
        if not manifest.is_file():
            raise FileNotFoundError(manifest)
        worlds.append({
            "world_id": world, "world_file": f"{world}.sdf", "split": "test",
            "files": {"scenario": manifest.name}, "site": site,
            "task_kind": "survey",
            "task_text_en": "Survey the site with seventy percent photo overlap and return home.",
        })
    path = directory / "city-regression-index.json"
    path.write_text(json.dumps({
        "schema_version": 1, "generator": "scripts.realworld.prepare_city_regression",
        "count": len(worlds), "worlds": worlds,
        "note": "Geographic survey regression only; no target identity ground truth is asserted.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def limit_model_textures(model: Path, scene_path: Path, maximum: int) -> None:
    """Bound generated texture memory without changing source rasters or UVs."""
    from PIL import Image

    texture_dir = model / "textures"
    paths = sorted(path for path in texture_dir.iterdir() if path.is_file())
    for path in paths:
        with Image.open(path) as source:
            if max(source.size) <= maximum:
                continue
            image = source.copy()
            image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
            temporary = path.with_name(path.stem + ".resize" + path.suffix)
            image.save(temporary, **({"quality": 90} if path.suffix.lower() in {".jpg", ".jpeg"} else {}))
        temporary.replace(path)
    decoded = prop_decoded = 0
    for path in paths:
        with Image.open(path) as image:
            size = image.width * image.height * 4
        decoded += size
        if path.name.startswith("inst_"):
            prop_decoded += size
    scene = json.loads(scene_path.read_text())
    stats = scene["stats"]
    stats.update(texture_limit_px=maximum, decoded_texture_bytes=decoded,
                 decoded_prop_texture_bytes=prop_decoded,
                 texture_decoded_gib=round(decoded * 4 / 3 / 2**30, 3))
    scene["render_profile"] = {"max_generated_texture_px": maximum,
                               "note": "Generated model textures resized; original geospatial rasters unchanged."}
    scene_path.write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True, help="site directories containing raw/provenance.json")
    parser.add_argument("--fetch-missing", action="store_true", help="download only sites without a completed provenance file")
    parser.add_argument("--rebuild", action="store_true", help="rebuild these three models even if a matching scene exists")
    parser.add_argument("--max-trees", type=int, default=1200)
    parser.add_argument("--max-texture-px", type=int, default=512)
    args = parser.parse_args(argv)
    config = json.loads((ROOT / "configs/sim-assets/realworld-sites.json").read_text())
    asset_root = Path(config["asset_root"])
    env = {**os.environ, "RW_SCENE_ROOT": str(asset_root)}
    resolved = Path(env.get("RW_RESOLVED_ROOT", ROOT / "artifacts/cache/gz-fuel/resolved"))
    if args.max_texture_px < 64:
        parser.error("--max-texture-px must be at least 64")

    def run(script: str, *arguments: str) -> None:
        subprocess.run([sys.executable, str(ROOT / "scripts/realworld" / script), *arguments],
                       cwd=ROOT, env=env, check=True)

    for site in SITES:
        provenance = args.raw_root / site / "raw/provenance.json"
        if not provenance.is_file() and args.fetch_missing:
            run("fetch_site.py", site, "--raw-root", str(args.raw_root))
        if not provenance.is_file():
            raise FileNotFoundError(f"{provenance}: fetch matching raw data first or use --fetch-missing")
        check_origin(site, config["sites"][site], json.loads(provenance.read_text()))
        scene_path = asset_root / "sites" / site / "scene.json"
        if scene_path.is_file():
            check_origin(site, config["sites"][site], json.loads(scene_path.read_text()))
        if args.rebuild or not scene_path.is_file():
            run("build_scene.py", site, "--raw-root", str(args.raw_root), "--max-trees", str(args.max_trees))
        limit_model_textures(resolved / site, scene_path, args.max_texture_px)
        run("make_world.py", site, "--weather", WEATHER, "--max-trees", str(args.max_trees))
    print(write_index())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

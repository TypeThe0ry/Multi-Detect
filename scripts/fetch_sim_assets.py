#!/usr/bin/env python3
"""Fetch and validate the Gazebo Fuel assets named in the local catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_sim_asset_catalog import build_resolved  # noqa: E402

DEFAULT_CATALOG = ROOT / "configs/sim-assets/fuel-catalog.json"
DEFAULT_RAW_ROOT = ROOT / "artifacts/cache/gz-fuel/OpenRobotics"
DEFAULT_RESOLVED_ROOT = ROOT / "artifacts/cache/gz-fuel/resolved"


class AssetFetchError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fuel_command(url: str, version: str | None, cache_root: Path) -> None:
    command = ["gz", "fuel", "download", "-u", url]
    if version:
        command.extend(("-v", version))
    env = os.environ.copy()
    env["GZ_FUEL_CACHE_PATH"] = str(cache_root)
    try:
        subprocess.run(command, env=env, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AssetFetchError(f"Fuel download failed for {url}: {exc}") from exc


def fuel_model_url(source_url: str) -> str:
    """Fuel model names contain spaces; the catalog stores them with underscores.

    ``Cardboard_box`` returns 404 while ``Cardboard box`` exists, so the model
    segment is sent with spaces (single-word names are unchanged).
    """
    head, _, name = source_url.rstrip("/").rpartition("/")
    return f"{head}/{quote(unquote(name).replace('_', ' '))}"


def _find_download(cache_root: Path, source_url: str, version: str | None) -> Path:
    parts = [unquote(part).lower() for part in urlparse(source_url).path.split("/") if part]
    try:
        owner, model = parts[-3].lower(), parts[-1].lower()
    except IndexError as exc:
        raise AssetFetchError(f"invalid Fuel source_url: {source_url}") from exc
    models = cache_root / "fuel.gazebosim.org" / owner / "models"
    # gz keeps multi-word names percent-encoded on disk (``cardboard%20box``).
    expected = next(
        (models / name for name in (model, quote(model)) if (models / name).is_dir()),
        models / model,
    )
    candidates = [expected / version] if version else sorted(expected.iterdir()) if expected.is_dir() else []
    if not candidates:
        # Fuel cache implementations have used both source-case and lower-case
        # directory names.  Match the model identity without relying on case.
        candidates = sorted(
            path.parent
            for path in cache_root.rglob("model.sdf")
            if model in (unquote(path.parent.name).lower(), unquote(path.parent.parent.name).lower())
            and any(part.lower() == "models" for part in path.parent.parts)
        )
    for candidate in candidates:
        if (candidate / "model.config").is_file() and (candidate / "model.sdf").is_file():
            return candidate
    raise AssetFetchError(f"downloaded model not found in Fuel cache: {expected}")


def _copy_asset(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def fetch_assets(
    catalog_path: Path = DEFAULT_CATALOG,
    raw_root: Path = DEFAULT_RAW_ROOT,
    resolved_root: Path = DEFAULT_RESOLVED_ROOT,
    *,
    downloader=_fuel_command,
    allow_hash_mismatch: bool = False,
) -> list[str]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise AssetFetchError("catalog assets must be a list")
    with tempfile.TemporaryDirectory(prefix="multidetect-fuel-") as temp:
        stage = Path(temp)
        cache = stage / "fuel-cache"
        raw_stage = stage / "OpenRobotics"
        raw_stage.mkdir()
        for asset in assets:
            source_url = asset.get("source_url")
            expected_hash = asset.get("model_sdf_sha256")
            source_name = asset.get("source")
            slug = asset.get("slug")
            if not all(isinstance(value, str) and value for value in (source_url, expected_hash, source_name, slug)):
                raise AssetFetchError(f"incomplete catalog record: {asset!r}")
            version = asset.get("version")
            if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", version)):
                raise AssetFetchError(f"invalid version for {slug}: {version!r}")
            fuel_url = fuel_model_url(source_url)
            downloader(fuel_url, version, cache)
            downloaded = _find_download(cache, fuel_url, version)
            _copy_asset(downloaded, raw_stage / source_name)

        staged_resolved = stage / "resolved"
        build_resolved(root=raw_stage, output=staged_resolved)
        mismatches = []
        for asset in assets:
            sdf = staged_resolved / asset["slug"] / "model.sdf"
            actual = _digest(sdf)
            if actual != asset["model_sdf_sha256"]:
                mismatches.append(
                    f"{asset['slug']}: catalog={asset['model_sdf_sha256']} actual={actual}"
                )
        if mismatches and not allow_hash_mismatch:
            raise AssetFetchError("SHA-256 mismatch for " + "; ".join(mismatches))
        for line in mismatches:
            # The catalog hashes predate later build_resolved changes (aba95e16
            # texture localization) and Windows CRLF output, so a rebuilt
            # resolved SDF can legitimately differ; report instead of failing.
            print(f"warning: resolved SDF differs from catalog: {line}")

        raw_root.mkdir(parents=True, exist_ok=True)
        for asset in assets:
            _copy_asset(raw_stage / asset["source"], raw_root / asset["source"])
        build_resolved(root=raw_root, output=resolved_root)
    return [str(asset["slug"]) for asset in assets]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--resolved-root", type=Path, default=DEFAULT_RESOLVED_ROOT)
    parser.add_argument(
        "--allow-hash-mismatch",
        action="store_true",
        help="warn instead of failing when a rebuilt resolved SDF differs from the catalog",
    )
    args = parser.parse_args(argv)
    try:
        fetched = fetch_assets(
            args.catalog, args.raw_root, args.resolved_root,
            allow_hash_mismatch=args.allow_hash_mismatch,
        )
    except (OSError, ValueError, AssetFetchError) as exc:
        parser.error(str(exc))
    print(f"validated and resolved {len(fetched)} Fuel assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

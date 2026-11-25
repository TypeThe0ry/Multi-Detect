"""Bind Iowa SIM site inventory to CPU plan-only output for PR #64."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from multidetect.autonomy.serialization import strict_json_loads
from scripts.realworld import x1_site_obstacles as obstacles

BOUNDS_SUFFIX = ".site-bounds.json"
PROVENANCE_SUFFIX = ".provenance.json"
MODELS_ENV = "MULTIDETECT_SIM_FUEL_RESOLVED_ROOT"


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _encode(data):
    return (json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write(path, content):
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temp = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _provenance_path(path):
    return path.with_name(path.name.removesuffix(BOUNDS_SUFFIX) + PROVENANCE_SUFFIX)


def bind_plan(result, inventory):
    """Project audited fields into the existing, strict host bounds schema."""
    prepared = result["prepared"]
    contingency = result["contingency_scenario"]
    live = contingency["live_evidence"]
    if (
        result["flight"]["status"] != "plan_only"
        or result["validator"]["verdict"] != "accepted"
        or Path(result["scenario"]).name != f"{obstacles.SCENARIO}.scenario.json"
        or result["world"] != obstacles.SITE
        or live["host_bounds_path"] is not None
        or any(prepared[key] != inventory[key] for key in ("world_sha256", "dem_sha256"))
    ):
        raise ValueError(
            "use accepted Iowa plan-only output with no host bounds and the exact audited inputs"
        )
    bindings = {
        "scope_sha256": contingency["motion_scope_sha256"],
        "dem_sha256": inventory["dem_sha256"],
        "base_mission_sha256": live["base_mission_sha256"],
    }
    if not all(_is_digest(value) for value in bindings.values()):
        raise ValueError("plan-only output lacks the host bounds digests")
    return {
        **bindings,
        "obstacle_coverage": inventory["obstacle_coverage"],
        "obstacles": inventory["obstacles"],
    }


def write_bounds(path, result, inventory):
    if not path.name.endswith(BOUNDS_SUFFIX):
        raise ValueError(f"Iowa bounds filename must end in {BOUNDS_SUFFIX}")
    bounds = _encode(bind_plan(result, inventory))
    provenance = {
        key: value
        for key, value in inventory.items()
        if key not in {"obstacle_coverage", "obstacles"}
    }
    provenance["bounds_sha256"] = hashlib.sha256(bounds).hexdigest()
    _write(_provenance_path(path), _encode(provenance))
    _write(path, bounds)


def verify_site_bounds(path, *, world_path, world_sha256, dem_sha256, models_root, rtf_model_root):
    """Check companion evidence before admitting Iowa site bounds in the runner."""
    try:
        bounds = path.read_bytes()
        receipt = _provenance_path(path)
        evidence = strict_json_loads(receipt.read_text(encoding="utf-8"), name=str(receipt))
        if (
            evidence["world_sha256"] != world_sha256
            or evidence["dem_sha256"] != dem_sha256
            or evidence["bounds_sha256"] != hashlib.sha256(bounds).hexdigest()
            or evidence["scene_sha256"] != obstacles.SCENE_SHA
            or world_sha256 != obstacles.WORLD_SHA
            or obstacles.sha256(Path(evidence["scene_path"])) != obstacles.SCENE_SHA
        ):
            raise ValueError("Iowa bounds, world, DEM or scene digest mismatch")
        if rtf_model_root or (world_path.parent / obstacles.WORLD_MODEL).exists():
            raise ValueError("Iowa model resolution may be shadowed by a SIM resource override")
        if not models_root or not Path(models_root).is_absolute():
            raise ValueError(f"set {MODELS_ENV} to the audited deployment model root")
        models = Path(models_root).resolve(strict=True)
        if Path(evidence["models_root"]) != models:
            raise ValueError("active deployment model root differs from Iowa evidence")
        meshes = evidence["mesh_sha256"]
        ground = f"{obstacles.WORLD_MODEL}/meshes/ground_collision.obj"
        if len(meshes) != 357 or meshes.get(ground) != obstacles.GROUND_SHA:
            raise ValueError("Iowa provenance is missing audited meshes")
        for relative, digest in meshes.items():
            if obstacles.sha256(obstacles._asset(models, Path(relative))) != digest:
                raise ValueError(f"deployed Iowa mesh changed: {relative}")
        return strict_json_loads(bounds.decode("utf-8"), name=str(path))
    except (KeyError, OSError, TypeError, AttributeError) as exc:
        raise ValueError(f"cannot verify Iowa site bounds: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world-sdf", required=True, type=Path)
    parser.add_argument("--dem", required=True, type=Path)
    parser.add_argument("--variant-source", required=True, type=Path)
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument(
        "--bind", action="append", nargs=2, metavar=("PLAN_RESULT", "BOUNDS_FILE"), default=[]
    )
    args = parser.parse_args(argv)
    try:
        inventory = obstacles.inventory(
            args.world_sdf, args.dem, args.variant_source, args.models_root
        )
        # Validate every binding before starting publication.
        plans = [(Path(path), json.loads(Path(result).read_bytes())) for result, path in args.bind]
        for path, result in plans:
            if not path.name.endswith(BOUNDS_SUFFIX):
                raise ValueError(f"Iowa bounds filename must end in {BOUNDS_SUFFIX}")
            bind_plan(result, inventory)
        for path, result in plans:
            write_bounds(path, result, inventory)
        _write(args.inventory, _encode(inventory))
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(1, f"Iowa site evidence unavailable: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

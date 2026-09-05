#!/usr/bin/env python3
"""Validate a non-flat Gazebo SDF plane against the shared DEM ranging core.

The output is deterministic software/asset evidence.  It does not start
Gazebo, QGroundControl, PX4, a camera, a network link, or any hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from multidetect.product_version import load_product_version  # noqa: E402
from multidetect.release_parity import (  # noqa: E402
    PARITY_FIELDS,
    build_release_parity_document,
    serialize_release_parity,
)
from multidetect.simulator.terrain_truth import (  # noqa: E402
    compare_dem_to_sdf_truth,
    load_sdf_terrain_truth,
)
from multidetect.terrain_dem import load_local_dem_grid  # noqa: E402

EVENT = "simulator_terrain_ranging_asset_truth_finished"
GATE_SCOPE = "deterministic_simulator_asset_truth"
DEFAULT_SCENARIO_ID = "terrain_ranging_validation_001"


def _resolve_repository_file(root: Path, value: str | Path, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    source = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = source.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} must resolve inside the repository root") from exc
    if source.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} must be a regular, non-symlink file")
    return source


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"artifact path is outside repository root: {path}") from exc


def _asset_identity(root: Path, path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    if not payload:
        raise ValueError(f"artifact is empty: {path}")
    return {
        "path": _relative_path(root, path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _load_parity_binding(
    root: Path,
    parity_path: Path,
    *,
    version_manifest_path: Path,
) -> tuple[dict[str, Any], bool]:
    payload = parity_path.read_bytes()
    raw = strict_json_loads(payload, name="release parity")
    if not isinstance(raw, Mapping):
        raise ValueError("release parity must contain a JSON object")
    parity = raw.get("parity")
    if not isinstance(parity, Mapping) or set(parity) != set(PARITY_FIELDS):
        raise ValueError("release parity must contain exactly the five shared contract hashes")
    hashes: dict[str, str] = {}
    for field in PARITY_FIELDS:
        value = parity[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise ValueError(f"release parity field {field} is not a SHA-256 digest")
        hashes[field] = value.lower()

    generated = build_release_parity_document(
        root,
        version_manifest_path=version_manifest_path,
        output_path=parity_path,
    )
    expected_bytes = serialize_release_parity(generated).encode("utf-8")
    current = raw == generated and payload == expected_bytes
    identity = _asset_identity(root, parity_path)
    identity["hashes"] = hashes
    return identity, current


def build_record(
    root: str | Path,
    *,
    world_sdf: str | Path | None = None,
    terrain_dem: str | Path | None = None,
    parity_json: str | Path = "configs/release-parity.json",
    version_manifest: str | Path = "configs/product-version.json",
) -> dict[str, Any]:
    """Build one deterministic, hash-bound non-flat terrain truth record."""

    repository_root = Path(root).expanduser().resolve()
    if not repository_root.is_dir():
        raise ValueError(f"repository root is not a directory: {repository_root}")
    world_value = world_sdf or (f"simulation/gazebo/worlds/{DEFAULT_SCENARIO_ID}.sdf")
    dem_value = terrain_dem or f"simulation/terrain/{DEFAULT_SCENARIO_ID}.dem.json"
    world_path = _resolve_repository_file(repository_root, world_value, label="world SDF")
    dem_path = _resolve_repository_file(repository_root, dem_value, label="terrain DEM")
    parity_path = _resolve_repository_file(repository_root, parity_json, label="release parity")
    version_path = _resolve_repository_file(
        repository_root,
        version_manifest,
        label="product version manifest",
    )

    version = load_product_version(version_path)
    truth = load_sdf_terrain_truth(world_path)
    grid = load_local_dem_grid(dem_path)
    comparison = compare_dem_to_sdf_truth(grid, truth)
    parity_binding, parity_current = _load_parity_binding(
        repository_root,
        parity_path,
        version_manifest_path=version_path,
    )

    checks = dict(comparison["checks"])
    checks["release_parity_matches_current_sources"] = parity_current
    reasons = list(comparison["failure_reasons"])
    if not parity_current:
        reasons.append("release-parity.json is not byte-identical to current shared sources")
    gate_passed = bool(comparison["gate_passed"] and parity_current)

    return {
        "schema_version": 1,
        "event": EVENT,
        "product": version.product,
        "release_label": version.release_label,
        "python_version": version.python,
        "scenario_id": truth.scenario_id,
        "gate_scope": GATE_SCOPE,
        "gate_passed": gate_passed,
        "deterministic_gate_passed": gate_passed,
        "external_gazebo_runtime_gate_passed": False,
        "external_runtime_required_for_real_3d": True,
        "inputs": {
            "world_sdf": _asset_identity(repository_root, world_path),
            "terrain_dem": _asset_identity(repository_root, dem_path),
            "release_parity": parity_binding,
        },
        "truth_surface": truth.to_mapping(),
        "comparison": {
            key: value
            for key, value in comparison.items()
            if key not in {"checks", "failure_reasons", "gate_passed"}
        },
        "checks": checks,
        "failure_reasons": reasons,
        "claims": {
            "non_flat_asset_geometry_truth": gate_passed,
            "shared_dem_intersection_matches_sdf_analytic_truth": gate_passed,
            "external_gazebo_runtime": "not_claimed",
            "qgc_visual_acceptance": "not_claimed",
            "hardware_acceptance": "not_claimed",
            "outdoor_gnss": "not_claimed",
            "hil_real_flight": "not_claimed",
            "final_v0_3_0": "not_released",
        },
    }


def _serialize(document: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def _write_output(path: Path, payload: str, *, overwrite: bool) -> None:
    output = path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root, help="repository root")
    parser.add_argument("--world-sdf", type=Path, help="repository-relative truth SDF")
    parser.add_argument("--terrain-dem", type=Path, help="repository-relative DEM JSON")
    parser.add_argument(
        "--parity-json",
        type=Path,
        default=Path("configs/release-parity.json"),
    )
    parser.add_argument(
        "--version-manifest",
        type=Path,
        default=Path("configs/product-version.json"),
    )
    parser.add_argument("--out", type=Path, required=True, help="new evidence JSON path")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="explicitly replace an existing evidence file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        record = build_record(
            root,
            world_sdf=args.world_sdf,
            terrain_dem=args.terrain_dem,
            parity_json=args.parity_json,
            version_manifest=args.version_manifest,
        )
        payload = _serialize(record)
        output = args.out if args.out.is_absolute() else root / args.out
        _write_output(output, payload, overwrite=args.overwrite)
    except (OSError, TypeError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"event": f"{EVENT}_failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(payload, end="")
    return 0 if record["gate_passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

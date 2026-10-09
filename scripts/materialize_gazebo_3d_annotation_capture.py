"""Materialize a capture-only Gazebo world with offline 3-D labels.

This command never edits a checked-in world.  It copies one hash-bound
mainline world to a caller-selected output path and replaces only the neutral
target include with the capture-only labelled wrapper.  The vehicle wrapper
URI is emitted in the binding receipt for the PX4/Gazebo launcher to select;
the runtime OnboardAI path must continue to subscribe only to RGB, depth,
CameraInfo and telemetry topics.

The command is a preparation step, not a Gazebo execution claim.  It does not
read scenario truth and it does not create policy samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "configs" / "scenarios" / "gazebo-3d-annotation-capture-v1.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AnnotationMaterializationError(ValueError):
    """The capture-only annotation world cannot be materialized safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_repo_path(value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AnnotationMaterializationError(f"{label} must be a non-empty path")
    candidate = (ROOT / value).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise AnnotationMaterializationError(f"{label} must stay inside the repository") from exc
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationMaterializationError(f"invalid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise AnnotationMaterializationError(f"JSON document must be an object: {path}")
    return document


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AnnotationMaterializationError(f"{label} must be a lowercase SHA-256")
    return value


def _uri(include: ET.Element) -> str | None:
    for child in include:
        if child.tag.rsplit("}", 1)[-1] == "uri":
            return child.text.strip() if isinstance(child.text, str) else None
    return None


def _replace_uri(include: ET.Element, value: str) -> None:
    for child in include:
        if child.tag.rsplit("}", 1)[-1] == "uri":
            child.text = value
            return
    raise AnnotationMaterializationError("target include has no uri element")


def _iter_includes(root: ET.Element):
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "include":
            yield element


def _copy_model_bundle(
    *,
    output_root: Path,
    vehicle_path: Path,
    target_path: Path,
) -> list[dict[str, Any]]:
    """Materialize a self-contained custom resource path for the annotation run.

    The checked-in annotation wrappers live outside the production model root.
    A temporary resource root avoids changing the mainline mount and makes the
    future Docker/Gazebo command explicit: mount this directory read-only and
    prepend it to ``GZ_SIM_RESOURCE_PATH``.  Upstream PX4 models referenced by
    the wrappers remain supplied by the pinned simulator image.
    """

    output_root = output_root.expanduser().resolve()
    if output_root.exists():
        if not output_root.is_dir() or any(output_root.iterdir()):
            raise AnnotationMaterializationError(
                "annotation resource root must be a new or empty directory"
            )
    output_root.mkdir(parents=True, exist_ok=True)
    source_dirs = {
        "multidetect_annotation_x500_v1": vehicle_path.parent,
        "multidetect_annotation_target_v1": target_path.parent,
        "x500_mono_cam": ROOT / "simulation/gazebo/models/x500_mono_cam",
        "multidetect_search_target_v1": ROOT
        / "simulation/gazebo/models/multidetect_search_target_v1",
    }
    files: list[dict[str, Any]] = []
    for model_name, source_dir in source_dirs.items():
        source_dir = source_dir.resolve()
        if not source_dir.is_dir():
            raise AnnotationMaterializationError(
                f"annotation resource model directory is missing: {source_dir}"
            )
        destination = output_root / model_name
        shutil.copytree(source_dir, destination, dirs_exist_ok=False)
        for source_file in sorted(source_dir.rglob("*")):
            if not source_file.is_file():
                continue
            relative = source_file.relative_to(source_dir).as_posix()
            destination_file = destination / relative
            files.append(
                {
                    "model": model_name,
                    "path": f"{model_name}/{relative}",
                    "source_path": source_file.as_posix(),
                    "source_sha256": _sha256(source_file),
                    "output_sha256": _sha256(destination_file),
                    "bytes": destination_file.stat().st_size,
                }
            )
    return files


def materialize(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    output_world: Path,
    output_binding: Path | None = None,
    output_resource_root: Path | None = None,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path.expanduser().resolve())
    if manifest.get("schema") != "multidetect.gazebo-3d-annotation-capture":
        raise AnnotationMaterializationError("annotation manifest schema is unsupported")
    if manifest.get("contract") != "multidetect.gazebo-3d-annotation-capture-v1":
        raise AnnotationMaterializationError("annotation manifest contract is unsupported")
    if manifest.get("purpose") != "offline_3d_annotation_only":
        raise AnnotationMaterializationError("annotation manifest is not offline-only")
    boundary = manifest.get("runtime_boundary")
    if not isinstance(boundary, dict) or any(
        boundary.get(field) is not expected
        for field, expected in (
            ("runtime_consumes_annotation_topics", False),
            ("truth_injected_into_policy", False),
            ("training_requires_independent_review", True),
        )
    ):
        raise AnnotationMaterializationError("annotation runtime boundary is invalid")

    mainline = manifest.get("mainline_manifest")
    base_world = manifest.get("base_world")
    vehicle = manifest.get("vehicle_wrapper")
    target = manifest.get("target_wrapper")
    if not all(isinstance(item, dict) for item in (mainline, base_world, vehicle, target)):
        raise AnnotationMaterializationError("annotation manifest bindings are incomplete")
    mainline_path = _resolve_repo_path(mainline["path"], label="mainline manifest")
    if _sha256(mainline_path) != _require_sha256(
        mainline["sha256"], label="mainline manifest hash"
    ):
        raise AnnotationMaterializationError("mainline manifest hash mismatch")
    world_path = _resolve_repo_path(base_world["path"], label="base world")
    if _sha256(world_path) != _require_sha256(base_world["sha256"], label="base world hash"):
        raise AnnotationMaterializationError("base world hash mismatch")
    vehicle_path = _resolve_repo_path(vehicle["path"], label="vehicle wrapper")
    target_path = _resolve_repo_path(target["path"], label="target wrapper")
    if not vehicle_path.is_file() or not target_path.is_file():
        raise AnnotationMaterializationError("annotation wrapper model is missing")
    if _sha256(vehicle_path) != _require_sha256(
        vehicle["sha256"], label="vehicle wrapper hash"
    ):
        raise AnnotationMaterializationError("vehicle wrapper hash mismatch")
    if _sha256(target_path) != _require_sha256(
        target["sha256"], label="target wrapper hash"
    ):
        raise AnnotationMaterializationError("target wrapper hash mismatch")
    if not str(vehicle.get("annotation_uri", "")).startswith("model://multidetect_annotation_"):
        raise AnnotationMaterializationError("vehicle annotation URI is not capture-only")
    if not str(target.get("annotation_uri", "")).startswith("model://multidetect_annotation_"):
        raise AnnotationMaterializationError("target annotation URI is not capture-only")

    try:
        tree = ET.parse(world_path)
    except (OSError, ET.ParseError) as exc:
        raise AnnotationMaterializationError(
            f"base world is not valid SDF XML: {world_path}"
        ) from exc
    root = tree.getroot()
    original_uri = str(target["base_uri"])
    annotation_uri = str(target["annotation_uri"])
    matches = [item for item in _iter_includes(root) if _uri(item) == original_uri]
    if len(matches) != 1:
        raise AnnotationMaterializationError(
            f"expected exactly one target include {original_uri!r}, found {len(matches)}"
        )
    _replace_uri(matches[0], annotation_uri)
    if any(_uri(item) == original_uri for item in _iter_includes(root)):
        raise AnnotationMaterializationError("unpatched target include remains")

    output_world = output_world.expanduser().resolve()
    output_world.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_world, encoding="utf-8", xml_declaration=True)
    output_hash = _sha256(output_world)
    resource_files = None
    resource_root_binding = None
    if output_resource_root is not None:
        resource_files = _copy_model_bundle(
            output_root=output_resource_root,
            vehicle_path=vehicle_path,
            target_path=target_path,
        )
        resource_root_binding = {
            "path": output_resource_root.expanduser().resolve().as_posix(),
            "mount_read_only": True,
            "resource_path_prepend": output_resource_root.expanduser().resolve().as_posix(),
            "file_count": len(resource_files),
            "files": resource_files,
        }
    binding = {
        "schema": "multidetect.gazebo-3d-annotation-materialization",
        "schema_version": 1,
        "contract": "multidetect.gazebo-3d-annotation-materialization-v1",
        "status": "prepared_not_executed",
        "source_world": {"path": world_path.as_posix(), "sha256": _sha256(world_path)},
        "output_world": {"path": output_world.as_posix(), "sha256": output_hash},
        "resource_root": resource_root_binding,
        "target_replacement": {"from": original_uri, "to": annotation_uri, "count": 1},
        "vehicle_model_uri": vehicle["annotation_uri"],
        "annotation_topics": {
            "bounding_boxes_3d": "multidetect/annotation/bounding_boxes_3d",
            "instance_segmentation": "multidetect/annotation/instance_segmentation",
        },
        "runtime_boundary": boundary,
        "training_ready": False,
        "review_required": True,
        "external_gazebo_executed": False,
    }
    if output_binding is not None:
        output_binding = output_binding.expanduser().resolve()
        output_binding.parent.mkdir(parents=True, exist_ok=True)
        output_binding.write_text(
            json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return binding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-world", type=Path, required=True)
    parser.add_argument("--output-binding", type=Path)
    parser.add_argument(
        "--output-resource-root",
        type=Path,
        help=(
            "new/empty directory receiving the annotation wrappers and their "
            "repository-owned base models for a future read-only Gazebo mount"
        ),
    )
    args = parser.parse_args(argv)
    try:
        binding = materialize(
            manifest_path=args.manifest,
            output_world=args.output_world,
            output_binding=args.output_binding,
            output_resource_root=args.output_resource_root,
        )
    except (AnnotationMaterializationError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

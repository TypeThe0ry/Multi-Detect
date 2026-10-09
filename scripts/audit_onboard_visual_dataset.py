"""Audit every offline 3-D curriculum frame before training, without inference.

This is a data-integrity and coverage report, not Gazebo, flight, HIL or
hardware acceptance. Existing datasets and reports are never rewritten.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scripts import evaluate_onboard_visual_curriculum as curriculum  # noqa: E402
from scripts import train_onboard_sdf_visual_candidate as trainer  # noqa: E402

CONTRACT = "multidetect.onboard-visual-dataset-offline-audit-v1"
SPLITS = ("train", "val", "test")
LOW_VIEW_DEG = 10.0
SMALL_BBOX_HEIGHT_PX = 55
SOURCE_PATHS = (
    "scripts/audit_onboard_visual_dataset.py",
    *curriculum.SOURCE_PATHS,
)
FALSE_FLAGS = (
    "training_ready",
    "gazebo_capture",
    "flight",
    "autonomous_flight_verified",
    "promotion",
    "candidate_promoted",
    "hardware_acceptance",
    "hil_acceptance",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            _finite_tree(child)


def _strict_document(manifest: Path) -> dict:
    document = json.loads(
        manifest.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_keys,
        parse_constant=_reject_constant,
    )
    if not isinstance(document, dict):
        raise ValueError("dataset manifest must be a JSON object")
    _finite_tree(document)
    for key in FALSE_FLAGS:
        if key in document and document[key] is not False:
            raise ValueError(f"offline dataset must not claim {key}")
    return document


def _camera_measurements(frame: dict) -> dict[str, float]:
    camera = frame.get("camera")
    if not isinstance(camera, dict):
        raise ValueError("frame requires camera location and target_center")
    vectors = []
    for key in ("location", "target_center"):
        vector = camera.get(key)
        if not isinstance(vector, list) or len(vector) != 3 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        ):
            raise ValueError(f"camera {key} requires three finite coordinates")
        vectors.append(vector)
    delta = [location - center for location, center in zip(*vectors, strict=True)]
    horizontal = math.hypot(delta[0], delta[1])
    slant = math.hypot(*delta)
    if not math.isfinite(slant) or slant == 0:
        raise ValueError("camera-target direction must be finite and nonzero")
    return {
        "actual_horizontal_distance_m": horizontal,
        "actual_height_above_target_center_m": delta[2],
        "actual_downward_elevation_deg": math.degrees(math.atan2(delta[2], horizontal)),
        "actual_slant_distance_m": slant,
    }


def _statistics(values: list[float | int]) -> dict:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "max": max(values) if values else None,
    }


def _bindings(document: dict, base: Path) -> dict[Path, str]:
    """Bind all declared assets, including raw renders and producer scripts."""
    bindings: dict[Path, str] = {}

    def bind(root: Path, relative: Any, digest: Any) -> None:
        if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("bound asset requires a lowercase SHA-256")
        path = trainer.bound_file(root, relative, digest)
        if path in bindings and bindings[path] != digest:
            raise ValueError("conflicting asset identities")
        bindings[path] = digest

    inventories = [document.get("source_hashes", {})]
    inventories.extend(frame["source_hashes"] for frame in document["frames"])
    for sources in inventories:
        if not isinstance(sources, dict):
            raise ValueError("source hash inventory must be an object")
        for relative, digest in sources.items():
            bind(trainer.ROOT, relative, digest)
    bind(base, document["dataset_yaml"], document["dataset_yaml_sha256"])
    output_paths: set[Path] = set()
    for frame in document["frames"]:
        for key in frame:
            if not key.endswith("_path") or key == "world_path":
                continue
            digest_key = key[:-5] + "_sha256"
            if digest_key not in frame:
                raise ValueError(f"unbound frame asset: {key}")
            bind(base, frame[key], frame[digest_key])
            path = (base / frame[key]).resolve()
            if path in output_paths:
                raise ValueError("dataset must not reuse frame asset paths")
            output_paths.add(path)
    return bindings


def _split_summary(frames: list[dict]) -> dict:
    visible = [frame for frame in frames if frame["visible_target_pixels"] > 0]
    low_view = [
        frame for frame in visible
        if frame["camera"]["actual_downward_elevation_deg"] <= LOW_VIEW_DEG
    ]
    small = [frame for frame in visible if frame["bbox"]["height_px"] <= SMALL_BBOX_HEIGHT_PX]
    geometries: dict[str, dict] = {}
    for digest in sorted({frame["target_geometry_sha256"] for frame in frames}):
        group = [frame for frame in frames if frame["target_geometry_sha256"] == digest]
        geometries[digest] = {
            "frame_count": len(group),
            "visible_frame_count": sum(frame["visible_target_pixels"] > 0 for frame in group),
            "target_model_paths": sorted({frame["target_model_path"] for frame in group}),
            "measured_condition_counts": dict(
                Counter(frame["measured_condition"] for frame in group)
            ),
        }
    camera_metrics = (
        "actual_horizontal_distance_m", "actual_downward_elevation_deg",
        "actual_height_above_target_center_m", "actual_slant_distance_m",
    )
    return {
        "frame_count": len(frames),
        "visible_frame_count": len(visible),
        "zero_visible_frame_count": len(frames) - len(visible),
        "measured_condition_counts": dict(Counter(frame["measured_condition"] for frame in frames)),
        "planned_condition_counts": dict(Counter(frame["planned_condition"] for frame in frames)),
        "world_paths": sorted({frame["world_path"] for frame in frames}),
        "geometries": geometries,
        "camera_all_frames": {
            key: _statistics([frame["camera"][key] for frame in frames]) for key in camera_metrics
        },
        "camera_visible_frames": {
            key: _statistics([frame["camera"][key] for frame in visible]) for key in camera_metrics
        },
        "bbox_visible_frames": {
            key: _statistics([frame["bbox"][key] for frame in visible])
            for key in ("width_px", "height_px", "center_x_px", "center_y_px")
        },
        "visible_low_view_le_10_deg_count": len(low_view),
        "visible_bbox_height_le_55_px_count": len(small),
        "visible_low_view_and_small_bbox_count": sum(
            frame["bbox"]["height_px"] <= SMALL_BBOX_HEIGHT_PX for frame in low_view
        ),
    }


def audit(manifest: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("refusing to overwrite offline dataset audit evidence")
    manifest = manifest.resolve()
    manifest_hash = trainer.sha256(manifest)
    runtime_sources = {relative: trainer.sha256(ROOT / relative) for relative in SOURCE_PATHS}
    strict_document = _strict_document(manifest)
    document, _ = trainer.verify_dataset(manifest)
    if document != strict_document:
        raise ValueError("manifest changed while loading dataset")
    base = manifest.parent
    bindings = _bindings(document, base)
    split_world_hashes = {
        split: {frame["world_sha256"] for frame in document["frames"] if frame["split"] == split}
        for split in SPLITS
    }
    if any(
        split_world_hashes[left] & split_world_hashes[right]
        for left, right in (("train", "val"), ("train", "test"), ("val", "test"))
    ):
        raise ValueError("different splits must use byte-independent source worlds")
    geometries = curriculum.verify_geometry_provenance(document)
    import cv2

    audited_frames = []
    identifiers: set[str] = set()
    for frame in document["frames"]:
        frame_id = frame.get("frame_id")
        if not isinstance(frame_id, str) or not frame_id or frame_id in identifiers:
            raise ValueError("frame identity must be nonempty and unique")
        identifiers.add(frame_id)
        width, height = frame.get("width"), frame.get("height")
        if any(type(value) is not int or value <= 0 for value in (width, height)):
            raise ValueError("frame dimensions must be positive integers")
        image_path = trainer.bound_file(base, frame["image_path"], frame["image_sha256"])
        image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if image is None or image.shape != (height, width, 3) or image.dtype.name != "uint8":
            raise ValueError("invalid actual RGB dimensions, channels or pixel type")
        condition, fraction = curriculum.verify_visibility(frame, base, cv2)
        visible = frame["visible_target_pixels"]
        if frame.get("target_visible") is not (visible > 0):
            raise ValueError("target_visible disagrees with actual visible pixels")
        box = frame["visible_bbox_xyxy_px"]
        if box is not None and (
            not isinstance(box, list) or len(box) != 4
            or any(type(value) is not int for value in box)
        ):
            raise ValueError("visible bbox requires four integer pixel coordinates")
        bbox = None if box is None else {
            "xyxy_px": box,
            "width_px": box[2] - box[0],
            "height_px": box[3] - box[1],
            "center_x_px": (box[0] + box[2]) / 2,
            "center_y_px": (box[1] + box[3]) / 2,
        }
        planned = frame["curriculum"].get("planned_condition")
        if not isinstance(planned, str) or not planned:
            raise ValueError("planned condition must be a nonempty string")
        audited_frames.append({
            "frame_id": frame_id,
            "split": frame["split"],
            "world_path": frame["world_path"],
            "target_model_path": frame["target_model"]["path"],
            "target_geometry_sha256": frame["target_model"]["geometry_sha256"],
            "rgb_shape_hwc": list(image.shape),
            "visible_target_pixels": visible,
            "amodal_target_pixels": frame["curriculum"]["amodal_target_pixels"],
            "visible_fraction": fraction,
            "measured_condition": condition,
            "planned_condition": planned,
            "camera": _camera_measurements(frame),
            "bbox": bbox,
            "yolo_label_matches_visible_mask": True,
        })
    splits = {
        split: _split_summary([frame for frame in audited_frames if frame["split"] == split])
        for split in SPLITS
    }
    if not splits["train"]["visible_frame_count"]:
        raise ValueError("training split has zero visible targets; reject pretraining dataset")
    trainer.verify_dataset(manifest)
    curriculum.verify_geometry_provenance(document)
    if trainer.sha256(manifest) != manifest_hash:
        raise ValueError("manifest changed during audit; no report sealed")
    for path, digest in bindings.items():
        if not path.is_file() or trainer.sha256(path) != digest:
            raise ValueError(f"bound input changed during audit: {path}; no report sealed")
    if any(trainer.sha256(ROOT / path) != digest for path, digest in runtime_sources.items()):
        raise ValueError("audit runtime source changed during audit; no report sealed")
    result = {
        "contract": CONTRACT,
        "audit_passed": True,
        "scope": "offline_dataset_integrity_and_coverage_only",
        "dataset": {"path": str(manifest), "sha256": manifest_hash},
        "frame_count": len(audited_frames),
        "splits": splits,
        "frames": audited_frames,
        "verified_geometry_sources": geometries,
        "test_worlds_disjoint_from_train_and_val": True,
        "source_world_byte_identity_disjoint_across_splits": True,
        "test_geometry_disjoint_from_train_and_val": True,
        "geometry_verification": "recomputed_from_hash_bound_target_sdf_primitives",
        "camera_measurement": "location_minus_target_center_not_declared_distance_or_look_at",
        "coverage_thresholds": {
            "low_view_downward_elevation_le_deg": LOW_VIEW_DEG,
            "small_bbox_height_le_px": SMALL_BBOX_HEIGHT_PX,
            "denominator": "actual_visible_frames_only",
        },
        "input_hashes_verified_before_and_after": True,
        "bound_asset_count": len(bindings),
        "runtime_sources": runtime_sources,
        "model_inference_performed": False,
        "training_performed": False,
        "original_detector_changed": False,
        "onboard_policy_reference_changed": False,
        **{key: False for key in FALSE_FLAGS},
    }
    serialized = json.dumps(result, indent=2, allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit(args.manifest, args.output)
    print(json.dumps({
        "contract": result["contract"],
        "audit_passed": result["audit_passed"],
        "frame_count": result["frame_count"],
        "splits": result["splits"],
        "output": str(args.output.resolve()),
    }, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

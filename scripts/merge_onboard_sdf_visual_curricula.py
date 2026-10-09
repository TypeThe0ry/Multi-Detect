"""Copy and merge source-bound offline SDF/Blender visual curricula.

Only the original training partitions enter training. Clip frames remain in
their original partitions; this prepares single-frame visual finetuning, not
temporal policy training, a Gazebo capture, or autonomous flight evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scripts import evaluate_onboard_visual_curriculum as evaluator  # noqa: E402
from scripts import render_onboard_sdf_blender_curriculum as curriculum  # noqa: E402
from scripts import render_onboard_sdf_blender_dataset as renderer  # noqa: E402
from scripts import replay_onboard_visual_clips as replay  # noqa: E402
from scripts import train_onboard_sdf_visual_candidate as trainer  # noqa: E402

CONTRACT = "multidetect.offline-sdf-visual-curriculum-merge-v1"
SPLITS = ("train", "val", "test")
SOURCE_MODULES = {
    "scripts/evaluate_onboard_visual_curriculum.py": evaluator,
    "scripts/render_onboard_sdf_blender_curriculum.py": curriculum,
    "scripts/render_onboard_sdf_blender_dataset.py": renderer,
    "scripts/replay_onboard_visual_clips.py": replay,
    "scripts/train_onboard_sdf_visual_candidate.py": trainer,
}
FILE_KINDS = (
    "image",
    "label",
    "mask",
    "amodal_mask",
    "mask_render",
    "amodal_render",
)
FALSE_FLAGS = (
    "training_ready",
    "gazebo_capture",
    "flight",
    "promotion",
    "candidate_promoted",
    "hardware_acceptance",
    "hil_acceptance",
    "hil_real_evidence",
    "jetson_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "flight_action_labels_created",
    "autonomous_flight_verified",
    "original_detector_changed",
    "onboard_policy_reference_changed",
    "task_policy_training",
    "temporal_training_performed",
)


def _strict_json(path: Path) -> dict:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ValueError(f"nonfinite manifest number: {value}")

    document = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite
    )
    if not isinstance(document, dict):
        raise ValueError("manifest must be an object")
    return document


def _snapshot(path: Path, snapshot: dict[Path, str], expected: str | None = None) -> str:
    path = path.resolve(strict=True)
    digest = trainer.sha256(path)
    if expected is not None and digest != expected:
        raise ValueError(f"bound source identity mismatch: {path}")
    if path in snapshot and snapshot[path] != digest:
        raise ValueError(f"source changed during merge: {path}")
    snapshot[path] = digest
    return digest


def _verify_snapshot(snapshot: dict[Path, str]) -> None:
    for path, expected in snapshot.items():
        if not path.is_file() or trainer.sha256(path) != expected:
            raise ValueError(f"source changed during merge: {path}")


def _source_snapshot() -> tuple[dict[str, str], dict[Path, str]]:
    snapshot: dict[Path, str] = {}
    own = "scripts/merge_onboard_sdf_visual_curricula.py"
    sources = {own: _snapshot(ROOT / own, snapshot)}
    _snapshot(Path(__file__), snapshot, sources[own])
    for relative, module in SOURCE_MODULES.items():
        digest = _snapshot(ROOT / relative, snapshot)
        # Check both canonical and actually imported helpers at admission and
        # sealing. A matching copy must not drift after its initial check.
        _snapshot(Path(module.__file__), snapshot, digest)
        sources[relative] = digest
    return sources, snapshot


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("source manifests must remain inside the repository") from exc


def _normalise_frame(frame: dict) -> dict:
    result = deepcopy(frame)
    model = result.get("target_model")
    if not isinstance(model, dict):
        raise ValueError("frame requires its actual bound target model")
    details = result.get("curriculum")
    if details is None:
        measured = result.get("visibility_measurement")
        if not isinstance(measured, dict):
            raise ValueError("clip frame requires measured visibility provenance")
        result["curriculum"] = {
            "target_model_path": model.get("path"),
            "target_model_sha256": model.get("sha256"),
            "target_geometry_sha256": model.get("geometry_sha256"),
            "planned_condition": result.get("planned_condition"),
            **deepcopy(measured),
        }
    elif not isinstance(details, dict):
        raise ValueError("invalid curriculum measurement")
    return result


def _audit_pixels(frame: dict, base: Path, cv2: Any) -> tuple[str, float | None]:
    if (
        any(type(frame.get(key)) is not int or frame[key] <= 0 for key in ("width", "height"))
        or type(frame.get("visible_target_pixels")) is not int
        or type(frame.get("target_visible")) is not bool
        or type(frame.get("target_present")) is not bool
    ):
        raise ValueError("invalid frame dimensions or target presence")
    rgb = cv2.imread(str(trainer.bound_file(base, frame["image_path"], frame["image_sha256"])))
    if rgb is None or rgb.shape != (frame["height"], frame["width"], 3):
        raise ValueError("invalid RGB dimensions")
    condition, fraction = evaluator.verify_visibility(frame, base, cv2)
    if frame["target_visible"] != (frame["visible_target_pixels"] > 0):
        raise ValueError("target visibility disagrees with actual mask")
    return condition, fraction


def _global_partitions(frames: list[dict]) -> dict:
    worlds = {split: set() for split in SPLITS}
    world_hashes = {split: set() for split in SPLITS}
    rgb_splits: dict[str, str] = {}
    for frame in frames:
        split = frame["split"]
        worlds[split].add(frame["world_path"])
        world_hashes[split].add(frame["world_sha256"])
        digest = frame["image_sha256"]
        if digest in rgb_splits and rgb_splits[digest] != split:
            raise ValueError("identical RGB pixels cross original splits in merged corpus")
        rgb_splits[digest] = split
    for left, right in (("train", "val"), ("train", "test"), ("val", "test")):
        if worlds[left] & worlds[right] or world_hashes[left] & world_hashes[right]:
            raise ValueError("source world path or content crosses merged splits")
    # Train/val intentionally share prototype geometries; the test prototype
    # must remain disjoint by actual SDF geometry, not filename or self-report.
    geometry = evaluator.verify_geometry_provenance({"frames": frames})
    return {
        "split_worlds": {split: sorted(worlds[split]) for split in SPLITS},
        "split_world_sha256": {split: sorted(world_hashes[split]) for split in SPLITS},
        "target_geometries": geometry,
        "test_geometry_disjoint_from_train_and_val": True,
        "cross_split_identical_rgb": False,
    }


def merge_datasets(manifests: list[Path], output_directory: Path) -> dict:
    if not manifests:
        raise ValueError("at least one source manifest is required")
    manifests = [path.resolve(strict=True) for path in manifests]
    if len(set(manifests)) != len(manifests):
        raise ValueError("duplicate source manifest")
    output = output_directory.resolve()
    if output.exists():
        raise FileExistsError("refusing to overwrite an existing merged dataset")
    if any(path.is_relative_to(output) for path in manifests):
        raise ValueError("merged output cannot contain a source manifest")
    sources, snapshot = _source_snapshot()
    manifest_refs = []
    for path in manifests:
        manifest_refs.append(
            {
                "path": _relative(path),
                "sha256": _snapshot(path, snapshot),
                "bytes": path.stat().st_size,
            }
        )

    import cv2

    frames, source_frames, measured_counts = [], [], {split: Counter() for split in SPLITS}
    for source_index, (manifest, reference) in enumerate(
        zip(manifests, manifest_refs, strict=True)
    ):
        strict = _strict_json(manifest)
        document, yaml = trainer.verify_dataset(manifest)
        if document != strict:
            raise ValueError("source manifest changed while it was parsed")
        if any(key in document and document[key] is not False for key in FALSE_FLAGS):
            raise ValueError("source must retain offline developer-only claim boundaries")
        _snapshot(yaml, snapshot, document["dataset_yaml_sha256"])
        if "clips" in document:
            replay.validate_clips(document)
        source_ids = set()
        for raw in document["frames"]:
            identifier = raw.get("frame_id")
            if not isinstance(identifier, str) or not identifier or identifier in source_ids:
                raise ValueError("source frame IDs must be nonempty and unique")
            source_ids.add(identifier)
            frame = _normalise_frame(raw)
            for relative, digest in frame["source_hashes"].items():
                _snapshot(trainer.bound_file(ROOT, relative, digest), snapshot, digest)
                if relative in sources and sources[relative] != digest:
                    raise ValueError("conflicting repository source hashes")
                sources[relative] = digest
            for kind in FILE_KINDS:
                if kind in {"mask_render", "amodal_render"} and f"{kind}_path" not in frame:
                    continue
                path = trainer.bound_file(
                    manifest.parent, frame[f"{kind}_path"], frame[f"{kind}_sha256"]
                )
                _snapshot(path, snapshot, frame[f"{kind}_sha256"])
            condition, _fraction = _audit_pixels(frame, manifest.parent, cv2)
            measured_counts[frame["split"]][condition] += 1
            frame["frame_id"] = f"dataset-{source_index:03d}:{identifier}"
            if "clip_id" in frame:
                frame["clip_id"] = f"dataset-{source_index:03d}:{frame['clip_id']}"
            frame["merge_provenance"] = {
                "manifest": reference,
                "frame_id": identifier,
                "original_split": raw["split"],
                "clip_frame_used_as_single_frame_sample": "clip_id" in raw,
            }
            frames.append(frame)
            source_frames.append((manifest.parent, deepcopy(raw)))
        _verify_snapshot(snapshot)

    partitions = _global_partitions(frames)
    _verify_snapshot(snapshot)
    output.mkdir(parents=True, exist_ok=False)
    for index, (frame, (base, raw)) in enumerate(zip(frames, source_frames, strict=True)):
        name, split = f"frame-{index:06d}", frame["split"]
        for kind in FILE_KINDS:
            if f"{kind}_path" not in raw:
                continue
            original = trainer.bound_file(base, raw[f"{kind}_path"], raw[f"{kind}_sha256"])
            copied = output / kind / split / f"{name}{original.suffix.lower()}"
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
            if trainer.sha256(copied) != raw[f"{kind}_sha256"]:
                raise ValueError("payload changed during independent copy; no manifest sealed")
            _snapshot(copied, snapshot, raw[f"{kind}_sha256"])
            frame[f"{kind}_path"] = copied.relative_to(output).as_posix()
        frame["source_hashes"] = {**frame["source_hashes"], **sources}
        _audit_pixels(frame, output, cv2)
    data = output / "dataset.yaml"
    with data.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "path": str(output),
                    "train": "image/train",
                    "val": "image/val",
                    "test": "image/test",
                    "names": {"0": "search_target"},
                },
                indent=2,
            )
            + "\n"
        )
    document = {
        "contract": trainer.DATASET_CONTRACT,
        "merge_contract": CONTRACT,
        "purpose": "offline_synthetic_developer_single_frame_visual_finetuning",
        "names": ["search_target"],
        "frame_count": len(frames),
        "split_frames": dict(Counter(frame["split"] for frame in frames)),
        "frames": frames,
        "dataset_yaml": data.name,
        "dataset_yaml_sha256": trainer.sha256(data),
        "source_manifests": manifest_refs,
        "source_hashes": sources,
        "partition_policy": "preserve_every_original_split_never_promote_val_or_test_into_train",
        "split_unit": "original_source_world_and_complete_clip",
        **partitions,
        "measured_condition_counts": {split: dict(measured_counts[split]) for split in SPLITS},
        "labels_verified_against_actual_visible_masks": True,
        "amodal_masks_reverified": True,
        "billboard_images_used": False,
        "offline_truth_only": True,
        "continuous_frames_used_as_single_frame_training_samples": True,
        "test_usage": "previously_diagnosed_development_holdout_not_untouched_blind_test",
        **dict.fromkeys(FALSE_FLAGS, False),
        "limitations": [
            "offline_Blender_pixels_not_Gazebo_sensor_or_physics_capture",
            "merged_clip_frames_do_not_train_a_temporal_model",
            "single_search_target_class_not_car_person_or_color_alias",
            "existing_development_holdouts_have_been_observed_not_blind_tests",
            "train_validation_prototype_geometry_overlap_is_intentional_test_is_disjoint",
            "no_action_labels_policy_training_candidate_promotion_or_flight_acceptance",
        ],
    }
    _snapshot(data, snapshot, document["dataset_yaml_sha256"])
    _verify_snapshot(snapshot)
    manifest = output / "manifest.json"
    # Check the frozen trainer's exact compatibility before sealing the final
    # inventory. The temporary candidate is not named as an accepted manifest.
    candidate = output / "manifest.pending.json"
    payload = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    with candidate.open("xb") as stream:
        stream.write(payload)
    _snapshot(candidate, snapshot, payload_sha256)
    _verify_snapshot(snapshot)
    if candidate.read_bytes() != payload:
        raise ValueError("pending manifest bytes changed before validation")
    trainer.verify_dataset(candidate)
    _verify_snapshot(snapshot)
    if candidate.read_bytes() != payload:
        raise ValueError("pending manifest bytes changed after validation; refusing publication")
    candidate.rename(manifest)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    document = merge_datasets(args.manifest, args.output_directory)
    print(
        json.dumps(
            {
                "output_manifest": str(args.output_directory.resolve() / "manifest.json"),
                "frame_count": document["frame_count"],
                "split_frames": document["split_frames"],
                "temporal_training_performed": False,
                "candidate_promoted": False,
                "final_release_allowed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

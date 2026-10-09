#!/usr/bin/env python3
"""Export visible Gazebo instance boxes and real pixels as YOLO detector data.

Use one explicit split per complete recording, not per randomly selected frame.
The v1 canonical annotation contract is unreviewed, so this exporter creates
staging data only. It does not run training, reinterpret neutral classes as
vehicles, or turn policy-action review receipts into visual-label approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    root = Path(__file__).resolve().parents[1]
    for source in (root, root / "src"):
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.backends.raw_replay import (  # noqa: E402
    RawSensorReplayRecording,
    _decode_image_bgr,
    _validate_decoded_image,
)
from scripts.gazebo_3d_annotation_frame_set import load_annotation_frame_set  # noqa: E402


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(raw: str) -> None:
    raise ValueError(f"non-finite JSON value: {raw}")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ValueError(f"input must be a JSON object: {path}")
    return value


def _file(path: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink() or not requested.is_file():
        raise ValueError(f"input file is missing or is a symlink: {requested}")
    return requested.resolve(strict=True)


def _contained_payload(root: Path, raw: object) -> Path:
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or ":" in raw
        or PurePosixPath(raw).is_absolute()
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ValueError("RGB payload path must be a contained portable relative path")
    candidate = root / raw
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("RGB payload path escaped its capture root")
    if any(
        parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
        for parent in (candidate, *candidate.parents)
        if parent != root
    ):
        raise ValueError("RGB payload path contains a symlink or junction")
    return _file(candidate)


def _class_mapping(path: Path) -> tuple[dict[int, str], dict[int, int]]:
    document = _load(path)
    if not document:
        raise ValueError("class map requires explicit label_id to name mappings")
    classes = {}
    for key, value in document.items():
        if (
            re.fullmatch(r"0|[1-9][0-9]*", key) is None
            or not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("class map must explicitly map integer label IDs to nonempty names")
        classes[int(key)] = value
    if len({name.casefold() for name in classes.values()}) != len(classes):
        raise ValueError("class names must be distinct")
    classes = dict(sorted(classes.items()))
    return classes, {label: index for index, label in enumerate(classes)}


def _write(path: Path, payload: bytes) -> dict[str, object]:
    with path.open("xb") as destination:
        destination.write(payload)
        destination.flush()
    if _hash_file(path) != _sha(payload):
        raise ValueError("exported dataset file differs from the encoded bytes")
    return {"sha256": _sha(payload), "bytes": len(payload)}


def export_dataset(
    *,
    rgb_manifests: list[Path],
    annotation_frame_sets: list[Path],
    recording_manifests: list[Path],
    splits: list[str],
    class_map: Path,
    output_directory: Path,
) -> dict[str, object]:
    """Export exact RGB/annotation pairs, partitioned by verified recordings."""
    if not rgb_manifests or not (
        len(rgb_manifests) == len(annotation_frame_sets) == len(recording_manifests) == len(splits)
    ):
        raise ValueError("RGB, annotation, recording and split arguments must have matching counts")
    if not {"train", "val"}.issubset(splits) or any(
        split not in {"train", "val", "test"} for split in splits
    ):
        raise ValueError("explicit recording-level splits must include both train and val")
    class_map = _file(class_map)
    classes, class_indices = _class_mapping(class_map)
    input_hashes = {class_map: _hash_file(class_map)}
    entries = []
    recording_splits: dict[str, str] = {}
    payload_splits: dict[str, str] = {}
    for rgb_path, annotation_path, recording_path, split in zip(
        rgb_manifests, annotation_frame_sets, recording_manifests, splits, strict=True
    ):
        rgb_path, annotation_path, recording_path = (
            _file(path) for path in (rgb_path, annotation_path, recording_path)
        )
        for path in (rgb_path, annotation_path, recording_path):
            input_hashes[path] = _hash_file(path)
        # Check payload containment before the shared loader opens image paths.
        rgb = _load(rgb_path)
        annotation = _load(annotation_path)
        if annotation.get("schema") != "multidetect.gazebo-3d-annotation-frame-set":
            raise ValueError("export requires the canonical annotation frame set, not staging rows")
        frames = rgb.get("frames")
        if not isinstance(frames, list) or not frames:
            raise ValueError("RGB capture must have nonempty frames")
        for frame in frames:
            if not isinstance(frame, dict):
                raise ValueError("RGB frame is invalid")
            _contained_payload(rgb_path.parent, frame.get("file"))
        bound = load_annotation_frame_set(annotation_path, rgb_manifest=rgb_path)
        recording = RawSensorReplayRecording.from_manifest(
            recording_path, expected_manifest_sha256=input_hashes[recording_path]
        )
        previous_split = recording_splits.get(recording.recording_hash)
        if previous_split is not None:
            raise ValueError("a recording hash cannot be repeated or assigned across splits")
        recording_splits[recording.recording_hash] = split
        if (
            recording.scenario_manifest_hash != rgb.get("scenario_manifest_hash")
            or recording.scenario_id != rgb.get("scenario_id")
            or len(recording.frames) != len(frames)
        ):
            raise ValueError("recording scenario/frame count does not match the RGB capture")
        for raw_frame, frame in zip(recording.frames, frames, strict=True):
            if raw_frame.frame_id != frame.get("frame_id"):
                raise ValueError("recording and RGB frame identity do not match")
            digest = frame.get("sha256")
            previous_split = payload_splits.get(digest)
            if previous_split is not None and previous_split != split:
                raise ValueError("RGB payload cannot be duplicated across dataset splits")
            payload_splits[digest] = split
            key = (frame["sequence"], frame["frame_id"], digest)
            if raw_frame.captured_at_s != bound[key]["captured_at_s"]:
                raise ValueError("recording and annotation frame timestamp do not match")
            segmentation = bound[key]["instance_segmentation"]
            if segmentation["label_id"] not in classes:
                raise ValueError("visible segmentation label_id has no explicit class mapping")
        entries.append((rgb_path, annotation_path, recording_path, split, recording, frames, bound))

    # Decode the recording image independently. Its actual pixels/dimensions
    # must reproduce the raw BGR payload, not just agree with manifest strings.
    import cv2
    import numpy as np

    requested_output = output_directory.expanduser()
    if requested_output.is_symlink():
        raise ValueError("dataset output must not be a symlink")
    output = requested_output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for split in ("train", "val", "test"):
        (output / "images" / split).mkdir(parents=True)
        (output / "labels" / split).mkdir(parents=True)
    exported = []
    sources = []
    for rgb_path, annotation_path, recording_path, split, recording, frames, bound in entries:
        for raw_frame, frame in zip(recording.frames, frames, strict=True):
            source = _contained_payload(rgb_path.parent, frame["file"])
            raw_payload = source.read_bytes()
            if _sha(raw_payload) != frame["sha256"]:
                raise ValueError("RGB payload changed before YOLO image export")
            image = _decode_image_bgr(raw_frame.image.read_verified_bytes(), raw_frame.image)
            _validate_decoded_image(image, raw_frame.image)
            height, width = image.shape[:2]
            provenance = frame.get("provenance", {})
            if (
                any(
                    isinstance(provenance.get(key), bool)
                    or not isinstance(provenance.get(key), int)
                    for key in ("height", "width")
                )
                or (height, width) != (provenance.get("height"), provenance.get("width"))
                or len(raw_payload) != width * height * 3
                or _sha(image.tobytes()) != frame["sha256"]
            ):
                raise ValueError("actual recording pixels/dimensions do not match raw RGB payload")
            key = (frame["sequence"], frame["frame_id"], frame["sha256"])
            segmentation = bound[key]["instance_segmentation"]
            if segmentation["visible_pixels"] > width * height:
                raise ValueError("visible segmentation pixel count exceeds actual image dimensions")
            x1, y1, x2, y2 = segmentation["visible_bbox_normalized"]
            class_index = class_indices[segmentation["label_id"]]
            label_text = (
                f"{class_index} {(x1 + x2) / 2:.10f} {(y1 + y2) / 2:.10f} "
                f"{x2 - x1:.10f} {y2 - y1:.10f}\n"
            )
            success, png = cv2.imencode(".png", image)
            if not success:
                raise ValueError("could not encode lossless YOLO image")
            decoded_export = cv2.imdecode(png, cv2.IMREAD_COLOR)
            if (
                decoded_export is None
                or decoded_export.shape != image.shape
                or not np.array_equal(decoded_export, image)
            ):
                raise ValueError("exported PNG does not preserve the input pixels")
            name = f"{recording.recording_hash}-{frame['sequence']:08d}-{frame['sha256'][:16]}"
            image_relative = f"images/{split}/{name}.png"
            label_relative = f"labels/{split}/{name}.txt"
            image_file = _write(output / image_relative, png.tobytes())
            label_file = _write(output / label_relative, label_text.encode("utf-8"))
            exported.append(
                {
                    "split": split,
                    "recording_hash": recording.recording_hash,
                    "frame_id": frame["frame_id"],
                    "sequence": frame["sequence"],
                    "rgb_payload_sha256": frame["sha256"],
                    "width": width,
                    "height": height,
                    "segmentation_payload_sha256": segmentation["payload_sha256"],
                    "visible_bbox_normalized": [x1, y1, x2, y2],
                    "label_id": segmentation["label_id"],
                    "class_index": class_index,
                    "image": {"path": image_relative, **image_file},
                    "label": {"path": label_relative, **label_file},
                }
            )
        sources.append(
            {
                "split": split,
                "recording_id": recording.recording_id,
                "recording_hash": recording.recording_hash,
                "recording_manifest": {
                    "path": recording_path.as_posix(),
                    "sha256": input_hashes[recording_path],
                },
                "rgb_manifest": {"path": rgb_path.as_posix(), "sha256": input_hashes[rgb_path]},
                "annotation_frame_set": {
                    "path": annotation_path.as_posix(),
                    "sha256": input_hashes[annotation_path],
                },
            }
        )

    # Reopen inputs before sealing outputs. Changes leave only unsealed files.
    for path, digest in input_hashes.items():
        if _hash_file(path) != digest:
            raise ValueError("source manifest or class mapping changed during export")
    for rgb_path, annotation_path, recording_path, _split, _recording, _frames, _bound in entries:
        load_annotation_frame_set(annotation_path, rgb_manifest=rgb_path)
        RawSensorReplayRecording.from_manifest(
            recording_path, expected_manifest_sha256=input_hashes[recording_path]
        )
    counts = dict(Counter(item["split"] for item in exported))
    yaml = [
        "# Offline staging only: export-receipt.json has training_ready=false.",
        f"path: {json.dumps(output.as_posix(), ensure_ascii=False)}",
        "train: images/train",
        "val: images/val",
    ]
    if counts.get("test", 0):
        yaml.append("test: images/test")
    yaml.extend((f"nc: {len(classes)}", "names:"))
    yaml.extend(
        f"  {class_indices[label]}: {json.dumps(name, ensure_ascii=False)}"
        for label, name in classes.items()
    )
    data_yaml = _write(output / "data.yaml", ("\n".join(yaml) + "\n").encode("utf-8"))
    receipt = {
        "schema": "multidetect.gazebo-annotation-yolo-export",
        "schema_version": 1,
        "contract": "multidetect.gazebo-annotation-yolo-export-v1",
        "purpose": "visual_detector_supervision_staging",
        "bbox_source": "visible_instance_segmentation_bbox",
        "scene_truth_used": False,
        "policy_40_feature_rows_used": False,
        "class_map": {"path": class_map.as_posix(), "sha256": input_hashes[class_map]},
        "classes": [
            {"label_id": label, "name": name, "class_index": class_indices[label]}
            for label, name in classes.items()
        ],
        "sources": sources,
        "rows": exported,
        "split_counts": counts,
        "split_unit": "complete_verified_recording",
        "independent_train_val_recording_hashes": True,
        "rgb_payload_disjoint_across_splits": True,
        "data_yaml": {"path": "data.yaml", **data_yaml},
        "training_ready": False,
        "training_blockers": [
            "canonical_v1_annotation_supervision_is_unreviewed",
            "independent_visual_bbox_and_class_review_missing",
            "runtime_annotation_world_asset_attestation_missing",
        ],
        "runtime_annotation_input": False,
        "runtime_asset_attestation_verified": False,
        "independent_visual_review_verified": False,
        "verification_scope": "supplied_canonical_capture_and_recording_files_only",
        "external_sim_execution_evidence": False,
        "external_flight_evidence": False,
        "hardware_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }
    receipt["canonical_hash"] = canonical_sha256(receipt)
    _write(
        output / "export-receipt.json",
        (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rgb-manifest", action="append", type=Path, required=True)
    parser.add_argument("--annotation-frame-set", action="append", type=Path, required=True)
    parser.add_argument("--recording-manifest", action="append", type=Path, required=True)
    parser.add_argument("--split", action="append", choices=("train", "val", "test"), required=True)
    parser.add_argument(
        "--class-map",
        type=Path,
        required=True,
        help='Explicit JSON label_id to name mapping, e.g. {"42":"search_target"}',
    )
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    receipt = export_dataset(
        rgb_manifests=args.rgb_manifest,
        annotation_frame_sets=args.annotation_frame_set,
        recording_manifests=args.recording_manifest,
        splits=args.split,
        class_map=args.class_map,
        output_directory=args.output_directory,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Adapt a verified Gazebo annotation staging bind to the canonical v1 frame set.

This is an explicit boundary between the capture-only v2 staging contract and
the rendered-data builder's v1 annotation contract.  It never creates an RGB
manifest, never invents production provenance, and never uses timestamps to
find a nearby frame.  The caller must provide the canonical, hash-bound RGB
manifest produced by the normal sensor capture path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.gazebo_3d_annotation_frame_set import AnnotationFrameSetError

ROOT = Path(__file__).resolve().parents[1]


class AnnotationStagingAdapterError(ValueError):
    """The staging bind cannot be safely projected into the v1 contract."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationStagingAdapterError(f"{name} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AnnotationStagingAdapterError(f"{name} must be a JSON object")
    return value


def _hash(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AnnotationStagingAdapterError(f"{name} must be a lowercase SHA-256")
    return value


def _resolve(path: Path, raw: object, name: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise AnnotationStagingAdapterError(f"{name} path is missing")
    candidates = [Path(raw)] if Path(raw).is_absolute() else [path.parent / raw, ROOT / raw]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise AnnotationStagingAdapterError(f"{name} file is missing: {raw}")


def _require_nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnotationStagingAdapterError(f"{name} must be non-empty")
    return value


def adapt(
    *,
    staging_bind_path: Path,
    rgb_manifest_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    staging_bind_path = staging_bind_path.expanduser().resolve(strict=True)
    rgb_manifest_path = rgb_manifest_path.expanduser().resolve(strict=True)
    output_path = output_path.expanduser().resolve()
    staging = _load(staging_bind_path, "staging bind")
    if (
        staging.get("schema") != "multidetect.gazebo-3d-annotation-staging-frame-set"
        or staging.get("schema_version") != 2
        or staging.get("contract") != "multidetect.gazebo-3d-annotation-staging-frame-set-v2"
        or staging.get("training_ready") is not False
    ):
        raise AnnotationStagingAdapterError("staging bind contract is unsupported")
    boundary = staging.get("runtime_boundary")
    if not isinstance(boundary, dict) or boundary != {
        "offline_supervision_only": True,
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
        "training_requires_independent_review": True,
    }:
        raise AnnotationStagingAdapterError("staging bind runtime boundary is invalid")

    rgb = _load(rgb_manifest_path, "RGB manifest")
    if rgb.get("schema") != "multidetect.gazebo-bgr24-frame-set":
        raise AnnotationStagingAdapterError("RGB manifest schema is unsupported")
    scenario_manifest_hash = _hash(
        rgb.get("scenario_manifest_hash"), "RGB scenario_manifest_hash"
    )
    scenario_id = _require_nonempty(staging.get("scenario_id"), "staging scenario_id")
    if scenario_id != rgb.get("scenario_id"):
        raise AnnotationStagingAdapterError("staging/RGB scenario binding mismatch")
    rgb_frames = rgb.get("frames")
    if not isinstance(rgb_frames, list) or not rgb_frames:
        raise AnnotationStagingAdapterError("RGB manifest has no frames")
    rgb_index: dict[tuple[int, str, str], dict[str, Any]] = {}
    for index, frame in enumerate(rgb_frames):
        if not isinstance(frame, dict):
            raise AnnotationStagingAdapterError(f"RGB frame {index} is invalid")
        sequence = frame.get("sequence")
        frame_id = _require_nonempty(frame.get("frame_id"), f"RGB frame {index}.frame_id")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise AnnotationStagingAdapterError(f"RGB frame {index}.sequence is invalid")
        digest = _hash(frame.get("sha256"), f"RGB frame {index}.sha256")
        key = (sequence, frame_id, digest)
        if key in rgb_index:
            raise AnnotationStagingAdapterError("RGB frame identities are not unique")
        rgb_index[key] = frame

    config = staging.get("annotation_config")
    wrappers = staging.get("wrapper_binding")
    if not isinstance(config, dict) or set(config) != {"path", "sha256"}:
        raise AnnotationStagingAdapterError("staging annotation_config binding is invalid")
    config_path = _resolve(staging_bind_path, config.get("path"), "annotation config")
    config_hash = _hash(config.get("sha256"), "annotation config sha256")
    if _sha256(config_path) != config_hash:
        raise AnnotationStagingAdapterError("annotation config hash mismatch")
    if not isinstance(wrappers, dict) or set(wrappers) != {
        "vehicle_wrapper_sha256",
        "target_wrapper_sha256",
    }:
        raise AnnotationStagingAdapterError("staging wrapper binding is invalid")
    wrapper_binding = {
        key: _hash(value, f"wrapper binding {key}") for key, value in wrappers.items()
    }

    staging_frames = staging.get("frames")
    if not isinstance(staging_frames, list) or not staging_frames:
        raise AnnotationStagingAdapterError("staging bind has no frames")
    canonical_frames: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for index, frame in enumerate(staging_frames):
        if not isinstance(frame, dict):
            raise AnnotationStagingAdapterError(f"staging frame {index} is invalid")
        sequence = frame.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise AnnotationStagingAdapterError(f"staging frame {index}.sequence is invalid")
        frame_id = _require_nonempty(frame.get("frame_id"), f"staging frame {index}.frame_id")
        rgb_hash = _hash(frame.get("rgb_payload_sha256"), f"staging frame {index}.rgb hash")
        key = (sequence, frame_id, rgb_hash)
        if key in seen or key not in rgb_index:
            raise AnnotationStagingAdapterError(
                f"staging frame {index} does not exactly bind the canonical RGB frame"
            )
        seen.add(key)
        sensor_time = frame.get("sensor_time_s")
        if (
            isinstance(sensor_time, bool)
            or not isinstance(sensor_time, (int, float))
            or sensor_time < 0
        ):
            raise AnnotationStagingAdapterError(f"staging frame {index}.sensor_time_s is invalid")
        bbox = frame.get("bounding_box_3d")
        segmentation = frame.get("instance_segmentation")
        if not isinstance(bbox, dict) or not isinstance(segmentation, dict):
            raise AnnotationStagingAdapterError(f"staging frame {index} supervision is incomplete")
        normalized = bbox.get("normalized_bbox")
        range_3d = bbox.get("range_3d_m")
        if (
            not isinstance(normalized, list)
            or len(normalized) != 4
            or not all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in normalized)
            or not normalized[0] < normalized[2]
            or not normalized[1] < normalized[3]
            or not isinstance(range_3d, list)
            or len(range_3d) != 3
        ):
            raise AnnotationStagingAdapterError(f"staging frame {index} geometry is invalid")
        canonical_frames.append(
            {
                "sequence": sequence,
                "frame_id": frame_id,
                "rgb_payload_sha256": rgb_hash,
                "captured_at_s": float(sensor_time),
                "bounding_box_3d": {
                    "topic": "multidetect/annotation/bounding_boxes_3d",
                    "label_id": 42,
                    "normalized_bbox": list(normalized),
                    "range_3d_m": [float(value) for value in range_3d],
                },
                "instance_segmentation": {
                    "topic": "multidetect/annotation/instance_segmentation",
                    "labels_map_topic": "multidetect/annotation/instance_segmentation/labels_map",
                    "label_id": 42,
                    "payload_sha256": _hash(
                        segmentation.get("payload_sha256"),
                        f"staging frame {index}.labels-map payload hash",
                    ),
                    "visible_bbox_normalized": list(
                        segmentation.get("visible_bbox_normalized", normalized)
                    ),
                    "visible_pixels": segmentation.get("visible_pixels"),
                },
            }
        )
    if seen != set(rgb_index):
        raise AnnotationStagingAdapterError("staging bind does not cover every RGB frame")

    result = {
        "schema": "multidetect.gazebo-3d-annotation-frame-set",
        "schema_version": 1,
        "contract": "multidetect.gazebo-3d-annotation-frame-set-v1",
        "source_attribution": {
            "annotation": "actual_gazebo_capture_only_annotation_output",
            "capture_only": True,
            "truth_loaded_by_runtime": False,
            "runtime_consumes_annotation_topics": False,
        },
        "rgb_frame_set": {
            "path": rgb_manifest_path.as_posix(),
            "manifest_sha256": _sha256(rgb_manifest_path),
            "scenario_id": scenario_id,
            "scenario_manifest_hash": scenario_manifest_hash,
        },
        "annotation_config": {"path": config_path.as_posix(), "sha256": config_hash},
        "wrapper_binding": wrapper_binding,
        "topic_contract": {
            "bounding_box_3d": "multidetect/annotation/bounding_boxes_3d",
            "instance_segmentation": "multidetect/annotation/instance_segmentation",
        },
        "label_contract": {"label_id": 42, "class_name": "search_target"},
        "runtime_boundary": {
            "offline_supervision_only": True,
            "runtime_consumes_annotation_topics": False,
            "truth_injected_into_policy": False,
            "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
            "training_requires_independent_review": True,
        },
        "frames": canonical_frames,
        "training_ready": False,
        "reviewed_annotation_manifest": None,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-bind", type=Path, required=True)
    parser.add_argument("--rgb-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = adapt(
            staging_bind_path=args.staging_bind,
            rgb_manifest_path=args.rgb_manifest,
            output_path=args.output,
        )
    except (OSError, AnnotationStagingAdapterError, AnnotationFrameSetError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

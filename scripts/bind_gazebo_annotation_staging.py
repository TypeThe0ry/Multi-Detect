#!/usr/bin/env python3
"""Bind Gazebo v2 annotation observations to an immutable sensor staging set.

The result is an offline supervision staging document, not a production RGB
manifest and not a runtime policy input.  Every join is exact on
``sensor_stamp_ns`` and the RGB payload hash carried by the sensor staging
manifest.  Missing, duplicate, behind-camera or non-visible targets fail
closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_KEYS = frozenset(
    {
        "truth",
        "ground_truth",
        "scenario_truth",
        "target_world_position",
        "target_model_id",
        "runtime_policy_input",
        "policy_features",
        "context_snapshot",
        "detector_input",
    }
)


class AnnotationStagingBindError(ValueError):
    """The annotation stream cannot be safely joined to sensor staging."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnnotationStagingBindError(f"{name} must be an object")
    return value


def _hash(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AnnotationStagingBindError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnnotationStagingBindError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise AnnotationStagingBindError(f"{name} must be finite")
    return result


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnnotationStagingBindError(f"{name} must be a non-negative integer")
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnotationStagingBindError(f"{name} must be a non-empty string")
    return value


def _scan_forbidden(value: object, path: str = "document") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise AnnotationStagingBindError(f"{path}.{key} is a truth/runtime input")
            _scan_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_forbidden(child, f"{path}[{index}]")


def _load(path: Path, name: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationStagingBindError(f"{name} is not valid JSON: {path}") from exc
    return _object(document, name)


def _resolve_file(path: Path, raw: object, name: str) -> Path:
    value = raw if isinstance(raw, str) else ""
    candidate = Path(value) if Path(value).is_absolute() else path.parent / value
    candidate = candidate.expanduser().resolve()
    try:
        candidate.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise AnnotationStagingBindError(f"{name} escapes its evidence directory") from exc
    if not candidate.is_file():
        raise AnnotationStagingBindError(f"{name} is missing: {value}")
    return candidate


def _resolve_repository_file(path: Path, raw: object, name: str) -> Path:
    """Resolve a checked-in binding referenced by a staging document.

    Payloads must remain inside their evidence directory.  Configuration and
    wrapper assets are different: they are repository-owned inputs, so an
    absolute path or a repository-relative path is allowed and is hash checked
    before it is copied into the result.
    """

    value = raw if isinstance(raw, str) else ""
    candidates = [Path(value)] if Path(value).is_absolute() else [path.parent / value, ROOT / value]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise AnnotationStagingBindError(f"{name} is missing: {value}")


def _annotation_config_binding(
    staging_manifest_path: Path, staging: dict[str, Any]
) -> dict[str, Any]:
    binding = _object(staging.get("annotation_config"), "sensor staging annotation_config")
    if set(binding) != {"path", "sha256"}:
        raise AnnotationStagingBindError("sensor staging annotation_config binding is invalid")
    config_path = _resolve_repository_file(
        staging_manifest_path, binding.get("path"), "annotation config"
    )
    config_hash = _hash(binding.get("sha256"), "annotation config sha256")
    if _sha256(config_path) != config_hash:
        raise AnnotationStagingBindError("annotation config hash mismatch")
    config = _load(config_path, "annotation config")
    if (
        config.get("schema") != "multidetect.gazebo-3d-annotation-capture"
        or config.get("schema_version") != 1
        or config.get("contract") != "multidetect.gazebo-3d-annotation-capture-v1"
        or config.get("purpose") != "offline_3d_annotation_only"
    ):
        raise AnnotationStagingBindError("annotation config contract is unsupported")
    boundary = _object(config.get("runtime_boundary"), "annotation config runtime_boundary")
    if boundary.get("runtime_consumes_annotation_topics") is not False:
        raise AnnotationStagingBindError("annotation config crosses the runtime boundary")
    wrappers: dict[str, str] = {}
    for field, config_key in (
        ("vehicle_wrapper_sha256", "vehicle_wrapper"),
        ("target_wrapper_sha256", "target_wrapper"),
    ):
        wrapper = _object(config.get(config_key), f"annotation config {config_key}")
        wrapper_hash = _hash(wrapper.get("sha256"), f"annotation config {config_key} sha256")
        wrapper_path = _resolve_repository_file(
            config_path, wrapper.get("path"), f"annotation config {config_key}"
        )
        if _sha256(wrapper_path) != wrapper_hash:
            raise AnnotationStagingBindError(f"annotation config {config_key} hash mismatch")
        wrappers[field] = wrapper_hash
    base_world = _object(config.get("base_world"), "annotation config base_world")
    world_path = str(base_world.get("path", ""))
    scenario_id = Path(world_path).stem
    if not scenario_id:
        raise AnnotationStagingBindError(
            "annotation config base_world does not identify a scenario"
        )
    return {
        "path": config_path.as_posix(),
        "sha256": config_hash,
        "scenario_id": scenario_id,
        "wrapper_binding": wrappers,
    }


def _visible_bbox(value: object, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise AnnotationStagingBindError(f"{name} must contain four coordinates")
    result = [_finite(item, f"{name}[{index}]") for index, item in enumerate(value)]
    x1, y1, x2, y2 = result
    if not all(0.0 <= item <= 1.0 for item in result) or not x1 < x2 or not y1 < y2:
        raise AnnotationStagingBindError(f"{name} is outside the visible image")
    return result


def _segmentation_payload_geometry(
    segmentation: dict[str, Any], payload: bytes, *, label_id: int = 42
) -> tuple[list[float], int, list[int]]:
    width = segmentation.get("width")
    height = segmentation.get("height")
    step = segmentation.get("step")
    pixel_format = segmentation.get("pixel_format")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (width, height, step)
    ):
        raise AnnotationStagingBindError("labels-map geometry is invalid")
    if pixel_format != 3:
        raise AnnotationStagingBindError("labels-map pixel_format must be RGB_INT8(3)")
    if step < width * 3 or len(payload) != step * height:
        raise AnnotationStagingBindError("labels-map payload length does not match geometry")
    min_x, min_y = width, height
    max_x, max_y = -1, -1
    visible_pixels = 0
    instance_ids: set[int] = set()
    for y in range(height):
        row_start = y * step
        for x in range(width):
            offset = row_start + x * 3
            if payload[offset + 2] != label_id:
                continue
            visible_pixels += 1
            instance_ids.add(payload[offset + 1] * 256 + payload[offset])
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
    if visible_pixels <= 0:
        raise AnnotationStagingBindError("labels-map contains no visible label-42 pixels")
    bbox = [
        min_x / width,
        min_y / height,
        (max_x + 1) / width,
        (max_y + 1) / height,
    ]
    return bbox, visible_pixels, sorted(instance_ids)


def _target_box(row: dict[str, Any]) -> dict[str, Any]:
    boxes = row.get("bounding_boxes_3d")
    if not isinstance(boxes, list):
        raise AnnotationStagingBindError("bounding_boxes_3d must be a list")
    targets = [item for item in boxes if isinstance(item, dict) and item.get("label_id") == 42]
    if len(targets) != 1:
        raise AnnotationStagingBindError(
            f"expected exactly one label-42 3-D target, received {len(targets)}"
        )
    target = targets[0]
    center_raw = target.get("center_xyz_m")
    size_raw = target.get("size_xyz_m")
    orientation_raw = target.get("orientation_xyzw")
    if not all(isinstance(item, list) and len(item) == 3 for item in (center_raw, size_raw)):
        raise AnnotationStagingBindError("3-D target center/size must be 3-vectors")
    if not isinstance(orientation_raw, list) or len(orientation_raw) != 4:
        raise AnnotationStagingBindError("3-D target orientation must be a quaternion")
    center = [_finite(item, "target.center_xyz_m") for item in center_raw]
    size = [_finite(item, "target.size_xyz_m") for item in size_raw]
    orientation = [_finite(item, "target.orientation_xyzw") for item in orientation_raw]
    if any(item <= 0.0 for item in size):
        raise AnnotationStagingBindError("3-D target size must be positive")
    # Gazebo rendering-view coordinates use -Z as the forward direction.
    if center[2] >= 0.0 or math.sqrt(sum(item * item for item in center)) <= 0.0:
        raise AnnotationStagingBindError("3-D target is behind or at the camera")
    return {
        "topic": "multidetect/annotation/bounding_boxes_3d",
        "label_id": 42,
        "center_xyz_m": center,
        "size_xyz_m": size,
        "orientation_xyzw": orientation,
    }


def bind(
    *,
    staging_manifest_path: Path,
    observation_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    staging_manifest_path = staging_manifest_path.expanduser().resolve(strict=True)
    observation_path = observation_path.expanduser().resolve(strict=True)
    output_path = output_path.expanduser().resolve()
    staging = _load(staging_manifest_path, "sensor staging manifest")
    observations = _load(observation_path, "annotation observation stream")
    _scan_forbidden(staging)
    _scan_forbidden(observations)
    if staging.get("schema") != "multidetect.gazebo-annotation-sensor-staging":
        raise AnnotationStagingBindError("unsupported sensor staging schema")
    if (
        staging.get("schema_version") != 1
        or staging.get("contract") != "multidetect.gazebo-annotation-sensor-staging-v1"
    ):
        raise AnnotationStagingBindError("sensor staging contract is unsupported")
    if staging.get("training_ready") is not False:
        raise AnnotationStagingBindError("sensor staging cannot be training-ready")
    staging_boundary = _object(staging.get("runtime_boundary"), "sensor staging runtime_boundary")
    if staging_boundary != {
        "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "offline_staging_only": True,
    }:
        raise AnnotationStagingBindError("sensor staging runtime boundary is invalid")
    annotation_config = _annotation_config_binding(staging_manifest_path, staging)
    if observations.get("schema") != "multidetect.gazebo-3d-annotation-observation-stream":
        raise AnnotationStagingBindError("unsupported annotation observation schema")
    if (
        observations.get("schema_version") != 2
        or observations.get("contract") != "multidetect.gazebo-3d-annotation-observation-stream-v2"
        or observations.get("capture_only") is not True
        or observations.get("truth_injected_into_policy") is not False
        or observations.get("join_key") != ["sensor_stamp_ns", "rgb_payload_sha256"]
        or observations.get("coordinate_frame") != "rendering_view_x_right_y_up_z_backward"
    ):
        raise AnnotationStagingBindError("annotation observation stream is not v2 capture-only")
    if observations.get("runtime_consumes_annotation_topics") is not False:
        raise AnnotationStagingBindError("annotation observation stream crosses runtime boundary")
    expected_labels_topic = "/multidetect/annotation/instance_segmentation/labels_map"
    if observations.get("labels_map_topic") != expected_labels_topic:
        raise AnnotationStagingBindError("annotation labels-map topic is not the canonical topic")
    staging_frames = staging.get("frames")
    observation_frames = observations.get("frames")
    if not isinstance(staging_frames, list) or not staging_frames:
        raise AnnotationStagingBindError("sensor staging has no frames")
    if not isinstance(observation_frames, list) or not observation_frames:
        raise AnnotationStagingBindError("annotation observations have no frames")
    by_stamp: dict[int, dict[str, Any]] = {}
    for index, raw in enumerate(observation_frames):
        row = _object(raw, f"annotation frame {index}")
        stamp = _nonnegative_integer(
            row.get("sensor_stamp_ns"), f"annotation frame {index}.sensor_stamp_ns"
        )
        if stamp in by_stamp:
            raise AnnotationStagingBindError(f"duplicate annotation sensor timestamp: {stamp}")
        by_stamp[stamp] = row
    if len(by_stamp) != len(staging_frames):
        raise AnnotationStagingBindError(
            f"annotation/staging frame count mismatch: {len(by_stamp)} != {len(staging_frames)}"
        )
    bound_frames: list[dict[str, Any]] = []
    seen_stamps: set[int] = set()
    for index, raw in enumerate(staging_frames):
        sensor = _object(raw, f"staging frame {index}")
        sequence = _nonnegative_integer(sensor.get("sequence"), f"staging frame {index}.sequence")
        stamp = _nonnegative_integer(
            sensor.get("sensor_stamp_ns"), f"staging frame {index}.sensor_stamp_ns"
        )
        if stamp in seen_stamps:
            raise AnnotationStagingBindError(f"duplicate staging sensor timestamp: {stamp}")
        seen_stamps.add(stamp)
        observation = by_stamp.get(stamp)
        if observation is None:
            raise AnnotationStagingBindError(f"missing exact annotation timestamp: {stamp}")
        observed_sequence = observation.get("sequence")
        if observed_sequence != sequence:
            raise AnnotationStagingBindError(
                f"annotation sequence mismatch at sensor timestamp {stamp}"
            )
        if observation.get("frame_id") != sensor.get("frame_id"):
            raise AnnotationStagingBindError(
                f"annotation frame_id mismatch at sensor timestamp {stamp}"
            )
        if observation.get("runtime_consumes_annotation_topics") is not False:
            raise AnnotationStagingBindError("annotation observation crosses runtime boundary")
        target = _target_box(observation)
        if observation.get("bounding_box_topic") != "multidetect/annotation/bounding_boxes_3d":
            raise AnnotationStagingBindError("annotation bounding-box topic is invalid")
        if (
            observation.get("instance_segmentation_topic")
            != "multidetect/annotation/instance_segmentation"
        ):
            raise AnnotationStagingBindError("annotation segmentation topic is invalid")
        if observation.get("instance_segmentation_labels_map_topic") != expected_labels_topic:
            raise AnnotationStagingBindError("annotation frame labels-map topic is invalid")
        segmentation = _object(
            observation.get("instance_segmentation"),
            f"annotation frame {index}.instance_segmentation",
        )
        if segmentation.get("sensor_stamp_ns") != stamp:
            raise AnnotationStagingBindError("labels-map timestamp does not match bounding box")
        visible_bbox = _visible_bbox(
            segmentation.get("visible_bbox_normalized"),
            f"annotation frame {index}.visible_bbox_normalized",
        )
        payload_hash = _hash(
            segmentation.get("payload_sha256"),
            f"annotation frame {index}.labels_map.payload_sha256",
        )
        rgb_hash = _hash(
            sensor.get("rgb_payload_sha256"), f"staging frame {index}.rgb_payload_sha256"
        )
        rgb_path = _resolve_file(
            staging_manifest_path,
            sensor.get("rgb_payload_path"),
            f"staging frame {index}.rgb_payload",
        )
        if _sha256(rgb_path) != rgb_hash:
            raise AnnotationStagingBindError(
                f"staging frame {index} RGB payload hash does not match the persisted file"
            )
        depth_hash = _hash(
            sensor.get("depth_payload_sha256"), f"staging frame {index}.depth_payload_sha256"
        )
        depth_path = _resolve_file(
            staging_manifest_path,
            sensor.get("depth_payload_path"),
            f"staging frame {index}.depth_payload",
        )
        if _sha256(depth_path) != depth_hash:
            raise AnnotationStagingBindError(
                f"staging frame {index} depth payload hash does not match the persisted file"
            )
        labels_path = segmentation.get("payload_path")
        labels_file = _resolve_file(
            observation_path,
            labels_path,
            f"annotation frame {index}.labels_map",
        )
        labels_payload = labels_file.read_bytes()
        if _sha256(labels_file) != payload_hash:
            raise AnnotationStagingBindError(
                f"annotation frame {index} labels-map hash does not match the persisted file"
            )
        payload_bbox, payload_pixels, payload_instance_ids = _segmentation_payload_geometry(
            segmentation, labels_payload
        )
        if payload_bbox != visible_bbox:
            raise AnnotationStagingBindError(
                f"annotation frame {index} labels-map visible bbox does not match payload"
            )
        if payload_pixels != segmentation.get("visible_pixels"):
            raise AnnotationStagingBindError(
                f"annotation frame {index} labels-map visible pixel count does not match payload"
            )
        if payload_instance_ids != segmentation.get("instance_ids"):
            raise AnnotationStagingBindError(
                f"annotation frame {index} labels-map instance ids do not match payload"
            )
        center = target["center_xyz_m"]
        bound_frames.append(
            {
                "sequence": sequence,
                "frame_id": _nonempty(sensor.get("frame_id"), f"staging frame {index}.frame_id"),
                "rgb_payload_sha256": rgb_hash,
                "sensor_stamp_ns": stamp,
                "sensor_time_s": _finite(
                    sensor.get("sensor_time_s"), f"staging frame {index}.sensor_time_s"
                ),
                "captured_at_clock": "gazebo_sensor",
                "annotation_sensor_frame_id": _nonempty(
                    observation.get("annotation_sensor_frame_id"),
                    f"annotation frame {index}.annotation_sensor_frame_id",
                ),
                "bounding_box_3d": {
                    **target,
                    "normalized_bbox": visible_bbox,
                    "visible_bbox_normalized": visible_bbox,
                    "range_3d_m": list(center),
                },
                "instance_segmentation": {
                    "topic": "multidetect/annotation/instance_segmentation",
                    "labels_map_topic": segmentation.get("labels_map_topic")
                    or observations.get("labels_map_topic"),
                    "label_id": 42,
                    "payload_sha256": payload_hash,
                    "payload_path": segmentation.get("payload_path"),
                    "visible_bbox_normalized": visible_bbox,
                    "visible_pixels": _nonnegative_integer(
                        segmentation.get("visible_pixels"),
                        f"annotation frame {index}.visible_pixels",
                    ),
                    "instance_ids": segmentation.get("instance_ids"),
                },
            }
        )
    if seen_stamps != set(by_stamp):
        raise AnnotationStagingBindError("annotation stream contains an unbound timestamp")
    result: dict[str, Any] = {
        "schema": "multidetect.gazebo-3d-annotation-staging-frame-set",
        "schema_version": 2,
        "contract": "multidetect.gazebo-3d-annotation-staging-frame-set-v2",
        "source_attribution": {
            "annotation": "actual_gazebo_capture_only_annotation_output",
            "capture_only": True,
            "truth_loaded_by_runtime": False,
            "runtime_consumes_annotation_topics": False,
            "coordinate_frame": "rendering_view_x_right_y_up_z_backward",
        },
        "sensor_staging": {
            "path": staging_manifest_path.as_posix(),
            "sha256": _sha256(staging_manifest_path),
        },
        "rgb_frame_set": {
            "path": staging_manifest_path.as_posix(),
            "manifest_sha256": _sha256(staging_manifest_path),
            "scenario_id": annotation_config["scenario_id"],
            "kind": "offline_sensor_staging_manifest",
        },
        "annotation_config": {
            "path": annotation_config["path"],
            "sha256": annotation_config["sha256"],
        },
        "wrapper_binding": annotation_config["wrapper_binding"],
        "scenario_id": annotation_config["scenario_id"],
        "annotation_observations": {
            "path": observation_path.as_posix(),
            "sha256": _sha256(observation_path),
        },
        "join_contract": ["sensor_stamp_ns", "rgb_payload_sha256"],
        "topic_contract": {
            "bounding_box_3d": "multidetect/annotation/bounding_boxes_3d",
            "instance_segmentation": "multidetect/annotation/instance_segmentation",
            "labels_map": observations.get("labels_map_topic"),
        },
        "label_contract": {"label_id": 42, "class_name": "search_target"},
        "runtime_boundary": {
            "offline_supervision_only": True,
            "runtime_consumes_annotation_topics": False,
            "truth_injected_into_policy": False,
            "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
            "training_requires_independent_review": True,
        },
        "frames": bound_frames,
        "training_ready": False,
        "reviewed_annotation_manifest": None,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as stream:
        stream.write(
            (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-manifest", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = bind(
            staging_manifest_path=args.staging_manifest,
            observation_path=args.observations,
            output_path=args.output,
        )
    except (OSError, AnnotationStagingBindError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

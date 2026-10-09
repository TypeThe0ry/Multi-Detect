"""Validate and bind capture-only Gazebo 3-D annotation frame sets.

The annotation topic is an offline supervision source.  This adapter has one
deliberately narrow job: prove that an annotation record belongs to the exact
RGB frame that produced it.  ``sequence``, ``frame_id`` and the immutable RGB
payload SHA-256 are a single identity; timestamps are never used to find a
nearby frame.  The adapter does not construct policy features or read scenario
truth.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "scenarios" / "gazebo-3d-annotation-capture-v1.json"

SCHEMA = "multidetect.gazebo-3d-annotation-frame-set"
CONTRACT = "multidetect.gazebo-3d-annotation-frame-set-v1"
BOUNDING_BOX_TOPIC = "multidetect/annotation/bounding_boxes_3d"
SEGMENTATION_TOPIC = "multidetect/annotation/instance_segmentation"
LABEL_ID = 42

_SHA256_ZERO = set("0123456789abcdef")
_FORBIDDEN_KEYS = frozenset(
    {
        "truth",
        "ground_truth",
        "scenario_truth",
        "target_world_position",
        "target_model_id",
        "nearest_object_truth",
        "runtime_annotation_input",
        "runtime_policy_input",
        "policy_input",
        "policy_features",
        "context_snapshot",
        "detector_input",
    }
)


class AnnotationFrameSetError(ValueError):
    """The annotation frame set is not safe to bind as offline supervision."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hash(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in _SHA256_ZERO for character in value)
    ):
        raise AnnotationFrameSetError(f"{label} must be a lowercase SHA-256")
    return value


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnnotationFrameSetError(f"{label} must be an object")
    return value


def _finite(value: object, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnnotationFrameSetError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise AnnotationFrameSetError(f"{label} must be a finite number >= {minimum}")
    return result


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnotationFrameSetError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnnotationFrameSetError(f"{label} must be an integer >= 0")
    return value


def _scan_forbidden(value: object, path: str = "annotation") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_KEYS:
                raise AnnotationFrameSetError(f"{path}.{key} is a runtime/truth input")
            _scan_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_forbidden(child, f"{path}[{index}]")


def _resolve_reference(path: Path, raw: object, label: str, *, allow_repository: bool) -> Path:
    value = _nonempty(raw, f"{label} path")
    candidates = [Path(value)] if Path(value).is_absolute() else [path.parent / value]
    if allow_repository and not Path(value).is_absolute():
        candidates.append(ROOT / value)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise AnnotationFrameSetError(f"{label} file is missing: {value}")


def _validate_annotation_config(
    frame_set_path: Path,
    config_binding: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if set(config_binding) != {"path", "sha256"}:
        raise AnnotationFrameSetError("annotation_config binding has unknown or missing fields")
    config_path = _resolve_reference(
        frame_set_path,
        config_binding.get("path"),
        "annotation config",
        allow_repository=True,
    )
    expected_hash = _require_hash(config_binding.get("sha256"), "annotation config hash")
    observed_hash = _sha256(config_path)
    if observed_hash != expected_hash:
        raise AnnotationFrameSetError("annotation config hash mismatch")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationFrameSetError("annotation config is not valid JSON") from exc
    config = _object(config, "annotation config")
    if (
        config.get("schema") != "multidetect.gazebo-3d-annotation-capture"
        or config.get("schema_version") != 1
        or config.get("contract") != "multidetect.gazebo-3d-annotation-capture-v1"
        or config.get("purpose") != "offline_3d_annotation_only"
    ):
        raise AnnotationFrameSetError("annotation config contract is unsupported")
    boundary = _object(config.get("runtime_boundary"), "annotation config runtime_boundary")
    if boundary != {
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
        "training_requires_independent_review": True,
    }:
        raise AnnotationFrameSetError("annotation config runtime boundary is invalid")
    sensors = _object(config.get("sensors"), "annotation config sensors")
    bbox_sensor = _object(sensors.get("bounding_box"), "annotation config bounding_box")
    segmentation_sensor = _object(sensors.get("segmentation"), "annotation config segmentation")
    if (
        bbox_sensor.get("topic") != BOUNDING_BOX_TOPIC
        or bbox_sensor.get("type") != "boundingbox_camera"
        or segmentation_sensor.get("topic") != SEGMENTATION_TOPIC
        or segmentation_sensor.get("type") != "segmentation"
    ):
        raise AnnotationFrameSetError("annotation config topic contract is invalid")
    label_mapping = _object(config.get("label_mapping"), "annotation config label_mapping")
    if (
        label_mapping.get("label_id") != LABEL_ID
        or label_mapping.get("class_name") != "search_target"
    ):
        raise AnnotationFrameSetError("annotation config label_id contract is invalid")
    wrappers = {
        "vehicle_wrapper_sha256": (
            _object(config.get("vehicle_wrapper"), "vehicle wrapper"),
            "vehicle_wrapper",
        ),
        "target_wrapper_sha256": (
            _object(config.get("target_wrapper"), "target wrapper"),
            "target_wrapper",
        ),
    }
    for field, (wrapper, wrapper_name) in wrappers.items():
        wrapper_hash = _require_hash(
            wrapper.get("sha256"), f"annotation config {wrapper_name} hash"
        )
        wrapper_path = _resolve_reference(
            config_path,
            wrapper.get("path"),
            f"annotation config {wrapper_name}",
            allow_repository=True,
        )
        if _sha256(wrapper_path) != wrapper_hash:
            raise AnnotationFrameSetError(f"annotation config {wrapper_name} hash mismatch")
        # The frame-set's binding is compared by the caller.  Preserve the
        # normalized value here to avoid trusting an annotation-side copy.
        wrappers[field] = (wrapper_hash, wrapper_name)
    normalized = dict(config)
    normalized["_resolved_path"] = config_path
    normalized["_wrapper_hashes"] = {field: values[0] for field, values in wrappers.items()}
    return config_path, normalized


def _validate_frame(raw: object, index: int) -> tuple[tuple[int, str, str], dict[str, Any]]:
    frame = _object(raw, f"annotation frame {index}")
    required = {
        "sequence",
        "frame_id",
        "rgb_payload_sha256",
        "captured_at_s",
        "bounding_box_3d",
        "instance_segmentation",
    }
    if set(frame) != required:
        unknown = sorted(set(frame) - required)
        missing = sorted(required - set(frame))
        detail = f"unknown={unknown}" if unknown else f"missing={missing}"
        raise AnnotationFrameSetError(
            f"annotation frame {index} fields are not canonical ({detail})"
        )
    sequence = _integer(frame.get("sequence"), f"annotation frame {index}.sequence")
    frame_id = _nonempty(frame.get("frame_id"), f"annotation frame {index}.frame_id")
    rgb_hash = _require_hash(
        frame.get("rgb_payload_sha256"), f"annotation frame {index}.rgb_payload_sha256"
    )
    _finite(frame.get("captured_at_s"), f"annotation frame {index}.captured_at_s", minimum=0.0)
    bbox = _object(frame.get("bounding_box_3d"), f"annotation frame {index}.bounding_box_3d")
    if set(bbox) != {"topic", "label_id", "normalized_bbox", "range_3d_m"}:
        raise AnnotationFrameSetError(f"annotation frame {index} bounding-box fields are invalid")
    if bbox.get("topic") != BOUNDING_BOX_TOPIC or bbox.get("label_id") != LABEL_ID:
        raise AnnotationFrameSetError(
            f"annotation frame {index} bounding-box topic/label is invalid"
        )
    normalized = bbox.get("normalized_bbox")
    if (
        not isinstance(normalized, list)
        or len(normalized) != 4
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in normalized
        )
    ):
        raise AnnotationFrameSetError(f"annotation frame {index} normalized bbox is invalid")
    x1, y1, x2, y2 = (float(value) for value in normalized)
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (x1, y1, x2, y2)):
        raise AnnotationFrameSetError(f"annotation frame {index} normalized bbox is out of range")
    if not x1 < x2 or not y1 < y2:
        raise AnnotationFrameSetError(f"annotation frame {index} normalized bbox is degenerate")
    range_3d = bbox.get("range_3d_m")
    if (
        not isinstance(range_3d, list)
        or len(range_3d) != 3
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in range_3d
        )
    ):
        raise AnnotationFrameSetError(f"annotation frame {index} 3-D range is invalid")
    vector = tuple(float(value) for value in range_3d)
    if (
        not all(math.isfinite(value) for value in vector)
        or math.sqrt(sum(value * value for value in vector)) <= 0.0
    ):
        raise AnnotationFrameSetError(f"annotation frame {index} 3-D range is invalid")
    segmentation = _object(
        frame.get("instance_segmentation"), f"annotation frame {index}.instance_segmentation"
    )
    if set(segmentation) != {
        "topic",
        "label_id",
        "labels_map_topic",
        "payload_sha256",
        "visible_bbox_normalized",
        "visible_pixels",
    }:
        raise AnnotationFrameSetError(f"annotation frame {index} segmentation fields are invalid")
    if segmentation.get("topic") != SEGMENTATION_TOPIC or segmentation.get("label_id") != LABEL_ID:
        raise AnnotationFrameSetError(
            f"annotation frame {index} segmentation topic/label is invalid"
        )
    if segmentation.get("labels_map_topic") != f"{SEGMENTATION_TOPIC}/labels_map":
        raise AnnotationFrameSetError(f"annotation frame {index} labels-map topic is invalid")
    payload_hash = _require_hash(
        segmentation.get("payload_sha256"),
        f"annotation frame {index} labels-map payload hash",
    )
    visible_bbox = segmentation.get("visible_bbox_normalized")
    if (
        not isinstance(visible_bbox, list)
        or len(visible_bbox) != 4
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in visible_bbox
        )
    ):
        raise AnnotationFrameSetError(f"annotation frame {index} visible bbox is invalid")
    visible_values = tuple(float(value) for value in visible_bbox)
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in visible_values):
        raise AnnotationFrameSetError(f"annotation frame {index} visible bbox is out of range")
    if not visible_values[0] < visible_values[2] or not visible_values[1] < visible_values[3]:
        raise AnnotationFrameSetError(f"annotation frame {index} visible bbox is degenerate")
    visible_pixels = segmentation.get("visible_pixels")
    if (
        isinstance(visible_pixels, bool)
        or not isinstance(visible_pixels, int)
        or visible_pixels < 1
    ):
        raise AnnotationFrameSetError(f"annotation frame {index} visible pixel count is invalid")
    normalized_frame = dict(frame)
    normalized_frame["bounding_box_3d"] = {
        "topic": BOUNDING_BOX_TOPIC,
        "label_id": LABEL_ID,
        "normalized_bbox": [x1, y1, x2, y2],
        "range_3d_m": list(vector),
    }
    normalized_frame["instance_segmentation"] = {
        "topic": SEGMENTATION_TOPIC,
        "label_id": LABEL_ID,
        "labels_map_topic": f"{SEGMENTATION_TOPIC}/labels_map",
        "payload_sha256": payload_hash,
        "visible_bbox_normalized": list(visible_values),
        "visible_pixels": visible_pixels,
    }
    return (sequence, frame_id, rgb_hash), normalized_frame


def _canonicalize_staging_binding(
    document: dict[str, Any],
    *,
    binding_path: Path,
    rgb_manifest: Path,
    annotation_config: Path,
) -> dict[str, Any]:
    """Convert the v2 timestamp-joined binder output to the strict frame set.

    The old binder joins its two input streams by sensor timestamp.  That is
    acceptable as an intermediate check only; this conversion requires the
    observation to carry the same ``frame_id`` as staging and then re-binds
    every row to the RGB manifest's immutable payload hash.  No nearest-time
    lookup is performed here.
    """

    if (
        document.get("schema") != "multidetect.gazebo-3d-annotation-staging-frame-set"
        or document.get("schema_version") != 2
        or document.get("contract") != "multidetect.gazebo-3d-annotation-staging-frame-set-v2"
        or document.get("training_ready") is not False
    ):
        raise AnnotationFrameSetError("unsupported annotation staging frame set")
    source = _object(document.get("source_attribution"), "staging source_attribution")
    if (
        source.get("annotation") != "actual_gazebo_capture_only_annotation_output"
        or source.get("capture_only") is not True
        or source.get("truth_loaded_by_runtime") is not False
        or source.get("runtime_consumes_annotation_topics") is not False
    ):
        raise AnnotationFrameSetError("staging annotation source attribution is invalid")
    boundary = _object(document.get("runtime_boundary"), "staging runtime_boundary")
    if (
        boundary.get("offline_supervision_only") is not True
        or boundary.get("runtime_consumes_annotation_topics") is not False
        or boundary.get("truth_injected_into_policy") is not False
    ):
        raise AnnotationFrameSetError("staging annotation runtime boundary is invalid")

    def _bound_file(field: str, label: str) -> tuple[Path, str]:
        binding = _object(document.get(field), f"staging {field}")
        if set(binding) != {"path", "sha256"}:
            raise AnnotationFrameSetError(f"staging {field} binding is incomplete")
        candidate = _resolve_reference(
            binding_path, binding.get("path"), label, allow_repository=False
        )
        expected = _require_hash(binding.get("sha256"), f"staging {field} hash")
        if _sha256(candidate) != expected:
            raise AnnotationFrameSetError(f"staging {field} hash mismatch")
        return candidate, expected

    staging_path, staging_hash = _bound_file("sensor_staging", "sensor staging")
    observations_path, observations_hash = _bound_file(
        "annotation_observations", "annotation observations"
    )
    try:
        staging = json.loads(staging_path.read_text(encoding="utf-8"))
        observations = json.loads(observations_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationFrameSetError("staging source binding is not valid JSON") from exc
    staging = _object(staging, "sensor staging")
    observations = _object(observations, "annotation observations")
    _scan_forbidden(observations, "annotation observations")
    if staging.get("schema") != "multidetect.gazebo-annotation-sensor-staging":
        raise AnnotationFrameSetError("sensor staging schema is unsupported")
    if staging.get("training_ready") is not False:
        raise AnnotationFrameSetError("sensor staging crosses training boundary")
    if (
        observations.get("schema") != "multidetect.gazebo-3d-annotation-observation-stream"
        or observations.get("schema_version") != 2
        or observations.get("capture_only") is not True
        or observations.get("runtime_consumes_annotation_topics") is not False
    ):
        raise AnnotationFrameSetError("annotation observations are not capture-only v2")
    labels_map_topic = observations.get("labels_map_topic")
    accepted_labels_topics = {
        f"{SEGMENTATION_TOPIC}/labels_map",
        f"/{SEGMENTATION_TOPIC}/labels_map",
    }
    if labels_map_topic not in accepted_labels_topics:
        raise AnnotationFrameSetError("labels-map topic contract is invalid")
    staging_frames = staging.get("frames")
    observation_frames = observations.get("frames")
    if not isinstance(staging_frames, list) or not staging_frames:
        raise AnnotationFrameSetError("sensor staging has no frames")
    if not isinstance(observation_frames, list) or len(observation_frames) != len(staging_frames):
        raise AnnotationFrameSetError("staging/annotation frame counts do not match")
    by_stamp: dict[int, dict[str, Any]] = {}
    for index, raw in enumerate(observation_frames):
        row = _object(raw, f"annotation observation {index}")
        stamp = _integer(
            row.get("sensor_stamp_ns"), f"annotation observation {index}.sensor_stamp_ns"
        )
        if stamp in by_stamp:
            raise AnnotationFrameSetError("duplicate annotation sensor timestamp")
        by_stamp[stamp] = row

    canonical_frames: list[dict[str, Any]] = []
    seen_keys: set[tuple[int, str, str]] = set()
    for index, raw in enumerate(staging_frames):
        sensor = _object(raw, f"sensor staging frame {index}")
        sequence = _integer(sensor.get("sequence"), f"sensor staging frame {index}.sequence")
        frame_id = _nonempty(sensor.get("frame_id"), f"sensor staging frame {index}.frame_id")
        stamp = _integer(
            sensor.get("sensor_stamp_ns"), f"sensor staging frame {index}.sensor_stamp_ns"
        )
        observation = by_stamp.get(stamp)
        if observation is None:
            raise AnnotationFrameSetError("missing exact annotation timestamp")
        observed_sequence = _integer(
            observation.get("sequence"), f"annotation observation {index}.sequence"
        )
        if observed_sequence != sequence:
            raise AnnotationFrameSetError("annotation sequence mismatch")
        if observation.get("frame_id") != frame_id:
            raise AnnotationFrameSetError("annotation frame_id mismatch")
        if observation.get("runtime_consumes_annotation_topics") is not False:
            raise AnnotationFrameSetError("annotation observation crosses runtime boundary")
        rgb_hash = _require_hash(
            sensor.get("rgb_payload_sha256"), f"sensor staging frame {index}.rgb_payload_sha256"
        )
        boxes = observation.get("bounding_boxes_3d")
        if not isinstance(boxes, list):
            raise AnnotationFrameSetError("bounding_boxes_3d must be a list")
        targets = [
            item for item in boxes if isinstance(item, dict) and item.get("label_id") == LABEL_ID
        ]
        if len(targets) != 1:
            raise AnnotationFrameSetError("expected exactly one label-42 3-D target")
        target = targets[0]
        center = target.get("center_xyz_m")
        size = target.get("size_xyz_m")
        orientation = target.get("orientation_xyzw")
        if (
            not isinstance(center, list)
            or len(center) != 3
            or not isinstance(size, list)
            or len(size) != 3
            or not isinstance(orientation, list)
            or len(orientation) != 4
        ):
            raise AnnotationFrameSetError("3-D annotation geometry is invalid")
        center_values = tuple(_finite(value, "annotation center") for value in center)
        if any(_finite(value, "annotation size") <= 0.0 for value in size):
            raise AnnotationFrameSetError("3-D annotation size must be positive")
        for value in orientation:
            _finite(value, "annotation orientation")
        if (
            center_values[2] >= 0.0
            or math.sqrt(sum(value * value for value in center_values)) <= 0.0
        ):
            raise AnnotationFrameSetError("3-D annotation target is behind or at camera")
        segmentation = _object(
            observation.get("instance_segmentation"),
            f"annotation observation {index}.instance_segmentation",
        )
        if segmentation.get("sensor_stamp_ns") != stamp:
            raise AnnotationFrameSetError("labels-map timestamp does not match annotation frame")
        payload_hash = _require_hash(
            segmentation.get("payload_sha256"), f"annotation observation {index} labels-map hash"
        )
        payload_path = segmentation.get("payload_path")
        if not isinstance(payload_path, str) or not payload_path.strip():
            raise AnnotationFrameSetError("labels-map payload path is missing")
        payload = _resolve_reference(
            observations_path, payload_path, "labels-map payload", allow_repository=False
        )
        if _sha256(payload) != payload_hash:
            raise AnnotationFrameSetError("labels-map payload hash mismatch")
        visible = segmentation.get("visible_bbox_normalized")
        if (
            not isinstance(visible, list)
            or len(visible) != 4
            or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in visible
            )
        ):
            raise AnnotationFrameSetError("visible annotation bbox is invalid")
        visible_values = tuple(float(value) for value in visible)
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in visible_values):
            raise AnnotationFrameSetError("visible annotation bbox is out of range")
        if not visible_values[0] < visible_values[2] or not visible_values[1] < visible_values[3]:
            raise AnnotationFrameSetError("visible annotation bbox is degenerate")
        visible_pixels = segmentation.get("visible_pixels")
        if (
            isinstance(visible_pixels, bool)
            or not isinstance(visible_pixels, int)
            or visible_pixels < 1
        ):
            raise AnnotationFrameSetError("visible annotation pixel count is invalid")
        key = (sequence, frame_id, rgb_hash)
        if key in seen_keys:
            raise AnnotationFrameSetError("duplicate annotation sequence/frame/RGB identity")
        seen_keys.add(key)
        canonical_frames.append(
            {
                "sequence": sequence,
                "frame_id": frame_id,
                "rgb_payload_sha256": rgb_hash,
                "captured_at_s": _finite(
                    sensor.get("sensor_time_s"), "sensor staging sensor_time_s", minimum=0.0
                ),
                "bounding_box_3d": {
                    "topic": BOUNDING_BOX_TOPIC,
                    "label_id": LABEL_ID,
                    "normalized_bbox": list(visible_values),
                    "range_3d_m": list(center_values),
                },
                "instance_segmentation": {
                    "topic": SEGMENTATION_TOPIC,
                    "label_id": LABEL_ID,
                    "labels_map_topic": f"{SEGMENTATION_TOPIC}/labels_map",
                    "payload_sha256": payload_hash,
                    "visible_bbox_normalized": list(visible_values),
                    "visible_pixels": visible_pixels,
                },
            }
        )
    if set(by_stamp) != {
        _integer(
            _object(raw, "staging frame").get("sensor_stamp_ns"), "sensor staging sensor_stamp_ns"
        )
        for raw in staging_frames
    }:
        raise AnnotationFrameSetError("annotation stream contains an unbound timestamp")

    raw_config_binding = document.get("annotation_config")
    if isinstance(raw_config_binding, dict):
        config_binding = {
            "path": raw_config_binding.get("path"),
            "sha256": raw_config_binding.get("sha256"),
        }
    else:
        config_binding = {
            "path": annotation_config.as_posix(),
            "sha256": _sha256(annotation_config),
        }
    config_path, config = _validate_annotation_config(binding_path, config_binding)
    if annotation_config != DEFAULT_CONFIG and annotation_config != config_path:
        raise AnnotationFrameSetError("annotation config override does not match staging binding")
    rgb_document = _object(json.loads(rgb_manifest.read_text(encoding="utf-8")), "RGB frame set")
    scenario_id = _nonempty(rgb_document.get("scenario_id"), "RGB scenario_id")
    scenario_hash = _require_hash(rgb_document.get("scenario_manifest_hash"), "RGB scenario hash")
    bound_scenario_id = document.get("scenario_id")
    if bound_scenario_id is not None and bound_scenario_id != scenario_id:
        raise AnnotationFrameSetError("staging scenario binding mismatch")
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "contract": CONTRACT,
        "source_attribution": {
            "annotation": "actual_gazebo_capture_only_annotation_output",
            "capture_only": True,
            "truth_loaded_by_runtime": False,
            "runtime_consumes_annotation_topics": False,
        },
        "rgb_frame_set": {
            "path": rgb_manifest.as_posix(),
            "manifest_sha256": _sha256(rgb_manifest),
            "scenario_id": scenario_id,
            "scenario_manifest_hash": scenario_hash,
        },
        "annotation_config": {"path": config_path.as_posix(), "sha256": config_binding["sha256"]},
        "wrapper_binding": config["_wrapper_hashes"],
        "topic_contract": {
            "bounding_box_3d": BOUNDING_BOX_TOPIC,
            "instance_segmentation": SEGMENTATION_TOPIC,
        },
        "label_contract": {"label_id": LABEL_ID, "class_name": "search_target"},
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


def load_annotation_frame_set(
    path: Path,
    *,
    rgb_manifest: Path,
    annotation_config: Path | None = None,
) -> dict[tuple[int, str, str], dict[str, Any]]:
    """Load one annotation set and bind it exactly to a persisted RGB set.

    The returned dictionary is keyed by ``(sequence, frame_id,
    rgb_payload_sha256)``.  No timestamp-nearest fallback exists by design.
    """

    path = path.expanduser().resolve()
    rgb_manifest = rgb_manifest.expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationFrameSetError(f"annotation frame set is not valid JSON: {path}") from exc
    document = _object(document, "annotation frame set")
    _scan_forbidden(document)
    if document.get("schema") == "multidetect.gazebo-3d-annotation-staging-frame-set":
        config_path = (annotation_config or DEFAULT_CONFIG).expanduser().resolve(strict=True)
        document = _canonicalize_staging_binding(
            document,
            binding_path=path,
            rgb_manifest=rgb_manifest,
            annotation_config=config_path,
        )
    required = {
        "schema",
        "schema_version",
        "contract",
        "source_attribution",
        "rgb_frame_set",
        "annotation_config",
        "wrapper_binding",
        "topic_contract",
        "label_contract",
        "runtime_boundary",
        "frames",
        "training_ready",
        "reviewed_annotation_manifest",
    }
    if set(document) != required:
        raise AnnotationFrameSetError("annotation frame set fields are not canonical")
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != 1
        or document.get("contract") != CONTRACT
    ):
        raise AnnotationFrameSetError("annotation frame set contract is unsupported")
    source = _object(document.get("source_attribution"), "source_attribution")
    if source != {
        "annotation": "actual_gazebo_capture_only_annotation_output",
        "capture_only": True,
        "truth_loaded_by_runtime": False,
        "runtime_consumes_annotation_topics": False,
    }:
        raise AnnotationFrameSetError("annotation source attribution is invalid")
    boundary = _object(document.get("runtime_boundary"), "runtime_boundary")
    if boundary != {
        "offline_supervision_only": True,
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
        "training_requires_independent_review": True,
    }:
        raise AnnotationFrameSetError("annotation runtime boundary is invalid")
    if (
        document.get("training_ready") is not False
        or document.get("reviewed_annotation_manifest") is not None
    ):
        raise AnnotationFrameSetError(
            "annotation frame set cannot be training-ready without review"
        )
    topics = _object(document.get("topic_contract"), "topic_contract")
    if topics != {
        "bounding_box_3d": BOUNDING_BOX_TOPIC,
        "instance_segmentation": SEGMENTATION_TOPIC,
    }:
        raise AnnotationFrameSetError("annotation topic contract is invalid")
    labels = _object(document.get("label_contract"), "label_contract")
    if labels != {"label_id": LABEL_ID, "class_name": "search_target"}:
        raise AnnotationFrameSetError("annotation label contract is invalid")
    config_binding = _object(document.get("annotation_config"), "annotation_config")
    _config_path, config = _validate_annotation_config(path, config_binding)
    wrappers = _object(document.get("wrapper_binding"), "wrapper_binding")
    if wrappers != config["_wrapper_hashes"]:
        raise AnnotationFrameSetError("annotation wrapper hash binding mismatch")

    rgb_binding = _object(document.get("rgb_frame_set"), "rgb_frame_set")
    if (
        _resolve_reference(path, rgb_binding.get("path"), "RGB frame set", allow_repository=True)
        != rgb_manifest
    ):
        raise AnnotationFrameSetError("annotation RGB frame-set path mismatch")
    if _sha256(rgb_manifest) != _require_hash(
        rgb_binding.get("manifest_sha256"), "RGB frame-set manifest hash"
    ):
        raise AnnotationFrameSetError("annotation RGB frame-set manifest hash mismatch")
    try:
        rgb_document = json.loads(rgb_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationFrameSetError("RGB frame set is not valid JSON") from exc
    rgb_document = _object(rgb_document, "RGB frame set")
    if rgb_document.get("schema") != "multidetect.gazebo-bgr24-frame-set":
        raise AnnotationFrameSetError("RGB frame set schema is unsupported")
    if rgb_binding.get("scenario_id") != rgb_document.get("scenario_id"):
        raise AnnotationFrameSetError("annotation scenario binding mismatch")
    rgb_scenario_hash = _require_hash(
        rgb_document.get("scenario_manifest_hash"), "RGB scenario manifest hash"
    )
    if rgb_binding.get("scenario_manifest_hash") != rgb_scenario_hash:
        raise AnnotationFrameSetError("annotation scenario manifest hash mismatch")
    rgb_frames = rgb_document.get("frames")
    if not isinstance(rgb_frames, list) or not rgb_frames:
        raise AnnotationFrameSetError("RGB frame set has no frames")
    rgb_index: dict[tuple[int, str, str], dict[str, Any]] = {}
    for index, raw in enumerate(rgb_frames):
        frame = _object(raw, f"RGB frame {index}")
        key = (
            _integer(frame.get("sequence"), f"RGB frame {index}.sequence"),
            _nonempty(frame.get("frame_id"), f"RGB frame {index}.frame_id"),
            _require_hash(frame.get("sha256"), f"RGB frame {index}.sha256"),
        )
        if key in rgb_index:
            raise AnnotationFrameSetError("RGB frame identities are not unique")
        rgb_file = _resolve_reference(
            rgb_manifest,
            frame.get("file"),
            f"RGB frame {index} payload",
            allow_repository=False,
        )
        if _sha256(rgb_file) != key[2]:
            raise AnnotationFrameSetError(f"RGB frame {index} payload hash mismatch")
        rgb_index[key] = frame
    annotation_frames = document.get("frames")
    if not isinstance(annotation_frames, list) or not annotation_frames:
        raise AnnotationFrameSetError("annotation frame set has no frames")
    result: dict[tuple[int, str, str], dict[str, Any]] = {}
    for index, raw in enumerate(annotation_frames):
        key, frame = _validate_frame(raw, index)
        if key in result:
            raise AnnotationFrameSetError("annotation frame identities are not unique")
        if key not in rgb_index:
            raise AnnotationFrameSetError(
                f"annotation frame {index} does not exactly bind an RGB sequence/frame/hash"
            )
        rgb_frame = rgb_index[key]
        rgb_time = _object(rgb_frame.get("provenance"), f"RGB frame {index}.provenance").get(
            "captured_at_s"
        )
        if isinstance(rgb_time, (int, float)) and float(frame["captured_at_s"]) != float(rgb_time):
            raise AnnotationFrameSetError(f"annotation frame {index} captured_at_s mismatch")
        result[key] = frame
    if set(result) != set(rgb_index):
        raise AnnotationFrameSetError("annotation frame set does not cover exactly every RGB frame")
    return result


# Private alias used by the rendered-data builder to keep its adapter seam
# explicit without making callers depend on the implementation name.
_load_annotation_frame_set = load_annotation_frame_set


__all__ = [
    "ANNOTATION_CONFIG",
    "AnnotationFrameSetError",
    "BOUNDING_BOX_TOPIC",
    "CONTRACT",
    "LABEL_ID",
    "SEGMENTATION_TOPIC",
    "load_annotation_frame_set",
]

# A stable public spelling for code that wants to display the default config.
ANNOTATION_CONFIG = DEFAULT_CONFIG

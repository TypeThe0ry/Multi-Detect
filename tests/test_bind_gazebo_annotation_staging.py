from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.bind_gazebo_annotation_staging import (
    AnnotationStagingBindError,
    bind,
)
from scripts.gazebo_3d_annotation_frame_set import load_annotation_frame_set


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _documents(tmp_path: Path) -> tuple[Path, Path]:
    rgb_payload = b"rgb-frame"
    rgb_hash = hashlib.sha256(rgb_payload).hexdigest()
    rgb_path = tmp_path / "rgb" / "000000.bgr24"
    rgb_path.parent.mkdir()
    rgb_path.write_bytes(rgb_payload)
    depth_payload = b"depth-frame"
    depth_hash = hashlib.sha256(depth_payload).hexdigest()
    depth_path = tmp_path / "depth" / "000000.f32"
    depth_path.parent.mkdir()
    depth_path.write_bytes(depth_payload)
    config_path = (
        Path(__file__).resolve().parents[1]
        / "configs/scenarios/gazebo-3d-annotation-capture-v1.json"
    )
    staging = {
        "schema": "multidetect.gazebo-annotation-sensor-staging",
        "schema_version": 1,
        "training_ready": False,
        "contract": "multidetect.gazebo-annotation-sensor-staging-v1",
        "runtime_boundary": {
            "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
            "runtime_consumes_annotation_topics": False,
            "truth_injected_into_policy": False,
            "offline_staging_only": True,
        },
        "annotation_config": {
            "path": config_path.as_posix(),
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        },
        "frames": [
            {
                "sequence": 0,
                "frame_id": "gazebo-sim-000000",
                "sensor_stamp_ns": 100,
                "sensor_time_s": 0.0,
                "rgb_payload_path": "rgb/000000.bgr24",
                "rgb_payload_sha256": rgb_hash,
                "depth_payload_path": "depth/000000.f32",
                "depth_payload_sha256": depth_hash,
            }
        ],
    }
    label_payload = bytearray(4 * 4 * 3)
    for x, y, instance_id in ((1, 1, 1), (2, 2, 2)):
        offset = (y * 4 + x) * 3
        label_payload[offset : offset + 3] = bytes((instance_id, 0, 42))
    label_payload = bytes(label_payload)
    (tmp_path / "labels-map.rgb8").write_bytes(label_payload)
    observations = {
        "schema": "multidetect.gazebo-3d-annotation-observation-stream",
        "schema_version": 2,
        "contract": "multidetect.gazebo-3d-annotation-observation-stream-v2",
        "capture_only": True,
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "join_key": ["sensor_stamp_ns", "rgb_payload_sha256"],
        "coordinate_frame": "rendering_view_x_right_y_up_z_backward",
        "labels_map_topic": "/multidetect/annotation/instance_segmentation/labels_map",
        "frames": [
            {
                "sequence": 0,
                "frame_id": "gazebo-sim-000000",
                "sensor_stamp_ns": 100,
                "annotation_sensor_frame_id": "bbox_camera",
                "runtime_consumes_annotation_topics": False,
                "bounding_box_topic": "multidetect/annotation/bounding_boxes_3d",
                "instance_segmentation_topic": "multidetect/annotation/instance_segmentation",
                "instance_segmentation_labels_map_topic": (
                    "/multidetect/annotation/instance_segmentation/labels_map"
                ),
                "bounding_boxes_3d": [
                    {
                        "label_id": 42,
                        "center_xyz_m": [0.0, 0.0, -10.0],
                        "size_xyz_m": [2.0, 1.0, 3.0],
                        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    }
                ],
                "instance_segmentation": {
                    "sensor_stamp_ns": 100,
                    "payload_sha256": hashlib.sha256(label_payload).hexdigest(),
                    "payload_path": "labels-map.rgb8",
                    "width": 4,
                    "height": 4,
                    "step": 12,
                    "pixel_format": 3,
                    "visible_bbox_normalized": [0.25, 0.25, 0.75, 0.75],
                    "visible_pixels": 2,
                    "instance_ids": [1, 2],
                },
            }
        ],
    }
    return (
        _write(tmp_path / "staging.json", staging),
        _write(tmp_path / "observations.json", observations),
    )


def test_bind_requires_exact_sensor_time_and_preserves_rgb_hash(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    output = tmp_path / "bound.json"
    result = bind(
        staging_manifest_path=staging,
        observation_path=observations,
        output_path=output,
    )
    assert result["schema_version"] == 2
    assert result["training_ready"] is False
    frame = result["frames"][0]
    assert frame["sensor_stamp_ns"] == 100
    assert frame["rgb_payload_sha256"] == hashlib.sha256(b"rgb-frame").hexdigest()
    assert frame["bounding_box_3d"]["normalized_bbox"] == [0.25, 0.25, 0.75, 0.75]
    assert frame["bounding_box_3d"]["range_3d_m"] == [0.0, 0.0, -10.0]
    assert frame["instance_segmentation"]["visible_bbox_normalized"] == [0.25, 0.25, 0.75, 0.75]
    assert (
        json.loads(output.read_text(encoding="utf-8"))["runtime_boundary"][
            "runtime_consumes_annotation_topics"
        ]
        is False
    )
    assert result["wrapper_binding"]["vehicle_wrapper_sha256"]
    assert result["scenario_id"] == "urban_inspection_multirotor_001"


def test_bind_rejects_persisted_rgb_hash_mismatch(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    document = json.loads(staging.read_text(encoding="utf-8"))
    document["frames"][0]["rgb_payload_sha256"] = "0" * 64
    staging.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationStagingBindError, match="RGB payload hash"):
        bind(
            staging_manifest_path=staging,
            observation_path=observations,
            output_path=tmp_path / "bound.json",
        )


def test_bind_rejects_annotation_frame_without_sensor_identity(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    document = json.loads(observations.read_text(encoding="utf-8"))
    document["frames"][0]["annotation_sensor_frame_id"] = ""
    observations.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationStagingBindError, match="annotation_sensor_frame_id"):
        bind(
            staging_manifest_path=staging,
            observation_path=observations,
            output_path=tmp_path / "bound.json",
        )


def test_bind_rejects_timestamp_nearest_fallback(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    document = json.loads(observations.read_text(encoding="utf-8"))
    document["frames"][0]["sensor_stamp_ns"] = 101
    observations.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationStagingBindError, match="missing exact annotation timestamp"):
        bind(
            staging_manifest_path=staging,
            observation_path=observations,
            output_path=tmp_path / "bound.json",
        )


def test_bind_rejects_frame_id_mismatch_even_when_timestamp_matches(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    document = json.loads(observations.read_text(encoding="utf-8"))
    document["frames"][0]["frame_id"] = "gazebo-sim-other"
    observations.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationStagingBindError, match="frame_id mismatch"):
        bind(
            staging_manifest_path=staging,
            observation_path=observations,
            output_path=tmp_path / "bound.json",
        )


def test_bind_rejects_behind_camera_target(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    document = json.loads(observations.read_text(encoding="utf-8"))
    document["frames"][0]["bounding_boxes_3d"][0]["center_xyz_m"][2] = 1.0
    observations.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationStagingBindError, match="behind"):
        bind(
            staging_manifest_path=staging,
            observation_path=observations,
            output_path=tmp_path / "bound.json",
        )


def test_bind_rejects_missing_visible_target(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    document = json.loads(observations.read_text(encoding="utf-8"))
    document["frames"][0]["instance_segmentation"]["visible_bbox_normalized"] = None
    observations.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationStagingBindError, match="four coordinates"):
        bind(
            staging_manifest_path=staging,
            observation_path=observations,
            output_path=tmp_path / "bound.json",
        )


def test_bound_staging_is_consumable_by_strict_annotation_adapter(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    bound_path = tmp_path / "bound.json"
    bind(
        staging_manifest_path=staging,
        observation_path=observations,
        output_path=bound_path,
    )
    rgb_path = tmp_path / "rgb-frame-set.json"
    rgb_payload = tmp_path / "rgb/000000.bgr24"
    rgb_hash = hashlib.sha256(rgb_payload.read_bytes()).hexdigest()
    rgb_path.write_text(
        json.dumps(
            {
                "schema": "multidetect.gazebo-bgr24-frame-set",
                "scenario_id": "urban_inspection_multirotor_001",
                "scenario_manifest_hash": "b" * 64,
                "frames": [
                    {
                        "sequence": 0,
                        "frame_id": "gazebo-sim-000000",
                        "file": "rgb/000000.bgr24",
                        "sha256": rgb_hash,
                        "provenance": {"captured_at_s": 0.0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bound = load_annotation_frame_set(bound_path, rgb_manifest=rgb_path)
    assert list(bound) == [(0, "gazebo-sim-000000", rgb_hash)]
    assert bound[(0, "gazebo-sim-000000", rgb_hash)]["bounding_box_3d"]["label_id"] == 42

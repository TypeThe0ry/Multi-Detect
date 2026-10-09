from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.backends.raw_replay import RawSensorReplayRecording
from scripts import export_gazebo_annotation_yolo_dataset as exporter

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/scenarios/gazebo-3d-annotation-capture-v1.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _capture(directory: Path, *, name: str, pixel_start: int = 0) -> tuple[Path, Path, Path]:
    """Synthetic PNG/wire metadata fixture, not a running Gazebo evidence claim."""
    directory.mkdir()
    rgb = np.arange(pixel_start, pixel_start + 12, dtype=np.uint8).reshape(2, 2, 3)
    raw = directory / "frame.bgr24"
    raw.write_bytes(rgb.tobytes())
    image = directory / "frame.png"
    assert cv2.imwrite(str(image), rgb)
    frame_id = f"{name}-frame"
    scenario_hash = "b" * 64
    manifest = directory / "rgb.json"
    _json(
        manifest,
        {
            "schema": "multidetect.gazebo-bgr24-frame-set",
            "scenario_id": "neutral_3d_fixture",
            "scenario_manifest_hash": scenario_hash,
            "frames": [
                {
                    "sequence": 0,
                    "frame_id": frame_id,
                    "file": raw.name,
                    "sha256": _sha(raw),
                    "provenance": {"width": 2, "height": 2, "captured_at_s": 1.0},
                }
            ],
        },
    )
    config = _read(CONFIG)
    annotations = directory / "annotations.json"
    _json(
        annotations,
        {
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
                "path": manifest.as_posix(),
                "manifest_sha256": _sha(manifest),
                "scenario_id": "neutral_3d_fixture",
                "scenario_manifest_hash": scenario_hash,
            },
            "annotation_config": {"path": CONFIG.as_posix(), "sha256": _sha(CONFIG)},
            "wrapper_binding": {
                "vehicle_wrapper_sha256": config["vehicle_wrapper"]["sha256"],
                "target_wrapper_sha256": config["target_wrapper"]["sha256"],
            },
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
            "frames": [
                {
                    "sequence": 0,
                    "frame_id": frame_id,
                    "rgb_payload_sha256": _sha(raw),
                    "captured_at_s": 1.0,
                    "bounding_box_3d": {
                        "topic": "multidetect/annotation/bounding_boxes_3d",
                        "label_id": 42,
                        "normalized_bbox": [0.0, 0.0, 1.0, 1.0],
                        "range_3d_m": [0.0, 1.0, -5.0],
                    },
                    "instance_segmentation": {
                        "topic": "multidetect/annotation/instance_segmentation",
                        "label_id": 42,
                        "labels_map_topic": (
                            "multidetect/annotation/instance_segmentation/labels_map"
                        ),
                        "payload_sha256": "f" * 64,
                        "visible_bbox_normalized": [0.25, 0.25, 0.75, 0.75],
                        "visible_pixels": 1,
                    },
                }
            ],
            "training_ready": False,
            "reviewed_annotation_manifest": None,
        },
    )
    events = directory / "events.jsonl"
    event_rows = [
        {
            "schema_version": 1,
            "kind": "telemetry",
            "observed_at_s": 1.0,
            "telemetry": {
                "altitude_agl_m": 10.0,
                "roll_deg": 0.0,
                "pitch_deg": 0.0,
                "ground_speed_mps": 0.0,
                "in_allowed_zone": True,
                "geofence_healthy": True,
                "position_healthy": True,
                "link_healthy": True,
                "flight_mode_allows_deploy": False,
                "release_zone_clear": None,
            },
        },
        {
            "schema_version": 1,
            "kind": "frame",
            "frame_id": frame_id,
            "captured_at_s": 1.0,
            "image": {
                "path": image.name,
                "sha256": _sha(image),
                "bytes": image.stat().st_size,
                "width": 2,
                "height": 2,
                "encoding": "png",
            },
        },
    ]
    events.write_text("".join(json.dumps(row) + "\n" for row in event_rows), encoding="utf-8")
    recording = RawSensorReplayRecording.from_jsonl(
        events,
        expected_sha256=_sha(events),
        expected_byte_count=events.stat().st_size,
        recording_id=name,
        airframe_profile_hash="a" * 64,
        started_at_utc="2026-10-07T00:00:00Z",
        scenario_id="neutral_3d_fixture",
        scenario_manifest_hash=scenario_hash,
        scenario_seed=0,
    )
    recording_path = directory / "recording.json"
    _json(
        recording_path,
        {
            "schema_version": 1,
            "recording_id": name,
            "airframe_profile_hash": "a" * 64,
            "started_at_utc": "2026-10-07T00:00:00Z",
            "recording_hash": recording.recording_hash,
            "scenario": {
                "scenario_id": "neutral_3d_fixture",
                "scenario_manifest_hash": scenario_hash,
                "scenario_seed": 0,
            },
            "events": {"path": events.name, "sha256": _sha(events), "bytes": events.stat().st_size},
        },
    )
    return manifest, annotations, recording_path


@pytest.fixture
def dataset(tmp_path: Path) -> dict:
    train = _capture(tmp_path / "train-capture", name="train")
    val = _capture(tmp_path / "val-capture", name="val", pixel_start=20)
    class_map = tmp_path / "class-map.json"
    _json(class_map, {"42": "neutral_search_target"})
    return {
        "rgb_manifests": [train[0], val[0]],
        "annotation_frame_sets": [train[1], val[1]],
        "recording_manifests": [train[2], val[2]],
        "splits": ["train", "val"],
        "class_map": class_map,
        "output_directory": tmp_path / "yolo",
    }


def _rebind_rgb(dataset: dict, index: int) -> None:
    path = dataset["annotation_frame_sets"][index]
    document = _read(path)
    document["rgb_frame_set"]["manifest_sha256"] = _sha(dataset["rgb_manifests"][index])
    _json(path, document)


def test_real_pixels_and_visible_not_3d_bbox_are_exported(dataset: dict) -> None:
    receipt = exporter.export_dataset(**dataset)
    output = dataset["output_directory"]
    assert receipt["training_ready"] is False
    assert receipt["purpose"] == "visual_detector_supervision_staging"
    assert receipt["split_counts"] == {"train": 1, "val": 1}
    assert receipt["bbox_source"] == "visible_instance_segmentation_bbox"
    assert receipt["classes"] == [
        {"label_id": 42, "name": "neutral_search_target", "class_index": 0}
    ]
    assert receipt["scene_truth_used"] is False
    assert receipt["policy_40_feature_rows_used"] is False
    assert receipt["candidate_promoted"] is False
    assert receipt["external_flight_evidence"] is False
    assert receipt["hardware_evidence"] is False
    for row in receipt["rows"]:
        label = output / row["label"]["path"]
        assert label.read_text().strip() == "0 0.5000000000 0.5000000000 0.5000000000 0.5000000000"
        exported = cv2.imread(str(output / row["image"]["path"]))
        assert exported.shape == (2, 2, 3)
        assert hashlib.sha256(exported.tobytes()).hexdigest() == row["rgb_payload_sha256"]
        assert _sha(label) == row["label"]["sha256"]
    yaml = (output / "data.yaml").read_text()
    assert "train: images/train" in yaml
    assert "val: images/val" in yaml
    assert '0: "neutral_search_target"' in yaml
    assert "car" not in yaml
    assert "test: images/test" not in yaml
    unsigned = dict(receipt)
    signature = unsigned.pop("canonical_hash")
    assert canonical_sha256(unsigned) == signature
    assert _sha(output / "data.yaml") == receipt["data_yaml"]["sha256"]
    assert _read(output / "export-receipt.json") == receipt


def test_test_recording_is_optional_and_never_frame_split(dataset: dict, tmp_path: Path) -> None:
    test = _capture(tmp_path / "test-capture", name="test", pixel_start=40)
    dataset["rgb_manifests"].append(test[0])
    dataset["annotation_frame_sets"].append(test[1])
    dataset["recording_manifests"].append(test[2])
    dataset["splits"].append("test")
    receipt = exporter.export_dataset(**dataset)
    assert receipt["split_unit"] == "complete_verified_recording"
    assert receipt["split_counts"] == {"train": 1, "val": 1, "test": 1}
    assert "test: images/test" in (dataset["output_directory"] / "data.yaml").read_text()
    assert len({row["recording_hash"] for row in receipt["rows"]}) == 3


@pytest.mark.parametrize("classes", [{}, {"1": "car"}, {"42": ""}, {"x": "object"}])
def test_no_inferred_or_missing_class_mapping(dataset: dict, classes: dict) -> None:
    _json(dataset["class_map"], classes)
    with pytest.raises(ValueError, match="class map|class mapping"):
        exporter.export_dataset(**dataset)
    assert not dataset["output_directory"].exists()


def test_requires_independent_train_val_recordings(dataset: dict) -> None:
    dataset["splits"] = ["train", "train"]
    with pytest.raises(ValueError, match="train and val"):
        exporter.export_dataset(**dataset)
    dataset["splits"] = ["train", "val"]
    dataset["recording_manifests"][1] = dataset["recording_manifests"][0]
    with pytest.raises(ValueError, match="recording hash"):
        exporter.export_dataset(**dataset)


def test_identical_rgb_is_rejected_across_independent_recordings(
    dataset: dict, tmp_path: Path
) -> None:
    duplicate = _capture(tmp_path / "duplicate", name="other", pixel_start=0)
    dataset["rgb_manifests"][1] = duplicate[0]
    dataset["annotation_frame_sets"][1] = duplicate[1]
    dataset["recording_manifests"][1] = duplicate[2]
    with pytest.raises(ValueError, match="RGB payload.*across"):
        exporter.export_dataset(**dataset)
    assert not dataset["output_directory"].exists()


@pytest.mark.parametrize("relative", ["../frame.bgr24", "C:/frame.bgr24", "/frame.bgr24"])
def test_rgb_path_escape_rejected(dataset: dict, relative: str) -> None:
    path = dataset["rgb_manifests"][0]
    document = _read(path)
    document["frames"][0]["file"] = relative
    _json(path, document)
    _rebind_rgb(dataset, 0)
    with pytest.raises(ValueError, match="contained"):
        exporter.export_dataset(**dataset)
    assert not dataset["output_directory"].exists()


def test_changed_payload_and_truth_fields_are_rejected(dataset: dict) -> None:
    raw = dataset["rgb_manifests"][0].parent / "frame.bgr24"
    previous = raw.read_bytes()
    raw.write_bytes(b"changed")
    with pytest.raises(ValueError, match="payload hash mismatch"):
        exporter.export_dataset(**dataset)
    raw.write_bytes(previous)
    path = dataset["annotation_frame_sets"][0]
    annotation = _read(path)
    annotation["frames"][0]["scenario_truth"] = {"object": "car"}
    _json(path, annotation)
    with pytest.raises(ValueError, match="runtime/truth"):
        exporter.export_dataset(**dataset)
    assert not dataset["output_directory"].exists()


def test_geometry_is_checked_against_actual_decoded_recording(dataset: dict) -> None:
    path = dataset["rgb_manifests"][0]
    rgb = _read(path)
    rgb["frames"][0]["provenance"].update({"width": 4, "height": 1})
    _json(path, rgb)
    _rebind_rgb(dataset, 0)
    with pytest.raises(ValueError, match="actual recording pixels/dimensions"):
        exporter.export_dataset(**dataset)
    assert not (dataset["output_directory"] / "export-receipt.json").exists()


def test_policy_review_flags_cannot_approve_visual_dataset(dataset: dict) -> None:
    path = dataset["annotation_frame_sets"][0]
    annotation = _read(path)
    annotation["training_ready"] = True
    annotation["reviewed_annotation_manifest"] = {"path": "policy-review.json", "sha256": "a" * 64}
    _json(path, annotation)
    with pytest.raises(ValueError, match="training-ready without review"):
        exporter.export_dataset(**dataset)


def test_annotation_and_recording_timestamp_must_match(dataset: dict) -> None:
    annotation_path = dataset["annotation_frame_sets"][0]
    annotation = _read(annotation_path)
    annotation["frames"][0]["captured_at_s"] = 2.0
    rgb_path = dataset["rgb_manifests"][0]
    rgb = _read(rgb_path)
    rgb["frames"][0]["provenance"]["captured_at_s"] = 2.0
    _json(rgb_path, rgb)
    _json(annotation_path, annotation)
    _rebind_rgb(dataset, 0)
    with pytest.raises(ValueError, match="timestamp"):
        exporter.export_dataset(**dataset)
    assert not dataset["output_directory"].exists()


def test_data_yaml_can_be_loaded_with_yolo_structure(dataset: dict) -> None:
    yaml = pytest.importorskip("yaml")
    exporter.export_dataset(**dataset)
    document = yaml.safe_load((dataset["output_directory"] / "data.yaml").read_text())
    assert document["names"] == {0: "neutral_search_target"}
    assert document["nc"] == 1
    assert Path(document["path"]) == dataset["output_directory"]
    assert (Path(document["path"]) / document["train"]).is_dir()
    assert (Path(document["path"]) / document["val"]).is_dir()


def test_source_mutation_during_export_never_seals_receipt(dataset: dict, monkeypatch) -> None:
    original = exporter._write
    changed = False

    def mutate_after_write(path: Path, payload: bytes) -> dict:
        nonlocal changed
        record = original(path, payload)
        if not changed:
            dataset["class_map"].write_text('{"42":"car"}')
            changed = True
        return record

    monkeypatch.setattr(exporter, "_write", mutate_after_write)
    with pytest.raises(ValueError, match="changed during export"):
        exporter.export_dataset(**dataset)
    assert not (dataset["output_directory"] / "export-receipt.json").exists()
    assert not (dataset["output_directory"] / "data.yaml").exists()


def test_existing_output_is_not_overwritten(dataset: dict) -> None:
    exporter.export_dataset(**dataset)
    receipt = dataset["output_directory"] / "export-receipt.json"
    original = _sha(receipt)
    with pytest.raises(FileExistsError):
        exporter.export_dataset(**dataset)
    assert _sha(receipt) == original

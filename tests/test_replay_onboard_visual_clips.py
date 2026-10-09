from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from multidetect.domain import BoundingBox, Detection
from multidetect.unified_tracking import UnifiedTargetPool
from scripts import replay_onboard_visual_clips as replay
from scripts import train_onboard_sdf_visual_candidate as training
from scripts.render_onboard_sdf_blender_dataset import _mask_png, yolo_label


class PixelDetector:
    class_names = ("search_target",)

    def __init__(self) -> None:
        self.inputs = []

    def detect(self, image_bgr):
        assert isinstance(image_bgr, np.ndarray)
        assert image_bgr.dtype == np.uint8
        self.inputs.append(image_bgr.copy())
        # The fixture signals absent RGB content through an image pixel. No
        # manifest, labels, masks or target pose are available to this detector.
        if image_bgr[0, 0, 2] == 0:
            return ()
        return (
            Detection(
                "search_target",
                0.9,
                BoundingBox(0.25, 0.25, 0.75, 0.75),
                model_version="fixture-pixel-detector",
            ),
        )


@pytest.fixture
def clip_fixture(tmp_path: Path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(training, "ROOT", repository)
    base = tmp_path / "dataset"
    base.mkdir()
    frames, clips = [], []
    for split_index, split in enumerate(("train", "val", "test")):
        world = repository / f"{split}.sdf"
        world.write_text(f"world-{split}", encoding="utf-8")
        for clip_index in range(2 if split == "test" else 1):
            clip_id = f"{split}-{clip_index}"
            clip_frames = []
            for index in range(6):
                frame_id = f"{clip_id}-{index}"
                directory = base / frame_id
                directory.mkdir()
                occluded = clip_index == 0 and index in {2, 3}
                mask = np.zeros((12, 16), dtype=np.uint8)
                amodal = mask.copy()
                amodal[3:9, 4:12] = 255
                if not occluded:
                    mask[:] = amodal
                image = np.full((12, 16, 3), 100, dtype=np.uint8)
                image[0, 0] = [split_index * 50 + clip_index, index, 0 if occluded else 255]
                assert cv2.imwrite(str(directory / "image.png"), image)
                (directory / "mask.png").write_bytes(_mask_png(mask.reshape(-1).tobytes(), 16, 12))
                (directory / "amodal.png").write_bytes(
                    _mask_png(amodal.reshape(-1).tobytes(), 16, 12)
                )
                (directory / "label.txt").write_text(
                    yolo_label(None if occluded else (4, 3, 12, 9), 16, 12), encoding="utf-8"
                )
                record = {
                    "frame_id": frame_id,
                    "split": split,
                    "clip_id": clip_id,
                    "frame_index": index,
                    "captured_at_s": index / 5,
                    "dt": 0 if index == 0 else 0.2,
                    "width": 16,
                    "height": 12,
                    "world_path": world.name,
                    "world_sha256": training.sha256(world),
                    "source_hashes": {world.name: training.sha256(world)},
                    "target_present": True,
                    "target_center": [999, 888, 777],
                }
                for kind, name in (
                    ("image", "image.png"),
                    ("label", "label.txt"),
                    ("mask", "mask.png"),
                    ("amodal_mask", "amodal.png"),
                ):
                    path = directory / name
                    record[f"{kind}_path"] = str(path.relative_to(base))
                    record[f"{kind}_sha256"] = training.sha256(path)
                record["rgb_payload_sha256"] = record["image_sha256"]
                frames.append(record)
                clip_frames.append(frame_id)
            clips.append(
                {
                    "clip_id": clip_id,
                    "split": split,
                    "frame_count": 6,
                    "frame_ids": clip_frames,
                    "fps": 5,
                    "captured_at_start_s": 0,
                    "captured_at_end_s": 1,
                }
            )
    yaml = base / "dataset.yaml"
    yaml.write_text("names: [search_target]\n", encoding="utf-8")
    document = {
        "contract": training.DATASET_CONTRACT,
        "names": ["search_target"],
        "training_ready": False,
        "gazebo_capture": False,
        "frame_count": len(frames),
        "frames": frames,
        "clips": clips,
        "dataset_yaml": "dataset.yaml",
        "dataset_yaml_sha256": training.sha256(yaml),
    }
    manifest = base / "manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fixture-model-not-an-executable-onnx")
    return document, manifest, model, tmp_path / "replay.json", repository


def write_manifest(document: dict, manifest: Path) -> None:
    manifest.write_text(json.dumps(document), encoding="utf-8")


def test_replay_uses_real_tracker_resets_clips_and_parses_truth_only_after_predictions(
    clip_fixture, monkeypatch
) -> None:
    _document, manifest, model, output, _root = clip_fixture
    detector = PixelDetector()
    pools = []

    def tracker_factory():
        pool = UnifiedTargetPool()
        pools.append(pool)
        return pool

    actual_truth = replay._offline_truth

    def assert_predictions_complete(base, frame):
        assert len(detector.inputs) == 12
        return actual_truth(base, frame)

    monkeypatch.setattr(replay, "_offline_truth", assert_predictions_complete)
    report = replay.replay(
        manifest,
        model,
        output,
        detector_factory=lambda *_: detector,
        tracker_factory=tracker_factory,
        model_sha256=training.sha256(model),
    )
    assert len(pools) == 2 and pools[0] is not pools[1]
    assert report["clip_count"] == 2
    for clip in report["clips"]:
        first = clip["frames"][0]["prediction"]
        assert first["created_track_ids"] == ("target-000001",)
        assert first["runtime_elapsed_s"] == 0
        assert first["input_fields"] == ["image_bgr", "dt"]
        assert first["tracks"][0]["label"] == "search_target"
    assert report["observed_counters"]["id_switches"] == 0
    assert report["observed_counters"]["fully_occluded_frames"] == 2
    assert report["observed_counters"]["occlusion_events"] == 1
    assert report["observed_counters"]["reacquired_occlusion_events"] == 1
    assert report["clips"][0]["occlusion_reacquisition"][0]["same_identity_recovered"] is True
    assert report["clips"][0]["occlusion_reacquisition"][0]["reacquisition_delay_s"] == 0
    assert report["model"]["label_aliases"] == {}
    assert json.loads(output.read_text("utf-8"))["model"]["sha256"] == training.sha256(model)
    for flag in (
        "training_ready",
        "task_policy_training",
        "gazebo_capture",
        "autonomous_flight_verified",
        "candidate_promoted",
        "hil_real_evidence",
        "jetson_hardware_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert report[flag] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "cross_split",
        "duplicate_index",
        "nan_dt",
        "inconsistent_dt",
        "reverse_time",
        "duplicate_id",
        "wrong_frame_inventory",
        "wrong_rgb_identity",
    ],
)
def test_bad_temporal_or_clip_binding_rejected_before_detector(clip_fixture, mutation) -> None:
    document, manifest, model, output, _root = clip_fixture
    if mutation == "cross_split":
        document["clips"][0]["split"] = "test"
    elif mutation == "duplicate_index":
        document["frames"][1]["frame_index"] = 0
    elif mutation == "nan_dt":
        document["frames"][1]["dt"] = float("nan")
    elif mutation == "inconsistent_dt":
        document["frames"][1]["dt"] = 0.3
    elif mutation == "reverse_time":
        document["frames"][1]["captured_at_s"] = 0
    elif mutation == "duplicate_id":
        document["clips"][1]["clip_id"] = document["clips"][0]["clip_id"]
    elif mutation == "wrong_frame_inventory":
        document["clips"][0]["frame_ids"].reverse()
    else:
        document["frames"][0]["rgb_payload_sha256"] = "0" * 64
    write_manifest(document, manifest)

    def forbidden(*_args):
        pytest.fail("invalid clip metadata must not reach detector construction")

    with pytest.raises(ValueError):
        replay.replay(manifest, model, output, detector_factory=forbidden)
    assert not output.exists()


@pytest.mark.parametrize("target", ["RGB", "amodal", "source", "model"])
def test_hash_failure_prevents_model_execution(clip_fixture, target) -> None:
    document, manifest, model, output, root = clip_fixture
    expected_model = training.sha256(model)
    if target == "RGB":
        (manifest.parent / document["frames"][0]["image_path"]).write_bytes(b"tampered")
    elif target == "amodal":
        (manifest.parent / document["frames"][0]["amodal_mask_path"]).write_bytes(b"tampered")
    elif target == "source":
        (root / document["frames"][0]["world_path"]).write_text("changed", encoding="utf-8")
    else:
        model.write_bytes(b"changed")

    def forbidden(*_args):
        pytest.fail("hash failure must not reach detector construction")

    with pytest.raises(ValueError):
        replay.replay(
            manifest, model, output, detector_factory=forbidden, model_sha256=expected_model
        )
    assert not output.exists()


def test_truth_geometry_does_not_change_runtime_predictions(clip_fixture) -> None:
    document, manifest, model, output, _root = clip_fixture
    first = replay.replay(manifest, model, output, detector_factory=lambda *_: PixelDetector())
    for frame in document["frames"]:
        frame["target_center"] = [-9000, -8000, -7000]
        frame["camera"] = {"location": [1, 2, 3], "look_at": [4, 5, 6]}
    write_manifest(document, manifest)
    second = replay.replay(
        manifest,
        model,
        output.with_name("second.json"),
        detector_factory=lambda *_: PixelDetector(),
    )
    assert [row["prediction"] for clip in first["clips"] for row in clip["frames"]] == [
        row["prediction"] for clip in second["clips"] for row in clip["frames"]
    ]


def test_runtime_refuses_aliases_and_has_pixels_dt_only_signature() -> None:
    detector = PixelDetector()
    runtime = replay.VisualClipRuntime(detector)
    image = np.full((12, 16, 3), 255, dtype=np.uint8)
    with pytest.raises(TypeError):
        runtime.predict(image, 0, target_center=[1, 2, 3])
    with pytest.raises(ValueError, match="initially"):
        runtime.predict(image, 0.2)
    detector.class_names = ("vehicle",)
    with pytest.raises(ValueError, match="true search_target"):
        replay.VisualClipRuntime(detector)


def test_offline_metrics_detect_identity_switch_fragmentation_and_late_reacquisition() -> None:
    frames, predictions, truths = [], [], []
    box = [0.25, 0.25, 0.75, 0.75]
    for index, identity in enumerate(("A", "A", None, None, None, "B", "B")):
        timestamp = index / 5
        occluded = index in {2, 3}
        frames.append(
            {
                "frame_id": str(index),
                "frame_index": index,
                "captured_at_s": timestamp,
                "dt": 0 if index == 0 else 0.2,
                "image_sha256": "0" * 64,
            }
        )
        predictions.append(
            {
                "runtime_elapsed_s": timestamp,
                "detections": [] if identity is None else [{"bbox_xyxy_normalized": box}],
                "tracks": []
                if identity is None
                else [
                    {
                        "track_id": identity,
                        "bbox_xyxy_normalized": box,
                        "missed_frame_count": 0,
                        "last_seen_at_s": timestamp,
                    }
                ],
            }
        )
        truths.append(
            {
                "bbox_xyxy_normalized": None if occluded else box,
                "fully_occluded": occluded,
                "visibility_fraction": 0 if occluded else 1,
                "absent": False,
                "out_of_frame": False,
            }
        )
    result = replay.evaluate_clip(frames, predictions, truths)
    assert result["observed_counters"]["id_switches"] == 1
    assert result["observed_counters"]["fragmentations"] == 1
    assert result["observed_counters"]["track_missed_visible_frames"] == 1
    event = result["occlusion_reacquisition"][0]
    assert event["reacquisition_delay_s"] == pytest.approx(0.2)
    assert event["same_identity_recovered"] is False


def test_predicted_stale_boxes_are_not_matches_without_current_rgb_evidence() -> None:
    frame = {
        "frame_id": "0",
        "frame_index": 0,
        "captured_at_s": 0,
        "dt": 0,
        "image_sha256": "0" * 64,
    }
    prediction = {
        "runtime_elapsed_s": 0,
        "detections": [],
        "tracks": [
            {
                "track_id": "stale",
                "bbox_xyxy_normalized": [0.25, 0.25, 0.75, 0.75],
                "missed_frame_count": 1,
                "last_seen_at_s": 0,
            }
        ],
    }
    truth = {
        "bbox_xyxy_normalized": [0.25, 0.25, 0.75, 0.75],
        "fully_occluded": False,
        "visibility_fraction": 1,
        "absent": False,
        "out_of_frame": False,
    }
    result = replay.evaluate_clip([frame], [prediction], [truth])
    assert result["observed_counters"]["track_matched_frames"] == 0
    assert result["observed_counters"]["track_missed_visible_frames"] == 1


def test_replay_does_not_overwrite_existing_report(clip_fixture) -> None:
    _document, manifest, model, output, _root = clip_fixture
    output.write_bytes(b"user report")
    with pytest.raises(ValueError, match="overwrite"):
        replay.replay(manifest, model, output)
    assert output.read_bytes() == b"user report"


def test_model_changed_during_prediction_cannot_seal_report(clip_fixture) -> None:
    _document, manifest, model, output, _root = clip_fixture

    class MutatingDetector(PixelDetector):
        def detect(self, image_bgr):
            model.write_bytes(b"changed-during-prediction")
            return super().detect(image_bgr)

    with pytest.raises(ValueError, match="changed during replay"):
        replay.replay(manifest, model, output, detector_factory=lambda *_: MutatingDetector())
    assert not output.exists()


def test_existing_tracker_state_cannot_carry_into_a_new_clip() -> None:
    pool = UnifiedTargetPool()
    pool.update(frame_id="old", captured_at_s=0, observations=())
    with pytest.raises(ValueError, match="fresh tracker"):
        replay.VisualClipRuntime(PixelDetector(), pool)


def test_disappearance_does_not_masquerade_as_occlusion(clip_fixture) -> None:
    document, manifest, _model, _output, _root = clip_fixture
    frame = document["frames"][2]
    amodal_path = manifest.parent / frame["amodal_mask_path"]
    amodal_path.write_bytes(_mask_png(bytes(16 * 12), 16, 12))
    frame["amodal_mask_sha256"] = training.sha256(amodal_path)
    frame["target_present"] = False
    truth = replay._offline_truth(manifest.parent, frame)
    assert truth["absent"] is True
    assert truth["fully_occluded"] is False
    assert truth["amodal_pixels"] == 0


def test_label_mask_mismatch_is_not_accepted_as_gt(clip_fixture) -> None:
    document, manifest, _model, _output, _root = clip_fixture
    frame = document["frames"][0]
    path = manifest.parent / frame["label_path"]
    path.write_text("0 0.1 0.1 0.1 0.1\n", encoding="utf-8")
    frame["label_sha256"] = training.sha256(path)
    with pytest.raises(ValueError, match="visible mask"):
        replay._offline_truth(manifest.parent, frame)


def test_one_visible_pixel_outside_amodal_is_rejected_even_with_other_overlap(clip_fixture) -> None:
    document, manifest, _model, _output, _root = clip_fixture
    frame = document["frames"][0]
    amodal = np.zeros((12, 16), dtype=np.uint8)
    amodal[3:9, 4:12] = 255
    amodal[5, 7] = 0
    path = manifest.parent / frame["amodal_mask_path"]
    path.write_bytes(_mask_png(amodal.reshape(-1).tobytes(), 16, 12))
    frame["amodal_mask_sha256"] = training.sha256(path)
    with pytest.raises(ValueError, match="outside amodal"):
        replay._offline_truth(manifest.parent, frame)


@pytest.mark.parametrize(
    "names,shape",
    [
        ("{0: 'person'}", [1, 5, 10]),
        ("{0: 'vehicle'}", [1, 5, 10]),
        ("{1: 'search_target'}", [1, 5, 10]),
        ("", [1, 5, 10]),
        ("{0: 'search_target'}", [1, 6, 10]),
        ("{0: 'search_target'}", [1, 10, 6]),
        ("{0: 'search_target'}", [2, 5, 10]),
    ],
)
def test_actual_detector_factory_rejects_wrong_embedded_names_or_shape_before_session(
    tmp_path, monkeypatch, names, shape
) -> None:
    onnx = pytest.importorskip("onnx")
    graph = onnx.helper.make_graph(
        [],
        "fixture-not-executed",
        [],
        [onnx.helper.make_tensor_value_info("output", onnx.TensorProto.FLOAT, shape)],
    )
    model = onnx.helper.make_model(graph)
    metadata = model.metadata_props.add()
    metadata.key, metadata.value = "names", names
    path = tmp_path / "contract-only.onnx"
    onnx.save_model(model, path)

    def forbidden(_config):
        pytest.fail("bad original model identity must fail before constructing any session")

    monkeypatch.setattr(replay, "OnnxRawYoloDetector", forbidden)
    with pytest.raises(ValueError):
        replay._detector(path, training.sha256(path))


def test_valid_embedded_search_target_raw_contract_reaches_factory_without_model_execution(
    tmp_path, monkeypatch
) -> None:
    onnx = pytest.importorskip("onnx")
    graph = onnx.helper.make_graph(
        [],
        "fixture-not-executed",
        [],
        [onnx.helper.make_tensor_value_info("output", onnx.TensorProto.FLOAT, [1, 5, 10])],
    )
    model = onnx.helper.make_model(graph)
    metadata = model.metadata_props.add()
    metadata.key, metadata.value = "names", "{0: 'search_target'}"
    path = tmp_path / "contract-only.onnx"
    onnx.save_model(model, path)
    configurations = []

    def no_session(config):
        configurations.append(config)
        return "fixture-no-session"

    monkeypatch.setattr(replay, "OnnxRawYoloDetector", no_session)
    assert replay._detector(path, training.sha256(path)) == "fixture-no-session"
    assert configurations[0].class_names == ("search_target",)
    assert "scripts/evaluate_onboard_visual_curriculum.py" in replay.SOURCE_PATHS


def test_new_occlusion_keeps_unreacquired_old_event_with_terminal_status() -> None:
    frames, predictions, truths = [], [], []
    box = [0.25, 0.25, 0.75, 0.75]
    for index, identity in enumerate(("A", "A", None, None, None, None, "B", "B")):
        timestamp = index / 5
        occluded = index in {2, 4, 5}
        frames.append(
            {
                "frame_id": str(index),
                "frame_index": index,
                "captured_at_s": timestamp,
                "dt": 0 if index == 0 else 0.2,
                "image_sha256": "0" * 64,
            }
        )
        predictions.append(
            {
                "runtime_elapsed_s": timestamp,
                "detections": [] if identity is None else [{"bbox_xyxy_normalized": box}],
                "tracks": []
                if identity is None
                else [
                    {
                        "track_id": identity,
                        "bbox_xyxy_normalized": box,
                        "missed_frame_count": 0,
                        "last_seen_at_s": timestamp,
                    }
                ],
            }
        )
        truths.append(
            {
                "bbox_xyxy_normalized": None if occluded else box,
                "fully_occluded": occluded,
                "visibility_fraction": 0 if occluded else 1,
                "absent": False,
                "out_of_frame": False,
            }
        )
    result = replay.evaluate_clip(frames, predictions, truths)
    events = result["occlusion_reacquisition"]
    assert len(events) == 2
    assert events[0]["occlusion_start_s"] == 0.4
    assert events[0]["status"] == "another_occlusion_before_reacquisition"
    assert events[0]["reappearance_at_s"] == 0.6
    assert events[0]["reacquired_at_s"] is None
    assert events[1]["occlusion_start_s"] == 0.8
    assert events[1]["status"] == "reacquired"
    assert result["observed_counters"]["reacquired_occlusion_events"] == 1

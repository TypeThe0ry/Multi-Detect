from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from multidetect.tracking_evaluation import GroundTruthVisibility
from multidetect.unified_tracking import AppearanceEmbedding, TargetObservation

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_person_tracking_video.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_person_tracking_video", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_mot_loader_normalizes_duplicate_rows_and_timeline_gaps(tmp_path: Path) -> None:
    module = _load_module()
    ground_truth = tmp_path / "gt.txt"
    ground_truth.write_text(
        "\n".join(
            (
                "1,1,10,10,20,40,1,1,1.0",
                "2,1,20,10,20,40,1,1,1.0",
                "2,1,60,10,5,5,1,1,0.5",
                "3,1,30,10,20,40,1,1,0.1",
                "1,2,5,5,10,20,1,1,1.0",
                "3,2,15,5,10,20,1,1,1.0",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    frames, stats = module.load_mot_ground_truth(
        ground_truth,
        frame_count=3,
        width=100,
        height=100,
        fps=1.0,
        minimum_visibility=0.2,
    )

    assert stats["deduplicated_row_count"] == 1
    assert frames[1].objects[0].identity_id == "person-1"
    assert frames[1].objects[0].bbox.x1 == pytest.approx(0.2)
    by_identity = {item.identity_id: item for item in frames[1].objects}
    assert by_identity["person-2"].visibility is GroundTruthVisibility.OUT_OF_FRAME
    assert frames[2].objects[0].visibility is GroundTruthVisibility.OCCLUDED


def test_person_tracking_observation_cache_round_trips_embeddings(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "observations.jsonl"
    observation = TargetObservation(
        label="person",
        confidence=0.8,
        bbox=module.BoundingBox(0.1, 0.2, 0.3, 0.8),
        appearance=AppearanceEmbedding((1.0, 0.0, 0.0)),
        appearance_reliable=True,
        source="test",
    )

    module.write_observations_cache(path, (("frame-000001", 0.0, (observation,)),))
    loaded = module.load_observations_cache(path)

    assert loaded[0][0] == "frame-000001"
    assert loaded[0][2][0].appearance is not None
    assert loaded[0][2][0].appearance.values == pytest.approx((1.0, 0.0, 0.0))


def test_mot_sequence_info_is_parsed_and_checked_against_video_contract(tmp_path: Path) -> None:
    module = _load_module()
    sequence_info = tmp_path / "seqinfo.ini"
    sequence_info.write_text(
        "[Sequence]\nname=PedestrianTracking\nframeRate=1\nseqLength=169\nimWidth=1288\nimHeight=964\n",
        encoding="utf-8",
    )

    parsed = module.load_sequence_info(sequence_info)

    assert parsed == {
        "seq_length": 169,
        "fps": 1.0,
        "width": 1288,
        "height": 964,
        "name": "PedestrianTracking",
    }
    module._validate_sequence_matches_video(
        parsed,
        {"frame_count": 169, "fps": 1.0, "width": 1288, "height": 964},
    )
    with pytest.raises(ValueError, match="does not match"):
        module._validate_sequence_matches_video(
            parsed,
            {"frame_count": 168, "fps": 1.0, "width": 1288, "height": 964},
        )


def test_cached_observations_reject_an_ambiguous_second_output_path(tmp_path: Path) -> None:
    module = _load_module()
    video = tmp_path / "video.mp4"
    ground_truth = tmp_path / "gt.txt"
    sequence_info = tmp_path / "seqinfo.ini"
    observations = tmp_path / "observations.jsonl"
    for path in (video, ground_truth, sequence_info, observations):
        path.write_text("fixture", encoding="utf-8")
    args = Namespace(
        video=video,
        ground_truth=ground_truth,
        sequence_info=sequence_info,
        model=None,
        observations_in=observations,
        observations_out=tmp_path / "copy.jsonl",
        person_reid_model=None,
        person_reid_engine=None,
        maximum_frames=None,
        maximum_detections=300,
        person_reid_maximum_batch_size=10,
        minimum_confirmed_hits=3,
        person_confidence=0.25,
        model_confidence=0.10,
        model_iou_threshold=0.45,
        minimum_visibility=0.20,
        iou_threshold=0.50,
        confidence_threshold=0.10,
        maximum_center_distance=0.16,
        maximum_appearance_distance=0.38,
        strict_reid_distance=0.22,
        person_maximum_appearance_distance=None,
        person_strict_reid_distance=None,
        occluded_after_seconds=0.35,
        reacquisition_timeout_seconds=2.0,
        kalman_gate_sigma=4.0,
    )

    with pytest.raises(ValueError, match="observations-out"):
        module._validate_args(args)


def test_person_tracking_parser_supports_the_production_raw_yolo_contract() -> None:
    module = _load_module()
    parser = module.build_parser()

    default_args = parser.parse_args(
        (
            "--video",
            "video.mp4",
            "--ground-truth",
            "gt.txt",
            "--sequence-info",
            "seq.ini",
            "--out",
            "out.json",
            "--model",
            "model.onnx",
        )
    )
    raw_args = parser.parse_args(
        (
            "--video",
            "video.mp4",
            "--ground-truth",
            "gt.txt",
            "--sequence-info",
            "seq.ini",
            "--out",
            "out.json",
            "--model",
            "raw.engine",
            "--model-format",
            "ultralytics_raw",
        )
    )

    assert default_args.model_format == "post_nms_nx6"
    assert raw_args.model_format == "ultralytics_raw"


def test_person_specific_reid_gate_is_validated_before_video_inference(tmp_path: Path) -> None:
    module = _load_module()
    video = tmp_path / "video.mp4"
    ground_truth = tmp_path / "gt.txt"
    sequence_info = tmp_path / "seqinfo.ini"
    model = tmp_path / "model.onnx"
    for path in (video, ground_truth, sequence_info, model):
        path.write_text("fixture", encoding="utf-8")
    args = Namespace(
        video=video,
        ground_truth=ground_truth,
        sequence_info=sequence_info,
        model=model,
        observations_in=None,
        observations_out=None,
        person_reid_model=None,
        person_reid_engine=None,
        maximum_frames=None,
        maximum_detections=300,
        person_reid_maximum_batch_size=10,
        minimum_confirmed_hits=3,
        person_confidence=0.25,
        model_confidence=0.10,
        model_iou_threshold=0.45,
        minimum_visibility=0.20,
        iou_threshold=0.50,
        confidence_threshold=0.10,
        maximum_center_distance=0.16,
        maximum_appearance_distance=0.38,
        strict_reid_distance=0.22,
        person_maximum_appearance_distance=0.60,
        person_strict_reid_distance=0.61,
        occluded_after_seconds=0.35,
        reacquisition_timeout_seconds=2.0,
        kalman_gate_sigma=4.0,
    )

    with pytest.raises(ValueError, match="person strict-reid-distance"):
        module._validate_args(args)

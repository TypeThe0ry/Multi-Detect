from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from multidetect.domain import BoundingBox, Detection
from multidetect.evaluation import GroundTruthFrame, LabeledBox, PredictionFrame

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_fire_video.py"


def _module():
    spec = importlib.util.spec_from_file_location("evaluate_fire_video", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_load_furg_annotations_normalizes_rectangles(tmp_path: Path) -> None:
    xml_path = tmp_path / "sample.xml"
    xml_path.write_text(
        """<?xml version=\"1.0\"?>
<opencv_storage>
  <frameWidth>100</frameWidth><frameHeight>50</frameHeight>
  <frames>
    <_><frameNumber>0</frameNumber><annotations><_>10 5 40 20</_></annotations></_>
    <_><frameNumber>1</frameNumber><annotations></annotations></_>
  </frames>
</opencv_storage>
""",
        encoding="utf-8",
    )

    annotations = _module().load_furg_annotations(xml_path)

    assert annotations.frame_count == 2
    assert annotations.annotated_frame_count == 1
    assert annotations.annotated_box_count == 1
    assert annotations.boxes_by_frame[0][0].rounded() == (0.1, 0.1, 0.5, 0.5)
    assert annotations.boxes_by_frame[1] == ()


def test_tracking_continuity_reports_confirmation_delay_and_gaps() -> None:
    module = _module()
    box = BoundingBox(0.1, 0.1, 0.4, 0.4)
    truth = tuple(
        GroundTruthFrame(str(index), (LabeledBox("flame", box),)) for index in range(8)
    )
    predictions = tuple(
        PredictionFrame(
            str(index),
            (LabeledBox("flame", box, 0.9),) if index in {5, 6, 7} else (),
            1.0,
        )
        for index in range(8)
    )

    report = module.tracking_continuity_document(truth, predictions, iou_threshold=0.25)

    assert report["positive_segment_count"] == 1
    assert report["matched_positive_frame_count"] == 3
    assert report["matched_positive_frame_rate"] == 0.375
    assert report["confirmation_delay_frames_by_positive_segment"] == [5]
    assert report["maximum_unmatched_positive_run_frames"] == 5


def test_label_presence_reports_stable_coverage_and_flame_aliases() -> None:
    module = _module()

    report = module.label_presence_document(
        (False, True, True, False, True, True, True),
        evaluated_frame_count=7,
    )

    assert report == {
        "detected_frame_count": 5,
        "detected_frame_rate": 5 / 7,
        "first_detected_evaluated_frame": 1,
        "longest_detected_run_frames": 3,
    }
    detections = (
        module.Detection("flame", 0.8, BoundingBox(0.1, 0.1, 0.2, 0.2)),
        module.Detection("smoke", 0.7, BoundingBox(0.3, 0.3, 0.4, 0.4)),
    )
    assert module._has_expected_label(detections, "fire") is True
    assert module._has_expected_label(detections, "smoke") is True
    assert module._has_expected_label(detections, "burned_area") is False


def test_optional_tiled_fire_configuration_keeps_full_frame_default_and_normalizes_labels() -> None:
    module = _module()
    parser = module.build_parser()

    baseline = parser.parse_args(
        ["--video", "video.mp4", "--onnx-model", "model.onnx", "--out", "result.json"]
    )
    assert module.tiled_detection_config_from_args(baseline) is None

    tiled = parser.parse_args(
        [
            "--video",
            "video.mp4",
            "--onnx-model",
            "model.onnx",
            "--out",
            "result.json",
            "--tile-columns",
            "2",
            "--tile-rows",
            "1",
            "--tile-overlap",
            "0.2",
            "--tile-scan-interval-frames",
            "4",
            "--tile-confidence-threshold",
            "0.31",
            "--tile-labels",
            " Flame , smoke ",
            "--tile-maximum-box-area",
            "0.5",
            "--minimum-bright-warm-fraction",
            "0.001",
        ]
    )
    config = module.tiled_detection_config_from_args(tiled)

    assert config is not None
    assert config.columns == 2
    assert config.rows == 1
    assert config.overlap_fraction == 0.2
    assert config.scan_interval_frames == 4
    assert config.tile_confidence_threshold == 0.31
    assert config.tile_labels == frozenset({"flame", "smoke"})
    assert config.maximum_tile_box_area == 0.5
    assert tiled.minimum_bright_warm_fraction == 0.001


def test_stage_documents_export_only_sanitized_fire_rgb_diagnostics() -> None:
    module = _module()

    document = module._detections_document(
        (
            Detection(
                "flame",
                0.9,
                BoundingBox(0.1, 0.2, 0.3, 0.4),
                metadata={
                    "fire_rgb_warm_fraction": 0.73,
                    "fire_rgb_bbox_aspect_ratio": 1.2,
                    "unrelated": "discard-me",
                    "fire_rgb_colorful_fraction": float("nan"),
                },
            ),
            Detection(
                "person",
                0.8,
                BoundingBox(0.5, 0.2, 0.7, 0.9),
                metadata={"fire_rgb_warm_fraction": 0.99},
            ),
        )
    )

    assert document[0]["diagnostics"] == {
        "fire_rgb_warm_fraction": 0.73,
        "fire_rgb_bbox_aspect_ratio": 1.2,
    }
    assert "diagnostics" not in document[1]

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scripts import export_proposer_for_orin as exporter
from scripts.export_proposer_for_orin import (
    _box_iou,
    _xywh_to_xyxy,
    parse_args,
)


def test_output_cannot_escape_worktree() -> None:
    with pytest.raises(ValueError, match="Output must stay inside"):
        exporter.workspace_path(exporter.ROOT.parent / "outside-export")


def test_missing_asset_fails_without_download(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Offline asset missing: mobileclip_blt.ts"):
        exporter.local_asset("mobileclip_blt.ts", tmp_path)


def test_score_drift_near_proposal_threshold_is_rejected() -> None:
    expected = np.zeros((1, 24, 3), dtype=np.float32)
    expected[:, 4:] = 0.049
    actual = expected.copy()
    actual[:, 4:] = 0.051
    with pytest.raises(AssertionError):
        exporter.verify_predictions(actual, expected, class_count=20)


def test_coordinate_roundoff_does_not_hide_score_drift() -> None:
    expected = np.ones((1, 24, 3), dtype=np.float32)
    actual = expected.copy()
    actual[:, :4] += 0.001
    actual[:, 4:] += 1e-6
    result = exporter.verify_predictions(actual, expected, class_count=20)
    assert result["passed"] is True
    assert result["max_abs_box_error"] > result["max_abs_score_error"]


def test_wrong_class_count_is_rejected() -> None:
    predictions = np.zeros((1, 84, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="Unexpected detection output shapes"):
        exporter.verify_predictions(predictions, predictions, class_count=20)


def test_export_parser_accepts_frame_comparison_options() -> None:
    args = parse_args(
        [
            "--imgsz",
            "960",
            "--comparison-frames-dir",
            "frames",
            "--comparison-frame-limit",
            "24",
        ]
    )
    assert args.imgsz == 960
    assert args.comparison_frame_limit == 24
    assert str(args.comparison_frames_dir) == "frames"


def test_frame_box_metrics_are_shape_stable_for_anchor_arrays() -> None:
    xywh = np.array([[10.0, 10.0, 4.0, 4.0], [5.0, 5.0, 2.0, 2.0]], dtype=np.float32)
    boxes = _xywh_to_xyxy(xywh)
    assert np.allclose(boxes[0], [8.0, 8.0, 12.0, 12.0])
    assert np.allclose(_box_iou(boxes, boxes), [1.0, 1.0])

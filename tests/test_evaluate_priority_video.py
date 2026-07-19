from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_priority_video.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_priority_video", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_priority_video_parser_defaults_to_strict_truck_source_gate() -> None:
    module = _load_module()
    args = module.build_parser().parse_args(
        ["--video", "traffic.mp4", "--model", "priority.onnx", "--out", "report.json"]
    )

    assert args.label_confidence_thresholds == "truck=0.80"
    assert args.frame_stride == 4
    assert args.vehicle_stability_frames == 3


def test_priority_video_source_thresholds_keep_nontruck_vehicle_gate() -> None:
    module = _load_module()
    thresholds = module.build_source_thresholds(
        person_confidence=0.30,
        vehicle_confidence=0.60,
        overrides=module.parse_label_confidence_thresholds("truck=0.80"),
    )

    assert thresholds["car"] == pytest.approx(0.60)
    assert thresholds["van"] == pytest.approx(0.60)
    assert thresholds["truck"] == pytest.approx(0.80)


def test_priority_video_rejects_invalid_label_thresholds() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="class=confidence"):
        module.parse_label_confidence_thresholds("truck")
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        module.parse_label_confidence_thresholds("truck=1.01")

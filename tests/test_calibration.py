from __future__ import annotations

import pytest

from multidetect.calibration import calibrate_class_thresholds, evaluate_class_thresholds
from multidetect.domain import BoundingBox
from multidetect.evaluation import GroundTruthFrame, LabeledBox, PredictionFrame


def _box() -> BoundingBox:
    return BoundingBox(0.1, 0.1, 0.4, 0.4)


def test_calibration_recommends_per_class_thresholds_with_recall_floor() -> None:
    truth = (
        GroundTruthFrame("a", (LabeledBox("flame", _box()),)),
        GroundTruthFrame("b", (LabeledBox("flame", _box()),)),
        GroundTruthFrame("c", (LabeledBox("smoke", _box()),)),
        GroundTruthFrame("d", ()),
    )
    predictions = (
        PredictionFrame("a", (LabeledBox("flame", _box(), 0.9),), 1.0),
        PredictionFrame("b", (LabeledBox("flame", _box(), 0.6),), 1.0),
        PredictionFrame("c", (LabeledBox("smoke", _box(), 0.7),), 1.0),
        PredictionFrame("d", (LabeledBox("flame", _box(), 0.55),), 1.0),
    )

    report = calibrate_class_thresholds(
        truth,
        predictions,
        thresholds=(0.5, 0.6, 0.7, 0.8),
        minimum_recall=0.5,
        beta=0.5,
    )

    recommendations = {item["label"]: item for item in report["recommendations"]}
    assert recommendations["flame"]["threshold"] == pytest.approx(0.6)
    assert recommendations["flame"]["precision"] == pytest.approx(1.0)
    assert recommendations["flame"]["recall"] == pytest.approx(1.0)
    assert recommendations["smoke"]["threshold"] == pytest.approx(0.7)
    assert recommendations["smoke"]["recall_floor_satisfied"] is True


def test_class_threshold_evaluation_applies_independent_thresholds() -> None:
    truth = (
        GroundTruthFrame(
            "a",
            (LabeledBox("flame", _box()), LabeledBox("smoke", _box())),
        ),
    )
    predictions = (
        PredictionFrame(
            "a",
            (
                LabeledBox("flame", _box(), 0.7),
                LabeledBox("smoke", _box(), 0.5),
            ),
            1.0,
        ),
    )

    report = evaluate_class_thresholds(
        truth,
        predictions,
        thresholds={"flame": 0.72, "smoke": 0.50},
    )

    metrics = {item.label: item for item in report.per_class}
    assert metrics["flame"].false_negatives == 1
    assert metrics["smoke"].true_positives == 1


@pytest.mark.parametrize("thresholds", [(), (-0.1,), (float("nan"),)])
def test_calibration_rejects_invalid_threshold_grids(thresholds: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="thresholds"):
        calibrate_class_thresholds((), (), thresholds=thresholds)

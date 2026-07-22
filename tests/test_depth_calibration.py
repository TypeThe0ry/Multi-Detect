from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.depth_calibration import (
    DepthCalibrationError,
    FieldDepthSample,
    calibration_document,
    fit_outdoor_depth_calibration,
    load_calibration_document,
    write_calibration_document,
)

ROOT = Path(__file__).resolve().parents[1]


def _samples() -> tuple[FieldDepthSample, ...]:
    return (
        FieldDepthSample(4.0, 2.2, "two-m"),
        FieldDepthSample(8.0, 4.2, "four-m"),
        FieldDepthSample(13.0, 6.7, "six-eight-m"),
        FieldDepthSample(20.0, 10.2, "ten-m"),
        FieldDepthSample(30.0, 15.1, "fifteen-m"),
        FieldDepthSample(50.0, 60.0, "bad-association"),
        FieldDepthSample(100.0, 50.1, "fifty-m"),
    )


def test_field_depth_fit_rejects_bad_association_and_round_trips_document(tmp_path) -> None:
    samples = _samples()
    fit = fit_outdoor_depth_calibration(samples)

    assert fit.scale == pytest.approx(0.5, abs=0.01)
    assert fit.offset_m == pytest.approx(0.2, abs=0.10)
    assert fit.inlier_count == 6
    assert 5 not in fit.inlier_indices
    assert fit.root_mean_square_error_m < 0.15

    path = tmp_path / "outdoor-field.json"
    write_calibration_document(
        path,
        calibration_document(profile="outdoor-survey-20260722", samples=samples, fit=fit),
    )
    loaded = load_calibration_document(path)
    assert loaded.profile == "outdoor-survey-20260722"
    assert loaded.scale == pytest.approx(fit.scale)
    assert loaded.inlier_count == 6
    assert len(loaded.document_sha256) == 64


def test_field_depth_fit_rejects_narrow_or_insufficient_evidence() -> None:
    with pytest.raises(DepthCalibrationError, match="at least 4"):
        fit_outdoor_depth_calibration(_samples()[:3])
    with pytest.raises(DepthCalibrationError, match="span"):
        fit_outdoor_depth_calibration(
            tuple(FieldDepthSample(10.0, 2.0 + index, f"near-{index}") for index in range(4))
        )


def test_loader_refuses_document_without_four_documented_inliers(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "invalid",
                "calibration": {"scale": 0.5, "offset_m": 0.0},
                "fit": {"sample_count": 4, "inlier_count": 3},
                "samples": [{"inlier": True}] * 3 + [{"inlier": False}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DepthCalibrationError, match="at least four"):
        load_calibration_document(path)


def test_field_calibration_script_writes_a_runtime_document(tmp_path) -> None:
    samples = tmp_path / "samples.csv"
    samples.write_text(
        "sample_id,raw_depth_m,actual_distance_m\n"
        "two,4.0,2.2\n"
        "four,8.0,4.2\n"
        "six-eight,13.0,6.7\n"
        "ten,20.0,10.2\n"
        "fifteen,30.0,15.1\n",
        encoding="utf-8",
    )
    output = tmp_path / "field.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
            str(samples),
            "--output",
            str(output),
            "--profile",
            "outdoor-survey-script",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = load_calibration_document(output)
    assert "METRIC_DEPTH_CALIBRATION_DOCUMENT=" in completed.stdout
    assert loaded.profile == "outdoor-survey-script"
    assert loaded.scale == pytest.approx(0.5, abs=0.01)

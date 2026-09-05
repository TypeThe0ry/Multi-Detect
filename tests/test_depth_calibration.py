from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.depth_calibration import (
    CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION,
    DepthCalibrationCurve,
    DepthCalibrationError,
    FieldDepthSample,
    calibration_document,
    fit_outdoor_depth_calibration,
    fit_outdoor_depth_piecewise_calibration,
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


def test_piecewise_outdoor_fit_preserves_nonlinear_endpoints_and_rejects_bad_ordering() -> None:
    samples = (
        FieldDepthSample(4.0, 2.0, "two-m"),
        FieldDepthSample(8.0, 4.5, "four-m"),
        FieldDepthSample(14.0, 8.5, "six-eight-m"),
        FieldDepthSample(20.0, 12.0, "ten-m"),
        FieldDepthSample(30.0, 20.0, "fifteen-m"),
        FieldDepthSample(50.0, 38.0, "fifty-m"),
        FieldDepthSample(100.0, 90.0, "hundred-m"),
        # A large, out-of-order tape association must not poison the valid
        # far-range endpoint or collapse the outdoor curve to affine-only.
        FieldDepthSample(60.0, 300.0, "bad-association"),
    )
    fit = fit_outdoor_depth_piecewise_calibration(samples)

    assert fit.model == "piecewise_linear"
    assert fit.inlier_indices == tuple(range(7))
    assert fit.raw_breakpoints_m == (4.0, 8.0, 14.0, 20.0, 30.0, 50.0, 100.0)
    assert fit.actual_breakpoints_m[-1] == pytest.approx(90.0)
    curve = DepthCalibrationCurve(fit.raw_breakpoints_m, fit.actual_breakpoints_m)
    assert curve.map_scalar(14.0) == pytest.approx(8.5)
    assert curve.map_scalar(75.0) == pytest.approx(64.0)
    assert fit.maximum_absolute_error_m == pytest.approx(0.0)


def test_piecewise_document_round_trips_as_schema_v2(tmp_path: Path) -> None:
    samples = (
        FieldDepthSample(4.0, 2.0, "two"),
        FieldDepthSample(8.0, 4.5, "four"),
        FieldDepthSample(14.0, 8.5, "six"),
        FieldDepthSample(20.0, 12.0, "ten"),
        FieldDepthSample(30.0, 20.0, "fifteen"),
        FieldDepthSample(50.0, 38.0, "fifty"),
    )
    fit = fit_outdoor_depth_piecewise_calibration(samples)
    document = calibration_document(profile="outdoor-piecewise", samples=samples, fit=fit)
    assert document["schema_version"] == CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION
    path = tmp_path / "outdoor-piecewise.json"
    write_calibration_document(path, document)
    profile = load_calibration_document(path)
    assert profile.calibration_mode == "piecewise_linear"
    assert profile.raw_breakpoints_m == fit.raw_breakpoints_m
    assert profile.actual_breakpoints_m == fit.actual_breakpoints_m
    assert profile.map_depth(14.0) == pytest.approx(8.5)
    assert profile.extrapolation_sigma_m > 0.0


@pytest.mark.parametrize(
    ("schema_version", "curve"),
    (
        (True, {"raw_breakpoints_m": [4.0, 8.0], "actual_breakpoints_m": [2.0, 4.0]}),
        (2, {"raw_breakpoints_m": [4.0, 8.0], "actual_breakpoints_m": [2.0, 2.0]}),
        (2, {"raw_breakpoints_m": [8.0, 4.0], "actual_breakpoints_m": [2.0, 4.0]}),
        (2, {"raw_breakpoints_m": [4.0, 8.0], "actual_breakpoints_m": [2.0, 4.0]}),
    ),
)
def test_piecewise_loader_rejects_invalid_schema_or_non_monotonic_curve(
    tmp_path: Path,
    schema_version: object,
    curve: dict[str, list[float]],
) -> None:
    path = tmp_path / "invalid-piecewise.json"
    document = {
        "schema_version": schema_version,
        "profile": "invalid-piecewise",
        "calibration": {
            "scale": 0.5,
            "offset_m": 0.0,
            "mode": "piecewise_linear",
            "curve": curve,
        },
        "fit": {
            "model": "piecewise_linear",
            "sample_count": 4,
            "inlier_count": 4,
            "residual_gate_m": 1.0,
        },
        "samples": [{"inlier": True}] * 4,
    }
    if schema_version == 2 and curve["actual_breakpoints_m"] == [2.0, 4.0]:
        document["calibration"]["mode"] = "affine"
    path.write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    with pytest.raises(DepthCalibrationError):
        load_calibration_document(path)


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


def test_calibration_loader_rejects_ambiguous_json_and_accepts_windows_bom(tmp_path) -> None:
    samples = _samples()[:5]
    fit = fit_outdoor_depth_calibration(samples)
    document = calibration_document(profile="strict-outdoor", samples=samples, fit=fit)

    valid = tmp_path / "valid-bom.json"
    valid.write_bytes(("\ufeff" + json.dumps(document)).encode("utf-8"))
    assert load_calibration_document(valid).profile == "strict-outdoor"

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )
    with pytest.raises(DepthCalibrationError, match="duplicate JSON key"):
        load_calibration_document(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"schema_version":NaN}', encoding="utf-8")
    with pytest.raises(DepthCalibrationError, match="non-finite JSON number NaN"):
        load_calibration_document(nonfinite)


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

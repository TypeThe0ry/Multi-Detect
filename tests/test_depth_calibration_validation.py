from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.depth_calibration import DepthCalibrationError, load_calibration_document

ROOT = Path(__file__).resolve().parents[1]


def test_gazebo_depth_transport_identity_is_not_a_metric_model_calibration(
    tmp_path: Path,
) -> None:
    document = {
        "schema_version": 1,
        "profile": "gazebo-front-depth-v1-sim",
        "calibration": {"mode": "affine", "scale": 1.0, "offset_m": 0.0},
        "fit": {
            "model": "affine",
            "sample_count": 4,
            "inlier_count": 4,
            "method": "gazebo_sensor_identity_contract",
        },
        "samples": [
            {
                "sample_id": f"z-{distance}",
                "raw_depth_m": distance,
                "actual_distance_m": distance,
                "inlier": True,
            }
            for distance in (1.0, 2.0, 4.0, 8.0)
        ],
        "source_kind": "gazebo_depth_wire_optical_axis_z_m",
        "field_calibration_evidence": False,
    }
    path = tmp_path / "gazebo-depth.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        DepthCalibrationError,
        match="Gazebo depth transport calibration cannot be used",
    ):
        load_calibration_document(path)


def test_validation_reports_independent_holdout_without_mutating_calibration(
    tmp_path: Path,
) -> None:
    fit_samples = tmp_path / "fit-samples.csv"
    fit_samples.write_text(
        "sample_id,raw_depth_m,actual_distance_m\n"
        "fit-1,4,2.1\n"
        "fit-2,8,4.1\n"
        "fit-3,14,7.1\n"
        "fit-4,20,10.1\n",
        encoding="utf-8",
    )
    samples = tmp_path / "samples.csv"
    samples.write_text(
        "sample_id,raw_depth_m,actual_distance_m\n"
        "fit-1,4,2.1\n"
        "fit-2,8,4.1\n"
        "fit-3,14,7.1\n"
        "fit-4,20,10.1\n"
        "holdout-1,30,15.1\n"
        "holdout-2,50,25.1\n"
        "holdout-3,100,50.1\n",
        encoding="utf-8",
    )
    calibration = tmp_path / "outdoor.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
            str(fit_samples),
            "--output",
            str(calibration),
            "--profile",
            "outdoor-validation",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    document_before = calibration.read_bytes()
    report = tmp_path / "validation.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_outdoor_depth_calibration.py"),
            str(samples),
            "--calibration",
            str(calibration),
            "--output",
            str(report),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    decoded = json.loads(report.read_text(encoding="utf-8"))
    assert document_before == calibration.read_bytes()
    assert decoded["measurement"]["holdout_sample_count"] == 3
    assert decoded["measurement"]["independent_validation_ready"] is True
    assert decoded["fitted_samples"]["sample_count"] == 4
    assert decoded["independent_holdout"]["sample_count"] == 3
    assert decoded["automatic_calibration_update"] is False
    assert "automatic_calibration_update=false" in completed.stdout


def test_json_sample_inputs_reject_duplicate_keys_and_nonfinite_numbers(
    tmp_path: Path,
) -> None:
    for name, payload, needle in (
        (
            "duplicate",
            '[{"sample_id":"a","sample_id":"b"}]',
            "duplicate JSON key",
        ),
        (
            "nonfinite",
            '[{"sample_id":"a","raw_depth_m":NaN,"actual_distance_m":2}]',
            "non-finite JSON number NaN",
        ),
    ):
        samples = tmp_path / f"{name}.json"
        samples.write_text(payload, encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
                str(samples),
                "--output",
                str(tmp_path / f"{name}.calibration.json"),
                "--profile",
                "strict-input-test",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert needle in completed.stderr


def test_strict_holdout_quality_gate_passes_only_with_independent_samples_and_thresholds(
    tmp_path: Path,
) -> None:
    fit_samples = tmp_path / "fit-samples.csv"
    fit_samples.write_text(
        "sample_id,raw_depth_m,actual_distance_m\n"
        "fit-1,4,2.1\n"
        "fit-2,8,4.1\n"
        "fit-3,14,7.1\n"
        "fit-4,20,10.1\n",
        encoding="utf-8",
    )
    samples = tmp_path / "samples.csv"
    samples.write_text(
        fit_samples.read_text(encoding="utf-8")
        + "holdout-1,30,15.1\n"
        + "holdout-2,50,25.1\n"
        + "holdout-3,100,50.1\n",
        encoding="utf-8",
    )
    calibration = tmp_path / "outdoor.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
            str(fit_samples),
            "--output",
            str(calibration),
            "--profile",
            "outdoor-strict-gate",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = tmp_path / "strict-validation.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_outdoor_depth_calibration.py"),
            str(samples),
            "--calibration",
            str(calibration),
            "--output",
            str(report),
            "--require-independent-holdout",
            "--minimum-independent-holdout",
            "3",
            "--maximum-holdout-mae-m",
            "0.2",
            "--maximum-holdout-rmse-m",
            "0.2",
            "--maximum-holdout-error-m",
            "0.2",
            "--maximum-holdout-relative-mae",
            "0.02",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    decoded = json.loads(report.read_text(encoding="utf-8"))
    assert decoded["quality_gate"]["status"] == "passed"
    assert decoded["quality_gate"]["passed"] is True
    assert decoded["independent_holdout_relative"]["sample_count"] == 3
    assert "quality_gate_status=passed" in completed.stdout


def test_strict_holdout_quality_gate_fails_closed_on_insufficient_or_bad_holdout(
    tmp_path: Path,
) -> None:
    fit_samples = tmp_path / "fit-samples.csv"
    fit_samples.write_text(
        "sample_id,raw_depth_m,actual_distance_m\n"
        "fit-1,4,2.1\n"
        "fit-2,8,4.1\n"
        "fit-3,14,7.1\n"
        "fit-4,20,10.1\n",
        encoding="utf-8",
    )
    samples = tmp_path / "samples.csv"
    samples.write_text(
        fit_samples.read_text(encoding="utf-8")
        + "holdout-bad,30,30.0\n",
        encoding="utf-8",
    )
    calibration = tmp_path / "outdoor.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
            str(fit_samples),
            "--output",
            str(calibration),
            "--profile",
            "outdoor-strict-fail",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = tmp_path / "strict-validation-fail.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_outdoor_depth_calibration.py"),
            str(samples),
            "--calibration",
            str(calibration),
            "--output",
            str(report),
            "--require-independent-holdout",
            "--minimum-independent-holdout",
            "3",
            "--maximum-holdout-mae-m",
            "1.0",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    decoded = json.loads(report.read_text(encoding="utf-8"))
    assert completed.returncode == 2
    assert decoded["quality_gate"]["status"] == "failed"
    assert decoded["quality_gate"]["passed"] is False
    assert any(
        reason.startswith("independent_holdout_count_below_minimum")
        for reason in decoded["quality_gate"]["failure_reasons"]
    )

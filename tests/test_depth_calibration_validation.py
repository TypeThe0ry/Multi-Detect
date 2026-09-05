from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

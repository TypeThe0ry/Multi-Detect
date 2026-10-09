from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _make_calibration_and_report(tmp_path: Path, *, strict: bool) -> tuple[Path, Path]:
    fit = tmp_path / "fit.csv"
    fit.write_text(
        "sample_id,raw_depth_m,actual_distance_m\n"
        "fit-1,4,2.1\n"
        "fit-2,8,4.1\n"
        "fit-3,14,7.1\n"
        "fit-4,20,10.1\n",
        encoding="utf-8",
    )
    calibration = tmp_path / "calibration.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
            str(fit),
            "--output",
            str(calibration),
            "--profile",
            "report-verifier-test",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    samples = tmp_path / "samples.csv"
    samples.write_text(
        fit.read_text(encoding="utf-8")
        + "holdout-1,30,15.1\n"
        + "holdout-2,50,25.1\n"
        + "holdout-3,100,50.1\n",
        encoding="utf-8",
    )
    report = tmp_path / "validation.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/validate_outdoor_depth_calibration.py"),
        str(samples),
        "--calibration",
        str(calibration),
        "--output",
        str(report),
    ]
    if strict:
        command.extend(
            [
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
            ]
        )
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return calibration, report


def test_report_verifier_binds_calibration_bytes_and_passed_gate(tmp_path: Path) -> None:
    calibration, report = _make_calibration_and_report(tmp_path, strict=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_outdoor_depth_calibration_report.py"),
            "--calibration",
            str(calibration),
            "--report",
            str(report),
            "--require-quality-gate",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "calibration_report_verified=true" in completed.stdout
    assert "quality_gate_status=passed" in completed.stdout


def test_report_verifier_rejects_advisory_report_when_strict(tmp_path: Path) -> None:
    calibration, report = _make_calibration_and_report(tmp_path, strict=False)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_outdoor_depth_calibration_report.py"),
            "--calibration",
            str(calibration),
            "--report",
            str(report),
            "--require-quality-gate",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "quality gate is not explicitly passed" in completed.stderr


def test_report_verifier_rejects_calibration_hash_drift(tmp_path: Path) -> None:
    calibration, report = _make_calibration_and_report(tmp_path, strict=True)
    document = json.loads(calibration.read_text(encoding="utf-8"))
    document["calibration"]["profile"] = "changed-after-report"
    calibration.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_outdoor_depth_calibration_report.py"),
            "--calibration",
            str(calibration),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "calibration hash does not match" in completed.stderr

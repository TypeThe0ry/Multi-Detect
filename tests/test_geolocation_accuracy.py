from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.geolocation_accuracy import (
    GeolocationAccuracySample,
    evaluate_geolocation_accuracy,
    verify_geolocation_accuracy_report,
    wgs84_distance_m,
)
from multidetect.target_geolocation import (
    target_geolocation_from_ned_offset,
    wgs84_forward_from_ned,
    wgs84_radii_of_curvature,
)

ROOT = Path(__file__).resolve().parents[1]


def test_wgs84_inverse_matches_target_geolocation_direct_projection() -> None:
    target = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=1.3,
        aircraft_longitude_deg=103.8,
        north_offset_m=100.0,
        east_offset_m=50.0,
        aircraft_horizontal_sigma_m=0.0,
        ground_range_ci95_m=None,
        ground_range_m=None,
        bearing_sigma_deg=None,
    )

    assert wgs84_distance_m(
        latitude1_deg=1.3,
        longitude1_deg=103.8,
        latitude2_deg=target.latitude_deg,
        longitude2_deg=target.longitude_deg,
    ) == pytest.approx(math.hypot(100.0, 50.0), abs=1e-6)


def test_wgs84_inverse_wraps_dateline_and_rejects_poles() -> None:
    assert wgs84_distance_m(
        latitude1_deg=0.0,
        longitude1_deg=179.9999,
        latitude2_deg=0.0,
        longitude2_deg=-179.9999,
    ) == pytest.approx(22.2639, abs=0.01)
    with pytest.raises(ValueError, match="strictly inside"):
        wgs84_distance_m(
            latitude1_deg=90.0,
            longitude1_deg=0.0,
            latitude2_deg=0.0,
            longitude2_deg=0.0,
        )


def _samples(*, sigma_m: float | None = 2.0) -> tuple[GeolocationAccuracySample, ...]:
    values: list[GeolocationAccuracySample] = []
    for index in range(10):
        truth_latitude = 1.3 + index * 0.00001
        truth_longitude = 103.8 + index * 0.00001
        _meridional_m, prime_vertical_m = wgs84_radii_of_curvature(truth_latitude)
        estimated_longitude = truth_longitude + math.degrees(
            1.0 / (prime_vertical_m * math.cos(math.radians(truth_latitude)))
        )
        values.append(
            GeolocationAccuracySample(
                sample_id=f"sample-{index:02d}",
                estimated_latitude_deg=truth_latitude,
                estimated_longitude_deg=estimated_longitude,
                ground_truth_latitude_deg=truth_latitude,
                ground_truth_longitude_deg=truth_longitude,
                reported_sigma_m=sigma_m,
                target_id="target-1",
                captured_at_s=float(index),
            )
        )
    return tuple(values)


def test_evaluate_geolocation_accuracy_reports_bias_and_sigma_coverage() -> None:
    report = evaluate_geolocation_accuracy(
        _samples(),
        minimum_samples=10,
        maximum_p95_m=2.0,
        minimum_one_sigma_coverage=0.9,
        minimum_two_sigma_coverage=0.99,
    )

    assert report["gate_passed"] is True
    measurement = report["measurement"]
    distance = report["distance_error_m"]
    assert measurement["sample_count"] == 10
    assert measurement["sigma_sample_count"] == 10
    assert measurement["one_sigma_coverage"] == pytest.approx(1.0)
    assert measurement["two_sigma_coverage"] == pytest.approx(1.0)
    assert distance["median_m"] == pytest.approx(1.0, abs=1e-5)
    assert distance["p95_m"] == pytest.approx(1.0, abs=1e-5)
    assert report["signed_east_error_m"]["median_m"] == pytest.approx(1.0, abs=1e-5)


def test_evaluate_geolocation_accuracy_reports_wgs84_error_ellipse() -> None:
    truth_latitude = 35.0
    truth_longitude = 139.0
    offsets = ((4.0, 1.0), (-4.0, -1.0), (4.0, -1.0), (-4.0, 1.0))
    samples = tuple(
        GeolocationAccuracySample(
            sample_id=f"ellipse-{index}",
            estimated_latitude_deg=estimated[0],
            estimated_longitude_deg=estimated[1],
            ground_truth_latitude_deg=truth_latitude,
            ground_truth_longitude_deg=truth_longitude,
        )
        for index, offset in enumerate(offsets)
        for estimated in (
            wgs84_forward_from_ned(
                latitude_deg=truth_latitude,
                longitude_deg=truth_longitude,
                north_offset_m=offset[0],
                east_offset_m=offset[1],
            ),
        )
    )

    report = evaluate_geolocation_accuracy(samples, minimum_samples=4)
    ellipse = report["error_ellipse"]

    assert ellipse["sample_count"] == 4
    assert ellipse["bias_north_m"] == pytest.approx(0.0, abs=1e-6)
    assert ellipse["bias_east_m"] == pytest.approx(0.0, abs=1e-6)
    assert ellipse["covariance_north_m2"] > ellipse["covariance_east_m2"]
    assert ellipse["major_1sigma_m"] > ellipse["minor_1sigma_m"]
    assert ellipse["orientation_deg"] == pytest.approx(0.0, abs=1e-4)
    assert ellipse["major_95_m"] == pytest.approx(
        ellipse["major_1sigma_m"] * math.sqrt(5.991464547107979),
        rel=1e-12,
    )
    assert ellipse["minor_95_m"] == pytest.approx(
        ellipse["minor_1sigma_m"] * math.sqrt(5.991464547107979),
        rel=1e-12,
    )
    assert verify_geolocation_accuracy_report(report)["passed"] is True


def test_error_ellipse_orientation_is_clockwise_from_true_north() -> None:
    truth_latitude = 35.0
    truth_longitude = 139.0
    angle_rad = math.radians(30.0)
    major_axis = (4.0 * math.cos(angle_rad), 4.0 * math.sin(angle_rad))
    minor_axis = (-1.0 * math.sin(angle_rad), 1.0 * math.cos(angle_rad))
    offsets = (
        major_axis,
        (-major_axis[0], -major_axis[1]),
        minor_axis,
        (-minor_axis[0], -minor_axis[1]),
    )
    samples = tuple(
        GeolocationAccuracySample(
            sample_id=f"rotated-{index}",
            estimated_latitude_deg=estimated[0],
            estimated_longitude_deg=estimated[1],
            ground_truth_latitude_deg=truth_latitude,
            ground_truth_longitude_deg=truth_longitude,
        )
        for index, offset in enumerate(offsets)
        for estimated in (
            wgs84_forward_from_ned(
                latitude_deg=truth_latitude,
                longitude_deg=truth_longitude,
                north_offset_m=offset[0],
                east_offset_m=offset[1],
            ),
        )
    )

    ellipse = evaluate_geolocation_accuracy(samples, minimum_samples=4)["error_ellipse"]

    assert ellipse["orientation_deg"] == pytest.approx(30.0, abs=1.0e-4)
    assert ellipse["major_1sigma_m"] > ellipse["minor_1sigma_m"]


def test_accuracy_report_verifier_rejects_tampered_error_ellipse() -> None:
    report = evaluate_geolocation_accuracy(_samples(), minimum_samples=10)
    report["error_ellipse"]["major_95_m"] = 999.0  # type: ignore[index]

    verification = verify_geolocation_accuracy_report(report)

    assert verification["passed"] is False
    assert any("error_ellipse" in reason for reason in verification["reasons"])


def test_accuracy_report_verifier_keeps_legacy_v1_reports_readable() -> None:
    report = evaluate_geolocation_accuracy(_samples(), minimum_samples=10)
    report.pop("error_ellipse")

    verification = verify_geolocation_accuracy_report(report)

    assert verification["passed"] is True
    assert verification["recomputed_gate_passed"] is True


def test_evaluate_geolocation_accuracy_keeps_accuracy_thresholds_fail_closed() -> None:
    report = evaluate_geolocation_accuracy(
        _samples(),
        minimum_samples=10,
        maximum_p95_m=0.5,
    )
    assert report["gate_passed"] is False
    assert any("p95_error" in reason for reason in report["reasons"])

    with pytest.raises(ValueError, match=r"within \[0, 1\]"):
        evaluate_geolocation_accuracy(_samples(), minimum_one_sigma_coverage=1.01)


def test_accuracy_report_verifier_recomputes_samples_and_exposes_threshold_state() -> None:
    report = evaluate_geolocation_accuracy(_samples(), minimum_samples=10, maximum_p95_m=2.0)
    verification = verify_geolocation_accuracy_report(report)
    assert verification["passed"] is True
    assert verification["gate_passed"] is True
    assert verification["recomputed_gate_passed"] is True
    assert verification["accuracy_threshold_defined"] is True

    report["distance_error_m"]["p95_m"] = 999.0  # type: ignore[index]
    tampered = verify_geolocation_accuracy_report(report)
    assert tampered["passed"] is False
    assert any("distance_error_m" in reason for reason in tampered["reasons"])


def test_accuracy_validator_cli_writes_json_report(tmp_path: Path) -> None:
    samples_path = tmp_path / "accuracy.csv"
    with samples_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "sample_id",
                "estimated_latitude_deg",
                "estimated_longitude_deg",
                "ground_truth_latitude_deg",
                "ground_truth_longitude_deg",
                "reported_sigma_m",
            ),
        )
        writer.writeheader()
        for sample in _samples():
            writer.writerow(
                {
                    "sample_id": sample.sample_id,
                    "estimated_latitude_deg": sample.estimated_latitude_deg,
                    "estimated_longitude_deg": sample.estimated_longitude_deg,
                    "ground_truth_latitude_deg": sample.ground_truth_latitude_deg,
                    "ground_truth_longitude_deg": sample.ground_truth_longitude_deg,
                    "reported_sigma_m": sample.reported_sigma_m,
                }
            )
    output = tmp_path / "accuracy.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_target_geolocation_accuracy.py"),
            str(samples_path),
            "--output",
            str(output),
            "--maximum-p95-m",
            "2",
            "--minimum-one-sigma-coverage",
            "0.9",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["evidence_type"] == "multidetect.target-geolocation-accuracy.v1"
    assert report["ground_truth_isolated"] is True
    assert report["gate_passed"] is True
    assert "gate_passed=True" in completed.stdout

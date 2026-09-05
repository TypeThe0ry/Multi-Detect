from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema

from multidetect.camera_mount_survey import (
    CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE,
    create_camera_mount_survey,
    verify_camera_mount_survey,
    write_camera_mount_survey,
)

ROOT = Path(__file__).resolve().parents[1]


def _calibration(tmp_path: Path, *, angular_sigma: float = 0.20) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "camera.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "calibration": {
                    "calibration_id": "camera-main-001",
                    "width_px": 1280,
                    "height_px": 960,
                    "fx_px": 540.0,
                    "fy_px": 540.0,
                    "cx_px": 640.0,
                    "cy_px": 480.0,
                    "mount_pitch_down_deg": 35.0,
                    "mount_yaw_right_deg": 0.0,
                    "mount_roll_clockwise_deg": 0.0,
                    "boresight_sigma_deg": angular_sigma,
                    "camera_offset_forward_m": 0.40,
                    "camera_offset_right_m": -0.08,
                    "camera_offset_down_m": 0.15,
                    "camera_offset_sigma_m": 0.02,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _samples() -> list[dict[str, object]]:
    perturbations = (-0.01, 0.01, 0.0, -0.005, 0.005)
    return [
        {
            "sample_id": f"measurement-{index + 1}",
            "pitch_down_deg": 35.0 + delta,
            "yaw_right_deg": delta,
            "roll_clockwise_deg": -delta,
            "offset_forward_m": 0.40 + delta / 10.0,
            "offset_right_m": -0.08 - delta / 10.0,
            "offset_down_m": 0.15 + delta / 20.0,
        }
        for index, delta in enumerate(perturbations)
    ]


def _attestations() -> dict[str, bool]:
    return {
        "aircraft_physically_present": True,
        "camera_measured_in_final_installed_configuration": True,
        "gnss_antenna_reference_point_marked": True,
        "aircraft_frd_axes_verified": True,
        "raw_measurements_retained": True,
        "simulated_or_nominal_values_excluded": True,
    }


def _report(
    tmp_path: Path,
    *,
    calibration: Path | None = None,
    surveyor: str = "surveyor-a",
    reviewer: str = "reviewer-b",
    attestations: dict[str, bool] | None = None,
) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "raw-measurements.csv"
    raw.write_text("physical field notes\n", encoding="utf-8")
    equipment_calibration = tmp_path / "equipment-calibration.pdf"
    equipment_calibration.write_bytes(b"equipment calibration certificate fixture")
    return create_camera_mount_survey(
        survey_id="mount-survey-001",
        aircraft_id="aircraft-001",
        camera_id="camera-main-001",
        gnss_antenna_id="gnss-main-001",
        method="digital inclinometer plus FRD datum tape survey",
        surveyor=surveyor,
        reviewer=reviewer,
        equipment_id="inclinometer-and-caliper-001",
        equipment_angular_accuracy_deg=0.05,
        equipment_linear_accuracy_m=0.002,
        equipment_calibration_path=equipment_calibration,
        calibration_path=calibration or _calibration(tmp_path),
        raw_measurements_path=raw,
        samples=_samples(),
        attestations=attestations or _attestations(),
        created_at_utc="2026-09-06T02:00:00Z",
    )


def test_physical_mount_survey_passes_and_reverifies(tmp_path: Path) -> None:
    report = _report(tmp_path)
    assert report["evidence_type"] == CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE
    assert report["gate_passed"] is True
    assert report["mount_survey_acceptance"] == "passed"
    assert report["hardware_acceptance"] == "not_claimed"
    assert report["release_eligible"] is False
    assert report["estimate"]["means"]["pitch_down_deg"] == 35.0

    output = tmp_path / "mount-survey.json"
    write_camera_mount_survey(output, report)
    verification = verify_camera_mount_survey(
        output,
        expected_release_label="0.3.0-pre.8",
        require_gate_passed=True,
    )
    assert verification == {"passed": True, "gate_passed": True, "reasons": []}


def test_mount_survey_fails_closed_on_identity_attestation_and_uncertainty(
    tmp_path: Path,
) -> None:
    same_person = _report(tmp_path / "same", surveyor="operator", reviewer="operator")
    assert same_person["gate_passed"] is False
    assert "surveyor_reviewer_distinct" in same_person["blocking_reasons"]

    incomplete = _attestations()
    incomplete["aircraft_physically_present"] = False
    no_aircraft = _report(tmp_path / "attestation", attestations=incomplete)
    assert no_aircraft["gate_passed"] is False
    assert "physical_attestations_complete" in no_aircraft["blocking_reasons"]

    understated = _report(
        tmp_path / "sigma",
        calibration=_calibration(tmp_path / "sigma", angular_sigma=0.01),
    )
    assert understated["gate_passed"] is False
    assert "calibration_angular_uncertainty_not_understated" in understated["blocking_reasons"]


def test_independent_verifier_rejects_changed_raw_measurements(tmp_path: Path) -> None:
    report = _report(tmp_path)
    output = tmp_path / "mount-survey.json"
    write_camera_mount_survey(output, report)
    raw_path = Path(report["inputs"]["raw_measurements"]["path"])
    raw_path.write_text("changed after review\n", encoding="utf-8")

    verification = verify_camera_mount_survey(output, require_gate_passed=True)
    assert verification["passed"] is False
    assert any("hash/size/path" in reason for reason in verification["reasons"])


def test_mount_survey_matches_strict_json_schema(tmp_path: Path) -> None:
    report = _report(tmp_path)
    schema = json.loads(
        (ROOT / "configs" / "schemas" / "camera-mount-survey.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        report
    )
    assert schema["properties"]["hardware_acceptance"]["const"] == "not_claimed"
    assert schema["properties"]["simulated_values_used"]["const"] is False


def test_mount_survey_cli_is_available_without_installed_package() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "camera_mount_survey.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "create" in completed.stdout
    assert "verify" in completed.stdout

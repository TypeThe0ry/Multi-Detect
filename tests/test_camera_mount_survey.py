from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from multidetect.camera_mount_survey import (
    CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE,
    CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE,
    check_camera_mount_measurements,
    create_camera_mount_equipment_calibration_record,
    create_camera_mount_survey,
    verify_camera_mount_survey,
    write_camera_mount_equipment_calibration_record,
    write_camera_mount_measurement_template,
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


def _write_raw_measurements(path: Path) -> None:
    fields = (
        "sample_id",
        "pitch_down_deg",
        "yaw_right_deg",
        "roll_clockwise_deg",
        "offset_forward_m",
        "offset_right_m",
        "offset_down_m",
    )
    rows = [",".join(fields)]
    rows.extend(",".join(str(sample[field]) for field in fields) for sample in _samples())
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


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
    samples: list[dict[str, object]] | None = None,
    equipment_record_id: str = "inclinometer-and-caliper-001",
    equipment_valid_until: str = "2027-01-01T00:00:00Z",
) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "raw-measurements.csv"
    _write_raw_measurements(raw)
    certificate = tmp_path / "equipment-certificate.pdf"
    certificate.write_bytes(b"equipment calibration certificate fixture")
    equipment_calibration = tmp_path / "equipment-calibration.json"
    equipment_record = create_camera_mount_equipment_calibration_record(
        equipment_id=equipment_record_id,
        issuer="calibration-lab-a",
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc=equipment_valid_until,
        angular_accuracy_deg=0.05,
        linear_accuracy_m=0.002,
        certificate_path=certificate,
    )
    write_camera_mount_equipment_calibration_record(equipment_calibration, equipment_record)
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
        samples=_samples() if samples is None else samples,
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

    expired = _report(
        tmp_path / "expired",
        equipment_valid_until="2026-08-01T00:00:00Z",
    )
    assert expired["gate_passed"] is False
    assert "equipment_calibration_valid" in expired["blocking_reasons"]

    wrong_equipment = _report(
        tmp_path / "wrong-equipment",
        equipment_record_id="different-equipment",
    )
    assert wrong_equipment["gate_passed"] is False
    assert "equipment_identity_matches_calibration" in wrong_equipment["blocking_reasons"]


def test_independent_verifier_rejects_changed_raw_measurements(tmp_path: Path) -> None:
    report = _report(tmp_path)
    output = tmp_path / "mount-survey.json"
    write_camera_mount_survey(output, report)
    raw_path = Path(report["inputs"]["raw_measurements"]["path"])
    raw_path.write_text("changed after review\n", encoding="utf-8")

    verification = verify_camera_mount_survey(output, require_gate_passed=True)
    assert verification["passed"] is False
    assert any("hash/size/path" in reason for reason in verification["reasons"])


def test_independent_verifier_rejects_changed_equipment_certificate(tmp_path: Path) -> None:
    report = _report(tmp_path)
    output = tmp_path / "mount-survey.json"
    write_camera_mount_survey(output, report)
    record_path = Path(report["inputs"]["equipment_calibration"]["path"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    Path(record["certificate"]["path"]).write_bytes(b"changed certificate")

    verification = verify_camera_mount_survey(output, require_gate_passed=True)
    assert verification["passed"] is False
    assert any("certificate hash/size/path" in reason for reason in verification["reasons"])


def test_declared_samples_cannot_diverge_from_hash_bound_raw_csv(tmp_path: Path) -> None:
    changed = _samples()
    changed[0] = {**changed[0], "pitch_down_deg": 34.5}
    report = _report(tmp_path, samples=changed)
    assert report["gate_passed"] is False
    assert report["checks"]["samples_match_raw_measurements"] is False
    assert "samples_match_raw_measurements" in report["blocking_reasons"]


def test_measurement_template_and_preflight_form_a_strict_field_workflow(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw-measurements.csv"
    output = write_camera_mount_measurement_template(raw, sample_count=5)
    assert output == raw.resolve()
    lines = raw.read_text(encoding="utf-8").splitlines()
    assert lines[0] == (
        "sample_id,pitch_down_deg,yaw_right_deg,roll_clockwise_deg,"
        "offset_forward_m,offset_right_m,offset_down_m"
    )
    assert lines[1] == "measurement-001,,,,,,"
    assert len(lines) == 6
    with pytest.raises(FileExistsError):
        write_camera_mount_measurement_template(raw)

    blank = check_camera_mount_measurements(raw)
    assert blank["gate_passed"] is False
    assert blank["release_eligible"] is False
    assert blank["hardware_acceptance"] == "not_claimed"

    _write_raw_measurements(raw)
    completed = check_camera_mount_measurements(raw)
    assert completed["evidence_type"] == CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE
    assert completed["sample_count"] == 5
    assert completed["sample_ids_unique"] is True
    assert completed["blocking_reasons"] == []
    assert completed["gate_passed"] is True
    schema = json.loads(
        (ROOT / "configs" / "schemas" / "camera-mount-measurement-preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema).validate(completed)


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
    equipment_schema = json.loads(
        (ROOT / "configs" / "schemas" / "camera-mount-equipment-calibration.schema.json").read_text(
            encoding="utf-8"
        )
    )
    equipment_record = json.loads(
        Path(report["inputs"]["equipment_calibration"]["path"]).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(
        equipment_schema, format_checker=jsonschema.FormatChecker()
    ).validate(equipment_record)


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
    assert "template" in completed.stdout
    assert "validate-raw" in completed.stdout
    assert "equipment-record" in completed.stdout


def test_measurement_template_and_preflight_cli_round_trip(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "camera_mount_survey.py"
    raw = tmp_path / "field-measurements.csv"
    generated = subprocess.run(
        [sys.executable, str(script), "template", "--output", str(raw)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert generated.returncode == 0, generated.stderr
    assert raw.is_file()
    _write_raw_measurements(raw)
    validated = subprocess.run(
        [sys.executable, str(script), "validate-raw", str(raw)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert validated.returncode == 0, validated.stderr
    result = json.loads(validated.stdout)
    assert result["sample_count"] == 5
    assert result["gate_passed"] is True


def test_equipment_calibration_record_cli_binds_certificate(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "camera_mount_survey.py"
    certificate = tmp_path / "certificate.pdf"
    certificate.write_bytes(b"issued calibration certificate")
    record = tmp_path / "equipment-calibration.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "equipment-record",
            "--equipment-id",
            "equipment-001",
            "--issuer",
            "calibration-lab-a",
            "--valid-from-utc",
            "2026-01-01T00:00:00Z",
            "--valid-until-utc",
            "2027-01-01T00:00:00Z",
            "--angular-accuracy-deg",
            "0.05",
            "--linear-accuracy-m",
            "0.002",
            "--certificate",
            str(certificate),
            "--output",
            str(record),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(record.read_text(encoding="utf-8"))
    assert document["equipment_id"] == "equipment-001"
    assert document["certificate"]["path"] == str(certificate.resolve())
    assert len(document["certificate"]["sha256"]) == 64

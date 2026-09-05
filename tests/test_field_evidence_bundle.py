from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import multidetect.field_evidence_bundle as bundle_module
import multidetect.field_session_capture as session_module
from multidetect.backends.raw_replay import (
    RawReplayFrame,
    RawReplayImageReference,
    RawReplayTelemetrySample,
    RawSensorReplayRecording,
)
from multidetect.camera_mount_survey import (
    create_camera_mount_survey,
    write_camera_mount_survey,
)
from multidetect.domain import VehicleTelemetry
from multidetect.field_session_capture import capture_field_session
from multidetect.geolocation_survey import build_accuracy_report_from_target_event_replay
from multidetect.target_event_replay import (
    TargetEventInput,
    TargetEventReplayBuilder,
    TargetGpsEvidence,
    TargetMediaInput,
    TargetRangeEvidence,
)

ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _audit_record(state: str, timestamp_s: float) -> dict[str, object]:
    if state == "CANCEL":
        return {
            "sequence": int(timestamp_s * 10) + 1,
            "session_id": SESSION_ID,
            "timestamp_s": timestamp_s,
            "event_type": "target.cancelled",
            "details": {"target_id": "target-a", "cancelled": True},
        }
    return {
        "sequence": int(timestamp_s * 10) + 1,
        "session_id": SESSION_ID,
        "timestamp_s": timestamp_s,
        "event_type": "field.state_changed",
        "details": {
            "state": state,
            "target_id": "target-a",
            "gps": {
                "position_healthy": True,
                "latitude_deg": 1.3001,
                "longitude_deg": 103.8001,
                "fix_type": 3,
                "satellites_visible": 12,
                "h_acc_m": 1.2,
                "v_acc_m": 2.4,
                "observed_at_s": timestamp_s,
            },
            "range": {
                "validity": "VALID",
                "distance_m": 12.5,
                "source_age_s": 0.1,
                "data_freshness_s": 0.1,
            },
        },
    }


def _telemetry_sample(observed_at_s: float) -> RawReplayTelemetrySample:
    return RawReplayTelemetrySample(
        observed_at_s=observed_at_s,
        telemetry=VehicleTelemetry(
            altitude_agl_m=20.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=1.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=False,
            release_zone_clear=None,
            latitude_deg=1.3001,
            longitude_deg=103.8001,
            gps_fix_type=3,
            satellites_visible=12,
            gps_horizontal_accuracy_m=1.2,
            gps_vertical_accuracy_m=2.4,
            gps_observed_at_s=observed_at_s,
            attitude_observed_at_s=observed_at_s,
            position_observed_at_s=observed_at_s,
            altitude_observed_at_s=observed_at_s,
            armed=False,
            flight_mode="HOLD",
        ),
    )


def _raw_recording(tmp_path: Path) -> tuple[Path, RawReplayTelemetrySample]:
    root = tmp_path / "raw-recording"
    root.mkdir()
    image = root / "frame.png"
    image.write_bytes(b"synthetic-image")
    reference = RawReplayImageReference(
        relative_path="frame.png",
        resolved_path=image,
        sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        byte_count=image.stat().st_size,
        width=2,
        height=2,
        encoding="png",
    )
    frame = RawReplayFrame(frame_id="frame-0", captured_at_s=1.0, image=reference)
    sample = _telemetry_sample(0.0)
    lines = "".join(
        json.dumps(item, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for item in (sample.to_mapping(), frame.to_mapping())
    )
    events = root / "events.jsonl"
    events.write_text(lines, encoding="utf-8", newline="\n")
    event_hash = hashlib.sha256(events.read_bytes()).hexdigest()
    recording = RawSensorReplayRecording.from_jsonl(
        events,
        recording_id="recording-a",
        airframe_profile_hash="aa" * 32,
        started_at_utc=datetime(2026, 9, 3, tzinfo=timezone.utc),
        expected_sha256=event_hash,
        expected_byte_count=events.stat().st_size,
    )
    manifest = root / "recording.manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "recording_id": recording.recording_id,
            "airframe_profile_hash": recording.airframe_profile_hash,
            "started_at_utc": recording.started_at_utc.isoformat().replace("+00:00", "Z"),
            "recording_hash": recording.recording_hash,
            "events": {
                "path": "events.jsonl",
                "sha256": recording.event_stream_sha256,
                "bytes": recording.event_stream_bytes,
            },
        },
    )
    return manifest, sample


def _camera_calibration_fixture(tmp_path: Path) -> Path:
    calibration = tmp_path / "camera-calibration.json"
    if not calibration.exists():
        _write_json(
            calibration,
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
                    "boresight_sigma_deg": 0.20,
                    "camera_offset_forward_m": 0.40,
                    "camera_offset_right_m": -0.08,
                    "camera_offset_down_m": 0.15,
                    "camera_offset_sigma_m": 0.02,
                },
            },
        )
    return calibration


def _target_manifest(
    tmp_path: Path,
    *,
    camera_calibration: Path | None = None,
    root_name: str = "target-events",
) -> Path:
    root = tmp_path / root_name
    builder = TargetEventReplayBuilder(
        root,
        session_id=SESSION_ID,
        bundle_id="bundle-a",
        camera_calibration_path=camera_calibration or _camera_calibration_fixture(tmp_path),
    )
    builder.append(
        TargetEventInput(
            event_id="event-0",
            frame_id="frame-0",
            target_id="target-a",
            target_label="person",
            captured_at_s=1.0,
            gps=TargetGpsEvidence(
                status="qualified",
                latitude_deg=1.3001,
                longitude_deg=103.8001,
                horizontal_sigma_m=2.0,
                fix_type=3,
                satellites_visible=12,
                horizontal_accuracy_m=1.2,
                vertical_accuracy_m=2.4,
                observed_at_s=1.0,
            ),
            ranging=TargetRangeEvidence(
                validity="VALID",
                range_m=12.5,
                relative_bearing_deg=0.0,
                range_kind="ground",
            ),
            video=TargetMediaInput(kind="video_frame", payload=b"jpeg", encoding="jpeg"),
        )
    )
    builder.finalize()
    return root / "manifest.json"


def _link_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    path = tmp_path / "field-link.json"
    path.write_bytes(b"link-fixture\n")
    summary = {"all_passed": True, "hardware_link_ready": True}
    result: dict[str, object] = {
        "report_path": str(path),
        "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "report_bytes": path.stat().st_size,
        "hardware_link_ready": True,
        "summary": summary,
        "checks": {},
        "reasons": [],
        "gate_passed": True,
    }
    return path, result


def _gnss_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "gnss.json"
    _write_json(
        path,
        {
            "records": [{"kind": "gps_raw", "qualified": True}],
            "summary": {"gps_raw_qualified_samples": 1},
        },
    )
    return path


def _camera_mount_fixture(tmp_path: Path) -> Path:
    calibration = _camera_calibration_fixture(tmp_path)
    raw = tmp_path / "camera-mount-raw.csv"
    certificate = tmp_path / "equipment-certificate.pdf"
    certificate.write_bytes(b"equipment calibration certificate fixture")
    equipment_calibration = tmp_path / "equipment-calibration.json"
    _write_json(
        equipment_calibration,
        {
            "schema_version": 1,
            "equipment_id": "inclinometer-and-caliper-001",
            "issuer": "calibration-lab-a",
            "valid_from_utc": "2026-01-01T00:00:00Z",
            "valid_until_utc": "2027-01-01T00:00:00Z",
            "angular_accuracy_deg": 0.05,
            "linear_accuracy_m": 0.002,
            "certificate": {
                "path": str(certificate.resolve()),
                "bytes": certificate.stat().st_size,
                "sha256": hashlib.sha256(certificate.read_bytes()).hexdigest(),
            },
        },
    )
    perturbations = (-0.01, 0.01, 0.0, -0.005, 0.005)
    samples = [
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
    fields = (
        "sample_id",
        "pitch_down_deg",
        "yaw_right_deg",
        "roll_clockwise_deg",
        "offset_forward_m",
        "offset_right_m",
        "offset_down_m",
    )
    raw.write_text(
        ",".join(fields)
        + "\n"
        + "\n".join(",".join(str(sample[field]) for field in fields) for sample in samples)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    report = create_camera_mount_survey(
        survey_id="mount-survey-001",
        aircraft_id="aircraft-001",
        camera_id="camera-main-001",
        gnss_antenna_id="gnss-main-001",
        method="physical inclinometer and FRD datum tape survey",
        surveyor="surveyor-a",
        reviewer="reviewer-b",
        equipment_id="inclinometer-and-caliper-001",
        equipment_angular_accuracy_deg=0.05,
        equipment_linear_accuracy_m=0.002,
        equipment_calibration_path=equipment_calibration,
        calibration_path=calibration,
        raw_measurements_path=raw,
        samples=samples,
        attestations={
            "aircraft_physically_present": True,
            "camera_measured_in_final_installed_configuration": True,
            "gnss_antenna_reference_point_marked": True,
            "aircraft_frd_axes_verified": True,
            "raw_measurements_retained": True,
            "simulated_or_nominal_values_excluded": True,
        },
        created_at_utc="2026-09-06T02:00:00Z",
    )
    path = tmp_path / "camera-mount-survey.json"
    write_camera_mount_survey(path, report)
    return path


def _session_fixture(
    tmp_path: Path,
    link_path: Path,
    link_result: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    source = tmp_path / "field.audit.jsonl"
    output = tmp_path / "field.session.jsonl"
    records = [
        _audit_record(state, timestamp)
        for state, timestamp in (
            ("DET", 0.0),
            ("TRK", 0.1),
            ("LCK", 0.2),
            ("TGT", 0.3),
            ("CANCEL", 0.4),
            ("TRK", 0.5),
        )
    ]
    source.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(
        session_module,
        "verify_field_link_report",
        lambda *_args, **_kwargs: link_result,
    )
    report = capture_field_session(
        source,
        output,
        link_preflight=link_path,
        require_link_preflight=True,
        require_target_state_evidence=True,
    )
    path = tmp_path / "field-session.capture.json"
    _write_json(path, report)
    return path


def test_missing_inputs_are_reported_without_exception() -> None:
    report = bundle_module.check_field_evidence_bundle()
    assert report["gate_passed"] is False
    assert report["release_eligible"] is False
    assert report["hardware_acceptance"] == "not_claimed"
    assert report["checks"]["all_inputs_present"] is False
    assert set(report["strata"]) == set(bundle_module.FIELD_EVIDENCE_STRATA)
    assert all(item["gate_passed"] is False for item in report["strata"].values())
    assert report["next_actions"]


def test_complete_synthetic_bundle_cross_binds_all_strata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_path, link_result = _link_fixture(tmp_path)
    monkeypatch.setattr(
        bundle_module,
        "verify_field_link_report",
        lambda *_args, **_kwargs: link_result,
    )
    session_path = _session_fixture(tmp_path, link_path, link_result, monkeypatch)
    gnss_path = _gnss_fixture(tmp_path)
    camera_mount_path = _camera_mount_fixture(tmp_path)
    monkeypatch.setattr(
        bundle_module,
        "verify_gnss_capture_report",
        lambda *_args, **_kwargs: {"passed": True, "gate_passed": True, "reasons": []},
    )
    target_path = _target_manifest(tmp_path)
    recording_path, sample = _raw_recording(tmp_path)
    sidecar_path = tmp_path / "telemetry.jsonl"
    sidecar_path.write_text(
        json.dumps(sample.to_mapping(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    accuracy_result = build_accuracy_report_from_target_event_replay(
        target_path,
        [
            {
                "sample_id": "event-0",
                "ground_truth_latitude_deg": 1.3001,
                "ground_truth_longitude_deg": 103.8001,
                "target_id": "target-a",
            }
        ],
        minimum_samples=1,
    )
    accuracy_path = tmp_path / "accuracy.json"
    _write_json(accuracy_path, accuracy_result.report)

    report = bundle_module.check_field_evidence_bundle(
        field_session_report=session_path,
        gnss_capture_report=gnss_path,
        target_event_manifest=target_path,
        recording_manifest=recording_path,
        telemetry_sidecar=sidecar_path,
        link_preflight=link_path,
        camera_mount_survey=camera_mount_path,
        accuracy_report=accuracy_path,
    )

    assert report["gate_passed"] is True
    assert report["release_eligible"] is False
    assert report["contains_flight_control"] is False
    assert report["checks"]["all_strata_passed"] is True
    assert report["checks"]["cross_bindings_passed"] is True
    assert all(item["gate_passed"] is True for item in report["strata"].values())
    assert report["bindings"]["target_event_ids"] == ["event-0"]
    assert report["bindings"]["recording_id"] == "recording-a"
    assert report["bindings"]["camera_mount_survey_id"] == "mount-survey-001"
    assert report["bindings"]["camera_mount_camera_id"] == "camera-main-001"
    assert report["bindings"]["target_event_camera_calibration_id"] == "camera-main-001"
    assert (
        report["bindings"]["target_event_camera_calibration_sha256"]
        == report["bindings"]["camera_calibration_sha256"]
    )

    alternate_calibration = tmp_path / "alternate-camera-calibration.json"
    alternate_document = json.loads(
        _camera_calibration_fixture(tmp_path).read_text(encoding="utf-8")
    )
    alternate_document["calibration"]["fx_px"] = 541.0
    _write_json(alternate_calibration, alternate_document)
    alternate_target_path = _target_manifest(
        tmp_path,
        camera_calibration=alternate_calibration,
        root_name="target-events-alternate-calibration",
    )
    alternate_accuracy = build_accuracy_report_from_target_event_replay(
        alternate_target_path,
        [
            {
                "sample_id": "event-0",
                "ground_truth_latitude_deg": 1.3001,
                "ground_truth_longitude_deg": 103.8001,
                "target_id": "target-a",
            }
        ],
        minimum_samples=1,
    )
    alternate_accuracy_path = tmp_path / "accuracy-alternate-calibration.json"
    _write_json(alternate_accuracy_path, alternate_accuracy.report)
    mismatch_report = bundle_module.check_field_evidence_bundle(
        field_session_report=session_path,
        gnss_capture_report=gnss_path,
        target_event_manifest=alternate_target_path,
        recording_manifest=recording_path,
        telemetry_sidecar=sidecar_path,
        link_preflight=link_path,
        camera_mount_survey=camera_mount_path,
        accuracy_report=alternate_accuracy_path,
    )
    assert mismatch_report["strata"]["target_event"]["gate_passed"] is True
    assert mismatch_report["checks"]["cross_bindings_passed"] is False
    assert any(
        "target-event camera calibration hash does not match mount survey" in reason
        for reason in mismatch_report["blocking_reasons"]
    )


def test_tampered_sidecar_closes_only_media_timeline_and_overall_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_path, link_result = _link_fixture(tmp_path)
    monkeypatch.setattr(
        bundle_module,
        "verify_field_link_report",
        lambda *_args, **_kwargs: link_result,
    )
    session_path = _session_fixture(tmp_path, link_path, link_result, monkeypatch)
    gnss_path = _gnss_fixture(tmp_path)
    camera_mount_path = _camera_mount_fixture(tmp_path)
    monkeypatch.setattr(
        bundle_module,
        "verify_gnss_capture_report",
        lambda *_args, **_kwargs: {"passed": True, "gate_passed": True, "reasons": []},
    )
    target_path = _target_manifest(tmp_path)
    recording_path, sample = _raw_recording(tmp_path)
    sidecar_path = tmp_path / "telemetry.jsonl"
    altered = sample.to_mapping()
    altered["observed_at_s"] = 0.1
    sidecar_path.write_text(
        json.dumps(altered, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    accuracy_result = build_accuracy_report_from_target_event_replay(
        target_path,
        [
            {
                "sample_id": "event-0",
                "ground_truth_latitude_deg": 1.3001,
                "ground_truth_longitude_deg": 103.8001,
                "target_id": "target-a",
            }
        ],
        minimum_samples=1,
    )
    accuracy_path = tmp_path / "accuracy.json"
    _write_json(accuracy_path, accuracy_result.report)
    report = bundle_module.check_field_evidence_bundle(
        field_session_report=session_path,
        gnss_capture_report=gnss_path,
        target_event_manifest=target_path,
        recording_manifest=recording_path,
        telemetry_sidecar=sidecar_path,
        link_preflight=link_path,
        camera_mount_survey=camera_mount_path,
        accuracy_report=accuracy_path,
    )
    assert report["strata"]["media_timeline"]["gate_passed"] is False
    assert report["gate_passed"] is False
    assert any(
        "differs from recording" in reason
        for reason in report["strata"]["media_timeline"]["reasons"]
    )


def test_cli_report_only_writes_a_new_report_and_help_is_available() -> None:
    script = ROOT / "scripts" / "check_field_evidence_bundle.py"
    help_result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0
    assert "--allow-sim-domain" in help_result.stdout
    assert "--report-only" in help_result.stdout


def test_schema_declares_fail_closed_release_boundary() -> None:
    schema = json.loads(
        (ROOT / "configs" / "schemas" / "field-evidence-bundle.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["evidence_type"]["const"] == (
        "multidetect.field-evidence-bundle.v1"
    )
    assert schema["properties"]["release_eligible"]["const"] is False
    assert schema["properties"]["contains_flight_control"]["const"] is False
    assert schema["properties"]["hardware_acceptance"]["const"] == "not_claimed"
    assert set(schema["$defs"]["strata"]["required"]) == set(bundle_module.FIELD_EVIDENCE_STRATA)


def test_writer_is_atomic_and_refuses_replacement(tmp_path: Path) -> None:
    report = bundle_module.check_field_evidence_bundle()
    destination = tmp_path / "field-evidence.json"
    written = bundle_module.write_field_evidence_bundle(destination, report)
    assert written == destination.resolve()
    assert json.loads(destination.read_text(encoding="utf-8")) == report
    with pytest.raises(FileExistsError):
        bundle_module.write_field_evidence_bundle(destination, report)


def test_serialized_report_is_recomputed_and_summary_tampering_is_detected(
    tmp_path: Path,
) -> None:
    report = bundle_module.check_field_evidence_bundle()
    destination = tmp_path / "field-evidence.json"
    bundle_module.write_field_evidence_bundle(destination, report)

    verified = bundle_module.verify_field_evidence_bundle_report(
        destination,
        expected_release_label="0.3.0-pre.8",
        require_real_domain=True,
    )
    assert verified["passed"] is True
    assert verified["gate_passed"] is False

    tampered = json.loads(destination.read_text(encoding="utf-8"))
    tampered["checks"]["all_inputs_present"] = True
    destination.write_text(
        json.dumps(tampered, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    changed = bundle_module.verify_field_evidence_bundle_report(
        destination,
        expected_release_label="0.3.0-pre.8",
        require_real_domain=True,
    )
    assert changed["passed"] is False
    assert any(
        "checks" in reason or "does not match re-evaluation" in reason
        for reason in changed["reasons"]
    )


def test_serialized_pending_paths_recompute_without_false_tamper_failure(
    tmp_path: Path,
) -> None:
    """Bring-up reports may bind future artifact paths before they exist."""

    missing = tmp_path / "future" / "gnss.json"
    report = bundle_module.check_field_evidence_bundle(
        gnss_capture_report=missing,
        maximum_telemetry_age_s=1.0,
        require_real_domain=True,
    )
    destination = tmp_path / "field-evidence-pending.json"
    bundle_module.write_field_evidence_bundle(destination, report)

    verified = bundle_module.verify_field_evidence_bundle_report(
        destination,
        expected_release_label="0.3.0-pre.8",
        require_real_domain=True,
    )
    assert verified["passed"] is True
    assert verified["gate_passed"] is False
    assert verified["reasons"] == []

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from base64 import b64decode
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy import AutonomyContractError
from multidetect.backends import RawSensorReplayRecording
from multidetect.deployment_recording_attestation import (
    ATTESTATION_NAME,
    derive_deployment_recording_time_window,
    verify_deployment_recording_attestation,
    write_deployment_recording_attestation,
)

ROOT = Path(__file__).resolve().parents[1]
PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
    "AAAACklEQVQIHWMAAgAABAABDTukuQAAAABJRU5ErkJggg=="
)


def _recording(
    root: Path,
    *,
    started_at_utc: str = "2026-09-06T01:00:00Z",
) -> Path:
    root.mkdir()
    (root / "frame.png").write_bytes(PNG)
    events = [
        {
            "schema_version": 1,
            "kind": "telemetry",
            "observed_at_s": 1.0,
            "telemetry": {
                "altitude_agl_m": 0.0,
                "roll_deg": 0.0,
                "pitch_deg": 0.0,
                "ground_speed_mps": 0.0,
                "in_allowed_zone": None,
                "geofence_healthy": None,
                "position_healthy": False,
                "link_healthy": True,
                "flight_mode_allows_deploy": False,
                "release_zone_clear": None,
            },
        },
        {
            "schema_version": 1,
            "kind": "frame",
            "frame_id": "field-frame-0001",
            "captured_at_s": 1.0,
            "image": {
                "path": "frame.png",
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "bytes": len(PNG),
                "width": 1,
                "height": 1,
                "encoding": "png",
            },
        },
    ]
    payload = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for event in events
    )
    (root / "events.jsonl").write_bytes(payload)
    recording = RawSensorReplayRecording.from_jsonl(
        root / "events.jsonl",
        recording_id="physical-day-001",
        airframe_profile_hash="b" * 64,
        started_at_utc=started_at_utc,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_byte_count=len(payload),
    )
    (root / "recording.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recording_id": recording.recording_id,
                "airframe_profile_hash": recording.airframe_profile_hash,
                "started_at_utc": started_at_utc,
                "recording_hash": recording.recording_hash,
                "events": {
                    "path": "events.jsonl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return root


def _attest(root: Path) -> dict[str, object]:
    return write_deployment_recording_attestation(
        root,
        stratum="day",
        captured_by="field-operator-a",
        capture_device_id="jetson-csi-main",
        capture_site="survey-site-01",
        source_type="local_camera",
        started_at_utc="2026-09-06T01:00:00Z",
        ended_at_utc="2026-09-06T01:05:00Z",
    )


def test_attestation_binds_recording_and_validates_schema(tmp_path: Path) -> None:
    recording = _recording(tmp_path / "recording")
    report = _attest(recording)

    assert report["stratum"] == "day"
    assert report["capture_origin"] == "physical_field"
    assert report["release_ready"] is False
    document = json.loads((recording / ATTESTATION_NAME).read_text(encoding="utf-8"))
    schema = json.loads(
        (ROOT / "configs/schemas/deployment-recording-source-attestation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    assert verify_deployment_recording_attestation(
        recording, expected_stratum="day"
    ) == report


def test_recording_time_window_is_derived_without_attesting_provenance(
    tmp_path: Path,
) -> None:
    recording = _recording(tmp_path / "recording")

    window = derive_deployment_recording_time_window(recording)

    assert window == {
        "event": "deployment_recording_time_window_derived",
        "started_at_utc": "2026-09-06T01:00:00Z",
        "minimum_ended_at_utc": "2026-09-06T01:00:01Z",
        "latest_event_at_s": 1.0,
        "provenance_attested": False,
        "operator_confirmation_required": True,
    }
    assert not (recording / ATTESTATION_NAME).exists()


def test_attestation_rejects_wrong_time_stratum_tamper_and_overwrite(
    tmp_path: Path,
) -> None:
    recording = _recording(tmp_path / "recording")
    with pytest.raises(AutonomyContractError, match="must match recording manifest"):
        write_deployment_recording_attestation(
            recording,
            stratum="day",
            captured_by="operator",
            capture_device_id="camera",
            capture_site="site",
            source_type="local_camera",
            started_at_utc="2026-09-06T01:00:01Z",
            ended_at_utc="2026-09-06T01:05:00Z",
        )
    with pytest.raises(AutonomyContractError, match="must cover the recording"):
        write_deployment_recording_attestation(
            recording,
            stratum="day",
            captured_by="operator",
            capture_device_id="camera",
            capture_site="site",
            source_type="local_camera",
            started_at_utc="2026-09-06T01:00:00Z",
            ended_at_utc="2026-09-06T01:00:00.5Z",
        )

    _attest(recording)
    with pytest.raises(AutonomyContractError, match="already exists"):
        _attest(recording)
    with pytest.raises(AutonomyContractError, match="stratum does not match"):
        verify_deployment_recording_attestation(recording, expected_stratum="night")

    path = recording / ATTESTATION_NAME
    document = json.loads(path.read_text(encoding="utf-8"))
    document["claims"]["synthetic"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="claims are invalid"):
        verify_deployment_recording_attestation(recording)


def test_attestation_rejects_placeholder_video_time_anchor(tmp_path: Path) -> None:
    recording = _recording(
        tmp_path / "recording",
        started_at_utc="1970-01-01T00:00:00Z",
    )

    with pytest.raises(AutonomyContractError, match="placeholder 1970 UTC start"):
        write_deployment_recording_attestation(
            recording,
            stratum="day",
            captured_by="operator",
            capture_device_id="camera",
            capture_site="site",
            source_type="local_camera",
            started_at_utc="1970-01-01T00:00:00Z",
            ended_at_utc="1970-01-01T00:00:01Z",
        )
    assert not (recording / ATTESTATION_NAME).exists()


def test_attestation_cli_create_and_verify(tmp_path: Path) -> None:
    recording = _recording(tmp_path / "recording")
    create = subprocess.run(
        [
            sys.executable,
            "scripts/attest_deployment_recording.py",
            str(recording),
            "--stratum",
            "day",
            "--captured-by",
            "field-operator-a",
            "--capture-device-id",
            "jetson-csi-main",
            "--capture-site",
            "survey-site-01",
            "--source-type",
            "local_camera",
            "--started-at-utc",
            "2026-09-06T01:00:00Z",
            "--ended-at-utc",
            "2026-09-06T01:05:00Z",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert create.returncode == 0, create.stderr
    assert json.loads(create.stdout)["canonical_hash"]

    verify = subprocess.run(
        [
            sys.executable,
            "scripts/attest_deployment_recording.py",
            str(recording),
            "--verify",
            "--stratum",
            "day",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["recording_hash"]


def test_attestation_cli_derives_times_but_still_requires_operator_metadata(
    tmp_path: Path,
) -> None:
    recording = _recording(tmp_path / "recording")
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/attest_deployment_recording.py",
            str(recording),
            "--derive-times-from-recording",
            "--stratum",
            "day",
            "--captured-by",
            "field-operator-a",
            "--capture-device-id",
            "jetson-csi-main",
            "--capture-site",
            "survey-site-01",
            "--source-type",
            "rtsp_camera",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads((recording / ATTESTATION_NAME).read_text(encoding="utf-8"))
    assert document["started_at_utc"] == "2026-09-06T01:00:00Z"
    assert document["ended_at_utc"] == "2026-09-06T01:00:01Z"
    assert document["source_type"] == "rtsp_camera"

    missing_metadata = _recording(tmp_path / "missing-metadata")
    rejected = subprocess.run(
        [
            sys.executable,
            "scripts/attest_deployment_recording.py",
            str(missing_metadata),
            "--derive-times-from-recording",
            "--stratum",
            "day",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "--captured-by" in rejected.stderr
    assert not (missing_metadata / ATTESTATION_NAME).exists()

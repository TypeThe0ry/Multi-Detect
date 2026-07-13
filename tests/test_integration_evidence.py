from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from multidetect.cli import main
from multidetect.integration_evidence import check_integration_evidence_bundle

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _record(path: Path) -> dict[str, str]:
    return {
        "artifact": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _software_artifact() -> dict:
    return {
        "event": "combined_flight_stack_software_hil_passed",
        "hardware_observed": False,
        "simulation_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "production_approved": False,
        "pixhawk_path": {"messages_transmitted_by_jetson": 0},
    }


def _hardware_artifact(event: str) -> dict:
    return {
        "event": event,
        "hardware_observed": True,
        "simulation_only": False,
        "passed": True,
        "observed_at_utc": NOW.isoformat(),
    }


def _bundle(path: Path, records: dict[str, dict[str, str]]) -> Path:
    return _write_json(
        path,
        {
            "schema_version": 1,
            "bundle_id": "test-bundle",
            "aircraft_id": "aircraft-1",
            "records": records,
        },
    )


def test_software_hil_profile_accepts_hashed_software_evidence(tmp_path: Path) -> None:
    artifact = _write_json(tmp_path / "software.json", _software_artifact())
    bundle = _bundle(tmp_path / "bundle.json", {"software_hil": _record(artifact)})

    result = check_integration_evidence_bundle(bundle, profile="software_hil", now=NOW)

    assert result["passed"] is True
    assert result["hardware_gate_count"] == 0
    assert result["production_approved"] is False
    assert result["physical_release_approved"] is False


def test_hardware_profile_fails_when_hardware_records_are_missing(tmp_path: Path) -> None:
    artifact = _write_json(tmp_path / "software.json", _software_artifact())
    bundle = _bundle(tmp_path / "bundle.json", {"software_hil": _record(artifact)})

    result = check_integration_evidence_bundle(bundle, profile="vision_bench", now=NOW)

    assert result["passed"] is False
    assert result["gates"]["software_hil"]["passed"] is True
    assert result["gates"]["rtsp_camera"]["reasons"] == [
        "required evidence record is missing"
    ]
    assert result["gates"]["jetson"]["passed"] is False


def test_software_hil_cannot_masquerade_as_rtsp_hardware(tmp_path: Path) -> None:
    artifact = _write_json(tmp_path / "software.json", _software_artifact())
    same_record = _record(artifact)
    bundle = _bundle(
        tmp_path / "bundle.json",
        {"software_hil": same_record, "rtsp_camera": same_record},
    )

    result = check_integration_evidence_bundle(bundle, profile="vision_bench", now=NOW)

    reasons = result["gates"]["rtsp_camera"]["reasons"]
    assert result["passed"] is False
    assert "artifact event does not match the requested gate" in reasons
    assert "hardware gate requires hardware_observed=true" in reasons
    assert "hardware gate cannot use simulation-only evidence" in reasons


def test_local_camera_hardware_cannot_satisfy_rtsp_gate(tmp_path: Path) -> None:
    software = _write_json(tmp_path / "software.json", _software_artifact())
    local_camera = _write_json(
        tmp_path / "local-camera.json",
        _hardware_artifact("local_camera_bench_passed")
        | {
            "processed_frames": 300,
            "duration_seconds": 60,
            "source_kind": "local_device",
            "resolution_stable": True,
            "credentials_recorded": False,
        },
    )
    bundle = _bundle(
        tmp_path / "bundle.json",
        {
            "software_hil": _record(software),
            "rtsp_camera": _record(local_camera),
        },
    )

    result = check_integration_evidence_bundle(bundle, profile="vision_bench", now=NOW)

    reasons = result["gates"]["rtsp_camera"]["reasons"]
    assert result["passed"] is False
    assert "artifact event does not match the requested gate" in reasons
    assert "camera source is not RTSP" in reasons


def test_modified_artifact_fails_hash_binding(tmp_path: Path) -> None:
    artifact = _write_json(tmp_path / "software.json", _software_artifact())
    record = _record(artifact)
    artifact.write_text("{}\n", encoding="utf-8")
    bundle = _bundle(tmp_path / "bundle.json", {"software_hil": record})

    result = check_integration_evidence_bundle(bundle, profile="software_hil", now=NOW)

    assert result["passed"] is False
    assert "artifact SHA-256 does not match" in result["gates"]["software_hil"]["reasons"]


def test_stale_and_future_hardware_evidence_fail_closed(tmp_path: Path) -> None:
    software = _write_json(tmp_path / "software.json", _software_artifact())
    stale_value = _hardware_artifact("rtsp_camera_bench_passed") | {
        "observed_at_utc": (NOW - timedelta(hours=169)).isoformat(),
        "processed_frames": 300,
        "duration_seconds": 60,
        "source_kind": "rtsp",
        "resolution_stable": True,
        "credentials_recorded": False,
    }
    future_value = _hardware_artifact("jetson_orin_nano_bench_passed") | {
        "observed_at_utc": (NOW + timedelta(seconds=1)).isoformat(),
        "device_model": "Jetson Orin Nano",
        "active_inference_provider": "TensorrtExecutionProvider",
        "soak_duration_seconds": 1800,
        "processed_frames": 1000,
        "maximum_temperature_c": 80,
    }
    stale = _write_json(tmp_path / "rtsp.json", stale_value)
    future = _write_json(tmp_path / "jetson.json", future_value)
    bundle = _bundle(
        tmp_path / "bundle.json",
        {
            "software_hil": _record(software),
            "rtsp_camera": _record(stale),
            "jetson": _record(future),
        },
    )

    result = check_integration_evidence_bundle(bundle, profile="vision_bench", now=NOW)

    assert "hardware evidence is stale" in result["gates"]["rtsp_camera"]["reasons"]
    assert "hardware evidence timestamp is in the future" in result["gates"]["jetson"]["reasons"]


def test_complete_inert_payload_profile_passes_without_production_approval(
    tmp_path: Path,
) -> None:
    documents = {
        "software_hil": _software_artifact(),
        "rtsp_camera": _hardware_artifact("rtsp_camera_bench_passed")
        | {
            "processed_frames": 300,
            "duration_seconds": 60,
            "source_kind": "rtsp",
            "resolution_stable": True,
            "credentials_recorded": False,
        },
        "jetson": _hardware_artifact("jetson_orin_nano_bench_passed")
        | {
            "device_model": "Jetson Orin Nano",
            "active_inference_provider": "TensorrtExecutionProvider",
            "soak_duration_seconds": 1800,
            "processed_frames": 1000,
            "maximum_temperature_c": 80,
        },
        "pixhawk_v6x": _hardware_artifact("pixhawk_v6x_bench_passed")
        | {
            "hardware_model": "Pixhawk V6X",
            "read_only": True,
            "messages_transmitted_by_jetson": 0,
            "qgc_snapshot_fresh": True,
            "qgc_field_match": True,
            "link_loss_fail_closed": True,
            "link_loss_method": "cached_staleness_without_receive",
            "sample_count": 100,
            "fresh_sample_count": 100,
            "firmware_version": "test-version",
        },
        "gr01": _hardware_artifact("gr01_bench_passed")
        | {
            "hardware_model": "GR01",
            "hardware_id": "GR01-TEST-001",
            "remote_is_loopback": False,
            "bidirectional_ip_verified": True,
            "signed_operator_round_trip": True,
            "application_hmac_verified": True,
            "mavlink2_signature_verified": True,
            "requested_round_trips": 100,
            "round_trip_samples": 100,
            "packet_loss_rate": 0.01,
            "ack_latency_p95_ms": 500,
        },
        "inert_payload": _hardware_artifact("inert_payload_hardware_bench_passed")
        | {
            "inert_load_only": True,
            "controller_and_sensor_id_separated": True,
            "controller_and_sensor_key_separated": True,
            "independent_confirmation_verified": True,
            "uncertain_result_no_retry_verified": True,
            "people_excluded_from_test_area": True,
            "command_channel_present": False,
            "physical_release_approved": False,
            "production_approved": False,
            "controller_firmware_version": "controller-fw-test",
            "authenticated_controller_records": 21,
            "authenticated_sensor_records": 20,
            "uncertain_fault_injection_cycles": 1,
            "confirmed_cycles": 20,
        },
    }
    records = {}
    for name, document in documents.items():
        artifact = _write_json(tmp_path / f"{name}.json", document)
        records[name] = _record(artifact)
    bundle = _bundle(tmp_path / "bundle.json", records)

    result = check_integration_evidence_bundle(bundle, profile="inert_payload_bench", now=NOW)

    assert result["passed"] is True
    assert all(gate["passed"] for gate in result["gates"].values())
    assert result["production_approved"] is False
    assert result["physical_release_approved"] is False


def test_cli_writes_machine_readable_result(tmp_path: Path, capsys) -> None:
    artifact = _write_json(tmp_path / "software.json", _software_artifact())
    bundle = _bundle(tmp_path / "bundle.json", {"software_hil": _record(artifact)})
    output = tmp_path / "result.json"

    assert (
        main(
            [
                "integration-evidence-check",
                str(bundle),
                "--profile",
                "software_hil",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    stdout = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert stdout["passed"] is True
    assert written == stdout
